"""Adapter verified against installed AstrBot 4.28.0 source."""

from __future__ import annotations

import json

from .directory import selections
from .models import KnowledgeError, digest


class AstrBotBackend:
    def __init__(self, context, config):
        self.context = context
        self.config = config

    def configured_names(self):
        return set(selections(self.config.get("template_kb", []), legacy_text=True))

    def selected(self, kb):
        names = self.configured_names()
        return not names or kb.kb_id in names or kb.kb_name in names

    async def catalog(self, blocked_ids=()):
        return [
            {"id": kb.kb_id, "name": kb.kb_name}
            for kb in await self.context.kb_manager.list_kbs()
            if kb.kb_id not in blocked_ids and self.selected(kb)
        ]

    async def ensure(self, project, plan=None, blocked_ids=()):
        manager = self.context.kb_manager
        requested = plan.knowledge_base if plan else ""
        if project["kb_id"]:
            helper = await manager.get_kb(project["kb_id"])
            if helper and requested and requested not in {helper.kb.kb_id, helper.kb.kb_name}:
                raise KnowledgeError("项目已绑定其他知识库，不能在保存时切换索引库。")
        else:
            choices = await self.catalog(blocked_ids)
            target = requested or project["name"]
            matches = [kb for kb in choices if target in {kb["id"], kb["name"]}]
            if len(matches) == 1:
                helper = await manager.get_kb(matches[0]["id"])
            elif requested or len(matches) > 1:
                raise KnowledgeError("指定知识库不可用或不唯一，请从可用库目录选择准确 ID。")
            else:
                if not plan or not plan.create_project:
                    raise KnowledgeError(
                        "找不到项目对应的知识库，请明确目标库，或说明要创建新项目资料。"
                    )
                if self.configured_names():
                    raise KnowledgeError(
                        "已限制可用知识库，请选择指定库；创建新库前需将可用知识库范围留空。"
                    )
                # Don't create an alternative project/library to bypass an existing scope ACL.
                existing = await manager.get_kb_by_name(project["name"])
                if existing:
                    raise KnowledgeError("同名知识库存在但当前范围不可访问，请联系管理员配置共享。")
                source_id = plan.model_source
                if source_id:
                    sources = [kb for kb in choices if source_id in {kb["id"], kb["name"]}]
                    if len(sources) != 1:
                        raise KnowledgeError("模型配置来源不可用或不唯一，请选择准确知识库 ID。")
                    source = await manager.get_kb(sources[0]["id"])
                    if source is None or source.init_error:
                        raise KnowledgeError("所选模型配置来源不可用。")
                else:
                    sources = [await manager.get_kb(kb["id"]) for kb in choices]
                    sources = [
                        kb
                        for kb in sources
                        if kb and not kb.init_error and kb.kb.embedding_provider_id
                    ]
                    if not sources:
                        raise KnowledgeError(
                            "没有可用的知识库模型配置，请先在 AstrBot 配置知识库。"
                        )
                    signatures = {
                        (
                            kb.kb.embedding_provider_id,
                            kb.kb.rerank_provider_id,
                            kb.kb.chunk_size,
                            kb.kb.chunk_overlap,
                        )
                        for kb in sources
                    }
                    if len(signatures) != 1:
                        raise KnowledgeError(
                            "新项目可复用的模型配置有多种，请明确 model_source；不会默认选择第一个库。"
                        )
                    source = sources[0]  # All available model/chunk configurations are identical.
                kb = source.kb
                helper = await manager.create_kb(
                    kb_name=project["name"],
                    description="聊天知识插件管理；有效版本和平台范围请通过插件工具检索。",
                    embedding_provider_id=kb.embedding_provider_id,
                    rerank_provider_id=kb.rerank_provider_id,
                    chunk_size=kb.chunk_size,
                    chunk_overlap=kb.chunk_overlap,
                    top_k_dense=kb.top_k_dense,
                    top_k_sparse=kb.top_k_sparse,
                    top_m_final=kb.top_m_final,
                )
        if helper is None or helper.init_error:
            raise KnowledgeError("项目知识库不可用，未完成保存。")
        if helper.kb.kb_id in blocked_ids or not self.selected(helper.kb):
            raise KnowledgeError("该项目知识库不在当前可用范围内。")
        if helper.kb.rerank_provider_id and await helper.get_rp() is None:
            raise KnowledgeError("现有重排序模型不可用，请恢复模型后重试。")
        return helper

    async def upload(self, helper, filename, body):
        matches = [
            d
            for d in await helper.list_documents(limit=100, search=filename)
            if d.doc_name == filename
        ]
        if len(matches) > 1:
            raise KnowledgeError("发现重复待处理索引，需要管理员检查。")
        if matches:
            return matches[0].doc_id
        doc = await helper.upload_document(
            file_name=filename,
            file_content=body.encode(),
            file_type="md",
            chunk_size=helper.kb.chunk_size or 512,
            chunk_overlap=helper.kb.chunk_overlap or 50,
            tasks_limit=1,
        )
        return doc.doc_id

    async def verify(self, helper, doc_id, query):
        doc = await helper.get_document(doc_id)
        count = await helper.get_chunk_count_by_doc_id(doc_id)
        if doc is None or doc.kb_id != helper.kb.kb_id or count < 1 or doc.chunk_count != count:
            raise KnowledgeError("索引元数据验证失败，本次尚未生效。")
        total = await self.total(helper)
        results = await self.candidates(helper, query, total, {"kb_doc_id": doc_id})
        if len(results) != count:
            raise KnowledgeError("向量检索验证失败，本次尚未生效。")

    async def candidates(self, helper, query, total, metadata_filters=None):
        # Core FaissVecDB.retrieve currently reads DocumentStorage with its default
        # limit=100. Use the same provider/index but explicitly disable that pagination
        # so newer/active chunks are not silently omitted once a project grows.
        import numpy as np

        vector = await helper.vec_db.embedding_provider.get_embedding(query)
        scores, indices = await helper.vec_db.embedding_storage.search(
            vector=np.array(vector).astype("float32"), k=total
        )
        ids = [int(i) for i in indices[0] if i != -1]
        if not ids:
            return []
        documents = await helper.vec_db.document_storage.get_documents(
            ids=ids, metadata_filters=metadata_filters or {}, limit=None
        )
        mapping = {int(doc["id"]): doc for doc in documents}
        return [
            {"data": mapping[int(ident)], "score": float(1.0 - scores[0][i] / 2.0)}
            for i, ident in enumerate(indices[0])
            if int(ident) in mapping
        ]

    async def total(self, helper):
        total = await helper.vec_db.count_documents()
        if total > int(self.config.get("max_index_chunks", 5000)):
            raise KnowledgeError("项目索引超过当前安全检索上限，需要清理历史索引或调整上限。")
        return max(total, 1)

    async def search(self, helper, query, allowed, limit=5):
        if not allowed:
            return []
        total = await self.total(helper)
        # Request all candidates within bounded project size so obsolete or other-platform
        # chunks cannot starve current results. Reuse the existing dense index and reranker.
        results = await self.candidates(helper, query, total)
        candidates = []
        for result in results:
            data = result["data"]
            metadata = data["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if metadata.get("kb_doc_id") in allowed:
                candidates.append(
                    {
                        "doc_id": metadata["kb_doc_id"],
                        "content": data["text"],
                        "score": result["score"],
                    }
                )
        candidates = candidates[:50]
        if candidates and helper.kb.rerank_provider_id:
            rp = await helper.get_rp()
            if rp is None:
                raise KnowledgeError("重排序模型不可用。")
            ranked = await rp.rerank(query, [r["content"] for r in candidates])
            candidates = [
                {**candidates[r.index], "score": r.relevance_score}
                for r in sorted(ranked, key=lambda r: r.relevance_score, reverse=True)
            ]
        return candidates[:limit]

    async def delete(self, helper, doc_id):
        if await helper.get_document(doc_id):
            await helper.delete_document(doc_id)

    async def documents(self, helper):
        count = await helper.count_documents()
        if count > int(self.config.get("max_index_chunks", 5000)):
            raise KnowledgeError("知识库文档数量超过读取上限，请缩小范围。")
        return await helper.list_documents(limit=max(count, 1))

    async def read_document(self, helper, doc_id):
        doc = await helper.get_document(doc_id)
        if doc is None or doc.kb_id != helper.kb.kb_id:
            raise KnowledgeError("原生文档不存在或不属于指定知识库。")
        count = await helper.get_chunk_count_by_doc_id(doc_id)
        if count < 1 or count > int(self.config.get("max_index_chunks", 5000)):
            raise KnowledgeError("文档索引为空或超过读取上限。")
        chunks = await helper.get_chunks_by_doc_id(doc_id, limit=count)
        chunks.sort(key=lambda c: c["chunk_index"])
        if len(chunks) != count or any(c["kb_id"] != helper.kb.kb_id for c in chunks):
            raise KnowledgeError("文档索引不完整。")
        body = "\n\n".join(c["content"] for c in chunks)
        if len(body) > 100000:
            raise KnowledgeError("原生文档索引正文超过 100000 字符，请先拆分文档。")
        return {
            "content": body,
            "sha256": digest(chunks),
            "title": doc.doc_name,
            "content_kind": "indexed_chunks",
            "complete": True,
            "notice": "这是原生库全部索引文本，可能含分块重叠，不等同于原始附件。",
        }
