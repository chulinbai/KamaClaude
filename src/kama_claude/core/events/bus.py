from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    # 可选绑定单轮身份，在事件到达观察者前统一补齐归属
    def __init__(
        self, *, session_id: str | None = None, agent_id: str | None = None,
        parent_agent_id: str | None = None,
    ) -> None:
        self._subscribers: list[EventHandler] = []
        self._identity = {
            "session_id": session_id, "agent_id": agent_id, "parent_agent_id": parent_agent_id,
        }

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # 按注册顺序依次调用所有订阅者
    async def publish(self, event: BaseModel) -> None:
        identity = {key: value for key, value in self._identity.items()
                    if value is not None and key in type(event).model_fields}
        if identity:
            event = event.model_copy(update=identity)
        for handler in self._subscribers:
            await handler(event)
