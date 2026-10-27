# 通过路径使用图片

在 `kama-tui` 的普通输入框中直接提问，例如：

```text
分析 F:\images\报错.png，解释错误原因。
比较 ./screenshots/before.png 和 ./screenshots/after.png 的布局。
```

Agent 会按需调用 `read_image`。相对路径以当前会话的工作目录为基准，
绝对路径、中文和空格均可使用。工作目录外的图片遵循现有文件读取审批；
拒绝审批后不会读取图片。仅在文字中提到路径不会自动读文件。

用户无需选择或上传附件，但程序会读取图片并将其内容随请求发送给当前模型。
当前模型与所使用的 Anthropic 兼容接口必须支持图片输入；系统不自动切换模型。

## 格式与限制

- PNG、JPEG、WebP、GIF，检查实际文件内容而非只看扩展名。
- 动画只读取首帧；首帧保存为 PNG 快照。
- 单张最多 5 MiB；动画原文件及转换后的首帧都受此限制。
- 单次模型请求最多 20 张图片，累计图片数据不超过 15 MiB，均按 Base64 编码前计算。
- 历史消息中的图片也计入请求预算。超限时缩小图片，或先执行 `/compact` 再读取。

## 会话快照

图片按内容哈希去重，保存于 `<session目录>/images/`，历史只保存图片引用。
原文件被修改或删除后，仍可继续追问当时读取的内容。
没有会话的单次运行使用 `<run目录>/images/`；同一会话的子 Agent 共用图片缓存，
同时继续遵循各自的工具白名单。

工具结果会显示图片 ID、来源、格式、尺寸和字节数。模型可调用
`read_image(image_id="...")` 重新查看快照；`path` 和 `image_id` 必须二选一。
压缩会话时模型会查看图片并保留识图结论，摘要会附带图片 ID，后续可以重新读取。
关闭会话不会删除图片缓存；图片与会话目录一起保留或清理。

TUI 不直接绘制图片，展示读取状态和图片摘要。历史、工具事件和 trace 不记录图片 Base64；
只有实际模型请求包含编码内容。没有新增上传接口，已有 IPC 输入字段保持兼容。

## MCP

图片处理工具的路径参数按照该工具的 `inputSchema` 传递，不会猜测参数名或自动上传文件。
MCP 服务必须能读取该路径，例如本机服务或共享文件系统；远程服务无法仅凭本机路径读取文件。

支持以下 MCP 工具结果：

- 标准 `image` 块：`data` 是 Base64，`mimeType` 是实际图片 MIME 类型。
- 内嵌图片资源：`type="resource"`，内部 `resource.blob` 为 Base64，提供 `mimeType`。
- 文本中的本机绝对图片路径：模型继续调用 `read_image`，执行正常的路径审批。

图文顺序及 `isError` 保留。无效编码、格式不符和超限会明确返回错误。
MCP 操作不会因执行失败或图片转换失败自动重放，以免重复产生副作用。
资源 URI 不会自动下载；需要 MCP 服务返回图片内容或可访问的本机绝对路径。

## 验证

```powershell
python -m pytest tests/unit/test_images.py tests/integration/test_image_flow.py -q
```

默认使用本机 MCP TCP 服务和模拟模型，不调用付费 API。
如需使用当前 `.env` 配置的真实模型验收（发送合成纯色图片，并产生 API 调用费用）：

```powershell
$env:KAMA_TEST_VISION = "1"
python -m pytest tests/integration/test_image_flow.py -k live -v
```

真实模型测试分别覆盖本地读图与 MCP 图片结果；MCP 工具返回合成图片，网络协议另由本机 TCP 测试覆盖。
