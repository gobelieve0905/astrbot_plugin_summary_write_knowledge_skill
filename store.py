"""SQLite is the authority for active versions; file/index writes are staged."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .models import KnowledgeError, utcnow


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "knowledge.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise KnowledgeError("知识数据库版本不兼容。")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, scope TEXT NOT NULL,
            kb_id TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bindings(
            scope TEXT NOT NULL, cid TEXT NOT NULL, project_id TEXT NOT NULL,
            PRIMARY KEY(scope,cid));
        CREATE TABLE IF NOT EXISTS revisions(
            op TEXT PRIMARY KEY, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            project_id TEXT NOT NULL, platform TEXT NOT NULL, title TEXT NOT NULL,
            kind TEXT NOT NULL, plan TEXT NOT NULL, source TEXT NOT NULL,
            state TEXT NOT NULL, doc_id TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL, created TEXT NOT NULL,
            UNIQUE(record_id,version));
        CREATE UNIQUE INDEX IF NOT EXISTS active_record ON revisions(record_id) WHERE state='active';
        CREATE TABLE IF NOT EXISTS messages(
            scope TEXT NOT NULL, cid TEXT NOT NULL, mid TEXT NOT NULL,
            actor TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL,
            PRIMARY KEY(scope,mid));
        PRAGMA user_version=1;
        """)

    def project(self, name):
        row = self.db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def projects(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM projects ORDER BY name")]

    def create_project(self, name, scope):
        with self.db:
            self.db.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, name, scope, "", utcnow()),
            )
        return self.project(name)

    def set_kb(self, project_id, kb_id):
        with self.db:
            self.db.execute("UPDATE projects SET kb_id=? WHERE id=?", (kb_id, project_id))

    def binding(self, scope, cid):
        row = self.db.execute(
            "SELECT p.name FROM bindings b JOIN projects p ON b.project_id=p.id WHERE b.scope=? AND b.cid=?",
            (scope, cid),
        ).fetchone()
        return row[0] if row else None

    def observe(self, topic):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)",
                (topic.scope, topic.cid, topic.message_id, topic.actor, topic.request, utcnow()),
            )

    def messages(self, scope, cid):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT mid,actor,created FROM messages WHERE scope=? AND cid=? ORDER BY created",
                (scope, cid),
            )
        ]

    def operation(self, op):
        row = self.db.execute("SELECT * FROM revisions WHERE op=?", (op,)).fetchone()
        return dict(row) if row else None

    def active(self, record_id):
        row = self.db.execute(
            "SELECT * FROM revisions WHERE record_id=? AND state='active'", (record_id,)
        ).fetchone()
        return dict(row) if row else None

    def records(self, project_id, history=False):
        clause = "" if history else " AND state='active'"
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM revisions WHERE project_id=?" + clause + " ORDER BY created DESC",
                (project_id,),
            )
        ]

    def stage(self, op, record_id, version, project_id, plan, source, filename):
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?,'pending','',?,?)",
                    (
                        op,
                        record_id,
                        version,
                        project_id,
                        plan.platform,
                        plan.title,
                        plan.kind,
                        json.dumps(plan.to_dict(), ensure_ascii=False),
                        json.dumps(source, ensure_ascii=False),
                        filename,
                        utcnow(),
                    ),
                )
        except sqlite3.IntegrityError:
            raise KnowledgeError(
                "此版本已有待处理写入，请重试原操作，或先处理待处理版本。"
            ) from None
        return self.operation(op)

    def set_doc(self, op, doc_id):
        with self.db:
            self.db.execute("UPDATE revisions SET doc_id=? WHERE op=?", (doc_id, op))

    def activate(self, op, expected_version, scope, cid):
        row = self.operation(op)
        with self.db:
            current = self.active(row["record_id"])
            if (current["version"] if current else 0) != expected_version:
                raise KnowledgeError("规则已被其他成员修改，请重新读取最新版本后再更新。")
            self.db.execute(
                "UPDATE revisions SET state='superseded' WHERE record_id=? AND state='active'",
                (row["record_id"],),
            )
            self.db.execute("UPDATE revisions SET state='active' WHERE op=?", (op,))
            # Keep the first unambiguous topic association; explicit one-off exports don't reroute it.
            self.db.execute(
                "INSERT OR IGNORE INTO bindings VALUES(?,?,?)", (scope, cid, row["project_id"])
            )

    def close(self):
        self.db.close()
