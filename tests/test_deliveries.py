import importlib
import tempfile
import unittest
from pathlib import Path

from support import models, store_mod, topic

D = importlib.import_module("knowledge_test_plugin.deliveries").Deliveries
T = importlib.import_module("knowledge_test_plugin.team_skills").TeamSkills


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = store_mod.Store(Path(self.tmp.name))
        self.d = D(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_artifact_owner_and_topic_isolation(self):
        t = topic()
        self.d.register(t, "job", "owner-hash")
        self.d.finish("job", "ready", files=[{"name": "a.md", "body": "真实文件"}])
        self.assertEqual(self.d.file(t, "job", "a.md")["body"], "真实文件")
        for other in [topic(actor="bob"), topic(cid="other"), topic(scope="other")]:
            self.assertIsNone(self.d.file(other, "job", "a.md"))
            self.assertEqual(self.d.catalog(other), [])
        self.s.close()
        self.s = store_mod.Store(Path(self.tmp.name))
        self.d = D(self.s)
        self.assertEqual(self.d.file(t, "job", "a.md")["body"], "真实文件")

    def test_resume_amend_and_no_repeat(self):
        t = topic()
        plan = {"skill": {"name": "x"}, "knowledge": {"title": "p"}}
        r = self.d.create(t, plan)
        self.assertEqual(self.d.create(t, plan)["id"], r["id"])
        self.d.record(t, r["id"], "skill", {"status": "installed"})
        self.assertEqual(self.d.status(t, r["id"])["status"], "partial")
        self.d.amend(t, r["id"], {"knowledge": {"title": "new"}})
        with self.assertRaises(models.KnowledgeError):
            self.d.amend(t, r["id"], {"skill": {"name": "new"}})
        with self.assertRaises(models.KnowledgeError):
            self.d.bundle(topic(actor="bob"), r["id"])
        self.d.record(t, r["id"], "knowledge", {"status": "saved"})
        self.assertEqual(self.d.status(t, r["id"])["status"], "complete")
        self.s.close()
        self.s = store_mod.Store(Path(self.tmp.name))
        self.d = D(self.s)
        self.assertEqual(self.d.status(t, r["id"])["status"], "complete")

    def test_private_import_cache_and_quoted_source(self):
        t = topic()
        teams = T(self.s)
        teams.cache_files(
            t, [{"name": "p.md", "text": "私有", "_owner": t.actor, "source_message_id": "old-mid"}]
        )
        self.assertEqual(teams.files(t)[0]["source_message_id"], "old-mid")
        self.assertEqual(teams.files(topic(actor="bob")), [])
        teams.cache_files(
            topic(actor="bob"), [{"name": "same.md", "text": "私有", "_owner": "bob"}]
        )
        self.assertEqual(teams.files(topic(actor="bob"))[0]["name"], "same.md")

    def test_skill_operation_idempotency(self):
        teams = T(self.s)
        who = {"actor": "ou_a", "tenant": "t", "admin": False}
        body = "---\nname: query\ndescription: 查询时使用\n---\n核对项目后查询。"
        first = teams.save("query", body, {}, who, [], {}, operation_key="bundle:skill")
        second = teams.save("query", body, {}, who, [], {}, operation_key="bundle:skill")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["version"], 1)
        self.assertEqual(len(teams.catalog(who, [])), 1)

    def test_legacy_cache_schema_remains_rollback_compatible(self):
        T(self.s)
        self.s.db.execute(
            "INSERT INTO topic_files VALUES('s','c','sha','a.md','text','mid','date')"
        )
        self.s.db.commit()
        self.assertEqual(len(list(self.s.db.execute("PRAGMA table_info(topic_files)"))), 7)
