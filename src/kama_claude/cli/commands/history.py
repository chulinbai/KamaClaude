from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient


# 为历史子命令注册统一范围、分页和 JSON 输出参数
def add_history_parser(subparsers: Any) -> None:
    history = subparsers.add_parser("history", help="Search durable session history")
    commands = history.add_subparsers(dest="history_command", required=True)
    for name in ("search", "read", "status", "reindex"):
        parser = commands.add_parser(name)
        parser.add_argument("--workspace", default=str(Path.cwd()))
        parser.add_argument("--all", action="store_true", help="Request cross-workspace history")
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
        if name == "search":
            parser.add_argument("query")
            parser.add_argument("--session", dest="target_session_id")
            parser.add_argument("--since", help="ISO 8601 lower time bound")
            parser.add_argument("--until", help="ISO 8601 upper time bound")
            parser.add_argument("--limit", type=int)
        elif name == "read":
            parser.add_argument("record_id")
            parser.add_argument("--before", type=int, default=2)
            parser.add_argument("--after", type=int, default=2)
            parser.add_argument("--offset", type=int, default=0)


# 通过现有 IPC 查询历史，审批读取与网络接收分开避免死锁
async def _history_async(args: argparse.Namespace, config: KamaConfig) -> int:
    client = SocketClient(config.host, config.port)
    approvals: set[asyncio.Task[None]] = set()

    # 仅交互终端接受范围审批，脚本不会隐式获得跨工作区权限
    async def approve(event: dict[str, Any]) -> None:
        decision = "deny_once"
        if sys.stdin.isatty():
            print("History access requested:", file=sys.stderr)
            for workspace in event.get("history_workspaces", []):
                print(f"  {workspace or '(unknown workspace)'}", file=sys.stderr)
            print("Allow once [y], for this connection [s], deny [n]: ",
                  end="", flush=True, file=sys.stderr)
            answer = await asyncio.to_thread(sys.stdin.readline)
            decision = {"y": "allow_once", "s": "allow_session"}.get(
                answer.strip().lower(), "deny_once",
            )
        await client.send_command("permission.respond", {
            "request_id": event["request_id"], "tool_use_id": event["tool_use_id"],
            "session_id": event["session_id"], "decision": decision,
        })

    # 接收审批时启动独立任务，让 permission.respond 的响应继续被分发
    async def event_received(event: dict[str, Any]) -> None:
        if event.get("type") == "permission.requested":
            task = asyncio.create_task(approve(event))
            approvals.add(task)
            task.add_done_callback(approvals.discard)

    client.on_event(event_received)
    try:
        await client.connect()
    except OSError as exc:
        print(f"History unavailable: {exc}; start kama-core first.", file=sys.stderr)
        return 1
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        params = {key: value for key, value in vars(args).items() if key in {
            "workspace", "query", "record_id", "target_session_id", "since", "until",
            "limit", "before", "after", "offset",
        } and value is not None}
        params["workspace"] = str(Path(params["workspace"]).resolve())
        params["scope"] = "all" if args.all else "workspace"
        result = await client.send_command(f"history.{args.history_command}", params)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.history_command == "search":
            for hit in result["results"]:
                print(f"{hit['record_id']}  {hit['timestamp']}  {hit['title']}\n"
                      f"  {hit['workspace'] or '(unknown workspace)'}\n{hit['excerpt']}\n")
            print(f"backend={result['backend']} complete={result['complete']} "
                  f"truncated={result['truncated']} "
                  f"searched={result['searched_sessions']}/{result['total_candidate_sessions']}")
            if result.get("reason"):
                print(f"reason={result['reason']}")
            for warning in result.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
        elif args.history_command == "read":
            print(result["text"])
            if result["next_offset"] is not None:
                print(f"Continue with --offset {result['next_offset']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (IpcError, OSError) as exc:
        print(f"History error: {exc}", file=sys.stderr)
        return 1
    finally:
        for approval in approvals:
            approval.cancel()
        if approvals:
            await asyncio.gather(*approvals, return_exceptions=True)
        await client.close()
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver


# 运行历史客户端，Ctrl+C 关闭连接并取消服务端查询
def cmd_history(args: argparse.Namespace, config: KamaConfig) -> None:
    try:
        code = asyncio.run(_history_async(args, config))
    except KeyboardInterrupt:
        code = 130
    if code:
        raise SystemExit(code)
