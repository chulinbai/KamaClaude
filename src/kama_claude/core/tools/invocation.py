from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.images import ImageError, check_image_budget
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.paths import PathAccessError, PathGrant, path_execution
from kama_claude.core.sandbox.errors import SandboxError
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import RateLimitedError
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.permissions.manager import PermissionManager

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
) -> ToolResult:
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            ts=_now(),
        )
    )
    return ToolResult(content=error_message, is_error=True, error_type=error_class)


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时指数退避重试，返回 ToolResult（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    image_history: list[dict[str, Any]] | None = None,
) -> ToolResult:
    t0 = time.monotonic()

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
        )

    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
            )

    access = tool.path_access
    grants: tuple[PathGrant, ...] = ()
    needed: tuple[PathGrant, ...] = ()
    try:
        if access is not None:
            if permission_manager is not None:
                grants = permission_manager.session_grants(session_id, str(access.cwd))
            requests = access.requirements(tool_call.name, dict(tool_call.input))
            needed = tuple(r for r in requests if not access.allows(r, grants))
            if needed and permission_manager is None:
                raise PathAccessError(f"Path approval required: {needed[0].path}")
    except (PathAccessError, SandboxError) as exc:
        code = exc.code if isinstance(exc, PathAccessError) else exc.error_type
        return await _fail(bus, run_id, tool_call, code, str(exc), elapsed())

    request_id: str | None = None
    if permission_manager is not None:
        # 保留服务器请求标识，使结果事件与审批块精确对应
        async def _emit_permission(raw: dict[str, Any]) -> None:
            nonlocal request_id
            request_id = raw.get("request_id")
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        try:
            allowed, decision = await permission_manager.check_and_wait(
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                params=dict(tool_call.input),
                session_id=session_id,
                event_emitter=_emit_permission,
                paths=needed,
                workspace=str(access.cwd) if access else "",
            )
        except asyncio.CancelledError:
            if request_id is not None:
                await bus.publish(PermissionDeniedEvent(
                    run_id=run_id, tool_use_id=tool_call.id, request_id=request_id,
                    decision="cancelled", ts=_now(),
                ))
            raise
        if allowed:
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        request_id=request_id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        request_id=request_id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            return await _fail(
                bus, run_id, tool_call,
                "permission_denied",
                "Permission denied by user. You may not execute this command. "
                "Try an alternative approach or ask the user what to do.",
                elapsed(),
            )

    grants = (*grants, *needed)

    # 为执行创建独立权限快照，避免并发调用与子 agent 泄漏单次授权
    async def execute() -> ToolResult:
        if permission_manager is not None and permission_manager.is_closed(session_id):
            raise PathAccessError("Session closed; path authorization has been revoked")
        await tool.prepare(dict(tool_call.input), tool_call.id)
        if access is None:
            return await tool.invoke(dict(tool_call.input))
        with path_execution(access, grants):
            return await tool.invoke(dict(tool_call.input))

    for attempt in range(1, _MAX_RETRIES + 2):
        error_class: str | None = None
        error_message: str | None = None

        try:
            if tool.managed_timeout:
                result = await execute()
            else:
                result = await asyncio.wait_for(
                    execute(), timeout=timeout + tool.execution_overhead_s
                )
            ms = elapsed()

            if result.content_blocks is not None:
                check_image_budget([
                    *(image_history or []), {"role": "user", "content": result.content_blocks},
                ])

            if result.is_error:
                if result.content_blocks is not None:
                    await _fail(bus, run_id, tool_call, result.error_type or "runtime_error",
                                result.content, ms, attempt=attempt)
                    return result
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=result.content,
                        ts=_now(),
                    )
                )
                return result

        except ImageError as exc:
            return await _fail(bus, run_id, tool_call, "image_error", str(exc), elapsed())
        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except (PathAccessError, SandboxError) as exc:
            error_class = exc.code if isinstance(exc, PathAccessError) else exc.error_type
            error_message = str(exc)
        except TimeoutError:
            return await _fail(
                bus, run_id, tool_call,
                "timeout", f"tool timed out after {timeout + tool.execution_overhead_s}s",
                elapsed(),
                attempt=attempt,
            )
        except OSError as exc:
            error_class = "permission_denied" if isinstance(exc, PermissionError) else "file_error"
            error_message = str(exc)
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        assert error_class is not None and error_message is not None
        ms = elapsed()

        if tool.retry_errors and error_class in _RETRYABLE and attempt <= _MAX_RETRIES:
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=error_message,
                    elapsed_ms=ms,
                    attempt=attempt,
                    ts=_now(),
                )
            )
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus, run_id, tool_call,
            error_class, error_message, ms,
            attempt=attempt,
        )

    # unreachable, but keeps mypy happy
    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")
