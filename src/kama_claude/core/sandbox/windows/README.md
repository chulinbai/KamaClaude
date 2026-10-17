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

- `readonlyPaths` 包括读授权与保护项；`readwritePaths` 包括工作区、额外写授权和私有临时目录；`deniedPaths` 保留显式拒绝。额外写授权不能覆盖保护项。系统目录、驱动器根和 Python 运行时补充为初始化所需的只读路径；Windows 盘符根不能当作 Linux 的递归全盘授权。
- `network=deny` 禁止出站、入站和宿主 loopback。Windows 的 `allow` 仅开放出站，入站和宿主 loopback 仍禁止，包括连接宿主 LAN 地址。代理、域名/IP 白名单和开放端口暂不提供。
- 拒绝路径控制字符、UNC/设备路径、ADS、保留名称、尾随点/空格及 reparse point/junction。宿主持有路径组件的禁止删除共享句柄，阻止重命名父目录或替换路径绕过限制；不修改宿主 DACL。
- 每次执行使用 `--probe` 和完整请求 `--dry-run` 检查 BaseContainer，要求无 DACL augmentation、BFS 未编译启用，并固定 `allowDaclMutation=false`。后端不可用或策略不可执行时返回非重试错误，不能无沙盒执行。
- 配置经 UTF-8/Base64 的 `--config-base64` 参数传递；Base64 不是加密，不记录完整参数。编码后超过 Windows 命令行长度上限时拒绝，不隐式改用文件或 stdin。
- PowerShell 使用 `-NoProfile -NonInteractive -EncodedCommand`，初始化 cwd、UTF-8 和文本输出。系统 PowerShell 需要 Win32k 初始化，因此 `ui.disable=false`；保留剪贴板/输入注入限制及 `registryRead`，隐藏控制台窗口。
- 仅传递 Windows 运行所需的环境白名单（SystemRoot、LOCALAPPDATA、PATH、用户目录、TEMP/TMP 等），不继承 API 密钥、MXC tier 环境覆盖或动态运行时注入变量。
- stdin 接 NUL，读取立即得到 EOF。stdout/stderr 分开排空；Windows 结果的 `stdout/stderr` 保存各路文本，`output` 按读取先后合并，不保证跨流严格时序。总输出限额 64 KiB，超过后继续排空；另保留有界 stderr 尾部识别 MXC 错误。
- MXC 先以挂起状态启动，加入禁止 breakaway 的 Job Object 后才恢复执行。正常退出、超时、取消均回收整个进程树，再清理临时目录和路径锁。管道通过非阻塞轮询读取，不产生不可取消的阻塞读线程。进程清理最多 5 秒；工具外层额外预留 35 秒用于两次最多 10 秒的检查及清理。

工作区文件修改可以保留；shell 变量、cwd 变化和后台进程不跨命令保留。后台子 Agent 的每次命令拥有独立 Job，不随父问答完成而被误杀。`read_file`、`write_file`、`list_dir` 仍在宿主进程运行，不受此沙盒保护。

## 模块

| 文件 | 职责 |
|---|---|
| `policy.py` | 默认权限、Windows 路径规则与 MXC 文件系统映射 |
| `command.py` | PowerShell 命令、环境白名单、JSON/Base64 与长度检查 |
| `launcher.py` | exe 架构检查、能力/策略预检及错误分类 |
| `process.py` | 挂起启动、Job Object、管道、超时与取消 |
| `path_locks.py` | 固定路径身份，防止重命名与 reparse 替换 |
| `scratch.py` | 每次执行的私有临时目录生命周期 |

参考 [MXC 1.0 协议](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/docs/schema.md) 及对应版本 `src/mxc-sdk/src/backends/process_container/common/`。

## 验证

```powershell
uv run pytest tests/unit/test_sandbox_windows.py tests/unit/test_sandbox.py tests/unit/test_sandbox_paths.py
$env:KAMA_REQUIRE_WINDOWS_SANDBOX = '1'
# 可替换为本机可达的外部 TCP 服务；不能使用宿主自身地址。
$env:KAMA_SANDBOX_TEST_ENDPOINT = 'github.com:443'
uv run pytest tests/integration/test_sandbox_windows_native.py -v
```

Windows 原生测试默认跳过；设置必测变量后能力缺失或隔离失败会报错。网络测试只建立 TCP 连接，不发送应用数据。需要在真实、支持 BaseContainer 的宿主运行；外层沙盒可能限制临时目录、网络和嵌套 Job，从而无法完成验证。
