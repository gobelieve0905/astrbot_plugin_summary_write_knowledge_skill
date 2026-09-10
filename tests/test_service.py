import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from support import Backend, allow, models, plan, service_mod, store_mod, topic


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(Path(self.temp.name))
        self.backend = Backend()
        self.config = {}
        self.service = service_mod.Service(self.store, self.backend, self.config)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def save(self, **changes):
        return await self.service.save(topic(), plan(**changes), allow)

    async def test_save_files_index_and_sources(self):
        result = await self.save()
        self.assertEqual(result["status"], "saved")
        row = self.store.active(result["record_id"])
        body = self.service.path(row).read_text()
        self.assertIn("AppLovin", body)
        self.assertIn("topic-A", body)
        self.assertNotIn('"snapshot":', body)
        source = json.loads((self.service.path(row).parent / "topic.json").read_text())
        self.assertEqual(source["actor"], "alice")
        self.assertEqual(self.store.binding("scope1", "topic-A"), "Idol Empire")

    async def test_index_failure_not_active_retry_resumes(self):
        self.backend.fail_verify = True
        with self.assertRaises(models.KnowledgeError):
            await self.save()
        self.assertEqual(self.store.records(self.store.project("Idol Empire")["id"]), [])
        self.backend.fail_verify = False
        result = await self.save()
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.backend.uploads, 1)

    async def test_later_message_retry_same_plan(self):
        self.backend.fail_verify = True
        with self.assertRaises(models.KnowledgeError):
            await self.save()
        self.backend.fail_verify = False
        result = await self.service.save(topic(mid="retry"), plan(), allow)
        self.assertEqual(result["version"], 1)
        self.assertEqual(self.backend.uploads, 1)

    async def test_duplicate_message_is_idempotent(self):
        a = await self.save()
        b = await self.save()
        self.assertEqual(a["record_id"], b["record_id"])
        self.assertEqual(self.backend.uploads, 1)

    async def test_update_only_one_active_and_keeps_history(self):
        first = await self.save()
        old = self.store.active(first["record_id"])
        result = await self.service.save(
            topic(mid="m2", actor="bob"),
            plan(record_id=first["record_id"], expected_version=1, content="新的标准"),
            allow,
        )
        self.assertEqual(result["version"], 2)
        rows = self.store.records(old["project_id"], history=True)
        self.assertEqual({r["state"] for r in rows}, {"active", "superseded"})
        self.assertTrue(self.service.path(old).exists())
        self.assertNotIn(old["doc_id"], self.backend.docs)

    async def test_conflicting_users_reject_second_update(self):
        first = await self.save()
        updated = plan(record_id=first["record_id"], expected_version=1, content="新的标准")
        results = await asyncio.gather(
            self.service.save(topic(mid="m2", actor="bob"), updated, allow),
            self.service.save(topic(mid="m3", actor="carol"), updated, allow),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, models.KnowledgeError) for r in results), 1)

    async def test_failed_update_retains_old(self):
        first = await self.save()
        self.backend.fail_verify = True
        with self.assertRaises(models.KnowledgeError):
            await self.service.save(
                topic(mid="m2"),
                plan(record_id=first["record_id"], expected_version=1, content="new"),
                allow,
            )
        self.assertEqual(self.store.active(first["record_id"])["version"], 1)

    async def test_failed_cleanup_excludes_stale_index(self):
        first = await self.save()
        self.backend.fail_delete = True
        result = await self.service.save(
            topic(mid="m2"),
            plan(record_id=first["record_id"], expected_version=1, content="新标准"),
            allow,
        )
        self.assertTrue(result["obsolete_index_cleanup_pending"])
        found = await self.service.search(topic(), "Idol Empire", "AppLovin", "标准")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["version"], 2)

    async def test_other_platform_never_returned(self):
        await self.save()
        self.assertEqual(await self.service.search(topic(), "Idol Empire", "Meta", "标准"), [])

    async def test_scope_is_acl_not_project_routing(self):
        first = await self.save()
        outsider = topic(scope="scope2")
        self.assertEqual(self.service.catalog(outsider)["projects"], [])
        with self.assertRaises(models.KnowledgeError):
            await self.service.read(outsider, first["record_id"], "AppLovin")
        self.config["project_shares"] = {"Idol Empire": ["scope2"]}
        self.assertEqual(
            (await self.service.read(outsider, first["record_id"], "AppLovin"))["project"],
            "Idol Empire",
        )
        self.assertIsNone(self.service.catalog(outsider)["bound_project"])

    async def test_old_topic_binding_survives_other_projects(self):
        await self.save()
        await self.service.save(
            topic(mid="m2", cid="topic-B"), plan(project="Other project"), allow
        )
        self.assertEqual(self.service.catalog(topic())["bound_project"], "Idol Empire")

    async def test_ambiguous_project_does_not_write(self):
        async def reject(*args):
            return {"allow": False, "question": "请明确项目"}

        with self.assertRaisesRegex(models.KnowledgeError, "请明确项目"):
            await self.service.save(topic(), plan(), reject)
        self.assertEqual(self.store.projects(), [])
        self.assertEqual(self.backend.uploads, 0)

    async def test_summary_without_authorization_no_write(self):
        async def reject(t, *args):
            return {"allow": t.request != "总结一下"}

        with self.assertRaises(models.KnowledgeError):
            await self.service.save(topic(request="总结一下"), plan(), reject)
        self.assertEqual(self.backend.uploads, 0)

    async def test_platform_change_cannot_replace_other_rule(self):
        first = await self.save()
        with self.assertRaises(models.KnowledgeError):
            await self.service.save(
                topic(mid="m2"),
                plan(record_id=first["record_id"], expected_version=1, platform="Meta"),
                allow,
            )

    async def test_duplicate_new_title_refused(self):
        await self.save()
        with self.assertRaises(models.KnowledgeError):
            await self.service.save(topic(mid="m2"), plan(content="偷偷新增标准"), allow)

    async def test_skill_is_scoped_and_readable(self):
        first = await self.save(
            kind="skill",
            content="## 触发\n测试分析\n## 输入\n数据\n## 步骤\n核对数据\n## 输出\n结论\n## 边界\n本项目",
        )
        result = await self.service.read(topic(), first["record_id"], "AppLovin")
        self.assertTrue(result["content"].startswith("---\nname:"))
        self.assertEqual(self.service.path(self.store.active(first["record_id"])).name, "SKILL.md")
        with self.assertRaises(models.KnowledgeError):
            await self.service.read(topic(), first["record_id"], "Meta")

    async def test_restart_retains_active_and_binding(self):
        first = await self.save()
        self.store.close()
        self.store = store_mod.Store(Path(self.temp.name))
        self.service = service_mod.Service(self.store, self.backend, self.config)
        self.assertEqual(self.store.active(first["record_id"])["version"], 1)
        self.assertEqual(self.service.catalog(topic())["bound_project"], "Idol Empire")

    async def test_bound_project_cannot_silently_change(self):
        await self.save()

        async def implicit(*args):
            return {"allow": True, "explicit_project": False}

        with self.assertRaises(models.KnowledgeError):
            await self.service.save(topic(mid="m2"), plan(project="Other"), implicit)

    async def test_pending_upload_cancellation_not_visible(self):
        async def cancelled(*args):
            raise asyncio.CancelledError

        self.backend.verify = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.save()
        self.assertEqual(self.store.records(self.store.project("Idol Empire")["id"]), [])

    async def test_corrupt_saved_file_refused(self):
        self.backend.fail_verify = True
        with self.assertRaises(models.KnowledgeError):
            await self.save()
        row = self.store.records(self.store.project("Idol Empire")["id"], history=True)[0]
        self.service.path(row).write_text("tampered")
        self.backend.fail_verify = False
        with self.assertRaises(models.KnowledgeError):
            await self.save()

    async def test_active_file_tampering_blocks_read_and_receipt(self):
        first = await self.save()
        self.service.path(self.store.active(first["record_id"])).write_text("tampered")
        with self.assertRaises(models.KnowledgeError):
            await self.service.read(topic(), first["record_id"], "AppLovin")
        with self.assertRaises(models.KnowledgeError):
            await self.save()

    async def test_source_ids_are_indexed_but_raw_chat_is_not(self):
        source = topic()
        source = models.TopicContext(
            **{
                **source.to_dict(),
                "source_messages": [
                    {
                        "mid": "old",
                        "actor": "bob",
                        "created": "2026-09-10",
                        "content": "unconfirmed-secret-idea",
                    }
                ],
            }
        )
        result = await self.service.save(source, plan(), allow)
        body = self.service.path(self.store.active(result["record_id"])).read_text()
        self.assertIn('"mid": "old"', body)
        self.assertNotIn("unconfirmed-secret-idea", body)

    async def test_old_operation_cannot_reactivate_superseded(self):
        first = await self.save()
        await self.service.save(
            topic(mid="m2"),
            plan(record_id=first["record_id"], expected_version=1, content="new"),
            allow,
        )
        with self.assertRaises(models.KnowledgeError):
            await self.save()


class ValidationTests(unittest.TestCase):
    def test_path_traversal_record_id(self):
        with self.assertRaises(models.KnowledgeError):
            plan(record_id="../../x", expected_version=1)

    def test_unknown_fields(self):
        with self.assertRaises(models.KnowledgeError):
            plan(command="rm")

    def test_future_time(self):
        with self.assertRaises(models.KnowledgeError):
            plan(effective_at="2099-01-01T00:00:00+00:00")

    def test_version_requires_id(self):
        with self.assertRaises(models.KnowledgeError):
            plan(expected_version=True)
        with self.assertRaises(models.KnowledgeError):
            plan(expected_version=1)

    def test_atomic_file_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "knowledge.md"
            service_mod.write_atomic(path, "original")
            with self.assertRaises(models.KnowledgeError):
                service_mod.write_atomic(path, "changed")
            self.assertEqual(path.read_text(), "original")
