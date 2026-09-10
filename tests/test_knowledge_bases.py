"""Library scope and selection tests; no AstrBot or network dependency."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import models, plan

AstrBotBackend = importlib.import_module("knowledge_test_plugin.backend").AstrBotBackend


def helper(name, ident, ep="ep", rp="rp"):
    kb = SimpleNamespace(
        kb_name=name,
        kb_id=ident,
        embedding_provider_id=ep,
        rerank_provider_id=rp,
        chunk_size=512,
        chunk_overlap=50,
        top_k_dense=50,
        top_k_sparse=50,
        top_m_final=5,
    )
    return SimpleNamespace(kb=kb, init_error=None, get_rp=AsyncMock(return_value=object()))


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.one = helper("Idol Empire", "kb1")
        self.two = helper("Project B", "kb2")
        self.helpers = [self.one, self.two]
        self.created = []

        async def by_id(ident):
            return next((h for h in self.helpers if h.kb.kb_id == ident), None)

        async def by_name(name):
            return next((h for h in self.helpers if name in {h.kb.kb_id, h.kb.kb_name}), None)

        async def create(**kwargs):
            self.created.append(kwargs)
            result = helper(
                kwargs["kb_name"],
                "created",
                kwargs["embedding_provider_id"],
                kwargs["rerank_provider_id"],
            )
            self.helpers.append(result)
            return result

        self.manager = SimpleNamespace(
            list_kbs=AsyncMock(side_effect=lambda: [h.kb for h in self.helpers]),
            get_kb=AsyncMock(side_effect=by_id),
            get_kb_by_name=AsyncMock(side_effect=by_name),
            create_kb=AsyncMock(side_effect=create),
        )
        self.config = {"template_kb": ""}
        self.backend = AstrBotBackend(SimpleNamespace(kb_manager=self.manager), self.config)
        self.project = {"name": "Idol Empire", "kb_id": "", "id": "project1"}

    async def test_empty_and_whitespace_include_all(self):
        for value in ("", " \n\t "):
            self.config["template_kb"] = value
            self.assertEqual(len(await self.backend.catalog()), 2)

    async def test_names_and_ids_restrict_scope(self):
        self.config["template_kb"] = "kb1"
        self.assertEqual(await self.backend.catalog(), [{"id": "kb1", "name": "Idol Empire"}])
        self.config["template_kb"] = "Project B"
        self.assertEqual(await self.backend.catalog(), [{"id": "kb2", "name": "Project B"}])

    async def test_multiple_lines_select_multiple_libraries(self):
        self.config["template_kb"] = " Idol Empire \n kb2 \n"
        self.assertEqual(len(await self.backend.catalog()), 2)

    async def test_unknown_filter_never_falls_back_to_all(self):
        self.config["template_kb"] = "typo"
        self.assertEqual(await self.backend.catalog(), [])
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure(self.project, plan())

    async def test_exact_project_reuses_existing_library(self):
        result = await self.backend.ensure(self.project, plan(create_project=False))
        self.assertIs(result, self.one)
        self.assertEqual(self.created, [])

    async def test_explicit_target_is_selected_not_first(self):
        result = await self.backend.ensure(
            {**self.project, "name": "Other"}, plan(knowledge_base="kb2")
        )
        self.assertIs(result, self.two)
        self.assertEqual(self.created, [])

    async def test_no_match_without_new_project_request_asks(self):
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure({**self.project, "name": "Other"}, plan(create_project=False))
        self.assertEqual(self.created, [])

    async def test_new_project_reuses_identical_model_config(self):
        result = await self.backend.ensure({**self.project, "name": "New project"}, plan())
        self.assertEqual(result.kb.kb_name, "New project")
        self.assertEqual(self.created[0]["embedding_provider_id"], "ep")
        self.assertEqual(self.created[0]["rerank_provider_id"], "rp")

    async def test_different_model_configs_require_choice(self):
        self.two.kb.embedding_provider_id = "other-ep"
        with self.assertRaisesRegex(models.KnowledgeError, "model_source"):
            await self.backend.ensure({**self.project, "name": "New"}, plan())
        self.assertEqual(self.created, [])
        result = await self.backend.ensure(
            {**self.project, "name": "New"}, plan(model_source="kb2")
        )
        self.assertEqual(result.kb.embedding_provider_id, "other-ep")

    async def test_restricted_mode_does_not_create_outside_scope(self):
        self.config["template_kb"] = "kb1"
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure({**self.project, "name": "New"}, plan())
        self.assertEqual(self.created, [])

    async def test_existing_binding_cannot_move(self):
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure({**self.project, "kb_id": "kb1"}, plan(knowledge_base="kb2"))

    async def test_existing_binding_obeys_changed_allowlist(self):
        self.config["template_kb"] = "kb2"
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure({**self.project, "kb_id": "kb1"})

    async def test_blocked_project_libraries_cannot_be_bypassed(self):
        self.assertEqual(await self.backend.catalog({"kb1"}), [{"id": "kb2", "name": "Project B"}])
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure(self.project, plan(), blocked_ids={"kb1"})
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure({**self.project, "kb_id": "kb1"}, blocked_ids={"kb1"})
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure(
                {**self.project, "name": "New"}, plan(knowledge_base="kb1"), blocked_ids={"kb1"}
            )
        self.assertEqual(self.created, [])

    async def test_no_libraries_requires_configuration(self):
        self.helpers.clear()
        with self.assertRaises(models.KnowledgeError):
            await self.backend.ensure(self.project, plan())

    async def test_invalid_config_is_not_treated_as_all(self):
        self.config["template_kb"] = []
        with self.assertRaises(models.KnowledgeError):
            await self.backend.catalog()
