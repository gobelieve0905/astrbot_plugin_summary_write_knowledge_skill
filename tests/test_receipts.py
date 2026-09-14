import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "receipt_guard_test", Path(__file__).resolve().parents[1] / "receipts.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Receipts(unittest.TestCase):
    def test_report_saved_is_not_a_knowledge_receipt(self):
        for answer in [
            "报表已保存为 JSON 文件。已查询 25 个账户，还需查询其余账户。",
            "筛选条件已保存。数据仍不完整。",
        ]:
            self.assertEqual(module.protect_knowledge_claims(answer), answer)

    def test_only_false_knowledge_statement_is_replaced(self):
        answer = "查到 4 份报表。已保存到知识库。尚未查完所有账户。"
        fixed = module.protect_knowledge_claims(answer)
        self.assertTrue(fixed.startswith("查到 4 份报表。"))
        self.assertTrue(fixed.endswith("尚未查完所有账户。"))
        self.assertIn("尚不能确认", fixed)

    def test_failed_write_still_cannot_claim_generic_success(self):
        self.assertIn(
            "尚不能确认", module.protect_knowledge_claims("已保存。", write_attempted=True)
        )

    def test_negative_conditional_and_quoted_statements_are_preserved(self):
        for answer in [
            "未获得已保存到知识库的回执。",
            "只有成功后才能说已保存到知识库。",
            "> 示例：已保存到知识库。",
            "```\n已保存到知识库。\n```",
        ]:
            self.assertEqual(module.protect_knowledge_claims(answer), answer)
