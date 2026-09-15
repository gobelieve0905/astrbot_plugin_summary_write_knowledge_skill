"""Knowledge receipt protection must never replace unrelated report answers."""

import re

CLAIM = re.compile(r"(?<![未不])已(?:保存|入库|写入知识库)|技能已启用")
KNOWLEDGE = re.compile(r"知识库|长期知识|项目知识|规则库|项目\s*Skill|技能已启用", re.I)
NEGATED = re.compile(
    r"未|没有|尚不能|不能确认|无法确认|只有|才能|才可以|如果|是否|声称|不要|不得|不能声称"
)
NOTICE = "本次没有成功的知识写入回执，尚不能确认知识已保存。"


def protect_knowledge_claims(completion, *, write_attempted=False):
    # Keep code blocks, quoted examples and all non-knowledge text verbatim.
    parts = re.split(r"(```[\s\S]*?```)", completion)
    for index in range(0, len(parts), 2):
        sentences = re.split(r"(?<=[。！？\n])", parts[index])
        for j, sentence in enumerate(sentences):
            if sentence.lstrip().startswith(">"):
                continue
            match = CLAIM.search(sentence)
            if not match or NEGATED.search(sentence[: match.start()]):
                continue
            if not (KNOWLEDGE.search(sentence) or write_attempted):
                continue
            # Suppress only the unsupported knowledge statement, not the whole answer.
            ending = "\n" if sentence.endswith("\n") else ""
            sentences[j] = NOTICE + ending
        parts[index] = "".join(sentences)
    return "".join(parts)


def protect_native_claims(completion):
    parts = re.split(r"(```[\s\S]*?```)", completion)
    for i in range(0, len(parts), 2):
        sentences = re.split(r"(?<=[。！？\n])", parts[i])
        for j, sentence in enumerate(sentences):
            match = re.search(r"已(?:安装|更新|启用|注册)|安装成功|更新成功", sentence)
            if (
                match
                and not sentence.lstrip().startswith(">")
                and not NEGATED.search(sentence[: match.start()])
            ):
                sentences[j] = "本次没有成功的原生技能安装回执，尚不能确认安装或更新成功。" + (
                    "\n" if sentence.endswith("\n") else ""
                )
        parts[i] = "".join(sentences)
    return "".join(parts)
