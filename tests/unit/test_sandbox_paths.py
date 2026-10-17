from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from kama_claude.core.sandbox import (
    FileSystemPolicy,
    SandboxCommand,
    SandboxManager,
    SandboxPolicy,
    SandboxPolicyError,
)
from kama_claude.core.sandbox.policy import normalize_path, normalize_policy

_CONTROL_CHARACTERS = tuple(chr(code) for code in (*range(0x20), *range(0x7F, 0xA0)))


# 功能：验证所有 C0、DEL、C1 控制字符在路径和 cwd 中均被拒绝
# 设计：把非法字符放在会被 .. 折叠的组件中，并禁止后续展开，证明输入没有被清洗后接受
@pytest.mark.parametrize("character", _CONTROL_CHARACTERS, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("location", ["path", "cwd"])
def test_control_characters_rejected_before_normalization(
    character: str, location: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = Path(f"bad{character}name") / ".." / "allowed"
    path = invalid if location == "path" else Path("allowed")
    cwd = invalid if location == "cwd" else Path.cwd()
    expansion = Mock(side_effect=AssertionError("Invalid input reached path expansion"))
    monkeypatch.setattr(Path, "expanduser", expansion)
    with pytest.raises(SandboxPolicyError) as error:
        normalize_path(path, cwd)
    message = str(error.value)
    assert error.value.error_type == "sandbox_policy_error"
    assert f"U+{ord(character):04X}" in message
    assert repr(str(invalid)) in message
    assert character not in message
    expansion.assert_not_called()


# 功能：验证四类权限路径统一拒绝控制字符，不会进入缺失限制路径的祖先回退
# 设计：使用空读取授权避免无关文件访问，分别传入非法授权根和限制项
@pytest.mark.parametrize("field", ["read_roots", "write_roots", "protected_paths", "deny_paths"])
@pytest.mark.parametrize("character", ["\0", "\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\x9f"])
def test_policy_path_fields_reject_controls(field: str, character: str) -> None:
    invalid = Path(f"bad{character}name")
    fs = replace(FileSystemPolicy(read_roots=()), **{field: (invalid,)})
    with pytest.raises(SandboxPolicyError, match="control character"):
        normalize_policy(SandboxPolicy(fs), Path.cwd())


# 功能：验证即使没有任何路径规则，非法 cwd 仍会被拒绝
# 设计：空规则不会调用逐路径规范化，单独验证策略入口的工作目录校验
def test_empty_policy_still_rejects_invalid_cwd() -> None:
    with pytest.raises(SandboxPolicyError, match=r"U\+000A"):
        normalize_policy(SandboxPolicy(FileSystemPolicy(read_roots=())), Path("bad\ncwd"))


# 功能：验证用户目录展开引入的控制字符在绝对化前被拒绝
# 设计：模拟展开结果中含可折叠的非法组件，防止 abspath 消除该组件后漏检
def test_expanded_path_rejects_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    expanded = Path.cwd() / "bad\x1bhome" / ".." / "allowed"
    monkeypatch.setattr(Path, "expanduser", lambda path: expanded)
    with pytest.raises(SandboxPolicyError, match=r"U\+001B"):
        normalize_path(Path("~/allowed"), Path.cwd())


# 功能：验证路径解析得到的新路径也不能包含控制字符
# 设计：模拟解析或符号链接目标含回车，覆盖最终结果与链接目标两个检查位置
@pytest.mark.parametrize("symlink", [False, True])
def test_resolved_path_rejects_controls(
    symlink: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = Path.cwd() / "bad\rtarget"
    monkeypatch.setattr(Path, "is_symlink", lambda path: symlink)
    monkeypatch.setattr(Path, "resolve", lambda path: resolved)
    with pytest.raises(SandboxPolicyError, match=r"U\+000D"):
        normalize_path(Path("allowed"), Path.cwd())


# 功能：验证空格、中文、引号与普通 Unicode 不会被控制字符校验误改或误拒绝
# 设计：隔离宿主文件系统的命名限制，仅检查规范化流程保留原始路径字符
@pytest.mark.parametrize("name", ["report.txt", "含 空格目录", "O'Brien notes", 'a"b', "a;b$(x)", "café", "🐱"])
def test_non_control_characters_preserved(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32" and '"' in name:
        with pytest.raises(SandboxPolicyError, match="Ambiguous Windows"):
            normalize_path(Path(name), Path.cwd())
        return
    expected = Path.cwd() / name
    monkeypatch.setattr(Path, "is_symlink", lambda path: False)
    monkeypatch.setattr(Path, "resolve", lambda path: path)
    assert normalize_path(Path(name), Path.cwd()) == expected


# 功能：验证非法工作目录在启动平台后端前变成不可重试的策略错误
# 设计：通过真实管理器入口执行校验，并确认后端命令生成函数没有被调用
def test_manager_rejects_invalid_cwd_before_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    backend = Mock()
    monkeypatch.setattr("kama_claude.core.sandbox.linux.command.build_command", backend)
    with pytest.raises(SandboxPolicyError, match="control character"):
        SandboxManager().prepare(
            SandboxCommand(("/bin/sh", "-c", "echo ok"), Path("bad\ncwd")), SandboxPolicy(),
        )
    backend.assert_not_called()
