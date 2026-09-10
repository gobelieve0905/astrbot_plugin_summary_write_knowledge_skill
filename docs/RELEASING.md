# 验收和发布

日常在 develop；main 保持正式线。0.0.0 为初始开发版本，只更新 Unreleased。

1. 执行 `python3 -B -m unittest discover -s tests -v`、`uv tool run ruff check .` 和隔离验证脚本。
2. 用户在指定测试会话验证真实模型：保存、澄清项目、引用旧话题、多成员更新、仅总结不保存、方法论、索引故障与重试。
3. 准备候选时更新 metadata 与正式更新说明；合并 main，创建不可移动注释测试标签 vX.Y.Z-test.N，记录范围、结果、限制。
4. 候选修复回 develop，合并并递增 N。同提交验收成功才建立同版本注释正式标签。
5. 人工检查 main、版本、同提交测试标签及验收记录；使用 git archive 生成 ZIP，核对实际清单，不得含测试/开发规范。
6. 不建立自动发布 Actions。不自动创建 GitHub 仓库或 Release、不自动推标签或送审 Cloud。两者记录分开。
