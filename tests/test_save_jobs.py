import asyncio
import tempfile
import unittest
from pathlib import Path

from support import Backend, allow, models, plan, service_mod, store_mod, topic


class SaveJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(Path(self.tmp.name))
        self.backend = Backend()
        self.service = service_mod.Service(self.store, self.backend, {})

    async def asyncTearDown(self):
        await asyncio.gather(*self.service.jobs.values(), return_exceptions=True)
        self.store.close()
        self.tmp.cleanup()

    async def test_background_survives_request_and_retry_deduplicates(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def review(*args):
            entered.set()
            await release.wait()
            return await allow()

        result = await self.service.submit(topic(), plan(), review, wait_seconds=0.001)
        self.assertEqual(result["status"], "processing")
        self.assertEqual(self.service.status(topic(), result["task_id"])["status"], "reviewing")
        retry = await self.service.submit(topic(mid="retry"), plan(), review, wait_seconds=0.001)
        self.assertEqual(retry["task_id"], result["task_id"])
        with self.assertRaises(models.KnowledgeError):
            self.service.status(topic(actor="bob"), result["task_id"])
        release.set()
        await asyncio.gather(*self.service.jobs.values())
        self.assertEqual(self.service.status(topic(), result["task_id"])["status"], "saved")
        self.assertEqual(self.backend.uploads, 1)

    async def test_review_timeout_reports_no_write(self):
        async def timeout(*args):
            raise TimeoutError()

        result = await self.service.submit(topic(), plan(), timeout)
        self.assertEqual(result["status"], "needs_attention")
        self.assertIn("尚未开始", result["message"])
        self.assertEqual(self.backend.uploads, 0)
        self.assertEqual(self.store.projects(), [])
        result = await self.service.submit(topic(mid="retry"), plan(), allow)
        self.assertEqual(result["status"], "saved")

    async def test_caller_cancellation_does_not_cancel_save(self):
        release = asyncio.Event()

        async def review(*args):
            await release.wait()
            return await allow()

        caller = asyncio.create_task(self.service.submit(topic(), plan(), review))
        await asyncio.sleep(0.001)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        await asyncio.gather(*self.service.jobs.values())
        self.assertEqual(self.service.status(topic())[0]["status"], "saved")

    async def test_source_survives_reload_and_is_scope_bound(self):
        original = topic()
        self.service.remember_source(
            original, plan(), [{"record_id": "example", "content": "read evidence"}]
        )
        restarted = service_mod.Service(self.store, self.backend, {})
        saved, evidence = restarted.restore_source(topic(mid="new message"), plan())
        self.assertEqual(saved.message_id, original.message_id)
        self.assertEqual(evidence[0]["content"], "read evidence")
        self.assertIsNone(restarted.restore_source(topic(actor="bob"), plan()))
        self.assertIsNone(restarted.restore_source(topic(cid="different"), plan()))
        self.assertIsNone(restarted.restore_source(topic(), plan(content="changed")))
