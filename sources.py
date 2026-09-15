"""Immutable per-request sources with explicit, bounded selection and provenance."""

import json
from dataclasses import replace

from .models import KnowledgeError, digest


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


class SourceWindow:
    def __init__(self, topic, maximum, attachments=()):
        self.topic = topic
        self.maximum = int(maximum)
        if self.maximum <= 0:
            raise KnowledgeError("单次整理上限必须大于 0。")
        self.sources = {}
        for index, message in enumerate(topic.history):
            self.sources[f"h:{index}"] = {
                "kind": "history",
                "role": message.get("role", "unknown") if isinstance(message, dict) else "unknown",
                "text": encoded(message),
            }
        self.sources["current"] = {"kind": "current_message", "role": "user", "text": topic.request}
        for index, attachment in enumerate(attachments):
            self.sources[f"a:{index}"] = {"kind": "attachment", **attachment}
        self.snapshot = digest({"topic": topic.to_dict(), "sources": self.sources})
        self.read_ranges = {}
        self.selected = None
        if len(encoded(topic.to_dict())) <= self.maximum and not attachments:
            self.selected = topic

    def directory(self, offset=0, limit=30):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise KnowledgeError("来源目录分页参数无效。")
        items = list(self.sources.items())
        return {
            "snapshot": self.snapshot,
            "total_sources": len(items),
            "total_history_chars": len(encoded(self.topic.history)),
            "max_selected_chars": self.maximum,
            "selection_required": self.selected is None,
            "sources": [
                {
                    "id": ident,
                    "kind": s["kind"],
                    "role": s.get("role"),
                    "name": s.get("name"),
                    "chars": len(s["text"]),
                    "preview": s["text"][:160],
                    "sha256": digest(s["text"]),
                }
                for ident, s in items[offset : offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(items) else None,
        }

    def read(self, ident, offset=0, limit=16000):
        if (
            ident not in self.sources
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 32000
        ):
            raise KnowledgeError("来源 ID 或读取范围无效，请先读取来源目录。")
        source = self.sources[ident]
        value = source["text"]
        if offset >= len(value):
            raise KnowledgeError("读取位置超出来源长度。")
        end = min(len(value), offset + limit)
        self.read_ranges.setdefault(ident, []).append((offset, end))
        return {
            "id": ident,
            "snapshot": self.snapshot,
            "sha256": digest(value),
            "text": value[offset:end],
            "offset": offset,
            "end": end,
            "next_offset": end if end < len(value) else None,
            "total_chars": len(value),
        }

    def select(self, snapshot, ranges, reason):
        if snapshot != self.snapshot:
            raise KnowledgeError("来源快照已变化，请重新读取目录。")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise KnowledgeError("必须说明选择范围与用户保存请求的关系。")
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= 100:
            raise KnowledgeError("请选择 1 至 100 个已读取的来源片段。")
        history, evidence = [], []
        for item in ranges:
            if not isinstance(item, dict) or set(item) != {"id", "start", "end"}:
                raise KnowledgeError("片段格式为 id/start/end。")
            ident, start, end = item["id"], item["start"], item["end"]
            if (
                not isinstance(ident, str)
                or ident not in self.sources
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(self.sources[ident]["text"])
            ):
                raise KnowledgeError("片段位置无效。")
            cursor = start
            for a, b in sorted(self.read_ranges.get(ident, [])):
                if a <= cursor < b:
                    cursor = b
            if cursor < end:
                raise KnowledgeError("请先用 knowledge_source_read 完整读取所选片段。")
            source = self.sources[ident]
            history.append(
                {
                    "source_id": ident,
                    "kind": source["kind"],
                    "role": source.get("role"),
                    "name": source.get("name"),
                    "content": source["text"][start:end],
                }
            )
            evidence.append({**item, "source_sha256": digest(source["text"])})
        coverage = (
            "明确选定来源片段及当前请求；未选择的历史没有进入本次校验，不代表整个话题最终结论。"
        )
        selected = replace(
            self.topic,
            history=history,
            source_messages=[],
            coverage=coverage,
            selection={
                "snapshot_sha256": self.snapshot,
                "total_history_messages": len(self.topic.history),
                "ranges": evidence,
                "reason": reason,
            },
        )
        if len(encoded(selected.to_dict())) > self.maximum:
            raise KnowledgeError("选定内容仍超过单次整理上限，请缩小范围或分条保存。")
        self.selected = selected
        return selected
