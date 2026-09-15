"""Scoped, transactional team skills. Never exported to the global skill directory."""

import hashlib
import json
import uuid

from .models import KnowledgeError, utcnow
from .skill_install import validate


class TeamSkills:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS team_skills(
          id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
          name TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS team_skill_versions(
          id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
          metadata TEXT NOT NULL, sha TEXT NOT NULL, actor TEXT NOT NULL,
          created TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(id,version));
        CREATE TABLE IF NOT EXISTS team_skill_events(
          id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, actor TEXT NOT NULL,
          kind TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topic_files(
          scope TEXT NOT NULL, cid TEXT NOT NULL, sha TEXT NOT NULL,
          name TEXT NOT NULL, content TEXT NOT NULL, mid TEXT NOT NULL,
          created TEXT NOT NULL, PRIMARY KEY(scope,cid,sha));
        """)

    def row(self, ident):
        r = self.db.execute("SELECT * FROM team_skills WHERE id=?", (ident,)).fetchone()
        if not r:
            raise KnowledgeError("技能不存在或不可访问。")
        v = self.revision(ident, r["version"])
        return {**dict(r), **v}

    def revision(self, ident, version):
        r = self.db.execute(
            "SELECT * FROM team_skill_versions WHERE id=? AND version=?", (ident, version)
        ).fetchone()
        if not r:
            raise KnowledgeError("技能版本不存在。")
        result = dict(r)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def allowed(self, r, who, projects):
        m = r["metadata"]
        return who["admin"] or (
            r["tenant"] == who["tenant"]
            and (
                r["owner"] == who["actor"]
                or who["actor"] in m["maintainers"]
                or m["sharing"] == "team"
                or (m["sharing"] == "project" and m["project"] in projects)
            )
        )

    def editable(self, r, who):
        return who["admin"] or (
            r["tenant"] == who["tenant"]
            and (r["owner"] == who["actor"] or who["actor"] in r["metadata"]["maintainers"])
        )

    def get(self, ident, who, projects, edit=False):
        r = self.row(ident)
        if not (self.editable(r, who) if edit else self.allowed(r, who, projects)):
            raise KnowledgeError("技能不存在或当前用户无权访问。")
        return r

    def metadata(self, name, value, who, projects):
        if not isinstance(value, dict) or set(value) - {
            "title",
            "category",
            "platform",
            "project",
            "sharing",
            "maintainers",
            "description",
        }:
            raise KnowledgeError("技能分组字段无效。")
        m = {
            "title": name,
            "category": "其他",
            "platform": "",
            "project": "",
            "sharing": "personal",
            "maintainers": [],
            "description": "",
            **value,
        }
        for key in ("title", "category", "platform", "project", "description"):
            if not isinstance(m[key], str) or len(m[key]) > (2000 if key == "description" else 200):
                raise KnowledgeError("技能分类、平台、项目或描述过长。")
            m[key] = m[key].strip()
        if (
            not m["title"]
            or not m["category"]
            or m["sharing"] not in ("personal", "project", "team")
        ):
            raise KnowledgeError("请明确标题、分类与共享范围。")
        if (
            not isinstance(m["maintainers"], list)
            or len(m["maintainers"]) > 50
            or any(
                not isinstance(x, str) or not x.startswith("ou_") or len(x) > 128
                for x in m["maintainers"]
            )
        ):
            raise KnowledgeError("维护者须为飞书 Open ID 列表。")
        if m["sharing"] == "project" and not m["project"]:
            raise KnowledgeError("项目共享必须明确项目。")
        if m["project"] and m["project"] not in projects and not who["admin"]:
            raise KnowledgeError("项目不存在或没有该项目的访问权限。")
        if m["sharing"] == "team" and m["project"]:
            raise KnowledgeError("项目专用技能请选择项目共享或个人，不能直接发布到整个团队。")
        return m

    @staticmethod
    def public(r):
        return {k: v for k, v in r.items() if k not in ("body", "source", "tenant")}

    def catalog(self, who, projects, query="", platform="", project="", include_disabled=False):
        rows = []
        for item in self.db.execute("SELECT id FROM team_skills ORDER BY rowid DESC"):
            r = self.row(item["id"])
            m = r["metadata"]
            if not self.allowed(r, who, projects) or (
                not r["active"] and (not include_disabled or not self.editable(r, who))
            ):
                continue
            if platform and m["platform"] and m["platform"].casefold() != platform.casefold():
                continue
            if project and m["project"] and m["project"] != project:
                continue
            if (
                query
                and query.casefold()
                not in (r["name"] + " " + json.dumps(m, ensure_ascii=False)).casefold()
            ):
                continue
            rows.append(self.public(r))
        return rows

    def save(self, name, body, meta, who, projects, source, ident="", expected=0):
        body = validate(name, body)
        import yaml

        if isinstance(meta, dict) and not meta.get("description"):
            meta = {**meta, "description": yaml.safe_load(body.split("---", 2)[1])["description"]}
        m = self.metadata(name, meta, who, projects)
        sha = hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            if ident:
                old = self.get(ident, who, projects, edit=True)
                if old["version"] != expected:
                    raise KnowledgeError("版本已变化，请重新读取后更新。")
                if old["name"] != name:
                    raise KnowledgeError("更新不能更改技能标识名。")
                if not old["active"]:
                    raise KnowledgeError("技能已停用，请先恢复后更新。")
                if not (who["admin"] or old["owner"] == who["actor"]) and (
                    m["sharing"] != old["metadata"]["sharing"]
                    or m["maintainers"] != old["metadata"]["maintainers"]
                    or m["project"] != old["metadata"]["project"]
                ):
                    raise KnowledgeError("仅创建人或管理员可调整共享范围和维护者。")
                version = expected + 1
            else:
                if expected:
                    raise KnowledgeError("新增技能版本必须为零。")
                for candidate in self.catalog(who, projects, include_disabled=True):
                    cm = candidate["metadata"]
                    if (
                        candidate["name"] == name
                        and cm["sharing"] == m["sharing"]
                        and cm["project"] == m["project"]
                        and (m["sharing"] != "personal" or candidate["owner"] == who["actor"])
                    ):
                        raise KnowledgeError("该范围已有同名技能，请读取后更新，或使用不同名称。")
                ident = "team-" + uuid.uuid4().hex
                version = 1
                self.db.execute(
                    "INSERT INTO team_skills VALUES(?,?,?,?,?,1)",
                    (ident, who["tenant"], who["actor"], name, version),
                )
            self.db.execute(
                "INSERT INTO team_skill_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    version,
                    body,
                    json.dumps(m, ensure_ascii=False),
                    sha,
                    who["actor"],
                    utcnow(),
                    json.dumps(source, ensure_ascii=False),
                ),
            )
            self.db.execute("UPDATE team_skills SET version=? WHERE id=?", (version, ident))
            # Verify before committing; SQLite keeps the active pointer and body atomic.
            readback = self.row(ident)
            if readback["sha"] != sha or readback["body"] != body:
                raise KnowledgeError("技能回读失败。")
        return {
            "status": "installed",
            **self.public(readback),
            "message": "团队技能已保存并启用；全文回读验证通过。",
        }

    def read(self, ident, who, projects, platform, project, version=None):
        r = self.get(ident, who, projects)
        if not r["active"]:
            raise KnowledgeError("技能已停用。")
        m = r["metadata"]
        if m["platform"] and m["platform"].casefold() != platform.casefold():
            raise KnowledgeError("请明确匹配的任务平台后再读取。")
        if m["project"] and (project != m["project"] or project not in projects):
            raise KnowledgeError("请确认当前任务项目并验证项目权限。")
        if version is not None:
            # A pin never bypasses a newly revoked sharing policy or disabled flag.
            prior = self.revision(ident, version)
            pm = prior["metadata"]
            if pm["project"] and pm["project"] != project:
                raise KnowledgeError("任务版本的适用项目不匹配。")
            if pm["platform"] and pm["platform"].casefold() != platform.casefold():
                raise KnowledgeError("任务版本的平台不匹配。")
            r = {**r, **prior}
        return r

    def history(self, ident, who, projects):
        self.get(ident, who, projects, edit=True)
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT version,sha,actor,created FROM team_skill_versions WHERE id=? ORDER BY version DESC",
                (ident,),
            )
        ]

    def manage(self, ident, action, who, projects, expected, content="", version=0):
        r = self.get(ident, who, projects, edit=action not in ("suggest", "feedback"))
        if r["version"] != expected:
            raise KnowledgeError("版本已变化，请刷新后操作。")
        if action in ("suggest", "feedback"):
            if not isinstance(content, str) or not content.strip() or len(content) > 8000:
                raise KnowledgeError("请填写不超过 8000 字的说明。")
            with self.db:
                self.db.execute(
                    "INSERT INTO team_skill_events VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, ident, who["actor"], action, content, utcnow()),
                )
            return {"status": "recorded", "message": "建议或反馈已记录，未修改当前有效技能。"}
        if action == "rollback":
            old = self.revision(ident, version)
            return self.save(
                r["name"],
                old["body"],
                r["metadata"],
                who,
                projects,
                {"rollback_from": version},
                ident,
                expected,
            )
        if action not in ("disable", "enable"):
            raise KnowledgeError("不支持的管理操作。")
        with self.db:
            self.db.execute(
                "INSERT INTO team_skill_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    expected + 1,
                    r["body"],
                    json.dumps(r["metadata"], ensure_ascii=False),
                    r["sha"],
                    who["actor"],
                    utcnow(),
                    json.dumps({"action": action}),
                ),
            )
            self.db.execute(
                "UPDATE team_skills SET active=?,version=? WHERE id=?",
                (int(action == "enable"), expected + 1, ident),
            )
            self.db.execute(
                "INSERT INTO team_skill_events VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, ident, who["actor"], action, "", utcnow()),
            )
        return {"status": "ok", "active": action == "enable"}

    def cache_files(self, topic, files):
        with self.db:
            for f in files:
                sha = hashlib.sha256(f["text"].encode()).hexdigest()
                self.db.execute(
                    "INSERT OR IGNORE INTO topic_files VALUES(?,?,?,?,?,?,?)",
                    (topic.scope, topic.cid, sha, f["name"], f["text"], topic.message_id, utcnow()),
                )
            self.db.execute(
                "DELETE FROM topic_files WHERE scope=? AND cid=? AND sha NOT IN (SELECT sha FROM topic_files WHERE scope=? AND cid=? ORDER BY rowid DESC LIMIT 30)",
                (topic.scope, topic.cid, topic.scope, topic.cid),
            )

    def files(self, topic):
        return [
            {
                "name": r["name"],
                "text": r["content"],
                "file_sha256": r["sha"],
                "source_message_id": r["mid"],
            }
            for r in self.db.execute(
                "SELECT * FROM topic_files WHERE scope=? AND cid=? ORDER BY rowid DESC LIMIT 30",
                (topic.scope, topic.cid),
            )
        ]
