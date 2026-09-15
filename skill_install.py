"""Transactional, administrator-only caller's store for managed text skills."""

import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import KnowledgeError
from .service import write_atomic


def validate(name, content):
    import yaml

    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or len(name) >= 64
    ):
        raise KnowledgeError("技能名须为小写字母、数字及单连字符，少于 64 字符。")
    if not isinstance(content, str) or len(content) > 48000 or "\x00" in content:
        raise KnowledgeError("技能正文须为不超过 48000 字符的文本。")
    parts = re.split(r"(?m)^---[ \t]*$", content.strip(), maxsplit=2)
    if len(parts) != 3 or parts[0] or not parts[2].strip():
        raise KnowledgeError("技能须包含 YAML frontmatter 和正文。")
    try:
        nodes = yaml.compose(parts[1])
        if not isinstance(nodes, yaml.MappingNode) or len(nodes.value) != 2:
            raise KnowledgeError("frontmatter 仅允许两个唯一字段。")
        header = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        raise KnowledgeError("技能 YAML 格式无效。") from None
    if (
        not isinstance(header, dict)
        or set(header) != {"name", "description"}
        or header["name"] != name
        or not isinstance(header["description"], str)
        or not header["description"].strip()
        or len(header["description"]) > 2000
    ):
        raise KnowledgeError("frontmatter 仅含匹配的 name 和非空 description。")
    return content.strip() + "\n"


class SkillInstaller:
    def __init__(self, root, records):
        self.root, self.records = Path(root), Path(records)

    def install(self, name, content, expected, source, discover):
        content = validate(name, content)
        created_at = datetime.now(timezone.utc).isoformat()
        new_sha = hashlib.sha256(content.encode()).hexdigest()
        if expected and not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise KnowledgeError("更新指纹须来自 native_skill_read。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.records.mkdir(parents=True, exist_ok=True)
        import fcntl

        with (self.records / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = self.root / name
            manifest_path = self.records / name / "current.json"
            before = None
            if target.is_symlink():
                raise KnowledgeError("拒绝修改符号链接技能。")
            if target.exists():
                if (
                    not target.is_dir()
                    or manifest_path.is_symlink()
                    or not manifest_path.is_file()
                    or set(p.name for p in target.iterdir()) != {"SKILL.md"}
                    or (target / "SKILL.md").is_symlink()
                ):
                    raise KnowledgeError("只允许更新本工具安装的单文件技能；不覆盖人工或插件技能。")
                before = (target / "SKILL.md").read_bytes()
                old_sha = hashlib.sha256(before).hexdigest()
                manifest = json.loads(manifest_path.read_text())
                if manifest["sha256"] != old_sha:
                    raise KnowledgeError("技能被外部修改，请先核对，拒绝覆盖。")
                if expected != old_sha:
                    raise KnowledgeError("技能版本冲突，请重新读取完整技能并使用当前指纹。")
            elif expected:
                raise KnowledgeError("待更新技能不存在。")
            # Preserve immutable revisions and provenance outside the global skill directory.
            revision = self.records / name / "versions" / new_sha
            write_atomic(revision / "SKILL.md", content)
            # Source may differ for identical content, keep an independent attempt record.
            attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=self.records))
            write_atomic(
                attempt / "source.json",
                json.dumps({"created_at": created_at, **source}, ensure_ascii=False),
            )
            staging = Path(tempfile.mkdtemp(prefix=".skill-", dir=self.root))
            old_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
            installed = False
            try:
                (staging / "SKILL.md").write_text(content, encoding="utf-8")
                if before is None:
                    staging.rename(target)
                else:
                    (staging / "SKILL.md").replace(target / "SKILL.md")
                installed = True
                if (
                    not discover(name, target / "SKILL.md")
                    or (target / "SKILL.md").read_text() != content
                ):
                    raise KnowledgeError("AstrBot 未发现或无法回读技能，已恢复旧状态。")
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = attempt / "current.json"
                tmp.write_text(
                    json.dumps(
                        {
                            "created_at": created_at,
                            "sha256": new_sha,
                            "previous_sha256": expected,
                            "source_record": attempt.name,
                        }
                    )
                )
                tmp.replace(manifest_path)
                return {
                    "status": "installed",
                    "name": name,
                    "created_at": created_at,
                    "sha256": new_sha,
                    "previous_sha256": expected,
                    "message": "原生技能已安装，AstrBot 发现与全文回读验证通过。",
                }
            except BaseException:
                if installed:
                    if before is None:
                        shutil.rmtree(target)
                    else:
                        rollback = attempt / "rollback.md"
                        rollback.write_bytes(before)
                        rollback.replace(target / "SKILL.md")
                    if old_manifest is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        manifest_path.write_bytes(old_manifest)
                raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
