# 项目协作规范

- 本仓库是独立 AstrBot 插件，与话题插件和飞书卡片插件保持职责分离。
- Git 提交标题和说明使用简体中文。
- 保留 main 正式分支和 develop 开发分支；日常开发仅在 develop，不创建 dev。
- 日常只更新 CHANGELOG 的 Unreleased；候选验收时才升 metadata.yaml 版本。
- 候选合并 main 后建立不可移动的注释测试标签 vX.Y.Z-test.N，记录验证范围、结果、限制；正式验收后同提交建立版本一致的注释正式标签。
- 不添加 GitHub Actions 自动测试、发布或市场送审。GitHub Release 与 AstrBot Cloud 市场验收分开记录。
- 开发文档和测试用 .gitattributes export-ignore 排除；保留 README、CHANGELOG、LICENSE、用户说明和运行代码。发布检查真实 ZIP 清单。
- 不使用生产聊天、凭据、模型调用做自动测试。部署与发布需要明确范围；本地开发不代表生产部署。
