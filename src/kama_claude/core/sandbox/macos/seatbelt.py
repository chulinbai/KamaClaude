from __future__ import annotations

import os
from pathlib import Path

from ..errors import SandboxUnavailableError
from ..models import NetworkPolicy, SandboxCommand, SandboxPolicy

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


# 从安装包读取可审查的静态 SBPL 模板
def _template(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


# 把最终资源策略转换成 SBPL 与独立路径参数，避免路径内容变成策略代码
def build_profile(policy: SandboxPolicy) -> tuple[str, dict[str, str]]:
    fs = policy.file_system
    parameters: dict[str, str] = {}
    rules = [_template("seatbelt_base_policy.sbpl")]

    # 为文件或目录生成参数化过滤器，所有动态路径都通过 -D 传入
    def path_filter(path: Path) -> str:
        key = f"P{len(parameters)}"
        parameters[key] = str(path)
        kind = "literal" if path.is_file() else "subpath"
        return f'({kind} (param "{key}"))'

    denied = [path_filter(path) for path in fs.deny_paths]
    protected = [path_filter(path) for path in fs.protected_paths]

    # 让每一条动态授权显式排除禁止项，避免宽泛父目录规则覆盖限制
    def grant(operation: str, root: Path, exclusions: list[str]) -> str:
        base = path_filter(root)
        if exclusions:
            parts = " ".join(f"(require-not {item})" for item in exclusions)
            base = f"(require-all {base} {parts})"
        return f"(allow {operation} {base})"

    if fs.include_platform_defaults:
        rules.append(_template("seatbelt_read_only_platform_defaults.sbpl"))
    for root in (*fs.read_roots, *fs.write_roots):
        rules.append(grant("file-read*", root, denied))
    for root in fs.write_roots:
        rules.append(grant("file-write*", root, denied + protected))
    if Path("/") in fs.read_roots and not fs.deny_paths:
        rules.append(_template("seatbelt_preferences_policy.sbpl"))
    if policy.network == NetworkPolicy.ALLOW:
        rules.append(_template("seatbelt_network_policy.sbpl"))
        rules.append('(allow network-outbound (remote ip "*:*"))')
        rules.append('(allow network-inbound (local ip "*:*"))')
        rules.append('(allow network-bind (local ip "*:*"))')
    else:
        rules.append("(deny network*)")
    for item in protected:
        rules.append(f"(deny file-write* {item})")
    for item in denied:
        rules.append(f"(deny file-read* file-write* file-test-existence {item})")
    # 禁止通过移动祖先目录绕过受保护路径，并保留兄弟文件的正常写入
    ancestors = {p for root in (*fs.protected_paths, *fs.deny_paths) for p in root.parents}
    for ancestor in sorted(ancestors, key=str):
        key = f"P{len(parameters)}"
        parameters[key] = str(ancestor)
        rules.append(f'(deny file-write-unlink (literal (param "{key}")))')
    return "\n".join(rules), parameters


# 固定调用系统 sandbox-exec，策略加载失败由统一执行层返回错误
def build_command(command: SandboxCommand, policy: SandboxPolicy) -> tuple[str, ...]:
    if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        raise SandboxUnavailableError("/usr/bin/sandbox-exec is unavailable")
    profile, parameters = build_profile(policy)
    argv = [str(SANDBOX_EXEC), "-p", profile]
    for key, value in parameters.items():
        argv.extend(("-D", f"{key}={value}"))
    return (*argv, "--", *command.argv)
