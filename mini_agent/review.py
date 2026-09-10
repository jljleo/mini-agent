"""code review pipeline：确定性输入 → 单通道双轴 review → 结构化 findings → CI 门禁。

定位：review 的机器接口（CI 触发器 / 本地复现共用），不是交互功能。
- 输入确定：diff 由 gitdiff 在 Python 侧抽取，不经模型工具调用
- 会话只读：researcher 工具表（read_file / search_tools / run_bash 只读策略），
  不写文件，非 tty 环境下审批默认拒绝——CI 零人工
- 输出机器可消费：--format json 时 stdout 只出 JSON（raw 字段保留原文防解析丢失）；
  exit code 默认门禁语义（high findings → 1），--no-fail 关闭
- 基线设计（评测纪律）：单通道双轴。并行通道/多轮深挖是 R2 的 A/B 实验组——
  没有基线，增强的收益无法度量
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

from mini_agent import config
from mini_agent.kernel.agent import ChatSession
from mini_agent.kernel.bridge import run_in_thread
from mini_agent.kernel.events import StreamStart, Warn
from mini_agent.tools import gitdiff
from mini_agent.tools.registry import get_tool_schemas
from mini_agent.ui import renderer as ui

# review 会话的轮次上限（无人值守保险丝加在调用侧——交互循环无硬上限是刻意设计）
MAX_REVIEW_ROUNDS = 8

_REVIEW_PROMPT = """请对以下变更做 code review。可以用 read_file / search_symbols / run_bash（只读命令）查看相关代码上下文，但审查对象只是变更本身。

审查分两个轴：
1. 【正确性】（主）：逻辑错误、边界条件、空值/溢出、错误处理遗漏、与既有代码行为不一致。只报有把握的真问题，宁缺毋滥；拿不准的不报。
2. 【规范】：对照项目 AGENTS.md 与已注入 skills 的约定检查违规。

输出格式（严格遵守，会被程序解析）：
- 每个 finding 一行：- [severity] 路径:行号 — 问题描述（severity 只能是 high / medium / low）
- 某条需要给修复建议时，下一行缩进两个空格写：建议：……
- 两个轴都没有 finding 时，写一行：无 findings
- findings 之后写一节「## 总结」，两三句话评价这次变更的整体质量

变更如下：

{diff}"""


@dataclass
class Finding:
    severity: str
    path: str
    line: int
    description: str
    suggestion: str | None = None


_FINDING_RE = re.compile(r"^-\s*\[(high|medium|low)]\s+(\S+?):(\d+)\s*[—–-]\s*(.+)$")
_SUGGESTION_RE = re.compile(r"^\s+建议[：:]\s*(.+)$")


def parse_findings(text: str) -> list[Finding]:
    """从 review 输出解析结构化 findings。容错：解析不动的行静默跳过（原文由 raw 保留）。"""
    findings: list[Finding] = []
    for line in text.splitlines():
        m = _FINDING_RE.match(line)
        if m:
            findings.append(Finding(m.group(1), m.group(2), int(m.group(3)), m.group(4).strip()))
            continue
        s = _SUGGESTION_RE.match(line)
        if s and findings and findings[-1].suggestion is None:
            findings[-1].suggestion = s.group(1).strip()
    return findings


def build_prompt(diff_text: str) -> str:
    return _REVIEW_PROMPT.format(diff=diff_text)


def _final_text(session: ChatSession) -> str:
    """终稿正文：最后一条非空 assistant 消息。"""
    for message in reversed(session.messages):
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def review(spec: str | None = None, *, fmt: str = "text", fail_on_high: bool = True,
           root: str | None = None) -> int:
    """跑一轮 review，返回 exit code（默认 high findings → 1，CI 门禁语义）。"""
    diff_text = gitdiff.collect(spec, root=root)
    if diff_text == "（无变更）":
        print("无变更，跳过 review")
        return 0

    tools = get_tool_schemas(config.SUBAGENT_TYPES["researcher"]["tools"])
    session = ChatSession(tools=tools, set_provider=False)
    events, control = run_in_thread(lambda c: session.chat(build_prompt(diff_text), control=c))

    def capped(stream):
        """轮次保险丝：超 MAX_REVIEW_ROUNDS 即 abort（防 context 深挖失控烧 token）。"""
        rounds = 0
        for ev in stream:
            rounds += isinstance(ev, StreamStart)
            if rounds > MAX_REVIEW_ROUNDS:
                control.abort()
                return
            yield ev

    quiet = fmt == "json"
    if quiet:
        # json 模式：stdout 只出 JSON；内核告警走 stderr，不污染机器输出
        for ev in capped(events):
            if isinstance(ev, Warn):
                print(f"⚠ {ev.message}", file=sys.stderr)
    else:
        # 文本模式：工具调用与 findings 流式渲染（本地可读、CI log 友好）
        ui.consume(capped(events))

    raw = _final_text(session)
    findings = parse_findings(raw)
    high = sum(f.severity == "high" for f in findings)

    if quiet:
        print(json.dumps({
            "spec": spec or "HEAD",
            "counts": {s: sum(f.severity == s for f in findings) for s in ("high", "medium", "low")},
            "findings": [asdict(f) for f in findings],
            "raw": raw,
        }, ensure_ascii=False, indent=2))
    else:
        gate = f"✗ {high} 个 high findings" if high else "✓ 无 high findings"
        print(f"\n{gate}（共 {len(findings)} 条）")

    return 1 if (fail_on_high and high) else 0


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-agent review",
        description="code review：审查一个 diff 范围（默认工作区+暂存 vs HEAD）",
    )
    parser.add_argument("spec", nargs="?", default=None,
                        help="revspec：main...HEAD / a..b / 单个 commit；缺省 = 工作区改动")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="json 时 stdout 只出结构化结果（CI/评测消费）")
    parser.add_argument("--no-fail", action="store_true",
                        help="有 high findings 也返回 0（关闭门禁语义）")
    args = parser.parse_args(argv)
    try:
        return review(args.spec, fmt=args.format, fail_on_high=not args.no_fail)
    except gitdiff.GitRepoError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
