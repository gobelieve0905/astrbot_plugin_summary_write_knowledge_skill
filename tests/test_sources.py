import importlib
import json
import unittest
from dataclasses import replace

from support import models, topic

SourceWindow = importlib.import_module("knowledge_test_plugin.sources").SourceWindow


class SourceTests(unittest.TestCase):
    def window(self):
        return SourceWindow(
            replace(
                topic(),
                history=[
                    {"role": "tool", "content": "x" * 350000},
                    {"role": "user", "content": "确认标准"},
                ],
            ),
            10000,
        )

    def test_long_history_directory_is_bounded_and_not_silently_selected(self):
        w = self.window()
        self.assertIsNone(w.selected)
        result = w.directory()
        self.assertGreater(result["total_history_chars"], 350000)
        self.assertLess(len(json.dumps(result)), 3000)
        self.assertEqual(result["sources"][0]["chars"], len(w.sources["h:0"]["text"]))

    def test_small_topic_retains_full_context(self):
        t = topic()
        self.assertEqual(SourceWindow(t, 10000).selected, t)

    def test_cannot_select_unread_text_or_stale_snapshot(self):
        w = self.window()
        span = [{"id": "h:1", "start": 0, "end": 10}]
        with self.assertRaises(models.KnowledgeError):
            w.select(w.snapshot, span, "用户要求的标准")
        w.read("h:1")
        with self.assertRaises(models.KnowledgeError):
            w.select("old", span, "用户要求的标准")

    def test_selected_text_retains_authorization_and_provenance(self):
        w = self.window()
        data = w.read("h:1")
        chosen = w.select(
            w.snapshot, [{"id": "h:1", "start": 0, "end": data["end"]}], "仅保存指定标准"
        )
        self.assertEqual(chosen.request, w.topic.request)
        self.assertEqual(chosen.message_id, w.topic.message_id)
        self.assertEqual(chosen.selection["snapshot_sha256"], w.snapshot)
        self.assertIn("未选择", chosen.coverage)
        self.assertLess(len(json.dumps(chosen.to_dict())), 3000)
        self.assertEqual(w.topic.history[0]["content"], "x" * 350000)

    def test_page_gaps_are_rejected_and_contiguous_pages_accepted(self):
        w = self.window()
        w.read("h:0", 0, 100)
        w.read("h:0", 200, 100)
        span = [{"id": "h:0", "start": 0, "end": 300}]
        with self.assertRaises(models.KnowledgeError):
            w.select(w.snapshot, span, "范围说明")
        w.read("h:0", 100, 100)
        self.assertEqual(len(w.select(w.snapshot, span, "范围说明").history[0]["content"]), 300)

    def test_oversize_selection_leaves_previous_selection_intact(self):
        w = self.window()
        w.read("h:0", 0, 16000)
        with self.assertRaises(models.KnowledgeError):
            w.select(w.snapshot, [{"id": "h:0", "start": 0, "end": 16000}], "范围说明")
        self.assertIsNone(w.selected)

    def test_attachment_requires_explicit_selection_and_changes_snapshot(self):
        t = topic()
        a = SourceWindow(t, 10000, [{"name": "test.md", "text": "confirmed"}])
        b = SourceWindow(t, 10000, [{"name": "test.md", "text": "changed"}])
        self.assertIsNone(a.selected)
        self.assertNotEqual(a.snapshot, b.snapshot)
        self.assertEqual(a.read("a:0")["text"], "confirmed")

    def test_current_request_can_be_selected_without_old_history(self):
        w = self.window()
        read = w.read("current")
        chosen = w.select(
            w.snapshot,
            [{"id": "current", "start": 0, "end": read["end"]}],
            "用户只要求保存当前正文",
        )
        self.assertEqual(chosen.history[0]["content"], w.topic.request)

    def test_directory_pagination_and_invalid_offsets(self):
        w = self.window()
        self.assertEqual(len(w.directory(limit=1)["sources"]), 1)
        self.assertEqual(w.directory(limit=1)["next_offset"], 1)
        for value in [-1, True, "0"]:
            with self.assertRaises(models.KnowledgeError):
                w.read("h:0", value)


if __name__ == "__main__":
    unittest.main()
