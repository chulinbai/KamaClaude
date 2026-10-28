from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.history.service import HistoryService, Scope, workspace_key
from kama_claude.core.permissions.manager import PermissionManager


# 将模型请求的历史范围转换为服务端授权快照，不扩大普通文件读取权限
async def authorize_history(
    service: HistoryService, workspace: Path, scope: str, *,
    manager: PermissionManager | None, owner_id: str, tool_name: str, tool_use_id: str,
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> Scope:
    current = workspace_key(workspace)
    targets = (await asyncio.to_thread(service.workspaces)
               if scope == "all" else frozenset({current}))
    if manager is None:
        if targets - {current}:
            raise PermissionError("Cross-workspace history requires an approval-capable client")
        return targets
    if manager.is_closed(owner_id):
        raise PermissionError("History authorization has been revoked")
    needed = targets - {current} - manager.history_grants(owner_id, current)
    request_id: str | None = None

    # 捕获服务端请求 ID，使审批组件能收到对应的完成事件
    async def requested(raw: dict[str, Any]) -> None:
        nonlocal request_id
        request_id = raw.get("request_id")
        await emit(raw)

    try:
        allowed, decision = await manager.check_and_wait(
            tool_use_id, tool_name, {"scope": scope}, owner_id, requested, workspace=current,
            history_workspaces=tuple(sorted(needed, key=lambda value: value or "")),
        )
    except asyncio.CancelledError:
        if request_id is not None:
            try:
                await emit({
                    "type": "permission.denied", "request_id": request_id,
                    "session_id": owner_id, "tool_use_id": tool_use_id,
                    "decision": "cancelled", "ts": datetime.now(UTC).isoformat(),
                })
            except OSError:
                pass
        raise
    if request_id is not None:
        await emit({
            "type": "permission.granted" if allowed else "permission.denied",
            "request_id": request_id, "session_id": owner_id, "tool_use_id": tool_use_id,
            "decision": decision, "ts": datetime.now(UTC).isoformat(),
        })
    if not allowed:
        raise PermissionError("History scope permission denied")
    return targets
