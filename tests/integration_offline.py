"""Real AstrBot/Lark/SQLite/FAISS; deterministic providers, no network or live data."""

import asyncio
import hashlib
import importlib
import json
import os
import socket
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock


def deny_network(*args, **kwargs):
    raise AssertionError("Network forbidden in offline integration")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
root = tempfile.TemporaryDirectory(prefix="knowledge-integration-")
os.environ["ASTRBOT_ROOT"] = root.name


class Embeddings:
    def get_dim(self):
        return 8

    async def get_embedding(self, value):
        import numpy as np

        values = np.array(list(hashlib.sha256(value.encode()).digest()[:8]), dtype=float) + 1
        return (values / np.linalg.norm(values)).tolist()

    async def get_embeddings_batch(self, values, **kwargs):
        return [await self.get_embedding(value) for value in values]


class Reranker:
    def __init__(self):
        self.calls = 0

    async def rerank(self, query, documents):
        self.calls += 1
        return [
            types.SimpleNamespace(index=i, relevance_score=1 / (i + 1))
            for i in range(len(documents))
        ]


async def main():
    import astrbot.api  # noqa: F401 -- initialize the public API before core managers
    import lark_oapi
    from astrbot.core import db_helper
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.star_handler import star_handlers_registry

    package = types.ModuleType("knowledge_integration")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    module = importlib.import_module(package.__name__ + ".main")
    await db_helper.initialize()
    conversations = ConversationManager(db_helper)
    ep, rp = Embeddings(), Reranker()
    providers = types.SimpleNamespace(
        get_provider_by_id=AsyncMock(side_effect=lambda key: {"ep": ep, "rp": rp}.get(key))
    )
    kb_manager = KnowledgeBaseManager(providers)
    await kb_manager.initialize()
    await kb_manager.create_kb(
        "existing-template", embedding_provider_id="ep", rerank_provider_id="rp"
    )
    chat = types.SimpleNamespace(
        text_chat=AsyncMock(
            return_value=types.SimpleNamespace(
                completion_text='{"allow":true,"explicit_project":true}'
            )
        )
    )
    context = types.SimpleNamespace(
        kb_manager=kb_manager,
        conversation_manager=conversations,
        get_using_provider_async=AsyncMock(return_value=chat),
    )
    config = dict(enabled=True, group_ids=["oc_test"], template_kb="existing-template")
    plugin = module.SummaryWriteKnowledgeSkill(context, config)
    bot = lark_oapi.Client.builder().app_id("cli_test").app_secret("unused").build()

    def event(mid, actor="alice", parent="q1"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "bot"
        msg.group_id = "oc_test"
        msg.sender = MessageMember(user_id=actor, nickname=actor)
        msg.message_id = mid
        msg.message_str = "把已确认标准保存到 Idol Empire，仅适用于 AppLovin"
        msg.message = [Plain(msg.message_str)]
        msg.raw_message = types.SimpleNamespace(chat_id="oc_test", parent_id=parent)
        evt = LarkMessageEvent(
            msg.message_str, msg, PlatformMetadata("lark", "test", "instance"), "oc_test", bot
        )
        evt.send = AsyncMock(side_effect=AssertionError("No messages may be sent"))
        return evt

    # Empty lists enable all peers; populated lists restrict only their own scope.
    group_peer = types.SimpleNamespace(
        get_group_id=lambda: "other-group",
        get_sender_id=lambda: "other-user",
        get_platform_id=lambda: "instance",
    )
    private_peer = types.SimpleNamespace(
        get_group_id=lambda: "",
        get_sender_id=lambda: "other-user",
        get_platform_id=lambda: "instance",
    )
    assert not plugin.enabled(group_peer)
    config["group_ids"] = []
    assert plugin.enabled(group_peer) and plugin.enabled(private_peer)
    config["private_ids"] = ["allowed-user"]
    assert plugin.enabled(group_peer) and not plugin.enabled(private_peer)
    config["private_ids"] = ["other-user"]
    assert plugin.enabled(private_peer)
    config["platform_ids"] = ["different-instance"]
    assert not plugin.enabled(group_peer) and not plugin.enabled(private_peer)
    config["platform_ids"] = []
    config["enabled"] = False
    assert not plugin.enabled(group_peer) and not plugin.enabled(private_peer)
    config["enabled"] = True
    config["group_ids"] = ["oc_test"]
    config["private_ids"] = []

    evt = event("q1")
    cid = await conversations.new_conversation(evt.unified_msg_origin, evt.get_platform_id())
    owner = evt.unified_msg_origin
    await conversations.update_conversation(
        owner,
        cid,
        history=[
            {"role": "user", "content": "Idol Empire 在 AppLovin 的测试标准确认：先核对样本。"}
        ],
    )

    async def prepare(evt, target=cid):
        conv = await conversations.get_conversation(owner, target)
        evt.set_extra(
            module.BINDING,
            {
                "scope": module.scope_of(evt),
                "topic": types.SimpleNamespace(cid=target, owner=owner),
                "original_mid": evt.message_obj.message_id,
            },
        )
        req = ProviderRequest(prompt=evt.message_str, conversation=conv)
        await plugin.prepare(evt, req)
        assert module.SYSTEM.strip() in req.system_prompt
        result = json.loads(await plugin.knowledge_context(evt))
        assert result["topic"]["cid"] == target
        return req

    data = dict(
        project="Idol Empire",
        platform="AppLovin",
        kind="rule",
        title="测试标准",
        content="先核对样本。",
        description="测试前核对",
        reason="话题确认",
        create_project=True,
    )
    try:
        # The real registry validates docstring schemas and hook priority.
        for name in module.TOOLS:
            tool = llm_tools.get_func(name)
            assert tool is not None, name
        hook = next(
            h
            for h in star_handlers_registry
            if h.handler == module.SummaryWriteKnowledgeSkill.prepare
        )
        assert hook.extras_configs["priority"] == -20000
        await prepare(evt)
        saved = json.loads(await plugin.knowledge_save(evt, json.dumps(data, ensure_ascii=False)))
        assert saved["status"] == "saved", saved
        assert saved["version"] == 1
        project = plugin.store.project("Idol Empire")
        helper = await kb_manager.get_kb(project["kb_id"])
        assert helper.kb.embedding_provider_id == "ep" and helper.kb.rerank_provider_id == "rp"
        found = json.loads(await plugin.knowledge_search(evt, "Idol Empire", "AppLovin", "样本"))
        assert found["results"] and rp.calls > 0, found
        assert (
            json.loads(await plugin.knowledge_search(evt, "Idol Empire", "Meta", "样本"))["results"]
            == []
        )
        row1 = plugin.store.active(saved["record_id"])
        assert await helper.get_document(row1["doc_id"])
        # A second member updates the same topic, using an actual read and current version.
        second = event("q2", actor="bob")
        await prepare(second)
        read = json.loads(await plugin.knowledge_read(second, saved["record_id"], "AppLovin"))
        assert read["version"] == 1
        update = {
            **data,
            "record_id": saved["record_id"],
            "expected_version": 1,
            "content": "先核对样本，再检查归因窗口。",
        }
        updated = json.loads(
            await plugin.knowledge_save(second, json.dumps(update, ensure_ascii=False))
        )
        assert updated["status"] == "saved" and updated["version"] == 2, updated
        assert await helper.get_document(row1["doc_id"]) is None
        found = json.loads(await plugin.knowledge_search(second, "Idol Empire", "AppLovin", "归因"))
        assert found["results"] and all(r["version"] == 2 for r in found["results"])
        # Missing topic bindings fail even if an agent somehow calls hidden tools.
        invalid = event("bad")
        assert json.loads(await plugin.knowledge_context(invalid))["status"] == "needs_attention"
        # User's provider selection is preserved for independent validation.
        context.get_using_provider_async.assert_awaited_with(second.unified_msg_origin)
        # Output safeguard uses the real LLMResponse setter.
        false_receipt = LLMResponse(role="assistant", completion_text="已保存到知识库。")
        await plugin.protect_receipt(invalid, false_receipt)
        assert "尚不能确认" in false_receipt.completion_text
        # Writer permissions and malformed independent validation fail before mutation.
        config["writer_ids"] = ["administrator"]
        denied = json.loads(await plugin.knowledge_save(second, json.dumps(update)))
        assert denied["status"] == "needs_attention"
        config["writer_ids"] = []
        chat.text_chat.return_value = types.SimpleNamespace(completion_text="not JSON")
        failed_plan = {**data, "title": "invalid-review-must-not-save"}
        rejected = json.loads(await plugin.knowledge_save(second, json.dumps(failed_plan)))
        assert rejected["status"] == "needs_attention"
        assert not any(
            r["title"] == failed_plan["title"] for r in plugin.store.records(project["id"])
        )
        chat.text_chat.return_value = types.SimpleNamespace(
            completion_text='{"allow":true,"explicit_project":true}'
        )
        # Skill is a real scoped SKILL.md and can be read without global registration.
        skill_evt = event("q3")
        await prepare(skill_evt)
        skill = {
            **data,
            "kind": "skill",
            "title": "测试检查方法",
            "content": "## 触发\n测试前\n## 输入\n报表\n## 步骤\n核对样本与归因\n## 输出\n报告\n## 边界\n仅本项目 AppLovin",
        }
        skill_result = json.loads(
            await plugin.knowledge_save(skill_evt, json.dumps(skill, ensure_ascii=False))
        )
        assert skill_result["status"] == "saved", skill_result
        skill_read = json.loads(
            await plugin.knowledge_read(skill_evt, skill_result["record_id"], "AppLovin")
        )
        assert skill_read["content"].startswith("---\nname:")
        # Regression: core vector retrieval has a default SQL page of 100.
        large = await helper.upload_document(
            file_name="many-chunks.md",
            file_content=None,
            file_type="md",
            pre_chunked_text=[f"chunk-{i} 样本校验" for i in range(130)],
            tasks_limit=1,
        )
        assert await helper.get_chunk_count_by_doc_id(large.doc_id) == 130
        await plugin.service.backend.verify(helper, large.doc_id, "chunk-129")
        await helper.delete_document(large.doc_id)
        # Simulate process restart: reopen both manifest and FAISS, then retrieve.
        await plugin.terminate()
        await kb_manager.terminate()
        kb_manager = KnowledgeBaseManager(providers)
        await kb_manager.initialize()
        context.kb_manager = kb_manager
        plugin = module.SummaryWriteKnowledgeSkill(context, config)
        restarted = event("q4")
        await prepare(restarted)
        found = json.loads(
            await plugin.knowledge_search(restarted, "Idol Empire", "AppLovin", "归因")
        )
        assert found["results"], found
        print(
            "PASS: real AstrBot 4.28.0 tools + Lark + Markdown parsing + SQLite + FAISS; template model reuse, write/read/update, platform filtering, reranking, Skill loading, restart. No network/live data."
        )
    finally:
        await plugin.terminate()
        await kb_manager.terminate()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        root.cleanup()
