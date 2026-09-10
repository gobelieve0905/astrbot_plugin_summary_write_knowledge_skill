# 开发架构与接入依据

## 已核验接口

2026-09-10 从现有 AstrBot 4.28.0 运行镜像读取程序源码（不读取生产聊天或凭据）：

- `Context.kb_manager`、`get_using_provider_async(umo)`。
- `KnowledgeBaseManager.create_kb/get_kb/get_kb_by_name`。
- `KBHelper.upload_document/list_documents/get_document/get_chunk_count_by_doc_id/delete_document`。
- `FaissVecDB.retrieve/count_documents`；使用原嵌入模型，候选过滤后调用原 Rerank Provider。
- `filter.llm_tool/on_llm_request/on_llm_response`、`ToolSet.remove_tool`。
- `quote_topics.binding.v1` 为现有引用续聊插件内部绑定，不是 AstrBot 公共标准。只读 `scope`、`topic.cid`、`topic.owner`、`original_mid`；不读写它的 SQLite。

本插件 prepare priority=-20000，在话题插件 -10000 请求校验后执行。工具再次核验事件身份、话题绑定和权限。没有绑定时隐藏工具，直接调用也拒绝。没有卡片私有依赖。

## 模块

- models.py：严格输入、来源对象、确定性 Markdown/Skill 生成。
- store.py：SQLite active/pending/superseded 版本状态；活动版本唯一约束。
- backend.py：真实入库与 FAISS 检索适配。
- service.py：权限、项目注册、写入状态协调、原子文件、有效版本检索。
- prompts.py：Agent 整理规则与独立模型校验提示。
- main.py：话题快照、工具、权限与最终回执防护。

## 状态与并发

单进程全局异步锁覆盖本插件的写入和检索，不持有跨进程事务。网络调用期间不打开 SQLite 事务。冻结来源、保存 pending、文件 fsync/replace、上传、记录 doc_id、检查所有块可从向量索引回读、核对文件、SQLite CAS 切换 active，最后清理旧索引。

文件名由摘要/UUID 生成，模型不能指定路径。重试根据消息+计划摘要去重；失败上传可按唯一文件名找回原文档；后续消息可在同一话题原样重试 pending。来源快照同时保存于 SQLite 来源字段，允许在文件写入中断后恢复 topic.json。正文只索引来源编号等元数据，不索引原始整段聊天。

跨文件、SQLite 和 FAISS 没有原子事务。SQLite active 清单是唯一有效版本依据，原生检索不具备本插件版本与权限过滤，管理规则应经插件工具查询。崩溃留下的 pending 不会通过插件返回。未自动完成的旧索引清理有回执标记，保留本地审计。

范围调整必须另行明确处理，不能把 AppLovin 规则更新成 all，或跨项目/类型替换。版本 CAS 拒绝多人覆盖。单条写入原子切换，多条成果独立，不提供整批事务。

## 尚未宣称的能力

不自动迁移原生旧文档、不捕获未进入 AstrBot 历史的全群聊天、不注册全局 Skill、不部署到生产。没有真实商业模型的语义准确性验收、卡片按钮验收、多进程保证或未来版本兼容保证。后续如引入原生文档迁移，必须明确记录旧 doc_id、范围与迁移完成状态，避免双份有效内容。

## 可用知识库范围

`template_kb` 保留字符串存储，以换行分隔名称/ID；空字符串或纯空白表示全部。配置只限定候选库，不混合项目，不广播写入。目录排除绑定到当前无权限项目的库。首次登记可匹配同名库或明确目标库；后续固定 `kb_id`，读、检索和写都重新检查范围。

新项目在不限库的模式下可建立同名库；多套模型配置时必须指定 `model_source`。实际写入的库名与 ID 进入来源记录及回执。原生旧文档不自动接管，历史内部管理库不迁移。
