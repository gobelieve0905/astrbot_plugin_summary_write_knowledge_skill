import importlib
import tempfile
import unittest
from pathlib import Path

from support import models

module = importlib.import_module("knowledge_test_plugin.skill_install")
BODY = (
    "---\nname: meta-query\ndescription: Meta 查询通用流程\n---\n先确认项目，再查询并核对分页。\n"
)


class InstallTests(unittest.TestCase):
    def test_install_update_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = module.SkillInstaller(root / "skills", root / "records")
            result = installer.install("meta-query", BODY, "", {}, lambda *a: True)
            target = root / "skills/meta-query/SKILL.md"
            self.assertEqual(target.read_text(), BODY)
            with self.assertRaises(models.KnowledgeError):
                installer.install("meta-query", BODY + "新版", "", {}, lambda *a: True)
            with self.assertRaises(models.KnowledgeError):
                installer.install(
                    "meta-query", BODY + "新版", result["sha256"], {}, lambda *a: False
                )
            self.assertEqual(target.read_text(), BODY)
            updated = installer.install(
                "meta-query", BODY + "新版", result["sha256"], {}, lambda *a: True
            )
            self.assertNotEqual(updated["sha256"], result["sha256"])
            target.write_text(BODY + "人工修改")
            with self.assertRaises(models.KnowledgeError):
                installer.install("meta-query", BODY, updated["sha256"], {}, lambda *a: True)

    def test_failed_discovery_removes_new_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = module.SkillInstaller(root / "skills", root / "records")
            with self.assertRaises(models.KnowledgeError):
                installer.install("meta-query", BODY, "", {}, lambda *a: False)
            self.assertFalse((root / "skills/meta-query").exists())

    def test_reject_invalid_and_unmanaged(self):
        for name, body in [
            ("../escape", BODY),
            ("meta-query", BODY.replace("description:", "name:")),
            ("meta-query", BODY.replace("\n---\n先", "\nextra: true\n---\n先")),
        ]:
            with self.assertRaises(models.KnowledgeError):
                module.validate(name, body)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "skills/meta-query"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(BODY)
            with self.assertRaises(models.KnowledgeError):
                module.SkillInstaller(root / "skills", root / "records").install(
                    "meta-query", BODY, "", {}, lambda *a: True
                )


class ReceiptTests(unittest.TestCase):
    def test_native_receipt(self):
        protect = importlib.import_module("knowledge_test_plugin.receipts").protect_native_claims
        self.assertIn("没有成功", protect("原生技能已安装。"))
        self.assertEqual(protect("尚不能确认已安装。"), "尚不能确认已安装。")
        self.assertEqual(protect("> 已安装\n"), "> 已安装\n")
