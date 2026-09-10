"""Adapter verified against installed AstrBot 4.28.0 source."""

from __future__ import annotations

import json

from .models import KnowledgeError


class AstrBotBackend:
    def __init__(self, context, config):
        self.context = context
        self.config = config

    async def ensure(self, project):
        manager = self.context.kb_manager
        if project["kb_id"]:
            helper = await manager.get_kb(project["kb_id"])
        else:
            name = "chat-knowledge-" + project["id"]
            helper = await manager.get_kb_by_name(name)
            if helper is None:
                template = await manager.get_kb_by_name(self.config.get("template_kb", ""))
                if template is None or template.init_error:
                    raise KnowledgeError(
                        "请先配置可用的 template_kb；新项目复用该库的嵌入和重排序模型。"
                    )
                kb = template.kb
                helper = await manager.create_kb(
                    kb_name=name,
                    description="聊天知识插件管理；请通过插件检索，不加入全局知识库列表。",
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
        # Core otherwise silently degrades reranking. Fail explicitly when configured but missing.
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
