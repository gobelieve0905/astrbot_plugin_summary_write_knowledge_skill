"""Read-only access to the same enabled skills the native agent can discover."""

import hashlib
import os
import stat
from pathlib import Path

from .models import KnowledgeError

TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".toml"}


def read_text_resource(root, relative, offset=0, limit=16000):
    try:
        return _read_text_resource(root, relative, offset, limit)
    except (OSError, ValueError):
        raise KnowledgeError("技能或附件文本不存在、不可读取或路径无效。") from None


def _read_text_resource(root, relative, offset=0, limit=16000):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
        raise KnowledgeError("读取位置或长度无效。")
    rel = Path(relative)
    if (
        rel.is_absolute()
        or ".." in rel.parts
        or not rel.parts
        or any(p.startswith(".") for p in rel.parts)
    ):
        raise KnowledgeError("只能读取技能目录内的相对文本文件。")
    root = Path(root).resolve(strict=True)
    path = root / rel
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise KnowledgeError("仅支持读取技能文本说明及脚本源码，不执行文件。")
    # Disallow symlinks rather than following references into other skills or files.
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise KnowledgeError("不读取符号链接。")
    if not path.resolve(strict=True).is_relative_to(root):
        raise KnowledgeError("文件超出技能目录。")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise KnowledgeError("文件不是普通文本文件或超过 1 MiB。")
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise KnowledgeError("文件超过 1 MiB。")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        raise KnowledgeError("文件不是 UTF-8 文本。") from None
    if offset > len(content):
        raise KnowledgeError("读取位置超出文件长度。")
    end = min(len(content), offset + limit)
    return {
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": content[offset:end],
        "offset": offset,
        "total_chars": len(content),
        "next_offset": end if end < len(content) else None,
        "complete": offset == 0 and end == len(content),
    }


async def available_skills(context, event):
    from astrbot.core.astr_main_agent import _filter_skills_for_current_config
    from astrbot.core.skills import SkillManager

    cfg = context.get_config(umo=event.unified_msg_origin).get("provider_settings", {})
    skills = SkillManager().list_skills(
        active_only=True, runtime=cfg.get("computer_use_runtime", "none"), show_sandbox_path=False
    )
    skills = _filter_skills_for_current_config(skills, cfg)
    _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
        umo=event.unified_msg_origin,
        conversation_persona_id=event.get_extra("summary_knowledge.persona_id"),
        platform_name=event.get_platform_name(),
        provider_settings=cfg,
    )
    if persona and persona.get("skills") is not None:
        allowed = set(persona["skills"])
        skills = [s for s in skills if s.name in allowed]
    return [s for s in skills if s.local_exists]


def skill_root(skill):
    from astrbot.core.utils.astrbot_path import (
        get_astrbot_builtin_plugin_path,
        get_astrbot_plugin_path,
        get_astrbot_skills_path,
    )

    path = Path(skill.path)
    resolved = path.resolve(strict=True)
    roots = [
        Path(f()).resolve()
        for f in (get_astrbot_skills_path, get_astrbot_plugin_path, get_astrbot_builtin_plugin_path)
    ]
    if path.is_symlink() or not any(resolved.is_relative_to(root) for root in roots):
        raise KnowledgeError("技能不在已安装的本地技能目录。")
    lexical = path.absolute()
    base = next((r for r in roots if lexical.is_relative_to(r)), None)
    if base is None:
        raise KnowledgeError("技能路径不在已安装目录。")
    for part in [lexical, *lexical.parents]:
        if part == base:
            break
        if part.is_symlink():
            raise KnowledgeError("不读取符号链接技能。")
    return resolved.parent
