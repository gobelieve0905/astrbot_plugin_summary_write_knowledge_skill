"""Deterministic project ownership checks for multi-topic conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import KnowledgeError, digest


@dataclass(frozen=True)
class Assignment:
    project: str
    confidence: str
    evidence: tuple[str, ...]
    segments: dict
    fingerprint: str

    def to_dict(self):
        return {
            "project": self.project,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "segments": self.segments,
            "fingerprint": self.fingerprint,
        }


def audit(topic, project: str, known_projects: list[dict], bound: str | None = None):
    """Check explicit project evidence and classify message segments.

    Group names and senders are deliberately excluded. A project mentioned in the
    final request or an existing topic binding wins over weak textual mentions.
    """
    text = "\n".join(
        [topic.request]
        + [str(item.get("content", "")) for item in topic.history if isinstance(item, dict)]
    )
    names = [str(p["name"]) for p in known_projects if p.get("name")]
    mentioned = [name for name in names if re.search(re.escape(name), text, re.I)]
    explicit = [name for name in names if re.search(re.escape(name), topic.request, re.I)]
    if bound and bound != project and not explicit:
        raise KnowledgeError(f"本话题已绑定项目“{bound}”，本次计划却指向“{project}”；请明确切换项目。")
    if len(set(explicit)) > 1:
        raise KnowledgeError("最终指令同时指定了多个项目，请明确本次保存目标。")
    if not explicit and len(set(mentioned)) > 1 and not bound:
        raise KnowledgeError("引用会话涉及多个项目，无法自动归类；请明确保存到哪个项目。")
    if not project.strip():
        raise KnowledgeError("保存前必须明确目标项目。")
    named_in_request = bool(re.search(re.escape(project), topic.request, re.I))
    confidence = "high" if explicit or bound == project or named_in_request else "medium" if project in mentioned else "low"
    if confidence == "low" and names:
        raise KnowledgeError(f"无法从会话确认项目“{project}”，请在指令中明确项目名称。")
    segments = {"target": [], "shared": [], "reference": [], "unknown": []}
    for index, item in enumerate(topic.history):
        content = str(item.get("content", "")) if isinstance(item, dict) else str(item)
        if re.search(r"通用|平台|方法论|流程|skill", content, re.I):
            segments["shared"].append(index)
        elif any(re.search(re.escape(name), content, re.I) for name in names if name == project):
            segments["target"].append(index)
        elif mentioned and any(re.search(re.escape(name), content, re.I) for name in mentioned):
            segments["reference"].append(index)
        else:
            segments["unknown"].append(index)
    fingerprint = digest({"project": project, "bound": bound or "", "request": topic.request, "history": topic.history})
    return Assignment(project, confidence, tuple([f"explicit:{x}" for x in explicit] + ([f"binding:{bound}"] if bound else [])), segments, fingerprint)
