"""skills 机制（纯读路径）：索引常驻 + 正文惰性，与 search_tools 两档制同构。

skill = cwd 的 skills/<name>/SKILL.md（agentskills.io 布局）：
    ---
    name: commit-style
    description: 提交信息规范：type(scope): 主题
    ---
    正文（可执行约定、正反例……）

ChatSession 构造时把索引（name/description/路径/字数）注入为一条 system 消息；
正文不注入——模型用已有 read_file 按需加载。零新工具、零新依赖。

刻意不做（ROADMAP 定调）：用户级 skills 目录、/skill-save 命令（agent 用
write_file 就能沉淀，命令是壳）、skill 自动创建（自循环深坑）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
_DESCRIPTION_FALLBACK_LIMIT = 100


@dataclass
class Skill:
    name: str
    description: str
    path: str   # 相对项目根：skills/<name>/SKILL.md（索引进 system prompt，模型据此 read_file）
    chars: int  # 正文字符数（给模型加载成本预期）


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 --- 围栏的简易 frontmatter（key: value 行），返回 (字段, 正文)。

    容错：无 frontmatter 返回空表 + 全文；围栏不闭合同样按无 frontmatter 处理；
    非 key: value 行静默忽略。不引入 YAML 依赖——这里只需要两个标量字段。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fields = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields, text[end + 4:].lstrip("\r\n")


def _fallback_description(body: str) -> str:
    """缺 description 时取正文首个非空行（剥 markdown 标题符），截断。"""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:_DESCRIPTION_FALLBACK_LIMIT]
    return ""


def scan_skills(root: str) -> list[Skill]:
    """扫 <root>/skills/*/SKILL.md，按名字排序。目录缺失返回空表（增强不是依赖）。"""
    base = os.path.join(root, SKILLS_DIR)
    if not os.path.isdir(base):
        return []
    skills = []
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        skill_path = os.path.join(entry.path, SKILL_FILE)
        try:
            with open(skill_path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue  # 目录存在但 SKILL.md 不可读：跳过这个 skill，不拖垮其余
        fields, body = _parse_frontmatter(text)
        rel_path = os.path.join(SKILLS_DIR, entry.name, SKILL_FILE)
        skills.append(Skill(
            name=fields.get("name") or entry.name,
            description=fields.get("description") or _fallback_description(body),
            path=rel_path,
            chars=len(body),
        ))
    return skills


def format_skills_index(skills: list[Skill]) -> str | None:
    """索引清单（system 消息文本）；无 skill 返回 None（不注入，零成本）。

    每行给模型四件事：名字、用途、加载路径、加载成本（字数）。
    """
    if not skills:
        return None
    lines = ["[可用的 skills（约定/流程性知识；需要时用 read_file 按路径加载正文）：]"]
    for s in skills:
        lines.append(f"- {s.name}：{s.description}（{s.path}，{s.chars} 字符）")
    return "\n".join(lines)
