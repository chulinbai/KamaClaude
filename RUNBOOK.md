# 运维手册（RUNBOOK）

## 日常操作

### 启动守护进程

```bash
uv run kama-core
```

默认监听 `127.0.0.1:7437`，按 `Ctrl+C` 优雅退出。

### 验证连通

```bash
uv run kama ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f kama-core)
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kama/config.toml` → 项目 `.kama/config.toml` → `.env` → 系统环境变量**。

### 文件访问与路径审批

会话工作区使用输入 `kama-tui` 命令时的终端目录，由 TUI 显式传给 Core 并保存。
Python 解释器、TUI 可执行文件和 Core 可以位于其他目录；`kama chat`、`kama run` 同样使用
客户端启动目录。旧客户端不传 `workspace` 时才沿用 Core 启动目录。
恢复会话要求启动目录与保存的工作区一致；旧记录没有工作区时，在恢复时绑定。
各会话的工具、沙盒 cwd、项目 context、Skill 和子 Agent 使用自己的工作区，不修改 Core
进程的全局 cwd。`./workspace` 是默认输出位置。`read_file`、`write_file`、
`list_dir`、`create_dir` 和 Shell 使用同一套文件系统策略；主 Agent 和子 Agent 均受约束。
访问项目目录及配置授权范围外的普通文件或目录时，TUI 会显示完整路径、读写权限、
文件或目录范围，以及“允许本次 / 允许当前会话 / 拒绝”。目录批准包含目标及其子目录，
单文件批准仅覆盖该文件。已有工具级 `Always allow` 不能绕过路径审批。

例如创建 `F:\code\python\test_file\`：

1. Agent 使用 `create_dir(path)`，审批显示目标 `F:\code\python\test_file`，不会申请父目录 `F:\code\python`。
2. 批准后 Core 仅创建这个目录，其父目录必须已存在。批准前不会创建任何目标。
3. 后续 Shell 通过 `required_paths` 声明该目录，选择会话批准后可复用路径权限；普通工具审批仍适用。

```json
{"command":"Set-Content -LiteralPath 'F:\\code\\python\\test_file\\hello.txt' -Value 'hello'",
 "required_paths":[{"path":"F:\\code\\python\\test_file","access":"write"}]}
```

Shell 的声明只是权限申请。缺失目标返回 `path_not_ready`，应先使用 `create_dir` 或
`write_file` 创建精确目标；不要改成申请父目录，也不要自动重放可能已有副作用的整段命令。
`write_file` 只有在缺失父目录已处于授权范围内时才自动创建它，否则须先逐级申请 `create_dir`。
创建成功的目录或文件即使后续 Shell 失败也会保留。显式禁止路径、运行代码保护目录、
`.git` 等受保护目录，以及不安全的链接路径不通过普通审批放行。

路径会话授权只存 Core 内存，不写 `policy.toml`，也不从日志恢复。同一会话的子 Agent 可复用，
其他会话不可复用；关闭会话、断开所属客户端连接或重启 Core 会清除它。TUI 默认启动创建新会话，
指定 `--resume` 或使用 `/resume` 可继续旧会话，断线重连自动恢复原会话；恢复不会找回旧授权。
正常退出若遇到 `session busy` 会保留界面并提示等待；异常断连会拒绝待审批请求，
撤销后续调用权限。已启动的沙箱进程按原有超时和退出机制结束，不会为后续调用保留权限。

可在全局或项目配置中预先授权已有路径：

```toml
[sandbox.filesystem]
readonly_paths = ["../reference"]
readwrite_paths = ["../outputs"]
denied_paths = ["../private"]
```

这些配置映射到已有的 `FileSystemPolicy`，相对权限路径以会话工作区为基准。
Core 的配置文件仍在 Core 启动时加载；TUI 传递工作区不会重新加载另一份 Core 配置。
读写授权路径必须已存在；缺失禁止路径沿用现有策略，收紧到最近已存在的祖先。
Windows 原有 `[sandbox.windows]` 路径字段仍兼容，并与通用配置合并，禁止项优先。
配置修改需要重启 Core；运行中的临时需求直接通过审批处理。
Shell 使用 Windows/MXC、Linux/Bubblewrap 或 macOS/Seatbelt，额外保留必要系统运行资源的只读权限，
Linux/macOS 默认不再全盘可读。内部日志、会话存储和 MCP 服务自身的文件访问不在此审批范围内。

修改代码后重启 Core 和 TUI（分别在两个终端运行）：

```powershell
.\.venv\Scripts\kama-core.exe
.\.venv\Scripts\kama-tui.exe
```

### 继续历史会话

在 TUI 中输入 `/resume`，使用上下键选择会话、Enter 恢复、Esc 取消；
也可以输入 `/resume <session_id>`，或启动时直接指定：

```powershell
.\.venv\Scripts\kama-tui.exe --resume sess-0123456789ab
```

恢复保留原 session ID、对话、笔记和 run 记录，不调用模型，也不自动重新执行旧任务。
界面只显示恢复成功提示，下一条消息会使用已保存的历史和笔记。
`--resume` 与用于事件回放的 `--replay` 互斥。

列表按最近更新时间排序，只包含 Core 当前项目的 chat 会话及标为“项目未知”的旧记录。
这里的项目是 **Core 启动目录**，不是 TUI 启动目录。旧记录第一次成功恢复后绑定当前项目；
其他项目的会话和 one_shot 会话不能恢复。已压缩的会话继续使用当前摘要，不自动展开备份。

同一会话只允许一个客户端持有。主任务或后台子 Agent 尚未结束时不能恢复或切换；
断线重连会等待原任务结束后恢复原 session。恢复目标不存在、损坏或被占用时显示错误，
保留当前会话；启动恢复失败时可重新输入 `/resume` 选择，不会自动创建替代会话。
关闭、断连前的会话授权和审批请求不会恢复，需要授权的操作会重新申请。

会话记录仍位于 `~/.kama/sessions/<session_id>/`。恢复不修复未落盘的消息，
不重启中断的工具、进程或子 Agent；后台任务仅用于检查当前 Core 中的真实忙碌状态。

### 多 Agent 协作

一个 Session 中的主 agent 和子 agent 共用执行内核。每个子 agent 有稳定的
`agent_id`、独立历史和收件箱；一轮任务结束后变为空闲，追加任务会创建新的
`run_id`，保留原历史和任务工具数据。

TUI 中输入 `/agents`，或在主任务运行时按 **Ctrl+G**，打开管理弹层：

- **发送消息**：进入目标收件箱；运行中的 agent 在完整工具批次后的安全检查点处理，空闲 agent 不自动启动。
- **追加任务**：发送消息并唤醒空闲子 agent；运行中的 agent 继续当前轮，不重置步数。
- **关闭 Agent**：取消并等待该 agent 及其后代清理，释放容量；未处理消息的丢弃数量显示在状态中。
- 列表显示运行、空闲、关闭状态与待处理数量，结果区域显示最近一轮的结果。

模型使用 `spawn_agent`、`send_input`、`agent_result`、`wait_agent`、`close_agent`
协作。兄弟 agent 可以互发消息；等待与关闭仅限后代。主 agent 不接受自动唤醒，
空闲时收到的消息在下一次用户输入前加入主历史。消息接受成功不代表任务已经完成。

默认最多 6 个未关闭子 agent（空闲也占名额），嵌套最多 2 层，每个 Inbox 最多
64 条消息、单条最多 16 KiB UTF-8。Run 结果保留最近 256 个终态记录，活跃身份的
最近结果和正在等待的结果受保护。满额时关闭不用的子 agent 后再创建。

主历史在运行时由 `AgentContext.messages` 持有，`thread.jsonl` 继续保存其持久化记录。
子历史与 Inbox 仅保存在内存；关闭、离开会话或 Core 重启后不能恢复子树。
聊天主轮完成后后台子任务可以继续运行；one-shot 和 Core 退出会先收回子任务，
再关闭共享 MCP 服务。角色白名单限制工具能力，同一会话仍共享工作目录和授权范围。

### `~/.kama/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kama/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KAMA_CONFIG` | `~/.kama/config.toml` | 覆盖配置文件路径 |
| `KAMA_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KAMA_PORT` | `7437` | TCP 监听端口 |
| `KAMA_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KAMA_LOG_FILE` | `~/.kama/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KAMA_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.kama/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f kama-core)` |
| `core not running` | 未启动守护进程 | `uv run kama-core` |
| `Address already in use` | 端口被其他进程占用 | `KAMA_PORT=8000 uv run kama-core` |
| `Config error: KAMA_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KAMA_PORT` 的值 |
