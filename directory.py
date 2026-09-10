"""Live ID/name choices for native AstrBot list+options configuration controls."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .models import KnowledgeError

FIELDS = ("platform_ids", "group_ids", "private_ids", "writer_ids", "template_kb")
BASE_HINTS = {
    "platform_ids": "留空表示全部平台实例；选择后仅启用指定实例。",
    "group_ids": "留空表示全部群聊；选择后仅启用指定群聊。候选来自机器人可见群及已知群记录。",
    "private_ids": "留空表示全部私聊；选择后仅启用指定用户的私聊。候选来自可见群成员和已知用户，不是企业全部通讯录。",
    "writer_ids": "留空表示已启用会话内的全部成员可写入；选择后仅允许指定用户。",
    "template_kb": "留空表示全部知识库；选择后仅使用指定库。按话题选择目标库，项目共享权限不变。",
}


def selections(raw, legacy_text=False):
    if legacy_text and isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise KnowledgeError("范围必须为 ID 多选列表；旧知识库名称文本可自动转换。")
    return list(dict.fromkeys(v.strip() for v in raw if v.strip()))


def normalize_config(config):
    for field in FIELDS:
        config[field] = selections(config.get(field, []), legacy_text=field == "template_kb")


class ChoiceDirectory:
    """Updates only this plugin's in-memory config schema; no frontend/core patch.

    Automatic refresh is bounded and cancellable. Identity metadata, never messages or
    API credentials, is cached. Missing permissions keep previously known/selected IDs.
    """

    def __init__(self, context, config, cache_path: Path):
        self.context = context
        self.config = config
        self.path = cache_path
        self.known = {"groups": {}, "users": {}}
        self.platforms = {}
        self.libraries = {}
        self.issues = []
        self.task = None
        self.closed = False
        self.lock = asyncio.Lock()
        self.read_cache()
        self.publish()

    def read_cache(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for kind in self.known:
                for ident, item in raw.get(kind, {}).items():
                    if (
                        isinstance(ident, str)
                        and isinstance(item, dict)
                        and isinstance(item.get("name"), str)
                        and isinstance(item.get("seen"), (float, int))
                        and time.time() - item["seen"] < 90 * 86400
                    ):
                        self.known[kind][ident] = item
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def write_cache(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.known, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def remember(self, kind, ident, name):
        if not isinstance(ident, str) or not ident.strip():
            return
        ident = ident.strip()
        name = " ".join(str(name or ident).split())[:120]
        previous = self.known[kind].get(ident)
        if previous and name == ident:
            name = previous["name"]
        self.known[kind][ident] = {"name": name, "seen": time.time()}
        if len(self.known[kind]) > 10000:
            oldest = min(self.known[kind], key=lambda k: self.known[kind][k]["seen"])
            del self.known[kind][oldest]

    def observe(self, event):
        if self.closed or event.get_platform_name() != "lark":
            return
        self.remember("users", event.get_sender_id(), event.get_sender_name())
        if event.get_group_id():
            self.remember("groups", event.get_group_id(), event.get_group_id())
        self.publish()

    def platform_instances(self):
        manager = getattr(self.context, "platform_manager", None)
        return list(getattr(manager, "platform_insts", []))

    async def refresh_local(self):
        self.platforms = {}
        for platform in self.platform_instances():
            meta = platform.meta()
            if meta.id:
                self.platforms[meta.id] = (
                    getattr(platform, "bot_name", "") or getattr(meta, "name", "") or meta.id
                )
            if getattr(meta, "name", "") == "lark":
                # The adapter already resolves names for incoming senders. Read only
                # these identity pairs; no credentials or message history is accessed.
                for ident, cached in list(getattr(platform, "_user_name_cache", {}).items()):
                    if isinstance(cached, (tuple, list)) and cached:
                        self.remember("users", ident, cached[0])
        try:
            libraries = await self.context.kb_manager.list_kbs()
            self.libraries = {kb.kb_id: kb.kb_name for kb in libraries}
            # Legacy name selections become IDs only with an unambiguous exact match.
            current = self.config.get("template_kb", [])
            resolved = []
            for value in current:
                matches = [ident for ident, name in self.libraries.items() if name == value]
                resolved.append(
                    matches[0] if value not in self.libraries and len(matches) == 1 else value
                )
            current[:] = list(dict.fromkeys(resolved))
        except Exception:
            self.issues.append("知识库列表暂不可用，保留已有选择")
        self.publish()

    async def pages(self, call, make_request):
        token = ""
        seen = set()
        for _ in range(20):
            response = await asyncio.wait_for(call(make_request(token)), timeout=6)
            if not response.success() or response.data is None:
                raise KnowledgeError("飞书目录读取失败")
            for item in response.data.items or []:
                yield item
            if getattr(response.data, "trigger_security_conf_limit", False):
                raise KnowledgeError("飞书成员可见范围受限")
            if not response.data.has_more:
                return
            token = response.data.page_token
            if not token or token in seen:
                raise KnowledgeError("飞书目录分页不完整")
            seen.add(token)
        raise KnowledgeError("飞书目录超过本次分页上限")

    async def refresh_remote(self):
        from lark_oapi.api.im.v1 import GetChatMembersRequest, ListChatRequest

        for platform in self.platform_instances():
            if platform.meta().name != "lark":
                continue
            api = getattr(platform, "lark_api", None)
            if api is None:
                continue
            groups = []
            try:
                async for chat in self.pages(
                    api.im.v1.chat.alist,
                    lambda token: (
                        ListChatRequest.builder()
                        .user_id_type("open_id")
                        .page_size(100)
                        .page_token(token)
                        .build()
                    ),
                ):
                    if getattr(chat, "chat_mode", None) == "p2p":
                        if getattr(chat, "p2p_target_type", None) == "user":
                            self.remember("users", chat.p2p_target_id, chat.name)
                    else:
                        self.remember("groups", chat.chat_id, chat.name)
                        groups.append(chat.chat_id)
            except Exception:
                self.issues.append("飞书群列表未完整读取，请检查应用的群信息读取权限或网络")
            for group in groups:
                try:
                    async for member in self.pages(
                        api.im.v1.chat_members.aget,
                        lambda token, gid=group: (
                            GetChatMembersRequest.builder()
                            .chat_id(gid)
                            .member_id_type("open_id")
                            .page_size(100)
                            .page_token(token)
                            .build()
                        ),
                    ):
                        if getattr(member, "member_id_type", "open_id") == "open_id":
                            self.remember("users", member.member_id, member.name)
                except Exception:
                    self.issues.append("部分群成员未读取，请检查成员读取权限；已知用户仍可选择")

    def publish(self):
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict):
            return
        maps = {
            "platform_ids": self.platforms,
            "group_ids": {k: v["name"] for k, v in self.known["groups"].items()},
            "private_ids": {k: v["name"] for k, v in self.known["users"].items()},
            "writer_ids": {k: v["name"] for k, v in self.known["users"].items()},
            "template_kb": self.libraries,
        }
        for field, choices in maps.items():
            if field not in schema:
                continue
            options = dict(choices)
            for selected in self.config.get(field, []):
                options.setdefault(selected, "已配置（暂未发现名称）")
            ordered = sorted(options, key=lambda ident: (options[ident].casefold(), ident))
            # Replace the whole field together so options and labels remain paired.
            node = {
                **schema[field],
                "type": "list",
                "default": [],
                "options": ordered,
                "labels": [
                    f"{options[ident]} · {ident}" if options[ident] != ident else ident
                    for ident in ordered
                ],
            }
            status = " 选择名称即可，实际保存 ID；候选每 5 分钟刷新，重新打开配置页查看。"
            if not choices:
                status += " 暂无已发现候选，空选择仍表示全部启用。"
            if self.issues and field in {"group_ids", "private_ids", "writer_ids"}:
                status += " 部分目录暂不可读取，当前展示已知及已选项。"
            node["hint"] = BASE_HINTS[field] + status
            schema[field] = node

    async def refresh(self, remote=True):
        async with self.lock:
            self.issues = []
            await self.refresh_local()
            if remote:
                try:
                    await asyncio.wait_for(self.refresh_remote(), timeout=45)
                except TimeoutError:
                    self.issues.append("目录刷新超时，保留已读取候选")
                except Exception:
                    self.issues.append("目录刷新暂不可用，保留已有候选")
            self.publish()
            try:
                self.write_cache()
            except OSError:
                self.issues.append("候选缓存写入失败")

    async def start(self):
        await self.refresh_local()
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while not self.closed:
            try:
                await self.refresh()
            except Exception:
                self.issues = ["目录刷新暂不可用，保留已有候选"]
                self.publish()
            await asyncio.sleep(300)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
