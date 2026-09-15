"""Validated domain objects; no AstrBot imports or executable generated content."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone


class KnowledgeError(Exception):
    """A user-actionable failure; never include provider exceptions or secrets."""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def text(value, field, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise KnowledgeError(f"{field}不能为空且不能超过 {limit} 字符。")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise KnowledgeError(f"{field}包含无效控制字符。")
    return value.strip()


@dataclass(frozen=True)
class TopicContext:
    scope: str
    cid: str
    owner: str
    message_id: str
    actor: str
    request: str
    history: list
    source_messages: list
    quoted_message_id: str = ""
    coverage: str = "仅 AstrBot 已登记的话题历史和当前消息；不保证包含未触发机器人的群聊发言。"

    selection: dict = dataclass_field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Plan:
    project: str
    platform: str
    kind: str
    title: str
    content: str
    description: str
    reason: str
    record_id: str = ""
    expected_version: int = 0
    create_project: bool = False
    effective_at: str = ""
    knowledge_base: str = ""
    model_source: str = ""

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise KnowledgeError("保存参数含未知字段，必须使用规定的结构。")
        values = {}
        for field in ("project", "platform", "kind", "title", "content", "description", "reason"):
            values[field] = text(raw.get(field), field, 24000 if field == "content" else 500)
        if len(values["project"]) > 80 or len(values["platform"]) > 80:
            raise KnowledgeError("项目和平台名称最多 80 字符。")
        if values["kind"] not in {"knowledge", "rule", "skill"}:
            raise KnowledgeError("kind 必须是 knowledge、rule 或 skill。")
        version = raw.get("expected_version", 0)
        if type(version) is not int or version < 0:
            raise KnowledgeError("expected_version 必须是非负整数。")
        rid = raw.get("record_id", "")
        if not isinstance(rid, str) or (
            rid and (len(rid) != 32 or not all(c in "0123456789abcdef" for c in rid))
        ):
            raise KnowledgeError("record_id 必须来自实际查询结果。")
        if bool(rid) != bool(version):
            raise KnowledgeError("更新必须同时提供 record_id 与 expected_version；新增版本为 0。")
        create = raw.get("create_project", False)
        if type(create) is not bool:
            raise KnowledgeError("create_project 必须是布尔值。")
        effective = raw.get("effective_at", "")
        if effective:
            try:
                parsed = datetime.fromisoformat(effective)
                if parsed.tzinfo is None or parsed > datetime.now(timezone.utc):
                    raise ValueError
            except (ValueError, TypeError):
                raise KnowledgeError(
                    "生效时间必须带时区且不晚于当前时间；暂不支持预约生效。"
                ) from None
        for field in ("knowledge_base", "model_source"):
            value = raw.get(field, "")
            if not isinstance(value, str):
                raise KnowledgeError(f"{field} 必须是知识库名称或 ID。")
            values[field] = text(value, field, 200) if value.strip() else ""
        return cls(
            **values,
            record_id=rid,
            expected_version=version,
            create_project=create,
            effective_at=effective,
        )

    def to_dict(self):
        return asdict(self)


def render(plan, record_id, version, source, created):
    metadata = {
        "record_id": record_id,
        "version": version,
        "project": plan.project,
        "platform": plan.platform,
        "kind": plan.kind,
        "created_at": created,
        "effective_at": plan.effective_at or created,
        "source": {k: v for k, v in source.items() if k != "snapshot"},
        "change_reason": plan.reason,
    }
    body = f"# {plan.title}\n\n{plan.content}\n\n## 来源与版本\n\n```json\n"
    body += json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n"
    if plan.kind == "skill":
        # JSON quoted scalars are valid YAML. Paths/names are generated, never model supplied.
        front = f"---\nname: knowledge-{record_id}\ndescription: "
        front += json.dumps(
            f"仅适用于 {plan.project} / {plan.platform}。{plan.description}", ensure_ascii=False
        )
        body = front + "\n---\n\n" + body
    return body
