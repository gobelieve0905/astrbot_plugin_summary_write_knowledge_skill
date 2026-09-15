"""Coordinated writes, optimistic versions, project ACLs and scoped skill loading."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from .models import KnowledgeError, Plan, digest, render


def write_atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise KnowledgeError("已暂存的文件与本次内容不一致，拒绝覆盖。")
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stage-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Service:
    def __init__(self, store, backend, config):
        self.store = store
        self.backend = backend
        self.config = config
        # One process supported. Serializing our reads/writes also protects core helper reloads.
        self.lock = asyncio.Lock()

    def allowed(self, project, scope):
        shares = self.config.get("project_shares", {})
        return project["scope"] == scope or scope in shares.get(project["name"], [])

    def project(self, name, scope):
        p = self.store.project(name)
        if not p or not self.allowed(p, scope):
            raise KnowledgeError("项目不存在或当前会话没有权限，请明确项目或联系管理员配置共享。")
        return p

    def catalog(self, topic):
        projects = []
        for p in self.store.projects():
            if self.allowed(p, topic.scope):
                projects.append(
                    {
                        "name": p["name"],
                        "knowledge_base_id": p["kb_id"],
                        "records": [
                            {
                                **{
                                    k: r[k]
                                    for k in (
                                        "record_id",
                                        "version",
                                        "platform",
                                        "title",
                                        "kind",
                                        "state",
                                    )
                                },
                                **(
                                    {"retry_plan": json.loads(r["plan"])}
                                    if r["state"] == "pending"
                                    else {}
                                ),
                            }
                            for r in self.store.records(p["id"], history=True)
                            if r["state"] != "superseded"
                        ],
                    }
                )
        return {"bound_project": self.store.binding(topic.scope, topic.cid), "projects": projects}

    def blocked_kbs(self, topic):
        return {
            p["kb_id"]
            for p in self.store.projects()
            if p["kb_id"] and not self.allowed(p, topic.scope)
        }

    async def context_catalog(self, topic):
        libraries = await self.backend.catalog(self.blocked_kbs(topic))
        ids = {kb["id"] for kb in libraries}
        catalog = self.catalog(topic)
        catalog["projects"] = [
            p
            for p in catalog["projects"]
            if not p["knowledge_base_id"] or p["knowledge_base_id"] in ids
        ]
        return {**catalog, "knowledge_bases": libraries}

    async def ensure_project(self, topic, project, plan=None):
        return await self.backend.ensure(project, plan=plan, blocked_ids=self.blocked_kbs(topic))

    def path(self, row):
        basename = "SKILL.md" if row["kind"] == "skill" else "knowledge.md"
        return (
            self.store.root
            / "projects"
            / row["project_id"]
            / row["record_id"]
            / f"v{row['version']}"
            / basename
        )

    def checked_body(self, row):
        plan = Plan.parse(json.loads(row["plan"]))
        expected = render(
            plan, row["record_id"], row["version"], json.loads(row["source"]), row["created"]
        )
        body = self.path(row).read_text(encoding="utf-8")
        if body != expected:
            raise KnowledgeError("知识文件与有效版本记录不一致，请恢复文件后重试。")
        return body

    @staticmethod
    def receipt(row, cleanup_pending=False):
        plan = json.loads(row["plan"])
        return {
            "status": "saved",
            "message": "已保存，文件和索引验证通过。",
            "record_id": row["record_id"],
            "version": row["version"],
            "project": plan["project"],
            "platform": row["platform"],
            "kind": row["kind"],
            "title": row["title"],
            "skill_usage": "通过 knowledge_read 按项目加载；未注册为全局技能。"
            if row["kind"] == "skill"
            else None,
            "coverage": json.loads(row["source"])["coverage"],
            "knowledge_base": json.loads(row["source"]).get("knowledge_base"),
            "obsolete_index_cleanup_pending": cleanup_pending,
        }

    async def save(self, topic, plan: Plan, review, operation_key=""):
        async with self.lock:
            op = digest([topic.scope, operation_key or topic.message_id, plan.to_dict()])
            prior = self.store.operation(op)
            if prior and prior["state"] == "active":
                # Even retries must prove storage remains usable.
                p = self.project(plan.project, topic.scope)
                helper = await self.ensure_project(topic, p)
                self.checked_body(prior)
                await self.backend.verify(helper, prior["doc_id"], plan.title)
                return self.receipt(prior)
            if prior and prior["state"] == "superseded":
                raise KnowledgeError(
                    "原操作已成功但该版本随后被替换；请读取当前版本，不重复执行旧写入。"
                )
            p = self.store.project(plan.project)
            if p and not self.allowed(p, topic.scope):
                raise KnowledgeError("项目不存在或当前会话没有权限。")
            catalog = await self.context_catalog(topic)
            existing_target = any(
                plan.project == kb["name"] or plan.knowledge_base in {kb["id"], kb["name"]}
                for kb in catalog["knowledge_bases"]
            )
            if not p and not plan.create_project and not existing_target:
                raise KnowledgeError("项目尚未建立，请明确目标知识库或说明这是新项目。")
            old = self.store.active(plan.record_id) if plan.record_id else None
            if plan.record_id and (
                not old
                or not p
                or old["project_id"] != p["id"]
                or old["platform"] != plan.platform
                or old["kind"] != plan.kind
            ):
                raise KnowledgeError(
                    "更新对象不存在，或项目、平台、文档类型不一致；不能扩大或改写原适用范围。"
                )
            if old and old["version"] != plan.expected_version:
                raise KnowledgeError("版本冲突：请读取最新规则后重新整理。")
            if p and not old:
                duplicate = [
                    r
                    for r in self.store.records(p["id"])
                    if r["title"].casefold() == plan.title.casefold()
                    and r["platform"].casefold() == plan.platform.casefold()
                ]
                if duplicate:
                    raise KnowledgeError("已有同名同范围知识，请读取其编号并更新，不能重复新增。")
            verdict = await review(topic, plan, old, catalog)
            if verdict.get("allow") is not True:
                raise KnowledgeError(
                    str(verdict.get("question") or "保存意图、归属或内容尚不明确，请补充说明。")[
                        :1000
                    ]
                )
            bound = self.store.binding(topic.scope, topic.cid)
            if bound and bound != plan.project and verdict.get("explicit_project") is not True:
                raise KnowledgeError("本话题已有其他项目归属，请明确这次是否要保存到不同项目。")
            if not p:
                p = self.store.create_project(plan.project, topic.scope)
            helper = await self.ensure_project(topic, p, plan)
            self.store.set_kb(p["id"], helper.kb.kb_id)
            # A later explicit retry can resume an identical staged version without duplicates.
            if not prior:
                pending = [
                    r
                    for r in self.store.records(p["id"], history=True)
                    if r["state"] == "pending"
                    and Plan.parse(json.loads(r["plan"])).to_dict() == plan.to_dict()
                    and json.loads(r["source"])["scope"] == topic.scope
                    and json.loads(r["source"])["topic_id"] == topic.cid
                ]
                if pending:
                    prior = pending[0]
                    op = prior["op"]
            if prior is None:
                rid = plan.record_id or uuid.uuid4().hex
                version = plan.expected_version + 1
                source = {
                    "scope": topic.scope,
                    "knowledge_base": getattr(helper.kb, "kb_name", p["name"]),
                    "knowledge_base_id": helper.kb.kb_id,
                    "topic_id": topic.cid,
                    "owner": topic.owner,
                    "message_id": topic.message_id,
                    "quoted_message_id": topic.quoted_message_id,
                    "actor": topic.actor,
                    "coverage": topic.coverage,
                    "selection": topic.selection,
                    "snapshot_sha256": digest(topic.to_dict()),
                    "source_messages": [
                        {k: m[k] for k in ("mid", "actor", "created") if k in m}
                        for m in topic.source_messages
                    ],
                    "snapshot": topic.to_dict(),
                }
                prior = self.store.stage(op, rid, version, p["id"], plan, source, f"{op}.md")
            write_atomic(
                self.path(prior).parent / "topic.json",
                json.dumps(json.loads(prior["source"])["snapshot"], ensure_ascii=False, indent=2),
            )
            body = render(
                plan,
                prior["record_id"],
                prior["version"],
                json.loads(prior["source"]),
                prior["created"],
            )
            path = self.path(prior)
            write_atomic(path, body)
            doc_id = prior["doc_id"] or await self.backend.upload(helper, prior["filename"], body)
            self.store.set_doc(op, doc_id)
            await self.backend.verify(helper, doc_id, plan.title)
            if path.read_text(encoding="utf-8") != body:
                raise KnowledgeError("文件回读验证失败，本次未生效。")
            self.store.activate(op, plan.expected_version, topic.scope, topic.cid)
            cleanup_pending = False
            if old:
                try:
                    await self.backend.delete(helper, old["doc_id"])
                except Exception:
                    cleanup_pending = (
                        True  # The old doc is already excluded by our active manifest.
                    )
            return self.receipt(self.store.operation(op), cleanup_pending)

    async def search(self, topic, project, platform, query):
        async with self.lock:
            p = self.project(project, topic.scope)
            if not platform.strip():
                raise KnowledgeError(
                    "请明确适用平台，不能混用不同平台规则；明确不限平台的资料用 all。"
                )
            rows = [r for r in self.store.records(p["id"]) if r["platform"] in {platform, "all"}]
            helper = await self.ensure_project(topic, p)
            mapping = {r["doc_id"]: r for r in rows}
            results = await self.backend.search(helper, query, set(mapping))
            return [
                {
                    **item,
                    "record_id": mapping[item["doc_id"]]["record_id"],
                    "version": mapping[item["doc_id"]]["version"],
                    "platform": mapping[item["doc_id"]]["platform"],
                    "kind": mapping[item["doc_id"]]["kind"],
                }
                for item in results
            ]

    async def read(self, topic, record_id, platform):
        async with self.lock:
            row = self.store.active(record_id)
            if not row:
                raise KnowledgeError("找不到当前有效记录。")
            plan = json.loads(row["plan"])
            project = self.project(plan["project"], topic.scope)
            await self.ensure_project(topic, project)
            if row["platform"] not in {platform, "all"}:
                raise KnowledgeError("该知识不适用于指定平台。")
            return {
                "record_id": record_id,
                "version": row["version"],
                "project": plan["project"],
                "platform": row["platform"],
                "kind": row["kind"],
                "content": self.checked_body(row),
                "source": json.loads(row["source"]),
            }
