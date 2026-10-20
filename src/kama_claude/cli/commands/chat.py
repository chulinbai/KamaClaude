from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending: dict[str, dict[str, Any]] = {}
        self.session_id: str | None = None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if (t.startswith("session.") or t == "permission.requested") and self.session_id:
            if event.get("session_id") != self.session_id:
                return
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            paths = event.get("paths", [])
            for path in paths:
                scope = "directory and subtree" if path["kind"] == "directory" else "exact file"
                print(f"  {path['operation']} / {path['access']} / {scope}: {path['path']}")
            print("  y=allow once  s=allow session  n=deny" if paths else
                  "  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending[str(event.get("request_id") or tool_use_id)] = event
        elif t in ("permission.granted", "permission.denied"):
            self.pending.pop(str(event.get("request_id") or event.get("tool_use_id")), None)
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# 异步核心：创建 chat session，循环读取用户输入并发送到 daemon；权限请求时优先处理审批
async def _chat_async(config: KamaConfig) -> int:
    workspace = Path.cwd()
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())
    send_tasks: set[asyncio.Task[None]] = set()

    # 发送消息时继续读取审批输入，避免同步等待 run 导致审批死锁
    async def send_message(session_id: str, content: str) -> None:
        try:
            await client.send_command("session.send_message", {
                "session_id": session_id, "content": content,
            })
        except (IpcError, RuntimeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": "global",
            },
        )
        created = await client.send_command("session.create", {
            "mode": "chat", "workspace": str(workspace),
        })
        session_id = str(created["session_id"])
        printer.session_id = session_id
        print(f"[session: {session_id}]")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending:
                key, event = next(iter(printer.pending.items()))
                decisions = ({"y": "allow_once", "s": "allow_session", "n": "deny_once"}
                             if event.get("paths") else _DECISION_MAP)
                decision = decisions.get(content.lower())
                if decision is None:
                    print("  allowed keys: " + ", ".join(decisions))
                    continue
                response = await client.send_command("permission.respond", {
                    "tool_use_id": event["tool_use_id"], "decision": decision,
                    "request_id": event.get("request_id"), "session_id": session_id,
                })
                if response.get("ok"):
                    printer.pending.pop(key, None)
                else:
                    print("Permission response rejected (expired or invalid request).")
                continue

            task = asyncio.create_task(send_message(session_id, content))
            send_tasks.add(task)
            task.add_done_callback(send_tasks.discard)

        await client.send_command("session.close", {"session_id": session_id})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
        for task in send_tasks:
            task.cancel()
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)
    return 0


# 执行 kama chat 命令
def cmd_chat(config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
