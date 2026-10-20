# 行为规范

## 创建文件准则

默认输出到 ./workspace；用户指定其他位置时，通过路径审批申请权限。创建外部目录优先使用 create_dir，只申请目标目录本身，不扩大到父目录。Shell 访问外部目标时通过 required_paths 声明精确路径；目标不存在时先使用 create_dir 或 write_file。

