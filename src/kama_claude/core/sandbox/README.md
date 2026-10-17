# Bash 系统沙盒

本包负责操作系统资源限制。`core/permissions/` 仍负责工具调用的 allow/ask/deny；审批通过不会自动关闭沙盒。

## 当前行为

- Linux/macOS 默认全盘只读、工作区可写、网络禁止。Windows 执行 PowerShell，默认授予工作区写权限及必要运行时读权限。工作目录在创建 BashTool 时固定。
- 每个可写根下**已经存在**的 `.git`、`.agents`、`.codex`、`.aws`、`.kama` 重新设为只读。
- 运行中的 KamaClaude 包及 Python 安装目录始终只读，防止改写辅助程序后绕过下一次启动。建议把 KamaClaude 安装在工作区外；editable 安装位于工作区内时，该包源码也会受到保护。
- 自动元数据保护目前不保留尚不存在的上述名称；显式配置的缺失 protected/deny 路径则收紧到最近的现有祖先。两者有意区分，避免为默认元数据保护在宿主创建占位文件。
- 授权路径必须存在；策略根穿过可变符号链接时拒绝执行。macOS 的 `/tmp`、`/var`、`/etc` 标准 `/private` 别名可规范化。
- `cwd` 和策略路径在展开、规范化及解析时拒绝 C0（U+0000–U+001F）、DEL（U+007F）、C1（U+0080–U+009F）控制字符，返回 `SandboxPolicyError`，不会删除字符或改写成另一个路径；该错误中的路径使用转义形式展示。
- 禁止项优先于额外可读写授权；本版不支持在 deny 子树中重新开放更深的路径。
- Linux 的 `/tmp` 是独立临时文件系统；macOS 为每次 Bash 调用分配并回收私有临时目录。显式授权宿主临时目录会扩大该范围。
- 后端缺失或初始化失败返回 `sandbox_*` 错误；疑似权限拒绝返回 `sandbox_denied`；目标命令非零退出返回 `command_failed`。这些错误不会被通用工具执行层自动重试。
- 输出按字节限额持续排空并截断，超时和取消会清理进程组。Linux 另用 PID 命名空间回收后代；macOS 的进程组清理不保证终止主动脱离进程组的后代，但这些进程仍继承 Seatbelt 限制。

本版只覆盖命令子进程。主进程中的 `read_file`、`write_file`、`list_dir`、MCP 及其他工具没有因此获得系统沙盒保护。文件辅助进程、网络代理、旧 Landlock 兼容和内置 Bubblewrap 均不在本版范围。

## 平台依赖

Linux：Python 3.12、系统 `libseccomp.so.2`、安装在 `/usr/bin/bwrap` 或 `/bin/bwrap` 的 Bubblewrap。二进制及其祖先必须由 root 拥有且不能被组或其他用户写入；需要 `--disable-userns`、用户/PID/网络命名空间等功能。内核或外层容器不允许这些功能时明确失败，不降低隔离级别。当前 seccomp 支持 x86_64、aarch64。

macOS：系统 `/usr/bin/sandbox-exec`。四个 `.sbpl` 模板随 Python 包分发，通过路径参数生成动态规则；策略编译或应用失败不回退为普通进程。

Windows：官方 MXC exe 的 ProcessContainer 后端，严格要求 BaseContainer，使用 one-shot 执行；不回退到 AppContainer/BFS/DACL。[配置、模块与验证说明](windows/README.md)。

## 调用方式

```python
import os
from dataclasses import replace
from pathlib import Path

from kama_claude.core.sandbox import (
    NetworkPolicy, SandboxCommand, SandboxManager, spawn, workspace_policy,
)

cwd = Path.cwd()
policy = workspace_policy(cwd)
# 只有应用配置或已经审批的资源授权可以选择放开网络。
policy = replace(policy, network=NetworkPolicy.ALLOW)
command = SandboxCommand(("/bin/sh", "-c", "echo hello"), cwd, dict(os.environ))
request = SandboxManager().prepare(command, policy)
result = await spawn(request)
```

`BashTool(cwd=..., policy=...)` 可注入资源策略。策略不属于模型的工具参数，也不会因 `always_allow` 工具审批而扩大。Windows 另支持 `[sandbox.windows]` TOML 配置。macOS/Windows 的私有临时目录由 BashTool 使用各平台 `process_scratch()` 管理；直接调用底层接口时由调用方管理需要的临时目录授权。

Linux 外层辅助进程先固定挂载源描述符，再调用 Bubblewrap；内层校验挂载身份、关闭宿主描述符、验证能力清零、设置 no_new_privs/seccomp，最后启动目标。辅助程序以 `python -I -S` 加载，执行环境移除 Python、动态加载器和 shell 的启动注入变量。允许网络时只允许 IP socket，保留 Unix socketpair 供内部进程通信，避免经 Unix socket、AF_VSOCK 或 io_uring 访问宿主服务。

限制读取模式额外需要 Python 辅助程序及其运行时的只读挂载；它不是任意二进制的最小文件集隔离。系统默认读取路径和辅助程序依赖可见，显式 deny 仍在所有授权后应用。

## 文件与 Codex 对照

参考版本：[openai/codex ac9db7d8](https://github.com/openai/codex/tree/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs)。下表 Codex 路径相对 `codex-rs/`。本实现按职责借鉴并用 Python 重写，不宣称达到 Codex 的全部兼容性或安全加固程度。

| 本包文件 | Codex 文件 | 职责 |
|---|---|---|
| `__init__.py` | `sandboxing/src/lib.rs` | 公共导出 |
| `models.py` | `protocol/src/permissions.rs`、`protocol/src/models.rs`、`sandboxing/src/manager.rs` | 策略、命令、执行请求和结果 |
| `policy.py` | `sandboxing/src/policy_transforms.rs`、`protocol/src/permissions.rs` | 路径规范化、默认保护、额外授权合并 |
| `manager.py` | `sandboxing/src/manager.rs` | 平台选择与执行请求转换 |
| `spawn.py` | `sandboxing/src/spawn.rs` | 异步执行、输出限额、超时取消 |
| `errors.py` | `sandboxing/src/manager.rs` 的错误类型 | 稳定的错误分类 |
| `denial.py` | `sandboxing/src/denial.rs`、`violation.rs` | 启动失败与疑似沙盒拒绝诊断 |
| `linux/__init__.py` | `linux-sandbox/src/lib.rs` | Linux 包入口 |
| `linux/__main__.py` | `linux-sandbox/src/main.rs` | 独立辅助程序入口 |
| `linux/runner.py` | `linux-sandbox/src/linux_run_main.rs` | 外层隔离、内层校验与执行 |
| `linux/command.py` | `sandboxing/src/landlock.rs` | Linux 辅助程序参数协议 |
| `linux/launcher.py` | `sandboxing/src/bwrap.rs`、`linux-sandbox/src/launcher.rs` | 可信二进制与功能探测 |
| `linux/bwrap.py` | `linux-sandbox/src/bwrap.rs` | 文件系统与命名空间布局 |
| `linux/fd_mount.py` | `linux-sandbox/src/fd_mount.rs` | 固定来源、验证挂载身份、关闭描述符 |
| `linux/seccomp.py` | `linux-sandbox/src/seccomp.rs` | no_new_privs 与系统调用过滤 |
| `macos/__init__.py` | `sandboxing/src/lib.rs` 的 macOS 导出 | macOS 包入口 |
| `macos/seatbelt.py` | `sandboxing/src/seatbelt.rs` | 参数化 SBPL 与启动参数 |
| `macos/seatbelt_scratch.py` | `sandboxing/src/seatbelt_scratch.rs` | 私有临时目录生命周期 |
| `macos/seatbelt_base_policy.sbpl` | `sandboxing/src/seatbelt_base_policy.sbpl` | 默认拒绝与进程基础能力 |
| `macos/seatbelt_network_policy.sbpl` | `sandboxing/src/seatbelt_network_policy.sbpl` | DNS、证书与网络系统服务 |
| `macos/seatbelt_read_only_platform_defaults.sbpl` | `sandboxing/src/seatbelt_read_only_platform_defaults.sbpl` | 基础系统只读路径 |
| `macos/seatbelt_preferences_policy.sbpl` | `sandboxing/src/seatbelt_preferences_policy.sbpl` | 无读取限制时的偏好服务 |

接口细节还参考 [Bubblewrap 官方说明](https://github.com/containers/bubblewrap/blob/main/bwrap.xml) 和 [libseccomp 公共头文件](https://github.com/seccomp/libseccomp/blob/main/include/seccomp.h.in)。

## 验证

```sh
uv run pytest tests/unit/test_sandbox.py tests/integration/test_s5_permission_flow.py -v
uv run ruff check src/kama_claude/core/sandbox tests/unit/test_sandbox.py tests/integration/test_sandbox_native.py
uv run mypy src

# 分别在 Linux 和 macOS 执行；缺少后端时必须失败，不能误报测试通过。
KAMA_REQUIRE_NATIVE_SANDBOX=1 uv run pytest tests/integration/test_sandbox_native.py -v
```

原生测试检查可写工作区、越界写入、元数据保护、读取拒绝、符号链接、网络开关、超时及取消后的子进程清理。Windows 使用独立的 `test_sandbox_windows_native.py`；参数生成单测不能代替目标系统的实际隔离测试。
