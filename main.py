"""Chat-facing tools for AstrBot 4.28.0; independent of card rendering."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .backend import AstrBotBackend
from .directory import ChoiceDirectory, normalize_config
from .models import KnowledgeError, Plan, TopicContext, text
from .native_skills import available_skills, read_text_resource, skill_root
from .prompts import REVIEW, SYSTEM
from .receipts import protect_knowledge_claims
from .service import Service, write_atomic
from .sources import SourceWindow
from .store import Store

BINDING = "quote_topics.binding.v1"
SNAPSHOT = "summary_knowledge.context.v1"
KNOWLEDGE_TOOLS = (
    "knowledge_context",
    "knowledge_search",
    "knowledge_read",
    "knowledge_save",
    "knowledge_source_read",
    "knowledge_source_select",
)
SKILL_TOOLS = ("native_skill_list", "native_skill_read")
TOOLS = KNOWLEDGE_TOOLS + SKILL_TOOLS


def scope_of(event):
    group = event.get_group_id()
    parts = [
        event.get_platform_id(),
        event.get_self_id(),
        "group" if group else "private",
        group or event.get_sender_id(),
    ]
    if not all(isinstance(v, str) and v for v in parts):
        raise KnowledgeError("消息缺少可靠会话身份。")
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


class SummaryWriteKnowledgeSkill(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        normalize_config(config)
        self.config = config
        try:
            shares = json.loads(config.get("project_shares_json", "{}"))
            if not isinstance(shares, dict) or any(
                not isinstance(k, str)
                or not isinstance(v, list)
                or any(not isinstance(i, str) for i in v)
                for k, v in shares.items()
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise KnowledgeError(
                "project_shares_json 必须是项目名到会话范围列表的 JSON 对象。"
            ) from None
        # Store derived options in a private copy; never persist extra schema fields.
        service_config = dict(config)
        service_config["project_shares"] = shares
        self.store = Store(StarTools.get_data_dir("astrbot_plugin_summary_write_knowledge_skill"))
        self.service = Service(self.store, AstrBotBackend(context, service_config), service_config)
        self.directory = ChoiceDirectory(context, config, self.store.root / "directory.json")
        self.closed = False
        self.active = set()

    async def initialize(self):
        await self.directory.start()

    def enabled(self, event):
        if self.closed or not self.config.get("enabled", False):
            return False
        platforms = self.config.get("platform_ids", [])
        if platforms and event.get_platform_id() not in platforms:
            return False
        group = event.get_group_id()
        allowed = self.config.get("group_ids" if group else "private_ids", [])
        peer = group or event.get_sender_id()
        return not allowed or peer in allowed

    def guard(self, event, write=False):
        from astrbot import __version__

        if __version__ not in {"4.28.0", "4.28.1"} or event.get_platform_name() != "lark":
            raise KnowledgeError("本插件仅验证 AstrBot 4.28.0 / 4.28.1 飞书内置 Agent。")
        if not self.enabled(event):
            raise KnowledgeError("当前会话未启用知识管理。")
        if (
            write
            and self.config.get("writer_ids")
            and event.get_sender_id() not in self.config["writer_ids"]
        ):
            raise KnowledgeError("你没有当前知识管理插件的写入权限。")
        binding = event.get_extra(BINDING)
        if not binding or binding.get("scope") != scope_of(event):
            raise KnowledgeError("无法确认引用话题，请启用引用续聊插件并引用已登记的话题。")
        topic = event.get_extra(SNAPSHOT)
        if (
            not topic
            or topic.cid != binding["topic"].cid
            or topic.owner != binding["topic"].owner
            or topic.scope != scope_of(event)
            or topic.actor != event.get_sender_id()
            or topic.message_id != str(binding.get("original_mid") or event.message_obj.message_id)
        ):
            raise KnowledgeError("话题上下文未准备好，已停止操作。")
        return topic

    @filter.on_llm_request(priority=-20000)
    async def prepare(self, event: AstrMessageEvent, req):
        self.directory.observe(event)
        event.set_extra("summary_knowledge.skill_ready", True)
        event.set_extra(
            "summary_knowledge.persona_id", getattr(req.conversation, "persona_id", None)
        )
        event.set_extra("summary_knowledge.window", None)
        event.set_extra(SNAPSHOT, None)
        event.set_extra("summary_knowledge.context_read", False)
        if not self.enabled(event):
            if req.func_tool:
                for name in TOOLS:
                    req.func_tool.remove_tool(name)
            return
        req.system_prompt += "\n原生技能可通过 native_skill_list 和 native_skill_read 读取当前允许的技能及配套文本，无需 Computer Use。这不提供脚本执行、安装或写入权限。使用技能前必须读完整 SKILL.md；分页读取须全部完成。"
        binding = event.get_extra(BINDING)
        if not binding or not req.conversation or req.conversation.cid != binding["topic"].cid:
            if req.func_tool:
                for name in KNOWLEDGE_TOOLS:
                    req.func_tool.remove_tool(name)
            req.system_prompt += "\n知识管理不可用：没有可信引用话题绑定；不要声称已保存知识。"
            return
        try:
            conversation = await self.context.conversation_manager.get_conversation(
                binding["topic"].owner, binding["topic"].cid
            )
            if conversation is None or conversation.user_id != binding["topic"].owner:
                raise KnowledgeError("原话题已删除或所有者不符。")
            history = json.loads(conversation.history or "[]")
            if not isinstance(history, list):
                raise KnowledgeError("话题历史格式不正确。")
            maximum = int(self.config.get("max_topic_chars", 100000))
            raw = event.message_obj.raw_message
            parent = (
                raw.get("parent_id", "") if isinstance(raw, dict) else getattr(raw, "parent_id", "")
            )
            topic = TopicContext(
                scope=scope_of(event),
                cid=conversation.cid,
                owner=conversation.user_id,
                message_id=str(binding.get("original_mid") or event.message_obj.message_id),
                actor=event.get_sender_id(),
                request=event.message_str,
                history=history,
                source_messages=self.store.messages(scope_of(event), conversation.cid),
                quoted_message_id=str(parent or ""),
            )
            if not topic.message_id or not topic.request:
                raise KnowledgeError("消息缺少写入来源。")
            self.store.observe(topic)
            event.set_extra(SNAPSHOT, topic)
            attachments = self.current_attachments(event)
            event.set_extra("summary_knowledge.window", SourceWindow(topic, maximum, attachments))
            event.set_extra("summary_knowledge.read_ids", set())
            req.system_prompt += "\n" + SYSTEM
            req.system_prompt += "\n当前话题项目目录（数据）：" + json.dumps(
                await self.service.context_catalog(topic), ensure_ascii=False
            )
        except (KnowledgeError, ValueError, TypeError) as exc:
            event.set_extra("summary_knowledge.prepare_error", str(exc))
            if req.func_tool:
                for name in KNOWLEDGE_TOOLS:
                    req.func_tool.remove_tool(name)
            req.system_prompt += "\n知识管理上下文不完整，请告知用户本次不能保存，原因：" + str(exc)

    async def review_with(self, provider, topic, plan, old, catalog):
        if provider is None:
            raise KnowledgeError("当前聊天模型不可用，未执行写入。")
        payload = {
            "topic": topic.to_dict(),
            "plan": plan.to_dict(),
            "catalog": catalog,
            "previous": json.loads(old["plan"]) if old else None,
        }
        result = await asyncio.wait_for(
            provider.text_chat(
                prompt=json.dumps(payload, ensure_ascii=False), system_prompt=REVIEW, contexts=[]
            ),
            timeout=int(self.config.get("review_timeout", 120)),
        )
        raw = result.completion_text.strip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        try:
            verdict = json.loads(raw)
        except (ValueError, TypeError):
            raise KnowledgeError("模型未返回有效校验结果，未执行写入。") from None
        if not isinstance(verdict, dict) or type(verdict.get("allow")) is not bool:
            raise KnowledgeError("模型校验结果不完整，未执行写入。")
        return verdict

    async def run_tool(self, event, action):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            result = await action()
            return json.dumps(result, ensure_ascii=False)
        except KnowledgeError as exc:
            return json.dumps(
                {"status": "needs_attention", "message": str(exc)}, ensure_ascii=False
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Knowledge operation failed (%s)", type(exc).__name__)
            return json.dumps(
                {
                    "status": "failed",
                    "message": "操作未完成，请重试；暂存内容不会作为有效知识返回。",
                },
                ensure_ascii=False,
            )
        finally:
            self.active.discard(task)

    def current_attachments(self, event):
        from astrbot.core.message.components import File
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        root = Path(get_astrbot_temp_path()).resolve()
        result, notices = [], []
        event.set_extra("summary_knowledge.attachment_notices", notices)
        for component in event.message_obj.message:
            if not isinstance(component, File) or Path(component.name or "").suffix.lower() not in {
                ".md",
                ".txt",
            }:
                continue
            if len(result) >= 10:
                notices.append("本次仅列出前 10 个文本附件，其余请分批发送。")
                break
            try:
                # Paths originate only from this event's adapter-downloaded attachments.
                path = Path(component.file_ or "")
                if not path.is_absolute() or not path.resolve().is_relative_to(root):
                    raise KnowledgeError("文本附件尚未下载到允许的临时目录，请重新附上或粘贴正文。")
                data = read_text_resource(root, str(path.relative_to(root)), 0, 32000)
                body = data["content"]
                while data["next_offset"] is not None:
                    more = read_text_resource(
                        root, str(path.relative_to(root)), data["next_offset"], 32000
                    )
                    if more["sha256"] != data["sha256"]:
                        raise KnowledgeError("附件在读取期间发生变化，请重新发送。")
                    data = more
                    body += data["content"]
                result.append({"name": component.name, "text": body, "file_sha256": data["sha256"]})
            except KnowledgeError as exc:
                notices.append(str(exc))
        return result

    def skill_guard(self, event):
        from astrbot import __version__

        if (
            not self.enabled(event)
            or event.get_platform_name() != "lark"
            or __version__ not in {"4.28.0", "4.28.1"}
            or not event.get_extra("summary_knowledge.skill_ready")
        ):
            raise KnowledgeError("当前会话未启用技能读取。")

    @filter.llm_tool(name="native_skill_list")
    async def native_skill_list(self, event: AstrMessageEvent):
        """列出当前人格和配置允许且已启用的本地原生技能；不安装、不执行。"""

        async def action():
            self.skill_guard(event)
            skills = await available_skills(self.context, event)
            return {
                "status": "ok",
                "skills": [{"name": s.name, "description": s.description} for s in skills],
            }

        return await self.run_tool(event, action)

    @filter.llm_tool(name="native_skill_read")
    async def native_skill_read(
        self, event: AstrMessageEvent, name: str, path: str = "SKILL.md", offset: int = 0
    ):
        """读取允许使用的原生技能全文或配套文本，长文件按 next_offset 继续；不执行脚本。

        Args:
            name(string): native_skill_list 返回的技能名。
            path(string): 技能内相对文本路径，默认 SKILL.md，不能传宿主绝对路径。
            offset(int): 字符起点，首次为 0，后续使用 next_offset。
        """

        async def action():
            self.skill_guard(event)
            skills = [s for s in await available_skills(self.context, event) if s.name == name]
            if len(skills) != 1:
                raise KnowledgeError("技能不存在、未启用或当前人格无权使用。")
            root = skill_root(skills[0])
            result = read_text_resource(root, path, offset)
            return {"status": "ok", "name": name, **result}

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_context")
    async def knowledge_context(self, event: AstrMessageEvent, offset: int = 0):
        """获取项目目录和来源目录；长话题需分页读取来源并明确选择整理范围，再保存。

        Args:
            offset(int): 来源目录分页起点，首次为 0，后续使用 next_offset。
        """

        async def action():
            topic = self.guard(event)
            window = event.get_extra("summary_knowledge.window")
            event.set_extra("summary_knowledge.context_read", True)
            shown = window.selected or replace(
                topic,
                history=[],
                source_messages=[],
                coverage="尚未选取来源，不能保存；请读取目录中的片段或本条消息附件。",
            )
            return {
                "topic": shown.to_dict(),
                "source_directory": window.directory(offset),
                "attachment_notices": event.get_extra("summary_knowledge.attachment_notices", []),
                **(await self.service.context_catalog(topic)),
            }

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_source_read")
    async def knowledge_source_read(self, event: AstrMessageEvent, source_id: str, offset: int = 0):
        """分页读取当前话题消息或当前消息已下载的 Markdown/TXT 附件，不接受任意文件路径。

        Args:
            source_id(string): knowledge_context 来源目录中的 ID。
            offset(int): 字符起点，首次为 0，后续使用 next_offset。
        """

        async def action():
            self.guard(event)
            return event.get_extra("summary_knowledge.window").read(source_id, offset)

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_source_select")
    async def knowledge_source_select(
        self, event: AstrMessageEvent, snapshot: str, ranges_json: str, reason: str
    ):
        """将已读取片段明确选为本次保存依据；范围要符合用户请求，不得跳过反对意见冒充最终结论。

        Args:
            snapshot(string): knowledge_context 返回的快照指纹。
            ranges_json(string): JSON 数组，每项为来源 id 和字符 start/end，end 不包含自身。
            reason(string): 选择依据及与用户要求的对应关系，不清楚时询问用户。
        """

        async def action():
            self.guard(event, write=True)
            if len(ranges_json) > 20000:
                raise KnowledgeError("选取参数过长。")
            try:
                ranges = json.loads(ranges_json)
            except ValueError:
                raise KnowledgeError("来源范围不是有效 JSON。") from None
            selected = event.get_extra("summary_knowledge.window").select(snapshot, ranges, reason)
            return {
                "status": "selected",
                "coverage": selected.coverage,
                "selection": selected.selection,
            }

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_search")
    async def knowledge_search(
        self, event: AstrMessageEvent, project: str, platform: str, query: str
    ):
        """搜索指定项目和平台的有效知识，不返回失效版本。未知项目或平台请先询问。

        Args:
            project(string): 明确的项目名称。
            platform(string): 明确的平台，例如 AppLovin；不限平台资料用 all。
            query(string): 简短的相关知识查询。
        """

        async def action():
            topic = self.guard(event)
            return {
                "status": "ok",
                "results": await self.service.search(
                    topic,
                    text(project, "project"),
                    text(platform, "platform"),
                    text(query, "query", 1000),
                ),
            }

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_read")
    async def knowledge_read(self, event: AstrMessageEvent, record_id: str, platform: str):
        """读取当前有效知识或项目 Skill 全文；更新前必须读取，Skill 读取后可按其步骤完成当前任务。

        Args:
            record_id(string): 目录或检索返回的知识编号。
            platform(string): 当前任务适用的平台。
        """

        async def action():
            topic = self.guard(event)
            result = await self.service.read(topic, record_id, platform)
            event.get_extra("summary_knowledge.read_ids").add(record_id)
            return result

        return await self.run_tool(event, action)

    @filter.llm_tool(name="knowledge_save")
    async def knowledge_save(self, event: AstrMessageEvent, plan_json: str):
        """用户明确要求长期保存时，校验并写入一个知识/规则/项目 Skill，更新索引并验证；只有 status=saved 才能回复已保存。

        Args:
            plan_json(string): 按系统提示规定的 JSON 保存计划。更新带原编号和版本，新项目需明确创建意图。
        """

        event.set_extra("summary_knowledge.write_attempted", True)

        async def action():
            topic = self.guard(event, write=True)
            if not event.get_extra("summary_knowledge.context_read"):
                raise KnowledgeError("请先调用 knowledge_context 读取话题与目录，再整理保存。")
            if len(plan_json) > 40000:
                raise KnowledgeError("单条知识过长，请拆分。")
            try:
                plan = Plan.parse(json.loads(plan_json))
            except ValueError:
                raise KnowledgeError("plan_json 不是有效 JSON。") from None
            if plan.record_id and plan.record_id not in event.get_extra(
                "summary_knowledge.read_ids", set()
            ):
                raise KnowledgeError("修改前必须调用 knowledge_read 读取当前全文。")
            window = event.get_extra("summary_knowledge.window")
            topic = window.selected
            if topic is None:
                raise KnowledgeError(
                    "本次尚未选择来源。请读取所需消息或附件，再调用 knowledge_source_select；不会静默截断整个话题。"
                )
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)

            async def review(topic, plan, old, catalog):
                result = await self.review_with(provider, topic, plan, old, catalog)
                self.guard(event, write=True)
                if window.selected is not topic:
                    raise KnowledgeError("校验期间来源范围发生变化，请重新保存。")
                if result.get("allow") is True and topic.selection:
                    write_atomic(
                        self.store.root / "source-snapshots" / f"{window.snapshot}.json",
                        json.dumps(
                            {"topic": window.topic.to_dict(), "sources": window.sources},
                            ensure_ascii=False,
                        ),
                    )
                return result

            result = await self.service.save(topic, plan, review)
            receipts = event.get_extra("summary_knowledge.receipts", [])
            receipts.append(result)
            event.set_extra("summary_knowledge.receipts", receipts)
            return result

        return await self.run_tool(event, action)

    @filter.on_llm_response(priority=-20000)
    async def protect_receipt(self, event: AstrMessageEvent, response):
        if self.enabled(event) and not event.get_extra("summary_knowledge.receipts"):
            response.completion_text = protect_knowledge_claims(
                response.completion_text or "",
                write_attempted=bool(event.get_extra("summary_knowledge.write_attempted")),
            )

    @filter.command("知识管理状态")
    async def status(self, event: AstrMessageEvent):
        state = "已启用" if self.enabled(event) else "未启用"
        yield event.plain_result(
            f"知识管理：{state}。\n当前会话范围：{scope_of(event)}\n需要引用续聊和可用知识库（范围留空表示全部）；仅实际写入与索引验证通过才确认保存。"
        )

    async def terminate(self):
        self.closed = True
        await self.directory.close()
        tasks = [t for t in self.active if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
