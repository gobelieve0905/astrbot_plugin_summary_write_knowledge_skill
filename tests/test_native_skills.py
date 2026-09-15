import importlib
import tempfile
import unittest
from pathlib import Path

from support import models

read = importlib.import_module("knowledge_test_plugin.native_skills").read_text_resource


class SkillFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "skill"
        self.root.mkdir()
        (self.root / "SKILL.md").write_text("技能规范" * 10000)
        (self.root / "references").mkdir()
        (self.root / "references" / "guide.md").write_text("配套说明")

    def tearDown(self):
        self.temp.cleanup()

    def test_full_text_pagination_and_hash(self):
        first = read(self.root, "SKILL.md")
        self.assertFalse(first["complete"])
        parts = [first["content"]]
        result = first
        while result["next_offset"] is not None:
            result = read(self.root, "SKILL.md", result["next_offset"])
            self.assertEqual(first["sha256"], result["sha256"])
            parts.append(result["content"])
        self.assertEqual("".join(parts), (self.root / "SKILL.md").read_text())
        self.assertEqual(read(self.root, "references/guide.md")["content"], "配套说明")

    def test_traversal_absolute_and_hidden_files_denied(self):
        for name in [
            "../secret.md",
            "/etc/passwd",
            ".env",
            "references/../../secret.md",
            "secret.bin",
        ]:
            with self.assertRaises(models.KnowledgeError):
                read(self.root, name)

    def test_symlink_file_and_directory_denied(self):
        (Path(self.temp.name) / "secret.md").write_text("secret")
        (self.root / "link.md").symlink_to(Path(self.temp.name) / "secret.md")
        (self.root / "linked").symlink_to(self.root / "references", target_is_directory=True)
        for name in ["link.md", "linked/guide.md"]:
            with self.assertRaises(models.KnowledgeError):
                read(self.root, name)

    def test_binary_and_large_files_denied(self):
        p = self.root / "bad.md"
        p.write_bytes(b"\xff")
        with self.assertRaises(models.KnowledgeError):
            read(self.root, "bad.md")
        p.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(models.KnowledgeError):
            read(self.root, "bad.md")
