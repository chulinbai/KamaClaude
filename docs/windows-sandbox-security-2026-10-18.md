# Windows 沙盒安全测试报告（2026-10-18）

## 结论

发现并修复两个文件权限问题：自动盘符根授权导致越界读取/枚举；预置硬链接能够绕过禁止路径。修复后，普通文件和目录的拒绝、只读放行、读写放行以及真实审批生命周期均通过测试。

**仍有一个未修复的网络边界缺口：`network=allow` 时 Windows DNS API 可以向宿主回环地址发送 DNS 请求，即使 MXC 请求中设置了 `ingress.hostLoopback=deny`。** 直接 TCP/UDP 回环测试通过，不能据此推出系统 DNS API 也被隔离。本次保留失败测试，不标记 xfail、不跳过、不降级沙盒执行。

这是一份功能与对抗性回归结果，不是“沙盒绝对安全”的证明。

## 环境与结果

- 宿主：Windows `10.0.26200.0`，x64。
- Python：`3.12.14`。
- exe：项目现有 `bin/mxc/x64/wxc-exec.exe`，文件版本 `1.0.0`，对应源版本 `7bf210247986cb73b1b314df60c2f1109c479c0b`。
- exe SHA256：`AA444C0FC4B7F155FD8631068FFAFB453D71D8B47FC47D417A74C9545A344031`。
- `--probe`：`base-container`；`needsDaclAugmentation=false`；`bfsCompiledIn=false`；支持 deny/enumerate/ingress-host-loopback。
- 所有文件攻击只作用于测试临时文件。TCP 只握手；外部 UDP 只发送固定 example.com DNS 查询；本地 DNS 服务仅处理测试域名，不修改系统 DNS 配置。

| 验证批次 | 通过 | 失败 | 跳过 |
|---|---:|---:|---:|
| 全部单元测试 + Windows 原生沙盒 + 路径审批/IPC + 权限流程 + 通信集成回归 | 594 | 1 | 5 |
| 此后追加的 IPv4/IPv6 × allow/deny 直接 UDP 回环验证 | 4 | 0 | 0 |

全量批次耗时 95.16 秒，结果保存在 `.pytest_cache/win-security-results.xml`。新增测试文件共有 **25 项单元用例、42 项 Windows 原生用例**；后者包含 1 项已知失败。5 项跳过分别为 4 项仅面向 Linux/macOS 的旧 Bash 用例，以及 1 项宿主不允许创建符号链接的既有用例；目录联接测试实际执行通过。

本次改动的 Ruff 检查通过；Windows Mypy 检查通过（113 个源文件）。Linux/macOS Mypy 仍报告已有的 `core/permissions/paths.py:238` 对 `os.O_BINARY` 的平台类型错误，该文件不是本次修改范围。没有在 Linux/macOS 上运行原生隔离测试。

## 覆盖矩阵

| 领域 | 拒绝/攻击验证 | 批准/正向对照 | 结果 |
|---|---|---|---|
| 外部文件与目录 | 未授权读、写、枚举、删除、父目录写入 | 精确文件/目录的只读与读写授权；检查实际磁盘内容 | 通过 |
| 权限粒度 | 文件授权不覆盖同级文件；只读不能写；可写父目录中的 deny/readonly 优先 | 可写目录内创建、修改和删除文件 | 通过 |
| 路径变体 | `..`、大小写、扩展路径、ADS 不能绕过 deny；目录联接不能访问外部目标 | 含中文、空格、单引号、方括号的合法路径 | 通过 |
| 链接与重命名 | 预置硬链接拒绝启动；运行中新建越界硬链接失败；受保护祖先不能重命名 | 普通单链接文件和正常工作区可执行 | 通过 |
| 审批 | deny_once 不执行；allow_once 不复用；跨会话不复用；会话关闭后拒绝 | 真实 PermissionManager → invoke_tool → Bash → MXC，allow_once/allow_session 的读/写授权实际生效 | 通过 |
| TCP 出站 | deny 阻止连接，子进程继承策略 | 宿主先验证可达外部端点；allow 允许域名及字面 IP 连接 | 通过 |
| UDP 出站 | deny 阻止直接 UDP DNS 请求 | 宿主验证解析器可达，allow 收到匹配事务 ID 的 DNS 响应 | 通过 |
| 直接宿主回环 | IPv4/IPv6 TCP、UDP 在 allow/deny 下都无法访问宿主监听器 | 宿主自身连接同一测试监听器成功 | 通过 |
| TCP 入站 | 宿主无法连接沙盒监听器，或监听本身被拒绝 | 测试根据沙盒实际分配端口尝试连接 | 通过；未测试远端机器入站 |
| Windows DNS API | deny 模式请求被拦截 | 宿主 DNS API 正向对照成功 | deny 通过；allow 下回环隔离失败 |
| 进程与输出 | 超时/取消/正常退出回收子进程；Job 分配或 ResumeThread 失败时不执行命令并释放句柄 | 正常输出、中文、stderr、非零退出码、stdin EOF、大输出排空、并发另一命令存活 | 通过 |
| 后端与错误 | 缺失/错误架构 exe、probe 降级、dry-run 失败、策略错误都拒绝；沙盒错误不自动重试 | 可用 BaseContainer 正常执行，后端超时独立分类 | 通过 |
| 环境与工作目录 | 不继承模拟 API 密钥、启动钩子、MXC 强制降级变量 | 正确 cwd 和运行所需环境 | 通过 |

网络“批准”在现有实现中指配置 `NetworkPolicy.ALLOW` / `[sandbox.windows].network="allow"`，没有新增交互式网络审批。普通工具审批不会自动开放网络。

## 修复 1：自动根目录授权过宽

原 `windows/policy.py` 自动将系统盘及工作区盘符根加入 `readonlyPaths`。在实测宿主上，未授权外部文件及相邻文件均能读取。中间尝试将根目录改为 `enumeratePaths` 后，内容访问受限，但外部目录文件名仍可枚举。

最终移除两种自动根授权，保留工作区、已批准路径及明确的系统/Python 运行目录。PowerShell 通过已有 cwd 初始化正常运行。`test_external_grant_matrix` 用 none/read/write × file/directory 六种组合同时检查“该拒绝的拒绝、该放行的放行”，避免只检查禁止写入而漏掉读取泄露。

## 修复 2：预置硬链接绕过路径权限

复现：测试在可写工作区内创建一个硬链接，指向工作区外被显式 denied 的文件。修复前，对工作区内别名进行读取和写入均成功，禁止文件的原内容会被改写。路径锁阻止重命名，但不能改变已有硬链接共享同一文件对象的事实。

新增 `windows/hardlinks.py`，在取得路径锁后、启动 MXC 前审计用户授权树；遇到多硬链接文件直接抛出 `SandboxPolicyError`。测试确认拒绝启动且文件内容、链接身份保持不变。审计不删除链接、不复制文件、不扩大策略。新增请求字段仅携带审计根，未改变模型工具参数。

审计限制与代价：

- 正常硬链接也会保守拒绝，即使应用认为它有合法用途。
- 最多审计 100,000 项，并检查约 10 秒时间预算；扫描错误也拒绝执行。
- 自动保护的宿主代码、Python 和 MXC 安装目录作为可信运行时跳过；目录联接不递归跟随。
- 为扫描预留时间，Windows 工具外层执行余量从 35 秒增加到 45 秒，命令本身 timeout 不变。
- 审计属于启动前检查，不能替代对恶意宿主进程并发修改或已损坏运行时的防护。

## 未修复：allow 模式的 Windows DNS 回环通道

测试临时启动一个 `127.0.0.1:53` UDP DNS 服务，并用宿主 PowerShell 查询确认服务可达。每次使用新的测试域名，避免 DNS 缓存造成假阳性。

随后沙盒执行类似命令：

```powershell
Resolve-DnsName -Name kama-security-<随机值>.example.com -Type A `
  -Server 127.0.0.1 -DnsOnly -NoHostsFile -QuickTimeout -ErrorAction Stop
```

`network=deny` 时服务没有收到请求；`network=allow`、`ingress.default=deny`、`ingress.hostLoopback=deny` 时，服务实际收到请求。额外添加 `127.0.0.0/8` 与 `::1/128` 出站拒绝规则后再次复现，因此没有保留这项无效配置改动。

这证明了本机 DNS 服务可收到沙盒控制的 DNS 查询，不证明任意宿主 TCP 服务都可访问。直接 TCP/UDP 回环仍受限。当前调用路径及现有配置不能提供已验证的修复，涉及 MXC/Windows DNS 通道的实现边界；没有修改官方 exe，也没有擅自关闭已批准的全部出站能力。需要严格宿主网络隔离的部署，应继续使用已验证的 deny 模式，或另行设计并验证能够约束此通道的后端/代理方案。

失败用例保留为 `test_windows_dns_client_cannot_bypass_host_loopback[allow]`，原生安全测试会明确报红。

源码参考：[网络能力随出站策略自动添加](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/src/mxc-sdk/src/backends/process_container/common/network_policy_helpers.rs)、[PSEC 出入站规则转换](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/src/mxc-sdk/src/backends/process_container/common/base_container_helpers.rs)。源码表明有配置传递路径；上述限制的实际效果以本次收包证据为准。

## 复跑

```powershell
$env:KAMA_REQUIRE_WINDOWS_SANDBOX = '1'
# 可选：使用本机可达的外部 TCP/DNS 端点；不能用宿主地址作外部正向对照。
$env:KAMA_SANDBOX_TEST_ENDPOINT = 'github.com:443'
# $env:KAMA_SANDBOX_TEST_DNS = '<可达的 DNS IPv4 地址>'

.venv\Scripts\python.exe -m pytest tests/unit/test_sandbox_windows_security.py `
  tests/integration/test_sandbox_windows_native.py `
  tests/integration/test_sandbox_windows_security.py `
  tests/integration/test_path_permissions_native.py -v

# 单独复现剩余缺口；会返回失败。
.venv\Scripts\python.exe -m pytest tests/integration/test_sandbox_windows_security.py `
  -k windows_dns_client -v
```

DNS API 用例需要本机 UDP 53 端口空闲；如果已有服务占用，测试报错而不停止该服务。外部 DNS 可能丢包，测试使用最多三次有界尝试并保存错误诊断；不可达的正向对照导致测试失败，不会当作成功拦截。

## 尚未覆盖的边界

本次验证的是外部命令的 BaseContainer 边界及与路径审批的接入；`read_file/write_file/list_dir/create_dir` 仍由宿主权限控制器执行，不因这些原生测试通过而自动获得 OS 沙盒隔离。未覆盖 ARM64、其他 Windows/MXC 版本、外部 IPv6 网络、真实远端机器主动入站、SMB/命名管道、所有 Windows RPC/COM 服务、内核漏洞、长时间竞态压力与资源耗尽。测试通过的断言仅适用于已列出的用例和宿主版本。
