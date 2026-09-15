import importlib
import tempfile
import unittest
from pathlib import Path

from support import models, store_mod, topic

TeamSkills = importlib.import_module("knowledge_test_plugin.team_skills").TeamSkills
BODY = "---\nname: meta-query\ndescription: 查询 Meta 数据时使用\n---\n确认项目，分页查询，核对时区和币种。\n"
A = {"actor": "ou_a", "tenant": "tenant1", "admin": False}
B = {"actor": "ou_b", "tenant": "tenant1", "admin": False}
C = {"actor": "ou_c", "tenant": "tenant2", "admin": False}
ADMIN = {"actor": "admin", "tenant": "", "admin": True}


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(Path(self.tmp.name))
        self.skills = TeamSkills(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, meta=None):
        return self.skills.save(
            "meta-query", BODY, meta or {}, A, ["Idol Empire"], {"request": "保存"}
        )

    def test_personal_is_private_and_tenant_scoped(self):
        r = self.save()
        self.assertEqual(len(self.skills.catalog(A, [])), 1)
        self.assertEqual(self.skills.catalog(B, []), [])
        self.assertEqual(self.skills.catalog(C, []), [])
        self.assertEqual(len(self.skills.catalog(ADMIN, [])), 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(r["id"], B, [], "", "")

    def test_team_and_project_scope(self):
        r = self.save({"sharing": "team", "platform": "Meta"})
        self.assertEqual(len(self.skills.catalog(B, [])), 1)
        self.assertEqual(self.skills.catalog(C, []), [])
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(r["id"], B, [], "AppLovin", "")
        self.assertEqual(self.skills.read(r["id"], B, [], "Meta", "")["body"], BODY)
        p = self.save({"sharing": "project", "project": "Idol Empire"})
        self.assertEqual(len(self.skills.catalog(B, ["Idol Empire"])), 2)
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(p["id"], B, [], "", "Idol Empire")
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(p["id"], B, ["Idol Empire"], "", "Other")

    def test_update_pin_and_conflict(self):
        r = self.save({"sharing": "team"})
        ident = r["id"]
        new = self.skills.save("meta-query", BODY + "新规则", r["metadata"], A, [], {}, ident, 1)
        self.assertEqual(new["version"], 2)
        self.assertEqual(self.skills.read(ident, B, [], "", "", 1)["body"], BODY)
        with self.assertRaises(models.KnowledgeError):
            self.skills.save("meta-query", BODY, r["metadata"], A, [], {}, ident, 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.save("meta-query", BODY, r["metadata"], B, [], {}, ident, 2)
        self.skills.manage(ident, "disable", A, [], 2)
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(ident, B, [], "", "", 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.manage(ident, "enable", A, [], 2)
        self.skills.manage(ident, "enable", ADMIN, [], 3)
        rolled = self.skills.manage(ident, "rollback", A, [], 4, version=1)
        self.assertEqual(rolled["version"], 5)
        self.assertEqual(self.skills.row(ident)["body"], BODY)

    def test_revoke_sharing_blocks_pinned_version(self):
        r = self.save({"sharing": "team"})
        self.skills.save("meta-query", BODY, {}, A, [], {}, r["id"], 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.read(r["id"], B, [], "", "", 1)

    def test_maintainer_cannot_expand_scope(self):
        r = self.save({"maintainers": ["ou_b"]})
        self.skills.save("meta-query", BODY + "更新", r["metadata"], B, [], {}, r["id"], 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.save(
                "meta-query", BODY, {**r["metadata"], "sharing": "team"}, B, [], {}, r["id"], 2
            )

    def test_duplicate_feedback_and_history(self):
        r = self.save({"sharing": "team"})
        with self.assertRaises(models.KnowledgeError):
            self.save({"sharing": "team"})
        result = self.skills.manage(r["id"], "suggest", B, [], 1, "增加时区说明")
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(self.skills.row(r["id"])["version"], 1)
        with self.assertRaises(models.KnowledgeError):
            self.skills.history(r["id"], B, [])
        self.assertEqual(len(self.skills.history(r["id"], ADMIN, [])), 1)

    def test_invalid_metadata(self):
        for meta in [
            {"sharing": "other"},
            {"sharing": "project"},
            {"project": "Other"},
            {"project": "Idol Empire", "sharing": "team"},
            {"maintainers": ["not-open-id"]},
        ]:
            with self.assertRaises(models.KnowledgeError):
                self.save(meta)

    def test_attachment_cache_is_topic_scoped_and_persistent(self):
        t = topic()
        self.skills.cache_files(t, [{"name": "file.md", "text": "内容"}])
        self.assertEqual(self.skills.files(t)[0]["text"], "内容")
        self.assertEqual(self.skills.files(topic(cid="other")), [])
        self.assertEqual(self.skills.files(topic(scope="other")), [])
        self.store.close()
        self.store = store_mod.Store(Path(self.tmp.name))
        self.skills = TeamSkills(self.store)
        self.assertEqual(self.skills.files(t)[0]["name"], "file.md")

    def test_failed_readback_rolls_back_transaction(self):
        from unittest.mock import patch

        with patch.object(self.skills, "row", side_effect=models.KnowledgeError("回读失败")):
            with self.assertRaises(models.KnowledgeError):
                self.save()
        self.assertEqual(self.skills.catalog(A, []), [])

    def test_platform_and_category_catalog(self):
        self.save({"sharing": "team", "category": "数据查询", "platform": "Meta"})
        self.assertEqual(len(self.skills.catalog(B, [], query="数据查询", platform="Meta")), 1)
        self.assertEqual(self.skills.catalog(B, [], platform="AppLovin"), [])
