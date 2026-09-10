"""Chat-facing tools for AstrBot 4.28.0; independent of card rendering."""

from __future__ import annotations

import asyncio
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .backend import AstrBotBackend
from .models import KnowledgeError, Plan, TopicContext, text
from .prompts import REVIEW, SYSTEM
from .service import Service
from .store import Store

BINDING = "quote_topics.binding.v1"
SNAPSHOT = "summary_knowledge.context.v1"
TOOLS = ("knowledge_context", "knowledge_search", "knowledge_read", "knowledge_save")


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
        self.closed = False
        self.active = set()

    def enabled(self, event):
        if self.closed or not self.config.get("enabled", False):
            return False
        platforms = self.config.get("platform_ids", [])
        if platforms and event.get_platform_id() not in platforms:
            return False
        group = event.get_group_id()
        return (
            group in self.config.get("group_ids", [])
            if group
            else event.get_sender_id() in self.config.get("private_ids", [])
        )

    def guard(self, event, write=False):
        from astrbot import __version__

        if __version__ != "4.28.0" or event.get_platform_name() != "lark":
            raise KnowledgeError("本插件仅验证 AstrBot 4.28.0 飞书内置 Agent。")
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
        event.set_extra(SNAPSHOT, None)
        event.set_extra("summary_knowledge.context_read", False)
        if not self.enabled(event):
            if req.func_tool:
                for name in TOOLS:
                    req.func_tool.remove_tool(name)
            return
        binding = event.get_extra(BINDING)
        if not binding or not req.conversation or req.conversation.cid != binding["topic"].cid:
            if req.func_tool:
                for name in TOOLS:
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
            if len(json.dumps(history, ensure_ascii=False)) > maximum:
                raise KnowledgeError("话题历史超过整理上限，请拆分明确范围；本次不会静默截断保存。")
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
            event.set_extra("summary_knowledge.read_ids", set())
            req.system_prompt += "\n" + SYSTEM
            req.system_prompt += "\n当前话题项目目录（数据）：" + json.dumps(
                self.service.catalog(topic), ensure_ascii=False
            )
        except (KnowledgeError, ValueError, TypeError) as exc:
            event.set_extra("summary_knowledge.prepare_error", str(exc))
            if req.func_tool:
                for name in TOOLS:
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

    @filter.llm_tool(name="knowledge_context")
    async def knowledge_context(self, event: AstrMessageEvent):
        """获取当前引用话题的完整已登记历史、来源限制、项目目录和当前版本；整理保存前必须调用。"""

        async def action():
            topic = self.guard(event)
            event.set_extra("summary_knowledge.context_read", True)
            return {"topic": topic.to_dict(), **self.service.catalog(topic)}

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
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)

            async def review(topic, plan, old, catalog):
                result = await self.review_with(provider, topic, plan, old, catalog)
                self.guard(event, write=True)
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
            completion = response.completion_text or ""
            if re.search(r"(?<![未不])已(?:保存|入库|写入知识库)|技能已启用", completion):
                response.completion_text = "本次没有获得成功的知识写入回执，因此尚不能确认已保存。请查看操作结果，明确项目或重试。"

    @filter.command("知识管理状态")
    async def status(self, event: AstrMessageEvent):
        state = "已启用" if self.enabled(event) else "未启用"
        yield event.plain_result(
            f"知识管理：{state}。\n当前会话范围：{scope_of(event)}\n需要引用续聊和可用的 template_kb；仅实际写入与索引验证通过才确认保存。"
        )

    async def terminate(self):
        self.closed = True
        tasks = [t for t in self.active if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
