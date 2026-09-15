"""Private artifact registry and resumable, independently receipted deliveries."""

import hashlib
import json

from .models import KnowledgeError, utcnow


class Deliveries:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS task_artifacts(
          job TEXT PRIMARY KEY, scope TEXT NOT NULL, cid TEXT NOT NULL,
          actor TEXT NOT NULL, owner TEXT NOT NULL, mid TEXT NOT NULL,
          state TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifact_files(
          job TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL,
          PRIMARY KEY(job,name));
        CREATE TABLE IF NOT EXISTS deliveries(
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, cid TEXT NOT NULL,
          actor TEXT NOT NULL, plan TEXT NOT NULL, states TEXT NOT NULL, created TEXT NOT NULL);
        """)

    def register(self, topic, job, owner):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO task_artifacts VALUES(?,?,?,?,?,?,'pending','',?)",
                (job, topic.scope, topic.cid, topic.actor, owner, topic.message_id, utcnow()),
            )

    def jobs(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM task_artifacts WHERE state='pending' ORDER BY created LIMIT 100"
            )
        ]

    def finish(self, job, state, detail="", files=()):
        with self.db:
            for f in files:
                self.db.execute(
                    "INSERT OR REPLACE INTO artifact_files VALUES(?,?,?,?)",
                    (job, f["name"], f["body"], hashlib.sha256(f["body"].encode()).hexdigest()),
                )
            self.db.execute(
                "UPDATE task_artifacts SET state=?,detail=? WHERE job=?", (state, detail, job)
            )

    def catalog(self, topic):
        result = []
        for row in self.db.execute(
            "SELECT job,mid,state,detail,created FROM task_artifacts WHERE scope=? AND cid=? AND actor=? ORDER BY created DESC LIMIT 50",
            (topic.scope, topic.cid, topic.actor),
        ):
            r = dict(row)
            r["files"] = [
                dict(f)
                for f in self.db.execute(
                    "SELECT name,sha,length(body) AS chars FROM artifact_files WHERE job=?",
                    (r["job"],),
                )
            ]
            result.append(r)
        return result

    def file(self, topic, job, name):
        row = self.db.execute(
            "SELECT f.body,f.sha FROM artifact_files f JOIN task_artifacts a ON a.job=f.job WHERE a.job=? AND f.name=? AND a.scope=? AND a.cid=? AND a.actor=?",
            (job, name, topic.scope, topic.cid, topic.actor),
        ).fetchone()
        return dict(row) if row else None

    def bundle(self, topic, ident):
        r = self.db.execute(
            "SELECT * FROM deliveries WHERE id=? AND scope=? AND cid=? AND actor=?",
            (ident, topic.scope, topic.cid, topic.actor),
        ).fetchone()
        if not r:
            raise KnowledgeError("保存任务不存在，或当前用户/话题没有访问权限。")
        return {**dict(r), "plan": json.loads(r["plan"]), "states": json.loads(r["states"])}

    def create(self, topic, plan):
        if (
            not isinstance(plan, dict)
            or set(plan) - {"skill", "knowledge"}
            or not plan
            or any(not isinstance(v, dict) or not v for v in plan.values())
        ):
            raise KnowledgeError("计划须包含 skill 和/或 knowledge 对象。")
        raw = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        if len(raw) > 100000:
            raise KnowledgeError("本次成果过长，请按范围拆分。")
        # Repeating the same tool in the same message must not start a second delivery.
        ident = hashlib.sha256(
            (topic.scope + topic.actor + topic.message_id + raw).encode()
        ).hexdigest()[:32]
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    topic.scope,
                    topic.cid,
                    topic.actor,
                    raw,
                    json.dumps({k: {"status": "pending"} for k in plan}),
                    utcnow(),
                ),
            )
        return self.bundle(topic, ident)

    def record(self, topic, ident, part, result):
        row = self.bundle(topic, ident)
        row["states"][part] = result
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET states=? WHERE id=?",
                (json.dumps(row["states"], ensure_ascii=False), ident),
            )

    def status(self, topic, ident):
        row = self.bundle(topic, ident)
        states = row["states"]
        done = all(v.get("status") in ("saved", "installed") for v in states.values())
        return {
            "status": "complete" if done else "partial",
            "delivery_id": ident,
            "parts": states,
            "message": "所列成果均已保存并验证。"
            if done
            else "仅回执成功的部分已完成；继续时只处理其余部分，不重新生成或执行查询。",
        }

    def recent(self, topic):
        return [
            self.status(topic, r["id"])
            for r in self.db.execute(
                "SELECT id FROM deliveries WHERE scope=? AND cid=? AND actor=? ORDER BY created DESC LIMIT 10",
                (topic.scope, topic.cid, topic.actor),
            )
        ]

    def amend(self, topic, ident, patch):
        row = self.bundle(topic, ident)
        if not isinstance(patch, dict) or not patch or set(patch) - set(row["plan"]):
            raise KnowledgeError("只能补充原任务中尚未完成的部分。")
        for part, value in patch.items():
            if row["states"][part].get("status") in ("saved", "installed"):
                raise KnowledgeError("已完成部分不能修改或重复执行。")
            if not isinstance(value, dict) or not value:
                raise KnowledgeError("请提供未完成部分的完整计划。")
            row["plan"][part] = value
            row["states"][part] = {"status": "pending"}
        raw = json.dumps(row["plan"], ensure_ascii=False)
        if len(raw) > 100000:
            raise KnowledgeError("成果计划过长。")
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET plan=?,states=? WHERE id=?",
                (raw, json.dumps(row["states"]), ident),
            )

    def admin_status(self):
        return [
            {
                "id": r["id"],
                "actor": r["actor"],
                "created": r["created"],
                "parts": json.loads(r["states"]),
            }
            for r in self.db.execute(
                "SELECT id,actor,created,states FROM deliveries ORDER BY created DESC LIMIT 50"
            )
        ]
