from __future__ import annotations

import asyncio
import datetime
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

from kama_claude.core.permissions.paths import PathGrant
from kama_claude.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from kama_claude.core.permissions.storage import load_policy_file, save_policy_file

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str
    tool_use_id: str
    paths: tuple[PathGrant, ...]
    workspace: str


# 管理工具调用权限：策略评估、用户审批挂起、session 级和持久化 always 缓存、超时
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # request_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow" | "deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow" | "deny"（持久化，从 policy_file 加载）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        # 0 表示不超时
        self._timeout_s = timeout_s
        self._path_grants: dict[tuple[str, str], tuple[PathGrant, ...]] = {}
        self._closed_sessions: set[str] = set()

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from kama_claude.core.permissions.policy import evaluate
        policy = self._policies.get(tool_name)
        return evaluate(tool_name, params, policy)

    # 读取会话内存中的路径授权，不从工具级持久化设置恢复
    def session_grants(self, session_id: str, workspace: str) -> tuple[PathGrant, ...]:
        return self._path_grants.get((session_id, workspace), ())

    # 会话关闭或断连后禁止启动新的工具操作
    def is_closed(self, session_id: str) -> bool:
        return session_id in self._closed_sessions

    # 重新打开会话但清除旧授权和待审批请求，后续操作重新进行审批
    def reopen_session(self, session_id: str) -> None:
        self.cancel_session(session_id, "session_reopened")
        self._closed_sessions.discard(session_id)

    # 查找精确请求所属会话，供 IPC 校验当前连接的响应来源
    def request_session(self, request_id: str) -> str | None:
        request = self._pending.get(request_id)
        return request.session_id if request else None

    # 检查工具和路径权限，合并为一个审批请求并绑定服务器生成的请求标识
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        paths: tuple[PathGrant, ...] = (),
        workspace: str = "",
    ) -> tuple[bool, str]:
        if self.is_closed(session_id):
            return False, "auto_deny"
        command = str(params.get("command", "")) if tool_name == "bash" else ""
        policy = self._policies.get(tool_name)
        cached = self._session_always.get(
            (session_id, tool_name), self._persistent_always.get(tool_name),
        )
        # 显式禁止优先，路径请求与外部路径启发式都不能覆盖它
        if cached == "deny" or (policy and policy.default == PermissionDecision.DENY):
            return False, "auto_deny"
        if command and policy and any(re.search(pat, command) for pat in policy.deny_patterns):
            return False, "auto_deny"
        outside_cwd = bool(command and matches_outside_cwd(command))
        if not paths and not outside_cwd:
            if cached == "allow":
                return True, "auto_allow"
            if command and policy and any(re.search(pat, command) for pat in policy.allow_patterns):
                return True, "auto_allow"
            if policy and policy.default == PermissionDecision.ALLOW:
                return True, "auto_allow"

        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        req = _PendingRequest(future, session_id, tool_name, tool_use_id, paths, workspace)
        self._pending[request_id] = req
        try:
            await event_emitter({
                "type": "permission.requested", "request_id": request_id,
                "tool_use_id": tool_use_id, "tool_name": tool_name, "params": params,
                "param_preview": param_preview(tool_name, params), "session_id": session_id,
                "paths": [path.model_dump() for path in paths], "workspace": workspace,
                "allowed_decisions": list(self._decisions(req)), "ts": _now(),
                "reason": "outside_allowed_paths" if paths else "tool_policy",
            })
            if self._timeout_s > 0:
                raw = await asyncio.wait_for(future, timeout=self._timeout_s)
            else:
                raw = await future
            if self.is_closed(session_id):
                return False, "deny_once"
            if paths:
                if raw == "allow_session":
                    key = (session_id, workspace)
                    self._path_grants[key] = tuple(dict.fromkeys(
                        (*self._path_grants.get(key, ()), *paths),
                    ))
                return raw in ("allow_once", "allow_session"), raw
            return self._apply_response(raw, session_id, tool_name), raw
        except TimeoutError:
            logger.info("permission timeout request_id=%s", request_id)
            return False, "timeout"
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    # 路径审批仅允许单次或当前会话授权，保留普通工具原有选项
    @staticmethod
    def _decisions(req: _PendingRequest) -> tuple[str, ...]:
        if req.paths:
            return ("allow_once", "allow_session", "deny_once")
        return ("allow_once", "always_allow", "deny_once", "always_deny")

    # 确认对应请求和决策仍然有效，旧客户端不得批准路径权限
    def respond(
        self, tool_use_id: str, decision: str, request_id: str | None = None,
        *, session_id: str | None = None,
    ) -> bool:
        if request_id:
            req = self._pending.get(request_id)
        else:
            candidates = [r for r in self._pending.values()
                          if r.tool_use_id == tool_use_id and not r.paths]
            req = candidates[0] if len(candidates) == 1 else None
        if (req is None or req.tool_use_id != tool_use_id or req.future.done()
                or self.is_closed(req.session_id) or decision not in self._decisions(req)
                or (session_id is not None and session_id != req.session_id)):
            return False
        req.future.set_result(decision)
        return True

    # 应用审批决策，更新 session + persistent 缓存，返回是否放行
    def _apply_response(self, decision: str, session_id: str, tool_name: str) -> bool:
        allow = decision in ("allow_once", "always_allow")
        if decision == "always_allow":
            self._session_always[(session_id, tool_name)] = "allow"
            self._persistent_always[tool_name] = "allow"
            logger.info(
                "permission: always allow tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )
            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception(
                        "permission: failed to write policy.toml path=%s", self._policy_file,
                    )
            else:
                logger.warning("permission: policy_file is None, skipping persistence")
        elif decision == "always_deny":
            self._session_always[(session_id, tool_name)] = "deny"
            self._persistent_always[tool_name] = "deny"
            logger.info(
                "permission: always deny tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )
            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception(
                        "permission: failed to write policy.toml path=%s", self._policy_file,
                    )
            else:
                logger.warning("permission: policy_file is None, skipping persistence")
        return allow

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        self._closed_sessions.add(session_id)
        self._path_grants = {k: v for k, v in self._path_grants.items() if k[0] != session_id}
        self._session_always = {k: v for k, v in self._session_always.items() if k[0] != session_id}
        to_cancel = [
            uid for uid, req in self._pending.items()
            if req.session_id == session_id
        ]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug(
                    "permission: cancel pending tool_use_id=%s reason=%s", uid, reason
                )
                req.future.set_result("deny_once")
