"""code review pipeline：确定性输入 → 单通道双轴 review → 结构化 findings → CI 门禁。

定位：review 的机器接口（CI 触发器 / 本地复现共用），不是交互功能。
- 输入确定：diff 由 gitdiff 在 Python 侧抽取，不经模型工具调用
- 会话只读：researcher 工具表（read_file / search_tools / run_bash 只读策略），
  不写文件，非 tty 环境下审批默认拒绝——CI 零人工
- 输出机器可消费：--format json 时 stdout 只出 JSON（raw 字段保留原文防解析丢失）；
  exit code 默认门禁语义（high findings → 1），--no-fail 关闭
- 基线设计（评测纪律）：默认单通道双轴；parallel 是 E11 的 A/B 实验组
  （run_review(mode=) / bench --review-mode 开关）——有基线才有度量，结论是
  默认 single、parallel 留作精度敏感（门禁）场景的实验开关
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

from led_review import config
from led_review.kernel.agent import ChatSession
from led_review.kernel.bridge import run_in_thread
from led_review.kernel.events import StreamStart, Warn
from led_review.tools import gitdiff
from led_review.tools.registry import get_tool_schemas
from led_review.ui import renderer as ui

# review 会话的轮次保险丝（无人值守保险丝加在调用侧——交互循环无硬上限是刻意设计）。
# 两档：SOFT 档经 steering 注入「立即收敛输出 findings」（E10：真实仓库的调研轮次
# 远超合成任务，硬断会得到半成品中间文本而非 findings）；HARD 档才 abort。
SOFT_CAP_ROUNDS = 8
HARD_CAP_ROUNDS = 12

_SOFT_CAP_MESSAGE = (
    "[系统] 轮次预算即将用尽：不要再调用工具，基于已收集的信息"
    "立即按格式输出 findings（无把握就写「无 findings」）。"
)


def _round_fuse(stream, control):
    """轮次保险丝（生成器包装）：SOFT 档注入收敛指令一次，HARD 档 abort。"""
    rounds = 0
    steered = False
    for ev in stream:
        if isinstance(ev, StreamStart):
            rounds += 1
            if rounds >= HARD_CAP_ROUNDS:
                control.abort()
                return
            if rounds >= SOFT_CAP_ROUNDS and not steered:
                control.steer.put(_SOFT_CAP_MESSAGE)
                steered = True
        yield ev

_REVIEW_HEADER = """请对以下变更做 code review。可以用 read_file / search_symbols / run_bash（只读命令）查看相关代码上下文，但审查对象只是变更本身。

{axes}

输出格式（严格遵守，会被程序解析）：
- 每个 finding 一行：- [severity] 路径:行号 — 问题描述（severity 只能是 high / medium / low）
- 某条需要给修复建议时，下一行缩进两个空格写：建议：……
- {no_findings_line}
- findings 之后写一节「## 总结」，两三句话评价这次变更的整体质量

变更如下：

{diff}"""

# E15 实验变体：输出规格强制化。与基线的差异——定位纪律（实现位置而非
# docstring）、每条附复现思路与 confidence 自评。目标是拉高可接受定位率
# （E12.8：模型把 bug 定位在类 docstring 14 行而非实现键 23 行）。
_REVIEW_HEADER_STRONG = """请对以下变更做 code review。可以用 read_file / search_symbols / run_bash（只读命令）查看相关代码上下文，但审查对象只是变更本身。

{axes}

输出格式（严格遵守，会被程序解析）：
- 每个 finding 一行：- [severity] 路径:行号 — 问题描述（severity 只能是 high / medium / low）
- 【定位纪律】行号必须是问题代码的**实现位置所在行**：字段/语句/分支的具体行；
  类或函数的 docstring、声明行、调用点都不算（若问题在函数体内，报函数体内
  实际出错的语句行；宁可少报位置不确定的，也不要报错位置）。
- 每条 finding 下一行缩进两个空格写：复现：……（一两句话的复现思路或触发条件）
- 再下一行缩进两个空格写：置信：N/5（N 为 1-5 整数，你对这条 finding 确为
  真问题的把握，不报 < 3 的条目）
- 某条需要给修复建议时，再下一行缩进两个空格写：建议：……
- 确实没有任何问题时，写一行「未发现问题」再写「## 总结」，总结两三句话评价
  这次变更的整体质量

变更如下：

{diff}"""

# 轴指令（E11 parallel 实验组）：single = 双轴合一基线；correctness / standards =
# 单轴聚焦 prompt。两轴 prompt 内容互斥——standards 轴不含【正确性】字样（用例区分轴）。
_AXES: dict[str, tuple[str, str]] = {
    "single": (
        "审查分两个轴：\n"
        "1. 【正确性】（主）：逻辑错误、边界条件、空值/溢出、错误处理遗漏、"
        "与既有代码行为不一致。只报有把握的真问题，宁缺毋滥；拿不准的不报。\n"
        "2. 【规范】：对照项目 AGENTS.md 与已注入 skills 的约定检查违规。",
        "两个轴都没有 finding 时",
    ),
    "correctness": (
        "审查只聚焦一个轴：\n"
        "【正确性】（主）：逻辑错误、边界条件、空值/溢出、错误处理遗漏、"
        "与既有代码行为不一致。只报有把握的真问题，宁缺毋滥；拿不准的不报。",
        "没有 finding 时",
    ),
    "standards": (
        "审查只聚焦一个轴：\n"
        "【规范】：对照项目 AGENTS.md 与已注入 skills 的约定检查违规。",
        "没有 finding 时",
    ),
}


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


def build_prompt(diff_text: str, *, strong: bool = False) -> str:
    """单通道基线 prompt：双轴合一（默认形态）。

    strong=True：E15 实验变体——强制输出规格（实现位置纪律 + 复现思路 +
    confidence 自评），目标是拉高「可接受定位率」（E12.8 教训：模型会把 bug
    定位在类 docstring 而非实现键）。
    """
    axes, no_findings = _AXES["single"]
    if not strong:
        return _REVIEW_HEADER.format(axes=axes, no_findings_line=no_findings, diff=diff_text)
    return _REVIEW_HEADER_STRONG.format(axes=axes, diff=diff_text)


def build_axis_prompt(diff_text: str, axis: str) -> str:
    """parallel 单轴 prompt：只留该轴指令（E11：轴聚焦防两轴注意力互稀释）。"""
    axes, no_findings = _AXES[axis]
    return _REVIEW_HEADER.format(axes=axes, no_findings_line=no_findings, diff=diff_text)


def _final_text(session: ChatSession) -> str:
    """终稿正文：最后一条非空 assistant 消息。"""
    for message in reversed(session.messages):
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _run_one(prompt: str, render: bool) -> tuple[str, ChatSession]:
    """跑单个 review 会话（只读工具面 + 轮次保险丝），返回 (终稿正文, session)。

    单通道与 parallel 两轴共用同一执行路径——差异只在 prompt（轴范围）。
    """
    tools = get_tool_schemas(config.SUBAGENT_TYPES["researcher"]["tools"])
    session = ChatSession(tools=tools, set_provider=False)
    events, control = run_in_thread(lambda c: session.chat(prompt, control=c))

    if render:
        # 文本模式：工具调用与 findings 流式渲染（本地可读、CI log 友好）
        ui.consume(_round_fuse(events, control))
    else:
        # 静默模式：内核告警走 stderr，不污染调用侧的结构化输出
        for ev in _round_fuse(events, control):
            if isinstance(ev, Warn):
                print(f"⚠ {ev.message}", file=sys.stderr)

    return _final_text(session), session


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _merge_findings(groups: list[list[Finding]]) -> list[Finding]:
    """合并多轴 findings（E11 parallel）：key=path:line 去重，冲突时 severity 高的
    胜出（描述一并采用；同 severity 先到先得）；按 severity 排序（high 在前）。
    纯 Python 确定性合并、零 LLM 成本——不用模型归并，可复现。"""
    merged: dict[tuple[str, int], Finding] = {}
    for group in groups:
        for f in group:
            key = (f.path, f.line)
            if key not in merged or _SEV_RANK[f.severity] < _SEV_RANK[merged[key].severity]:
                merged[key] = f
    return sorted(merged.values(), key=lambda f: _SEV_RANK[f.severity])


def _run_parallel(diff_text: str, render: bool) -> tuple[str, list[Finding], list[ChatSession]]:
    """parallel 模式：两轴独立只读会话 + Python 确定性合并（E11 实验组）。"""
    raws: list[str] = []
    sessions: list[ChatSession] = []
    groups: list[list[Finding]] = []
    for axis in ("correctness", "standards"):
        text, session = _run_one(build_axis_prompt(diff_text, axis), render)
        raws.append(text)
        sessions.append(session)
        groups.append(parse_findings(text))
    raw = f"【correctness 轴】\n\n{raws[0]}\n\n【standards 轴】\n\n{raws[1]}"
    return raw, _merge_findings(groups), sessions


def run_review(spec: str | None = None, *, root: str | None = None,
               render: bool = False, mode: str = "single") -> tuple[str, list[Finding], list[ChatSession]]:
    """跑一轮 review，返回 (终稿原文, 结构化 findings, sessions)。

    mode：single（默认，双轴合一基线）| parallel（E11 A/B 实验组，两轴独立会话
    + 确定性合并）。sessions 恒为列表（bench 按会话求和 token；无变更时为空）。
    输出与 exit code 由调用侧决定：CLI（review()）渲染+门禁；bench 取数据判分。
    """
    diff_text = gitdiff.collect(spec, root=root)
    if diff_text == "（无变更）":
        return "", [], []
    if mode == "parallel":
        return _run_parallel(diff_text, render)
    raw, session = _run_one(build_prompt(diff_text), render)
    return raw, parse_findings(raw), [session]


def review(spec: str | None = None, *, fmt: str = "text", fail_on_high: bool = True,
           max_findings: int = 0, root: str | None = None) -> int:
    """CLI 壳：跑一轮 review，返回 exit code（默认 high findings → 1，CI 门禁语义）。"""
    # 完整终端渲染（工具流/流式正文/thinking 计数）只在 tty 有意义；管道/CI 场景
    # 必须输出干净可解析的终稿（renderer 的「非 tty 纯文本直出」承诺）——否则
    # `led review > review.md` 会把整套 ⏺/⎿ 工具调用跟踪卷进 PR 评论（dogfood
    # 实拍：乱码重复、工具过程泄露进最终报告）。
    tty = fmt == "text" and sys.stdout.isatty()
    raw, findings, sessions = run_review(spec, root=root, render=tty)
    findings = cap_findings(findings, max_findings)
    if not sessions:
        print("无变更，跳过 review")
        return 0
    high = sum(f.severity == "high" for f in findings)

    if fmt == "json":
        print(json.dumps({
            "spec": spec or "HEAD",
            "counts": {s: sum(f.severity == s for f in findings) for s in ("high", "medium", "low")},
            "findings": [asdict(f) for f in findings],
            "raw": raw,
        }, ensure_ascii=False, indent=2))
    else:
        gate = f"✗ {high} 个 high findings" if high else "✓ 无 high findings"
        if tty:
            print(f"\n{gate}（共 {len(findings)} 条）")
        else:
            # 非 tty：只输出终稿原文 + 门禁行——机器/CI/PR 评论要的是正文，
            # 不是终端渲染过程
            print(raw)
            print(f"{gate}（共 {len(findings)} 条）")

    return 1 if (fail_on_high and high) else 0


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="led review",
        description="code review：审查一个 diff 范围（默认工作区+暂存 vs HEAD）",
    )
    parser.add_argument("spec", nargs="?", default=None,
                        help="revspec：main...HEAD / a..b / 单个 commit；缺省 = 工作区改动")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="json 时 stdout 只出结构化结果（CI/评测消费）")
    parser.add_argument("--no-fail", action="store_true",
                        help="有 high findings 也返回 0（关闭门禁语义）")
    parser.add_argument("--max-findings", type=int, default=0,
                        help="最多输出多少条 findings（0 = 不限制）")
    args = parser.parse_args(argv)
    try:
        return review(args.spec, fmt=args.format, fail_on_high=not args.no_fail,
                      max_findings=args.max_findings)
    except gitdiff.GitRepoError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


def cap_findings(findings: list[Finding], limit: int) -> list[Finding]:
    """限制 findings 输出条数（0 = 不限制）。"""
    if limit <= 0:
        return findings
    return findings[:limit]
