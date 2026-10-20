"""Adversarial checks against the real MXC backend, including positive controls.

All filesystem attack targets are test-owned fixtures. Network traffic contains
only a TCP handshake or a fixed example.com DNS question, never workspace data.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.sandbox import (
    FileSystemPolicy,
    NetworkPolicy,
    SandboxPolicy,
    SandboxPolicyError,
)
from kama_claude.core.sandbox.policy import normalize_path
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from tests.integration.test_sandbox_windows_native import execute, process_alive, ps_string

pytestmark = [pytest.mark.sandbox_native, pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("KAMA_REQUIRE_WINDOWS_SANDBOX") != "1",
    reason="Set KAMA_REQUIRE_WINDOWS_SANDBOX=1 on a BaseContainer-capable Windows host",
)]


def operations(steps: dict[str, str]) -> str:
    statements = ["$ErrorActionPreference='Stop'; $r=@{}"]
    for name, step in steps.items():
        statements.append(f"try {{ $null=& {{ {step} }}; $r.{name}=$true }} catch {{ $r.{name}=$false }}")
    return "; ".join([*statements, "$r | ConvertTo-Json -Compress"])


async def outcome(work: Path, steps: dict[str, str], policy: SandboxPolicy) -> dict[str, bool]:
    result = await execute(work, operations(steps), policy=policy)
    assert result.returncode == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("grant", ["none", "read", "write"])
async def test_external_grant_matrix(tmp_path: Path, kind: str, grant: str) -> None:
    work = tmp_path / "work"
    external = tmp_path / "external 中文 ' [x]"
    work.mkdir()
    external.mkdir()
    target, neighbor = external / "target", external / "neighbor"
    target.write_text("target")
    neighbor.write_text("neighbor")
    root = target if kind == "file" else external
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=(root,) if grant == "read" else (),
        write_roots=(work, root) if grant == "write" else (work,),
    ))
    got = await outcome(work, {
        "read": f"[IO.File]::ReadAllText({ps_string(target)})",
        "write": f"[IO.File]::WriteAllText({ps_string(target)},'changed')",
        "neighbor_read": f"[IO.File]::ReadAllText({ps_string(neighbor)})",
        "list": f"[IO.Directory]::GetFiles({ps_string(external)})",
        "create": f"[IO.File]::WriteAllText({ps_string(external / 'new')},'new')",
        "delete_neighbor": f"[IO.File]::Delete({ps_string(neighbor)})",
        "parent_create": f"[IO.File]::WriteAllText({ps_string(tmp_path / 'escape')},'bad')",
        "work_create": "[IO.File]::WriteAllText('local','ok')",
    }, policy)
    assert got == {
        "read": grant != "none", "write": grant == "write",
        "neighbor_read": kind == "directory" and grant != "none",
        "list": kind == "directory" and grant != "none",
        "create": kind == "directory" and grant == "write",
        "delete_neighbor": kind == "directory" and grant == "write",
        "parent_create": False, "work_create": True,
    }
    assert target.read_text() == ("changed" if grant == "write" else "target")
    assert not (tmp_path / "escape").exists()


async def test_readonly_and_deny_override_writable_parent(tmp_path: Path) -> None:
    work = tmp_path / "work"
    readonly, denied = work / "readonly", work / "denied"
    readonly.mkdir(parents=True)
    denied.mkdir()
    (readonly / "data").write_text("original")
    (denied / "data").write_text("secret")
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=(readonly,), write_roots=(work,), deny_paths=(denied,),
    ))
    got = await outcome(work, {
        "read": f"[IO.File]::ReadAllText({ps_string(readonly / 'data')})",
        "write": f"[IO.File]::WriteAllText({ps_string(readonly / 'data')},'bad')",
        "delete": f"[IO.File]::Delete({ps_string(readonly / 'data')})",
        "deny_read": f"[IO.File]::ReadAllText({ps_string(denied / 'data')})",
        "deny_write": f"[IO.File]::WriteAllText({ps_string(denied / 'new')},'bad')",
        "deny_list": f"[IO.Directory]::GetFiles({ps_string(denied)})",
    }, policy)
    assert got == {"read": True, "write": False, "delete": False,
                   "deny_read": False, "deny_write": False, "deny_list": False}
    assert (readonly / "data").read_text() == "original"
    assert (denied / "data").read_text() == "secret"


@pytest.mark.parametrize("alias", ["dotdot", "case", "extended", "ads"])
async def test_command_path_alias_cannot_bypass_deny(tmp_path: Path, alias: str) -> None:
    work = tmp_path / "work"
    secret = work / "secret"
    secret.mkdir(parents=True)
    target = secret / "data"
    target.write_text("original")
    variants = {"dotdot": str(work / "secret" / ".." / "secret" / "data"),
                "case": str(target).upper(), "extended": "\\\\?\\" + str(target),
                "ads": str(target) + ":payload"}
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(work,), deny_paths=(secret,)))
    got = await outcome(work, {
        "read": f"[IO.File]::ReadAllText({ps_string(variants[alias])})",
        "write": f"[IO.File]::WriteAllText({ps_string(variants[alias])},'bad')",
    }, policy)
    assert got == {"read": False, "write": False}
    assert target.read_text() == "original"


async def test_junction_inside_work_cannot_reach_external_target(tmp_path: Path) -> None:
    import _winapi

    work, external = tmp_path / "work", tmp_path / "external"
    work.mkdir()
    external.mkdir()
    target = external / "data"
    target.write_text("original")
    link = work / "junction"
    _winapi.CreateJunction(str(external), str(link))
    try:
        with pytest.raises(SandboxPolicyError, match="reparse"):
            normalize_path(link, work)
        policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(work,)))
        assert await outcome(work, {
            "read": f"[IO.File]::ReadAllText({ps_string(link / 'data')})",
            "write": f"[IO.File]::WriteAllText({ps_string(link / 'data')},'bad')",
        }, policy) == {"read": False, "write": False}
        assert target.read_text() == "original"
    finally:
        # Remove only the junction, never recurse through its target.
        link.rmdir()


async def test_preexisting_hardlink_does_not_bypass_deny(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "secret"
    target.write_text("original")
    link = work / "alias"
    os.link(target, link)
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(work,), deny_paths=(target,)))
    with pytest.raises(SandboxPolicyError, match="hardlink"):
        await outcome(work, {
            "read": f"[IO.File]::ReadAllText({ps_string(link)})",
            "write": f"[IO.File]::WriteAllText({ps_string(link)},'bad')",
        }, policy)
    assert target.read_text() == "original"


@pytest.mark.parametrize("restriction", ["ungranted", "readonly", "protected", "denied"])
async def test_runtime_hardlink_cannot_upgrade_access(tmp_path: Path, restriction: str) -> None:
    work, external = tmp_path / "work", tmp_path / "external"
    work.mkdir()
    external.mkdir()
    target = external / "data"
    target.write_text("original")
    link = work / "newlink"
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=(external,) if restriction == "readonly" else (), write_roots=(work,),
        protected_paths=(external,) if restriction == "protected" else (),
        deny_paths=(external,) if restriction == "denied" else (),
    ))
    got = await outcome(work, {
        "link": f"New-Item -ItemType HardLink -Path {ps_string(link)} -Target {ps_string(target)} -ErrorAction Stop",
        "write": f"[IO.File]::WriteAllText({ps_string(link)},'bad')",
    }, policy)
    assert not got["link"], got
    assert target.read_text() == "original"


@pytest.mark.parametrize("access_mode", ["read", "write"])
@pytest.mark.parametrize("decision", ["deny_once", "allow_once", "allow_session"])
async def test_real_approval_scope_and_lifetime(tmp_path: Path, access_mode: str, decision: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target, sibling = tmp_path / "approved", tmp_path / "sibling"
    target.write_text("original")
    sibling.write_text("sibling")
    access = PathAccessController(work)
    tool = BashTool(access=access)
    manager = PermissionManager(timeout_s=1)
    bus = EventBus()
    requests: list[PermissionRequestedEvent] = []
    next_decision = decision

    async def respond(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            assert manager.respond(event.tool_use_id, next_decision if event.paths else "allow_once",
                                   event.request_id, session_id=event.session_id)

    bus.subscribe(respond)
    registry = ToolRegistry()
    registry.register(tool)
    script = operations({
        "read": f"[IO.File]::ReadAllText({ps_string(target)})",
        "write": f"[IO.File]::WriteAllText({ps_string(target)},'changed')",
        "sibling": f"[IO.File]::ReadAllText({ps_string(sibling)})",
    })

    async def call(uid: str, *, declare: bool, session: str = "s1"):
        return await invoke_tool(registry, ToolCallBlock(id=uid, name="bash", input={
            "command": script,
            "required_paths": [{"path": str(target), "access": access_mode}] if declare else [],
        }), bus, "run", permission_manager=manager, session_id=session)

    first = await call("first", declare=True)
    assert [p.path for p in requests[0].paths] == [str(target)]
    if decision == "deny_once":
        assert first.error_type == "permission_denied"
    else:
        assert not first.is_error, first.content
        assert json.loads(first.content) == {"read": True, "write": access_mode == "write",
                                             "sibling": False}
    # No declaration: the command itself attempts access, so the kernel must enforce scope.
    next_decision = "deny_once"
    second = await call("second", declare=False)
    assert not second.is_error, second.content
    persists = decision == "allow_session"
    assert json.loads(second.content) == {"read": persists,
                                          "write": persists and access_mode == "write", "sibling": False}
    other = await call("other", declare=False, session="s2")
    assert json.loads(other.content) == {"read": False, "write": False, "sibling": False}
    assert sibling.read_text() == "sibling"
    expected = "changed" if decision != "deny_once" and access_mode == "write" else "original"
    assert target.read_text() == expected
    manager.cancel_session("s1")
    revoked = await call("revoked", declare=False)
    assert revoked.error_type == "permission_denied"


@pytest.fixture(scope="module")
def tcp_endpoint() -> tuple[str, int]:
    host, port = os.environ.get("KAMA_SANDBOX_TEST_ENDPOINT", "github.com:443").rsplit(":", 1)
    address = socket.gethostbyname(host)
    with socket.create_connection((address, int(port)), timeout=5):
        pass
    return address, int(port)


@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_tcp_literal_ip_and_child_inherit_policy(tmp_path: Path, tcp_endpoint, mode) -> None:
    address, port = tcp_endpoint
    script = (
        "$c=[Net.Sockets.TcpClient]::new(); try { "
        f"$t=$c.ConnectAsync({ps_string(address)},{port}); $null=$t.Wait(2000); "
        "[Console]::WriteLine($c.Connected) } catch { [Console]::WriteLine('False') } "
        "finally { $c.Dispose() }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
    result = await execute(tmp_path, script + "; & $PSHOME\\powershell.exe -NoProfile "
                           f"-NonInteractive -EncodedCommand {encoded}", policy=policy)
    assert result.returncode == 0, result.output
    assert result.stdout.split() == (["True", "True"] if mode == NetworkPolicy.ALLOW else ["False", "False"])


# A fixed DNS question for example.com A; only the matching response proves UDP delivery.
DNS_QUESTION = bytes.fromhex("4b4301000001000000000000076578616d706c6503636f6d0000010001")


@pytest.fixture(scope="module")
def udp_endpoint() -> str:
    configured = os.environ.get("KAMA_SANDBOX_TEST_DNS")
    if configured:
        candidates = [configured]
    else:
        command = ("Get-DnsClientServerAddress -AddressFamily IPv4 | "
                   "Select-Object -ExpandProperty ServerAddresses -Unique")
        candidates = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        ).split()
    for address in candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(1)
                client.connect((address, 53))
                client.send(DNS_QUESTION)
                if client.recv(4096)[:2] == DNS_QUESTION[:2]:
                    return address
        except OSError:
            continue
    pytest.fail("No reachable DNS/UDP positive control; set KAMA_SANDBOX_TEST_DNS")


@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_udp_dns_allow_and_deny(tmp_path: Path, udp_endpoint: str, mode: NetworkPolicy) -> None:
    payload = base64.b64encode(DNS_QUESTION).decode()
    script = (
        "$ok=$false; for ($i=0; $i -lt 3 -and -not $ok; $i++) { "
        "$c=[Net.Sockets.UdpClient]::new(); $c.Client.ReceiveTimeout=2000; try { "
        f"$c.Connect({ps_string(udp_endpoint)},53); $b=[Convert]::FromBase64String('{payload}'); "
        "$null=$c.Send($b,$b.Length); $peer=[Net.IPEndPoint]::new([Net.IPAddress]::Any,0); "
        "$reply=$c.Receive([ref]$peer); $ok=$reply[0] -eq 75 -and $reply[1] -eq 67 "
        "} catch { [Console]::Error.WriteLine($_.Exception.Message) } finally { $c.Dispose() } "
        "}; [Console]::WriteLine($ok)"
    )
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
    result = await execute(tmp_path, script, policy=policy)
    assert result.returncode == 0, result.output
    assert result.stdout.strip() == ("True" if mode == NetworkPolicy.ALLOW else "False"), result.output


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_ipv4_ipv6_host_loopback(tmp_path: Path, address: str, mode: NetworkPolicy) -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), address, 0)
    try:
        port = server.sockets[0].getsockname()[1]
        _, writer = await asyncio.open_connection(address, port)
        writer.close()
        await writer.wait_closed()
        script = ("$c=[Net.Sockets.TcpClient]::new([Net.Sockets.AddressFamily]::"
                  + ("InterNetworkV6" if ":" in address else "InterNetwork") + "); try { "
                  f"$t=$c.ConnectAsync({ps_string(address)},{port}); $null=$t.Wait(1500); "
                  "[Console]::WriteLine($c.Connected) } catch { [Console]::WriteLine('False') } "
                  "finally { $c.Dispose() }")
        policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
        result = await execute(tmp_path, script, policy=policy)
        assert result.returncode == 0 and result.stdout.strip() == "False", result.output
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_inbound_host_connection_denied(tmp_path: Path, mode: NetworkPolicy) -> None:
    marker = tmp_path / "listener"
    script = (
        "$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Any,0); try { $l.Start(); "
        f"[IO.File]::WriteAllText({ps_string(marker)},[string]$l.LocalEndpoint.Port); "
        "$t=$l.AcceptTcpClientAsync(); $ok=$t.Wait(3000); [Console]::WriteLine($ok); "
        "if ($ok) { $t.Result.Dispose() } } catch { "
        f"[IO.File]::WriteAllText({ps_string(marker)},'blocked'); [Console]::WriteLine('False') "
        "} finally { $l.Stop() }"
    )
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
    task = asyncio.create_task(execute(tmp_path, script, policy=policy))
    try:
        async with asyncio.timeout(10):
            while not marker.exists() or not marker.read_text():
                if task.done():
                    pytest.fail(str(task.result()))
                await asyncio.sleep(0.02)
        port = marker.read_text()
        if port != "blocked":
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(port)), 1)
            except (OSError, TimeoutError):
                pass
            else:
                writer.close()
                await writer.wait_closed()
                pytest.fail("Host connected to sandbox despite ingress/hostLoopback deny")
        result = await task
        assert result.returncode == 0 and result.stdout.strip() == "False", result.output
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_host_environment_and_cwd_are_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "security-fixture-secret")
    monkeypatch.setenv("DOTNET_STARTUP_HOOKS", "nonexistent-fixture-hook")
    monkeypatch.setenv("MXC_FORCE_TIER", "appcontainer-dacl")
    result = await execute(tmp_path, "@{ secret=$env:ANTHROPIC_API_KEY; hook=$env:DOTNET_STARTUP_HOOKS; "
                           "tier=$env:MXC_FORCE_TIER; cwd=$PWD.Path } | ConvertTo-Json -Compress")
    assert result.returncode == 0, result.output
    assert json.loads(result.stdout) == {"secret": None, "hook": None, "tier": None, "cwd": str(tmp_path)}


@pytest.mark.parametrize("failure", ["AssignProcessToJobObject", "ResumeThread"])
async def test_suspended_launch_failure_never_runs_and_cleans_handles(tmp_path, monkeypatch, failure) -> None:
    import _winapi

    from kama_claude.core.sandbox.errors import SandboxSetupError
    from kama_claude.core.sandbox.windows import process
    from kama_claude.core.sandbox.windows.command import powershell_command

    api = process._kernel()
    setattr(api, failure, lambda *_: 0 if failure == "AssignProcessToJobObject" else 0xFFFFFFFF)
    monkeypatch.setattr(process, "_kernel", lambda: api)
    original_create, original_close = _winapi.CreateProcess, _winapi.CloseHandle
    spawned, closed = [], []

    def create(*args):
        result = original_create(*args)
        spawned.append(result)
        return result

    def close(handle):
        closed.append(handle)
        return original_close(handle)

    monkeypatch.setattr(_winapi, "CreateProcess", create)
    monkeypatch.setattr(_winapi, "CloseHandle", close)
    marker = tmp_path / "must-not-run"
    command = powershell_command(f"[IO.File]::WriteAllText({ps_string(marker)},'bad')")
    with pytest.raises(SandboxSetupError):
        await process.run_process(command, tmp_path, dict(os.environ), 2, 1024)
    assert len(spawned) == 1
    handle, thread, pid, _ = spawned[0]
    assert not process_alive(pid)
    assert handle in closed and thread in closed
    assert not marker.exists()


@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_windows_dns_client_cannot_bypass_host_loopback(
    tmp_path: Path, mode: NetworkPolicy,
) -> None:
    """Check the Windows DNS API as well as direct sockets, using a test-owned server."""
    from kama_claude.core.sandbox.windows.command import powershell_command
    from kama_claude.core.sandbox.windows.process import run_process

    seen: list[bytes] = []

    class DNS(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, address):
            seen.append(data)
            end = 12
            while end < len(data) and data[end]:
                end += data[end] + 1
            question = data[12:end + 5]
            response = (data[:2] + bytes.fromhex("81800001000100000000") + question
                        + bytes.fromhex("c00c000100010000003c00047f000001"))
            self.transport.sendto(response, address)

    loop = asyncio.get_running_loop()
    # The Windows DNS API accepts server addresses, not custom UDP ports. Do not
    # change system DNS settings or stop an existing service to acquire port 53.
    transport, _ = await loop.create_datagram_endpoint(DNS, local_addr=("127.0.0.1", 53))
    try:
        def script(label: str) -> str:
            name = f"kama-security-{label}-{uuid.uuid4().hex}.example.com"
            return (f"Resolve-DnsName -Name {ps_string(name)} -Type A -Server 127.0.0.1 "
                    "-DnsOnly -NoHostsFile -QuickTimeout -ErrorAction Stop | Out-Null; 'resolved'")

        control = await run_process(powershell_command(script("control")), tmp_path,
                                    dict(os.environ), 10, 4096)
        assert control.result.returncode == 0 and seen, control.result.output
        seen.clear()
        policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
        result = await execute(tmp_path, script(mode.value), policy=policy, timeout=8)
        assert not seen, "Windows DNS client sent a packet despite hostLoopback deny"
        assert "resolved" not in result.stdout
    finally:
        transport.close()


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("mode", list(NetworkPolicy))
async def test_direct_udp_host_loopback(tmp_path: Path, address: str, mode: NetworkPolicy) -> None:
    seen: list[bytes] = []

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, peer):
            seen.append(data)
            self.transport.sendto(data, peer)

    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    transport, _ = await loop.create_datagram_endpoint(Echo, local_addr=(address, 0), family=family)
    try:
        port = transport.get_extra_info("sockname")[1]
        with socket.socket(family, socket.SOCK_DGRAM) as client:
            client.setblocking(False)
            endpoint = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            await loop.sock_sendto(client, b"control", endpoint)
            reply, _ = await asyncio.wait_for(loop.sock_recvfrom(client, 100), 2)
            assert reply == b"control"
        script = (
            "$c=[Net.Sockets.UdpClient]::new([Net.Sockets.AddressFamily]::"
            + ("InterNetworkV6" if family == socket.AF_INET6 else "InterNetwork") + "); try { "
            f"$c.Connect({ps_string(address)},{port}); $b=[Text.Encoding]::ASCII.GetBytes('sandbox'); "
            "$null=$c.Send($b,$b.Length); Start-Sleep -Milliseconds 200 "
            "} catch {} finally { $c.Dispose() }; 'done'"
        )
        policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
        result = await execute(tmp_path, script, policy=policy)
        assert result.returncode == 0 and result.stdout.strip() == "done", result.output
        assert b"sandbox" not in seen
    finally:
        transport.close()
