import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import models

module = importlib.import_module("knowledge_test_plugin.directory")


class Config(dict):
    def __init__(self, **values):
        super().__init__(**values)
        self.schema = {
            field: {"type": "list", "options": [], "hint": "old"} for field in module.FIELDS
        }


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "directory.json"
        self.config = Config(template_kb="Idol Empire\n unknown-id", group_ids=["saved-group"])
        module.normalize_config(self.config)
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="instance1", name="lark"),
            bot_name="Test bot",
            _user_name_cache={"ou_known": ("Alice", 0)},
        )
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[platform]),
            kb_manager=SimpleNamespace(
                list_kbs=AsyncMock(
                    return_value=[SimpleNamespace(kb_id="kb1", kb_name="Idol Empire")]
                )
            ),
        )
        self.directory = module.ChoiceDirectory(self.context, self.config, self.path)

    async def asyncTearDown(self):
        await self.directory.close()
        self.temp.cleanup()

    async def test_local_choices_labels_and_legacy_migration(self):
        await self.directory.refresh_local()
        self.assertEqual(self.config["template_kb"], ["kb1", "unknown-id"])
        self.assertEqual(self.config.schema["platform_ids"]["options"], ["instance1"])
        self.assertEqual(self.config.schema["platform_ids"]["labels"], ["Test bot · instance1"])
        self.assertEqual(self.config.schema["private_ids"]["options"], ["ou_known"])
        self.assertIn("Alice", self.config.schema["writer_ids"]["labels"][0])

    async def test_unknown_selected_ids_remain_selectable(self):
        await self.directory.refresh_local()
        self.assertIn("saved-group", self.config.schema["group_ids"]["options"])
        self.assertIn("unknown-id", self.config.schema["template_kb"]["options"])

    async def test_labels_cannot_replace_stored_ids(self):
        self.directory.remember("groups", "oc_real", "Marketing")
        self.directory.publish()
        self.assertIn("oc_real", self.config.schema["group_ids"]["options"])
        self.assertNotIn("Marketing", self.config.schema["group_ids"]["options"])

    async def test_unknown_id_observation_does_not_erase_name(self):
        self.directory.remember("groups", "oc_real", "Marketing")
        self.directory.remember("groups", "oc_real", "oc_real")
        self.assertEqual(self.directory.known["groups"]["oc_real"]["name"], "Marketing")

    async def test_cache_contains_identity_only_and_survives_reload(self):
        self.directory.remember("groups", "oc_real", "Marketing")
        await self.directory.refresh(remote=False)
        contents = json.loads(self.path.read_text())
        self.assertEqual(set(contents), {"groups", "users"})
        self.assertEqual(set(contents["groups"]["oc_real"]), {"name", "seen"})
        restored = module.ChoiceDirectory(self.context, self.config, self.path)
        self.assertEqual(restored.known["groups"]["oc_real"]["name"], "Marketing")

    async def test_refresh_failure_keeps_selected_and_known(self):
        self.directory.remember("groups", "oc_known", "Known")
        self.directory.refresh_remote = AsyncMock(side_effect=RuntimeError("private response body"))
        await self.directory.refresh()
        node = self.config.schema["group_ids"]
        self.assertEqual(set(node["options"]), {"saved-group", "oc_known"})
        self.assertNotIn("private response body", node["hint"])
        self.assertIn("部分目录", node["hint"])

    async def test_empty_directory_remains_multi_select_and_empty_means_all(self):
        self.config["group_ids"] = []
        self.directory.publish()
        node = self.config.schema["group_ids"]
        self.assertEqual(node["type"], "list")
        self.assertEqual(node["options"], [])
        self.assertIn("留空表示全部", node["hint"])

    async def test_ambiguous_legacy_name_is_not_guessed(self):
        self.context.kb_manager.list_kbs.return_value = [
            SimpleNamespace(kb_id="kb1", kb_name="Idol Empire"),
            SimpleNamespace(kb_id="kb2", kb_name="Idol Empire"),
        ]
        await self.directory.refresh_local()
        self.assertEqual(self.config["template_kb"][0], "Idol Empire")

    async def test_pagination_reads_all_pages(self):
        def response(items, more, token):
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(items=items, has_more=more, page_token=token),
            )

        call = AsyncMock(side_effect=[response([1], True, "next"), response([2], False, "")])
        result = [item async for item in self.directory.pages(call, lambda token: token)]
        self.assertEqual(result, [1, 2])
        self.assertEqual(call.call_args_list[1].args, ("next",))

    async def test_repeating_page_token_fails_without_loop(self):
        call = AsyncMock(
            return_value=SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(items=[], has_more=True, page_token="same"),
            )
        )
        with self.assertRaises(models.KnowledgeError):
            _ = [item async for item in self.directory.pages(call, lambda token: token)]
        self.assertEqual(call.await_count, 2)

    async def test_permission_denial_fails_without_using_response_text(self):
        call = AsyncMock(return_value=SimpleNamespace(success=lambda: False, data=None))
        with self.assertRaises(models.KnowledgeError):
            _ = [item async for item in self.directory.pages(call, lambda token: token)]


class MigrationTests(unittest.TestCase):
    def test_old_text_and_new_arrays_supported(self):
        config = Config(template_kb="a\nb\na", writer_ids=[" ", "ou_test", "ou_test"])
        module.normalize_config(config)
        self.assertEqual(config["template_kb"], ["a", "b"])
        self.assertEqual(config["writer_ids"], ["ou_test"])
        module.normalize_config(config)
        self.assertEqual(config["template_kb"], ["a", "b"])

    def test_invalid_list_is_not_silently_widened(self):
        with self.assertRaises(models.KnowledgeError):
            module.normalize_config(Config(group_ids=[1]))
