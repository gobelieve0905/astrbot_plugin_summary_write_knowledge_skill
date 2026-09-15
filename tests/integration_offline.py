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
    config = dict(enabled=True, group_ids=["oc_test"], template_kb="")
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
        report = LLMResponse(
            role="assistant", completion_text="报表已保存为 JSON。尚未查询全部账户。"
        )
        await plugin.protect_receipt(invalid, report)
        assert report.completion_text == "报表已保存为 JSON。尚未查询全部账户。"
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
        # Blank scope includes all existing libraries and writes to the chosen real library.
        existing = await kb_manager.create_kb(
            "Existing project", embedding_provider_id="ep", rerank_provider_id="rp"
        )
        original = await existing.upload_document(
            file_name="original.md", file_content=b"original material", file_type="md"
        )
        existing_evt = event("existing-project-save")
        await prepare(existing_evt)
        library_catalog = json.loads(await plugin.knowledge_context(existing_evt))
        assert {"existing-template", "Idol Empire", "Existing project"}.issubset(
            {kb["name"] for kb in library_catalog["knowledge_bases"]}
        )
        existing_plan = {
            **data,
            "project": "Existing project",
            "knowledge_base": existing.kb.kb_id,
            "create_project": False,
            "title": "新增结论",
        }
        existing_result = json.loads(
            await plugin.knowledge_save(existing_evt, json.dumps(existing_plan))
        )
        assert existing_result["status"] == "saved", existing_result
        assert plugin.store.project("Existing project")["kb_id"] == existing.kb.kb_id
        assert await existing.get_document(original.doc_id) is not None
        assert (
            await existing.get_document(plugin.store.active(existing_result["record_id"])["doc_id"])
            is not None
        )
        # Changing the allowlist applies to catalog, reads and searches for existing bindings.
        plugin.service.backend.config["template_kb"] = "Idol Empire"
        restricted = json.loads(await plugin.knowledge_context(existing_evt))
        assert [kb["name"] for kb in restricted["knowledge_bases"]] == ["Idol Empire"]
        assert "Existing project" not in {p["name"] for p in restricted["projects"]}
        denied_read = json.loads(
            await plugin.knowledge_read(existing_evt, existing_result["record_id"], "AppLovin")
        )
        assert denied_read["status"] == "needs_attention", denied_read
        denied_search = json.loads(
            await plugin.knowledge_search(existing_evt, "Existing project", "AppLovin", "结论")
        )
        assert denied_search["status"] == "needs_attention", denied_search
        plugin.service.backend.config["template_kb"] = ""
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
        # Long topics remain usable: no implicit truncation and no review before selection.
        long_evt = event("long-topic")
        long_cid = await conversations.new_conversation(owner, long_evt.get_platform_id())
        await conversations.update_conversation(
            owner,
            long_cid,
            history=[
                {"role": "tool", "content": "synthetic-tool-result " * 18000},
                {"role": "user", "content": "确认 Idol Empire 的 AppLovin 标准：先核对样本。"},
            ],
        )
        await prepare(long_evt, long_cid)
        overview = json.loads(await plugin.knowledge_context(long_evt))
        assert overview["source_directory"]["selection_required"]
        assert not overview["topic"]["history"]
        plan_long = {**data, "title": "长话题选定标准"}
        calls = chat.text_chat.await_count
        blocked = json.loads(await plugin.knowledge_save(long_evt, json.dumps(plan_long)))
        assert blocked["status"] == "needs_attention"
        assert chat.text_chat.await_count == calls
        fragment = json.loads(await plugin.knowledge_source_read(long_evt, "h:1"))
        spans = json.dumps([{"id": "h:1", "start": 0, "end": fragment["end"]}])
        selection = json.loads(
            await plugin.knowledge_source_select(
                long_evt,
                overview["source_directory"]["snapshot"],
                spans,
                "用户仅要求保存已确认标准",
            )
        )
        assert selection["status"] == "selected", selection
        result = json.loads(await plugin.knowledge_save(long_evt, json.dumps(plan_long)))
        assert result["status"] == "saved", result
        reviewed = json.loads(chat.text_chat.call_args.kwargs["prompt"])
        assert "synthetic-tool-result" not in json.dumps(reviewed)
        assert reviewed["topic"]["request"] == long_evt.message_str
        snap = (
            plugin.store.root
            / "source-snapshots"
            / (overview["source_directory"]["snapshot"] + ".json")
        )
        assert "synthetic-tool-result" in snap.read_text()
        row = plugin.store.active(result["record_id"])
        assert "synthetic-tool-result" not in plugin.service.checked_body(row)
        assert json.loads(row["source"])["selection"]["ranges"][0]["id"] == "h:1"

        # A newly attached file can be selected even when the original topic is huge.
        from astrbot.core.message.components import File
        from astrbot.core.utils.astrbot_path import get_astrbot_skills_path, get_astrbot_temp_path

        temp_path = Path(get_astrbot_temp_path())
        temp_path.mkdir(parents=True, exist_ok=True)
        attached = temp_path / "project.md"
        attached.write_text("Idol Empire / AppLovin：先核对样本。")
        file_evt = event("file-source")
        file_evt.message_obj.message.append(File(name="project.md", file=str(attached)))
        await prepare(file_evt, long_cid)
        overview = json.loads(await plugin.knowledge_context(file_evt))
        assert any(x["id"] == "a:0" for x in overview["source_directory"]["sources"])
        fragment = json.loads(await plugin.knowledge_source_read(file_evt, "a:0"))
        assert fragment["text"] == attached.read_text()

        # Real native SkillManager, persona filtering and enabled-state checks without CUA.
        skills_path = Path(get_astrbot_skills_path())
        skill_dir = skills_path / "fixture-reader"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fixture-reader\ndescription: offline fixture\n---\nRead the task inputs first."
        )
        settings = {"provider_settings": {"computer_use_runtime": "none"}}
        context.get_config = lambda **kwargs: settings
        context.persona_manager = types.SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("fixture", {"skills": ["fixture-reader"]}, None, False)
            )
        )
        skill_evt = event("skill-without-topic")
        await plugin.prepare(skill_evt, ProviderRequest(prompt="读取技能"))
        skill_list = json.loads(await plugin.native_skill_list(skill_evt))
        assert [x["name"] for x in skill_list["skills"]] == ["fixture-reader"], skill_list
        skill_text = json.loads(await plugin.native_skill_read(skill_evt, "fixture-reader"))
        assert skill_text["complete"] and "Read the task" in skill_text["content"], skill_text
        context.persona_manager.resolve_selected_persona.return_value = (
            "fixture",
            {"skills": []},
            None,
            False,
        )
        denied = json.loads(await plugin.native_skill_read(skill_evt, "fixture-reader"))
        assert denied["status"] == "needs_attention", denied
        context.persona_manager.resolve_selected_persona.return_value = (
            "fixture",
            {"skills": ["fixture-reader"]},
            None,
            False,
        )
        from astrbot.core.skills import SkillManager

        manager = SkillManager()
        cfg_path = Path(manager.config_path)
        skill_cfg = json.loads(cfg_path.read_text())
        skill_cfg["skills"]["fixture-reader"]["active"] = False
        cfg_path.write_text(json.dumps(skill_cfg))
        denied = json.loads(await plugin.native_skill_read(skill_evt, "fixture-reader"))
        assert denied["status"] == "needs_attention", denied

        # Built-in skill-creator is read through the same real tool (no fixture content).
        from astrbot.core.star.star import star_registry

        builtin = types.SimpleNamespace(
            root_dir_name="astrbot", name="astrbot", activated=True, reserved=True
        )
        star_registry.append(builtin)
        try:
            context.persona_manager.resolve_selected_persona.return_value = (
                "fixture",
                {"skills": ["skill-creator"]},
                None,
                False,
            )
            actual = json.loads(await plugin.native_skill_read(skill_evt, "skill-creator"))
            assert actual["status"] == "ok" and "skill" in actual["content"].lower(), actual
            assert actual["total_chars"] > 1000
            context.persona_manager.resolve_selected_persona.return_value = (
                "fixture",
                {"skills": ["skill-creator", "meta-query"]},
                None,
                False,
            )
            install_evt = event("native-install")
            install_evt.role = "member"
            await prepare(install_evt)
            page = json.loads(await plugin.native_skill_read(install_evt, "skill-creator"))
            while page["next_offset"] is not None:
                page = json.loads(
                    await plugin.native_skill_read(
                        install_evt, "skill-creator", offset=page["next_offset"]
                    )
                )
            body = "---\nname: meta-query\ndescription: Meta 通用查询流程\n---\n确认项目后用现有查询工具取数并核对分页，只读。\n"
            installed = json.loads(
                await plugin.native_skill_install(install_evt, "meta-query", body, "用户要求安装")
            )
            assert installed["status"] == "installed", installed
            # Team installation does not expose the file in the global native directory.
            assert not any(
                x.name == "meta-query" for x in manager.list_skills(show_sandbox_path=False)
            )
            saved_response = LLMResponse(role="assistant", completion_text="技能已启用。")
            await plugin.protect_receipt(install_evt, saved_response)
            assert saved_response.completion_text == "技能已启用。"
            readback = json.loads(await plugin.native_skill_read(install_evt, installed["id"]))
            assert readback["content"] == body, readback
            updated = json.loads(
                await plugin.native_skill_install(
                    install_evt,
                    "meta-query",
                    body + "核对时区。",
                    "更新规范",
                    readback["sha256"],
                    skill_id=installed["id"],
                    expected_version=1,
                )
            )
            assert updated["status"] == "installed", updated
            chat.text_chat.return_value = types.SimpleNamespace(
                completion_text='{"allow":false,"question":"未明确共享"}'
            )
            rejected = json.loads(
                await plugin.native_skill_install(
                    install_evt,
                    "another-skill",
                    body.replace("meta-query", "another-skill"),
                    "安装",
                )
            )
            assert rejected["status"] == "needs_attention"
            assert not any(
                r["name"] == "another-skill"
                for r in plugin.team_skills.catalog(plugin.team_identity(install_evt), [])
            )
            chat.text_chat.return_value = types.SimpleNamespace(
                completion_text='{"allow":true,"explicit_project":true}'
            )
            install_evt.role = "member"
            denied = json.loads(
                await plugin.native_skill_install(install_evt, "meta-query", body, "安装")
            )
            assert denied["status"] == "needs_attention", denied

            # One part can fail while the other succeeds; resume never reinstalls Skill.
            paired = {
                "knowledge": {**data, "title": "组合任务知识", "content": "已确认的独立结论"},
                "skill": {
                    "name": "paired-skill",
                    "content": body.replace("meta-query", "paired-skill"),
                    "reason": "保存方法与项目资料",
                },
            }
            chat.text_chat.side_effect = [
                types.SimpleNamespace(completion_text='{"allow":false,"question":"补齐资料"}'),
                types.SimpleNamespace(completion_text='{"allow":true,"explicit_project":true}'),
            ]
            partial = json.loads(await plugin.knowledge_delivery(install_evt, json.dumps(paired)))
            chat.text_chat.side_effect = None
            assert partial["status"] == "partial", partial
            assert partial["parts"]["skill"]["status"] == "installed", partial
            count = len(plugin.team_skills.catalog(plugin.team_identity(install_evt), []))
            complete = json.loads(
                await plugin.knowledge_delivery(install_evt, delivery_id=partial["delivery_id"])
            )
            assert complete["status"] == "complete", complete
            assert len(plugin.team_skills.catalog(plugin.team_identity(install_evt), [])) == count

        finally:
            star_registry.remove(builtin)
        # An unavailable attachment is reported without disabling all history tools.
        missing_evt = event("missing-file")
        missing_evt.message_obj.message.append(
            File(name="missing.md", file=str(temp_path / "missing.md"))
        )
        await prepare(missing_evt, long_cid)
        missing = json.loads(await plugin.knowledge_context(missing_evt))
        assert missing["attachment_notices"]
        assert missing["source_directory"]["selection_required"]

        # Adapter-downloaded files nested in the genuine Reply are usable directly.
        from astrbot.core.message.components import Reply

        quoted_evt = event("quoted-file-request")
        quoted_path = temp_path / "quoted.md"
        quoted_path.write_text("引用文件全文", encoding="utf-8")
        quoted_evt.message_obj.raw_message = {"parent_id": "old-file-mid"}
        quoted_evt.message_obj.message.append(
            Reply(id="old-file-mid", chain=[File(name="quoted.md", file=str(quoted_path))])
        )
        await prepare(quoted_evt)
        quoted_files = plugin.team_skills.files(quoted_evt.get_extra(module.SNAPSHOT))
        assert any(
            f["name"] == "quoted.md" and f["source_message_id"] == "old-file-mid"
            for f in quoted_files
        )

        # Import an existing task artifact through its owner's checked API.
        from dataclasses import replace

        original = missing_evt.get_extra(module.SNAPSHOT)
        imported_topic = replace(original, request=original.request + " 导入任务 fixture-job")
        missing_evt.set_extra(module.SNAPSHOT, imported_topic)
        result_file = temp_path / "generated.md"
        result_file.write_text("已确认的任务结果全文", encoding="utf-8")

        def artifact(job_id, name, owner):
            assert job_id == "fixture-job" and name == "generated.md" and len(owner) == 64
            return result_file

        context.get_registered_star = lambda name: types.SimpleNamespace(
            activated=True,
            star_cls=types.SimpleNamespace(
                closed=False,
                config={"enabled": True},
                jobs=types.SimpleNamespace(artifact=artifact),
            ),
        )
        from mcp.types import CallToolResult, TextContent

        code_plugin = types.SimpleNamespace(
            closed=False,
            config={"enabled": True},
            jobs=types.SimpleNamespace(
                artifact=artifact,
                get=lambda job, owner: {"state": "succeeded", "files": [{"name": "generated.md"}]},
            ),
        )
        context.get_registered_star = lambda name: types.SimpleNamespace(
            activated=True, star_cls=code_plugin
        )
        await plugin.register_artifact(
            missing_evt,
            types.SimpleNamespace(name="code_start", plugin=code_plugin),
            {},
            CallToolResult(
                content=[TextContent(type="text", text='{"ok":true,"id":"fixture-job"}')]
            ),
        )
        assert plugin.deliveries.file(imported_topic, "fixture-job", "generated.md")
        # Original executor may now be unavailable: registered bytes still resolve.
        context.get_registered_star = lambda name: None
        imported = json.loads(
            await plugin.knowledge_artifact_import(missing_evt, "fixture-job", "generated.md")
        )
        assert imported["status"] == "imported", imported
        assert not missing_evt.get_extra("summary_knowledge.context_read")
        assert any(
            f["text"] == "已确认的任务结果全文" for f in plugin.team_skills.files(imported_topic)
        )

        print(
            "PASS: real AstrBot tools + Lark + SQLite + FAISS; long-topic selection/save, attachment sources, member team-skill install/update, review refusal, native isolation, private artifact registration/cache, quoted files, partial delivery/resume, persona guards, versioned writes and restart. No network/live data."
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
