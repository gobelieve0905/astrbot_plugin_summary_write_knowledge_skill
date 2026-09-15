"""Coordinated writes, optimistic versions, project ACLs and scoped skill loading."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from .models import KnowledgeError, Plan, digest, render, utcnow
from .project_validation import audit


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
        self.jobs = {}
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS save_jobs(id TEXT PRIMARY KEY, scope TEXT, cid TEXT, actor TEXT, state TEXT, detail TEXT, updated TEXT, plan TEXT)"
        )
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS save_sources(id TEXT PRIMARY KEY, topic TEXT NOT NULL, evidence TEXT NOT NULL)"
        )
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS project_audits(id TEXT PRIMARY KEY, project TEXT NOT NULL, fingerprint TEXT NOT NULL, report TEXT NOT NULL, created TEXT NOT NULL)"
        )
        with store.db:
            store.db.execute(
                "UPDATE save_jobs SET state='interrupted', detail=? WHERE state IN ('queued','reviewing','writing','indexing')",
                (
                    json.dumps(
                        {"message": "服务重载中断，请用相同计划重试；已暂存的版本会继续验证。"},
                        ensure_ascii=False,
                    ),
                ),
            )

    def progress(self, ident, state, detail=None):
        if ident:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE save_jobs SET state=?,detail=?,updated=? WHERE id=?",
                    (state, json.dumps(detail or {}, ensure_ascii=False), utcnow(), ident),
                )

    def status(self, topic, ident=""):
        rows = self.store.db.execute(
            "SELECT * FROM save_jobs WHERE scope=? AND cid=? AND actor=?"
            + (" AND id=?" if ident else "")
            + " ORDER BY updated DESC LIMIT 30",
            (topic.scope, topic.cid, topic.actor) + ((ident,) if ident else ()),
        ).fetchall()
        output = [
            {
                "task_id": r["id"],
                "status": r["state"],
                **json.loads(r["detail"]),
                "updated": r["updated"],
                **(
                    {"retry_plan": json.loads(r["plan"])}
                    if r["state"] in {"needs_attention", "interrupted"}
                    else {}
                ),
            }
            for r in rows
        ]
        if ident:
            found = next((r for r in output if r["task_id"] == ident), None)
            if found is None:
                raise KnowledgeError("保存任务不存在或不属于当前用户和话题。")
            return found
        return output

    def task_id(self, topic, plan, operation_key=""):
        return digest([topic.scope, topic.cid, topic.actor, operation_key, plan.to_dict()])

    def resumable(self, topic, plan, operation_key=""):
        return (
            self.store.db.execute(
                "SELECT 1 FROM save_jobs WHERE id=?", (self.task_id(topic, plan, operation_key),)
            ).fetchone()
            is not None
        )

    def remember_source(self, topic, plan, evidence, operation_key=""):
        ident = self.task_id(topic, plan, operation_key)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO save_sources VALUES(?,?,?)",
                (
                    ident,
                    json.dumps(topic.to_dict(), ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )

    def restore_source(self, current, plan, operation_key=""):
        ident = self.task_id(current, plan, operation_key)
        row = self.store.db.execute(
            "SELECT topic,evidence FROM save_sources WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            return None
        from .models import TopicContext

        original = TopicContext(**json.loads(row["topic"]))
        if (original.scope, original.cid, original.actor) != (
            current.scope,
            current.cid,
            current.actor,
        ):
            raise KnowledgeError("原任务来源不属于当前用户和话题。")
        return original, json.loads(row["evidence"])

    async def submit(self, topic, plan, review, operation_key="", wait_seconds=15):
        ident = self.task_id(topic, plan, operation_key)
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO save_jobs VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    topic.scope,
                    topic.cid,
                    topic.actor,
                    "queued",
                    "{}",
                    utcnow(),
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                ),
            )
        running = self.jobs.get(ident)
        if running is None or running.done():

            async def execute():
                try:
                    result = await self.save(topic, plan, review, operation_key=ident, job_id=ident)
                    self.progress(ident, "saved", result)
                    return {**result, "task_id": ident}
                except asyncio.CancelledError:
                    self.progress(
                        ident,
                        "interrupted",
                        {"message": "保存被中断；请用原计划重试以核对或继续暂存写入。"},
                    )
                    raise
                except Exception as exc:
                    message = (
                        str(exc)
                        if isinstance(exc, KnowledgeError)
                        else "保存阶段未完成，请用相同计划重试；不会重复创建暂存版本。"
                    )
                    if (
                        isinstance(exc, TimeoutError)
                        and self.status(topic, ident)["status"] == "reviewing"
                    ):
                        message = "模型校验超时，尚未开始本次文档写入；可缩小明确来源范围后重试。"
                    self.progress(
                        ident,
                        "needs_attention",
                        {"message": message, "stage": self.status(topic, ident)["status"]},
                    )
                    return self.status(topic, ident)

            running = asyncio.create_task(execute())
            self.jobs[ident] = running
            running.add_done_callback(
                lambda task: self.jobs.pop(ident, None) if self.jobs.get(ident) is task else None
            )
        try:
            return await asyncio.wait_for(asyncio.shield(running), timeout=wait_seconds)
        except TimeoutError:
            return {
                "status": "processing",
                "task_id": ident,
                "stage": self.status(topic, ident)["status"],
                "message": "保存仍在处理，请用 knowledge_save_status 查询；不要重复新增或宣称已保存。",
            }

    def allowed(self, project, scope):
        shares = self.config.get("project_shares", {})
        if project["scope"] == "*":
            return project["name"] not in shares or scope in shares[project["name"]]
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

    async def save(self, topic, plan: Plan, review, operation_key="", job_id=""):
        async with self.lock:
            assignment = audit(
                topic,
                plan.project,
                self.store.projects(),
                self.store.binding(topic.scope, topic.cid),
            )
            if assignment.confidence == "low":
                raise KnowledgeError("项目归属证据不足，请明确本次保存项目。")
            with self.store.db:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO project_audits VALUES(?,?,?,?,?)",
                    (operation_key or digest([topic.cid, plan.to_dict()]), plan.project,
                     assignment.fingerprint, json.dumps(assignment.to_dict(), ensure_ascii=False), utcnow()),
                )
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
            if existing_target or p:
                _, target_helper = await self.resolve(topic, plan.knowledge_base or plan.project)
                catalog["target_documents"] = [
                    {"doc_id": d.doc_id, "title": d.doc_name}
                    for d in await self.backend.documents(target_helper)
                ]
            native_old = None
            if plan.native_document:
                native_old = json.loads(prior["source"]).get("native_previous") if prior else None
                if native_old is None:
                    native_old = await self.native_read(topic, plan.native_document)
                if native_old["sha256"] != plan.expected_sha256:
                    raise KnowledgeError("原生文档已变化，请重新读取后更新。")
                _, target = await self.resolve(topic, plan.knowledge_base or plan.project)
                if native_old["knowledge_base"] != target.kb.kb_id:
                    raise KnowledgeError("不能替换其他知识库的文档。")
            self.progress(job_id, "reviewing")
            verdict = await review(
                topic,
                plan,
                old
                or ({"plan": json.dumps(native_old, ensure_ascii=False)} if native_old else None),
                catalog,
            )
            if verdict.get("allow") is not True:
                raise KnowledgeError(
                    str(verdict.get("question") or "保存意图、归属或内容尚不明确，请补充说明。")[
                        :1000
                    ]
                )
            bound = self.store.binding(topic.scope, topic.cid)
            latest = audit(topic, plan.project, self.store.projects(), bound)
            if latest.fingerprint != assignment.fingerprint:
                raise KnowledgeError("会话项目归属在保存期间发生变化，请重新确认项目后再试。")
            if bound and bound != plan.project and verdict.get("explicit_project") is not True:
                raise KnowledgeError("本话题已有其他项目归属，请明确这次是否要保存到不同项目。")
            self.progress(job_id, "writing")
            if not p:
                p = self.store.create_project(plan.project, "*" if existing_target else topic.scope)
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
                    "native_previous": native_old,
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
            self.progress(job_id, "indexing")
            await self.backend.verify(helper, doc_id, plan.title)
            if path.read_text(encoding="utf-8") != body:
                raise KnowledgeError("文件回读验证失败，本次未生效。")
            if plan.native_document:
                doc_id_old = plan.native_document.split(":")[2]
                current_old = await helper.get_document(doc_id_old)
                if current_old is not None:
                    checked = await self.backend.read_document(helper, doc_id_old)
                    if checked["sha256"] != plan.expected_sha256:
                        raise KnowledgeError("旧文档在保存期间变化，暂不替换，请检查待处理任务。")
                    await self.backend.delete(helper, doc_id_old)
                if await helper.get_document(doc_id_old) is not None:
                    raise KnowledgeError("旧文档清理未完成，尚不能确认保存。")
            if old:
                # Native AstrBot retrieval must also stop seeing the old rule.
                # Do not issue a success receipt while cleanup has failed.
                await self.backend.delete(helper, old["doc_id"])
            self.store.activate(op, plan.expected_version, topic.scope, topic.cid)
            return self.receipt(self.store.operation(op))

    async def resolve(self, topic, name):
        p = self.store.project(name)
        if p is None:
            p = next((item for item in self.store.projects() if item["kb_id"] == name), None)
        if p:
            if not self.allowed(p, topic.scope):
                raise KnowledgeError("此项目已配置会话共享限制，当前会话无权访问。")
            return p, await self.ensure_project(topic, p)
        choices = await self.backend.catalog(self.blocked_kbs(topic))
        matches = [kb for kb in choices if name in {kb["id"], kb["name"]}]
        if len(matches) != 1:
            raise KnowledgeError(
                "没有唯一匹配的可用原生知识库，请从 knowledge_context 选择名称或 ID。"
            )
        p = {"name": matches[0]["name"], "kb_id": matches[0]["id"], "id": ""}
        return p, await self.ensure_project(topic, p)

    async def native_read(self, topic, ref):
        parts = ref.split(":")
        if len(parts) != 3 or parts[0] != "native":
            raise KnowledgeError("原生文档编号无效。")
        _, helper = await self.resolve(topic, parts[1])
        # Managed pending/obsolete documents cannot be read via the native fallback.
        if self.store.db.execute("SELECT 1 FROM revisions WHERE doc_id=?", (parts[2],)).fetchone():
            raise KnowledgeError("此文档已有版本记录，请使用目录中的有效 record_id。")
        return {
            **await self.backend.read_document(helper, parts[2]),
            "record_id": ref,
            "knowledge_base": helper.kb.kb_id,
            "platform": "unknown",
            "version": 0,
            "notice_scope": "原生旧文档未标注平台，请根据正文确认适用范围，不能自动视为 all。",
        }

    async def search(self, topic, project, platform, query):
        async with self.lock:
            p, helper = await self.resolve(topic, project)
            if not platform.strip():
                raise KnowledgeError("请明确适用平台。")
            rows = [r for r in self.store.records(p["id"]) if r["platform"] in {platform, "all"}]
            mapping = {r["doc_id"]: r for r in rows}
            managed = {r[0] for r in self.store.db.execute("SELECT doc_id FROM revisions")}
            docs = await self.backend.documents(helper)
            native = {d.doc_id: d for d in docs if d.doc_id not in managed}
            results = await self.backend.search(helper, query, set(mapping) | set(native))
            output = []
            for item in results:
                r = mapping.get(item["doc_id"])
                output.append(
                    {
                        **item,
                        **(
                            {
                                "record_id": r["record_id"],
                                "version": r["version"],
                                "platform": r["platform"],
                                "kind": r["kind"],
                            }
                            if r
                            else {
                                "record_id": f"native:{helper.kb.kb_id}:{item['doc_id']}",
                                "title": native[item["doc_id"]].doc_name,
                                "platform": "unknown",
                                "version": 0,
                                "notice": "原生文档候选，须读取正文确认平台，未自动判为适用。",
                            }
                        ),
                    }
                )
            return output

    async def read(self, topic, record_id, platform):
        async with self.lock:
            if record_id.startswith("native:"):
                return await self.native_read(topic, record_id)
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
                "source": {
                    k: v
                    for k, v in json.loads(row["source"]).items()
                    if k not in {"snapshot", "native_previous"}
                },
            }
