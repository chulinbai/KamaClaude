# 历史检索

KamaClaude 在当前工作区的会话中提供 `history_search` 和 `history_read` 工具。
模型可以搜索旧决策、错误和尝试，再展开带来源的原文。历史不会自动注入每轮上下文，
也不会自动写入 `context.md` 或 `notes.md`；旧结论需要结合当前代码判断。

## 使用

启动 daemon 后，在项目目录执行：

```console
kama history search "历史 检索"
kama history search "auth_token" --since 2026-10-01 --limit 20 --json
kama history read sess-xxxxxxxxxxxx:12
kama history read sess-xxxxxxxxxxxx:12 --offset 12000
kama history status
kama history reindex
```

搜索按空白分隔字面关键词，所有词必须出现在同一记录中。支持中文短词、路径和代码标识符，
不执行正则或 SQL。`--session SESSION_ID` 可限制目标会话。
命中时优先展示原始消息和工具证据，笔记与压缩摘要排在其后，不把派生记录累计为独立佐证。
`--workspace ABSOLUTE_PATH` 指定工作区，默认使用 CLI 启动目录。

`read` 默认展开前后各两条记录，按会话序号跨分片读取。
有 `next_offset` 时保留相同的记录 ID、`--before` 和 `--after`，继续传入该 offset。
CLI 的所有子命令支持 `--json`，调用不会创建聊天会话或运行模型。

`--all` 请求跨工作区查询，需要单独批准历史范围。交互终端可批准本次或当前连接；
非交互脚本无法新批准其他工作区，会返回权限错误。Agent 的跨工作区请求显示在现有
TUI/CLI 审批组件中，工具级“始终允许”不能跳过范围审批。
历史授权不授予项目文件的读取权限，断连或会话关闭后失效，按 ID 读取也会重新检查范围。

结果中的 `complete` 表示是否完成本次范围的搜索，`truncated` 表示是否裁剪输出数量。
`complete=false` 可能是超时或源文件损坏，不能解释成“没有匹配历史”。
`reason=index_building` 可以与 `complete=true` 同时出现：这表示索引正在构建，
本次使用扫描完整搜索了目标范围。

## 配置

将以下小节加入全局 `~/.kama/config.toml`，或 daemon 启动目录的 `.kama/config.toml`：

```toml
[history]
backend = "auto"                   # auto / scan / sqlite
segment_max_bytes = 33554432        # 32 MiB，按实际序列化字节滚动
index_threshold_bytes = 104857600   # 100 MiB，按本次候选范围的文本体积上界选择
search_timeout_seconds = 3.0
max_results = 10                   # 1–50；调用参数可指定 limit
max_read_chars = 12000
```

这些参数使用现有 TOML 配置优先级，不新增单独的环境变量。
索引构建不受单次搜索的 3 秒预算限制；查询取消会通知实际工作线程停止。
首次构建、缓存损坏或索引不支持时，查询可降级为有预算的扫描。
纯一至两个字符查询无法利用 trigram 索引，较大范围下仍可能超时并返回部分结果。

## 存储与恢复

每个会话拥有独立的目录：

```text
~/.kama/sessions/<session-id>/
  meta.json
  thread.jsonl
  notes.md
  history/
    writer.lock
    manifest.json
    history-000001.jsonl
    history-000002.jsonl
```

`thread.jsonl` 继续保存用于续聊的上下文，compact 可以改写它。
`history/` 保存原始用户消息、完整模型回复、工具调用及结果、笔记和压缩摘要。
原始记录与派生记录具有不同类型，thinking 和图片编码不参与搜索，历史工具返回的摘录不再次索引。

分片只有最后一个可追加；封存后不再修改。单条记录不会跨文件拆开，超过 32 MiB 的记录
独占一个超大分片，写完封存，状态命令会报告它。记录身份为 `session-id:sequence`，
会话序号跨分片连续，不因滚动或重建索引改变。

普通消息完整追加并 flush 后才进入后续处理；compact、滚动、run 完成和正常关闭执行 fsync。
compact 前归档失败会保留当前原文并报告失败。原文归档和 thread 是分别提交的文件，
不保证跨文件事务；thread 写入失败不会撤销归档，也不会重新执行工具来补写。

manifest 是可重建的目录。重启检查到活动分片未完成尾行时，将残留字节保存在
`recovery-*.bin` 后恢复可追加位置。封存分片损坏或缺失会产生警告，不静默当作完整结果。
已经丢失的旧原文无法从摘要恢复；流式回复中断前尚未形成完整回复的 token 不承诺归档。

旧会话首次使用归档时导入现存 thread、备份、notes 和 summary，保留出处，支持中断后重试。
相同副本会去重，真实重复提问仍保留。旧文件不删除，现有 resume 行为不变。

SQLite 数据库位于 `~/.kama/sessions/history-index.sqlite3`，只保存检索投影和位置。
缓存按分片增量维护，原始记录按当前归档再次验证。新索引构建完成后才发布，
原始归档不会因 `reindex` 被删除。索引就绪后小范围查询仍可使用扫描。

第一版长期保留归档，不自动删除、合并或压缩旧分片；分片不限制总磁盘占用。
不导入其他 Agent 的历史，也不持久化子 Agent 私有上下文；传回主会话的协作内容正常归档。

## 验证与测量

```console
python -m pytest tests/unit/test_history_archive.py tests/unit/test_history_search.py tests/unit/test_history_tools.py tests/integration/test_history_ipc.py
python scripts/benchmark_history.py --sizes 10 100 1024
```

性能脚本只在 `workspace/history-benchmark-<id>/` 生成合成数据，保留分片、索引与
`results.json` 供复查，不读取用户的真实历史，不调用模型。默认会生成约 1.1 GiB 原文，
索引还会额外占用磁盘。测量包括直接扫描、首次构建、精确索引查询、中文短词查询及进程峰值内存。
合成语料具有重复性，结果用于验证规模行为，不代表所有真实历史的性能。
