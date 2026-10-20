# Windows BaseContainer one-shot

Python 调用官方 `wxc-exec.exe`，每条命令独立创建/释放 BaseContainer。基线为项目现有 MXC `1.0.0+7bf21024`，使用精确 JSON 协议 `1.0.0`。不使用 State-aware、`sandboxId` 或常驻 worker；`containerId` 每次随机生成，只标识本次执行。

## 配置

默认使用源码项目的 `bin/mxc/x64/wxc-exec.exe`（ARM64 使用 `arm64` 子目录）。安装 wheel 后应配置外部 exe 的绝对路径；不会搜索 PATH、下载或修改 MXC，也不将整套发行包打进 wheel。

```toml
[sandbox.windows]
# 源码项目可省略；安装 wheel 后指定外部 exe。
mxc_executable = 'F:\code\python\KamaClaude\bin\mxc\x64\wxc-exec.exe'
network = "deny"
readonly_paths = []
readwrite_paths = []
denied_paths = []
```

沿用全局/项目 TOML 的覆盖规则；`KAMA_MXC_EXECUTABLE` 覆盖 exe 路径。权限数组中的相对路径相对工具工作区。主 Agent、子 Agent、嵌套子 Agent 继承相同配置。明确传入 `BashTool(policy=...)` 时以该策略为准。

## 权限与执行

- `readonlyPaths` 包括读授权与保护项；`readwritePaths` 包括工作区、额外写授权和私有临时目录；`deniedPaths` 保留显式拒绝。额外写授权不能覆盖保护项。系统目录和 Python 运行时补充为初始化所需的只读路径；不自动授予盘符根读取或枚举权限，两者在实测宿主上分别泄露外部文件内容和文件名。
- `network=deny` 的 TCP、直接 UDP 与 Windows DNS API 禁网测试通过。Windows 的 `allow` 开放出站，同时请求禁止入站和宿主 loopback；直接 TCP/UDP 回环和 TCP 入站测试通过。但在当前 Windows/MXC 版本，`Resolve-DnsName -Server 127.0.0.1` 仍能向本机 DNS 服务发送请求。因此 **allow 模式不能宣称完全隔离宿主网络服务**；额外出站回环 CIDR 拒绝规则也未消除此缺口。代理、域名/IP 白名单和开放端口暂不提供。
- 拒绝路径控制字符、UNC/设备路径、ADS、保留名称、尾随点/空格及 reparse point/junction。宿主持有路径组件的禁止删除共享句柄，阻止重命名父目录或替换路径绕过限制；不修改宿主 DACL。
- 执行前扫描用户授予的文件/目录，发现多硬链接文件、扫描错误、超过 100,000 项或约 10 秒的扫描预算时拒绝整次执行，不删除或复制用户文件。自动保护的宿主代码、Python 和 MXC 安装目录属于可信运行时，跳过扫描。此检查防止已有硬链接绕过路径规则；本次测试也验证了运行中向未授权/只读/禁止目标创建硬链接被内核阻止。它不是对恶意宿主并发修改文件系统的完整证明。
- 每次执行使用 `--probe` 和完整请求 `--dry-run` 检查 BaseContainer，要求无 DACL augmentation、BFS 未编译启用，并固定 `allowDaclMutation=false`。后端不可用或策略不可执行时返回非重试错误，不能无沙盒执行。
- 配置经 UTF-8/Base64 的 `--config-base64` 参数传递；Base64 不是加密，不记录完整参数。编码后超过 Windows 命令行长度上限时拒绝，不隐式改用文件或 stdin。
- PowerShell 使用 `-NoProfile -NonInteractive -EncodedCommand`，初始化 cwd、UTF-8 和文本输出。系统 PowerShell 需要 Win32k 初始化，因此 `ui.disable=false`；保留剪贴板/输入注入限制及 `registryRead`，隐藏控制台窗口。
- 仅传递 Windows 运行所需的环境白名单（SystemRoot、LOCALAPPDATA、PATH、用户目录、TEMP/TMP 等），不继承 API 密钥、MXC tier 环境覆盖或动态运行时注入变量。
- stdin 接 NUL，读取立即得到 EOF。stdout/stderr 分开排空；Windows 结果的 `stdout/stderr` 保存各路文本，`output` 按读取先后合并，不保证跨流严格时序。总输出限额 64 KiB，超过后继续排空；另保留有界 stderr 尾部识别 MXC 错误。
- MXC 先以挂起状态启动，加入禁止 breakaway 的 Job Object 后才恢复执行。正常退出、超时、取消均回收整个进程树，再清理临时目录和路径锁。管道通过非阻塞轮询读取，不产生不可取消的阻塞读线程。进程清理最多 5 秒；工具外层额外预留 45 秒用于路径审计、两次最多 10 秒的检查及清理。

工作区文件修改可以保留；shell 变量、cwd 变化和后台进程不跨命令保留。后台子 Agent 的每次命令拥有独立 Job，不随父问答完成而被误杀。`read_file`、`write_file`、`list_dir` 仍在宿主进程运行，不受此沙盒保护。

## 模块

| 文件 | 职责 |
|---|---|
| `policy.py` | 默认权限、Windows 路径规则与 MXC 文件系统映射 |
| `command.py` | PowerShell 命令、环境白名单、JSON/Base64 与长度检查 |
| `launcher.py` | exe 架构检查、能力/策略预检及错误分类 |
| `process.py` | 挂起启动、Job Object、管道、超时与取消 |
| `path_locks.py` | 固定路径身份，防止重命名与 reparse 替换 |
| `hardlinks.py` | 在用户授权树中检查预置硬链接，失败时拒绝启动 |
| `scratch.py` | 每次执行的私有临时目录生命周期 |

参考 [MXC 1.0 协议](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/docs/schema.md) 及对应版本 `src/mxc-sdk/src/backends/process_container/common/`。

## 验证

```powershell
uv run pytest tests/unit/test_sandbox_windows.py tests/unit/test_sandbox.py tests/unit/test_sandbox_paths.py
$env:KAMA_REQUIRE_WINDOWS_SANDBOX = '1'
# 可替换为本机可达的外部 TCP 服务；不能使用宿主自身地址。
$env:KAMA_SANDBOX_TEST_ENDPOINT = 'github.com:443'
uv run pytest tests/integration/test_sandbox_windows_native.py -v
uv run pytest tests/unit/test_sandbox_windows_security.py tests/integration/test_sandbox_windows_security.py -v
```

Windows 原生测试默认跳过；设置必测变量后能力缺失或隔离失败会报错。TCP 测试只建立连接；UDP 测试发送固定的 example.com DNS 问题，可用 `KAMA_SANDBOX_TEST_DNS` 指定可达解析器。DNS API 用例临时绑定 `127.0.0.1:53`，只回答本地测试问题，不修改系统 DNS 配置；端口被占用时测试会报错，不停止已有服务。当前 allow 模式的 DNS API 用例保留为真实失败，不标记 xfail 或 skip。需要在真实、支持 BaseContainer 的宿主运行；外层沙盒可能限制临时目录、网络和嵌套 Job，从而无法完成验证。

完整范围、修复与剩余问题见 [2026-10-18 安全测试报告](../../../../../docs/windows-sandbox-security-2026-10-18.md)。
