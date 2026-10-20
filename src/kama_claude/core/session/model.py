from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    run_ids: list[str] = field(default_factory=list)
    workspace: str | None = None

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_ids": list(self.run_ids),
            "workspace": self.workspace,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        if data.get("mode") not in ("chat", "one_shot") or data.get("status") not in (
            "active", "waiting_for_input", "closed",
        ):
            raise ValueError("Invalid session mode or status")
        for key in ("id", "created_at", "updated_at"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Invalid session {key}")
        if not isinstance(data.get("title", ""), str):
            raise ValueError("Invalid session title")
        if not isinstance(data.get("run_ids", []), list) or not all(
            isinstance(value, str) for value in data.get("run_ids", [])
        ):
            raise ValueError("Invalid session run_ids")
        if data.get("workspace") is not None and (
            not isinstance(data["workspace"], str) or not data["workspace"]
        ):
            raise ValueError("Invalid session workspace")
        return cls(
            id=str(data["id"]),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            run_ids=[str(x) for x in data.get("run_ids", [])],
            workspace=data.get("workspace"),
        )
