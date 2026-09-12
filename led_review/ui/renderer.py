"""TUI 组件库：全程序唯一的界面层，所有终端渲染统一走这里。

设计：
    - 单一 Console + 语义化 Theme：业务代码只调 banner/warn/tool_call 等语义接口，
      不再散落 \\033 转义码；调整配色只改 THEME 一处
    - StreamRenderer：一轮 API 响应的渲染管线——spinner（等首字，思考期间计数）→
      正文 Markdown 流式渲染（Live 增量重排）；思考原文不流出（折叠为一行计数）
    - 工具调用可视化采用 Claude Code 风格 ⏺ / ⎿：美观之外仍是"幻觉测谎仪"
    - 非 tty（管道/重定向）自动降级为纯文本直出：rich 自动去色，
      Live/spinner 不启用，管道输出保持干净可解析

动态文本一律用 Text/markup=False 渲染，杜绝模型输出里的 "[xxx]" 被当 markup 解析。

两个反凌乱决策（2026-09，真实 tty transcript 驱动）：
    - 思考原文不流出：碎片化的内部独白（多轮 reasoning 在渲染态切换时裸 print 的
      残片）是 tty 最大的乱源；折叠为「✻ 思考 N 字」一行，完整推理仍进历史/轨迹
    - Usage 缓存到 TurnEnd 只打一次累计行：一轮对话 N 个 API 请求打 N 行 token 是
      第二乱源
"""

from __future__ import annotations

import json
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from led_review.config import format_context_tokens
from led_review.kernel.events import (
    Note,
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnEnd,
    Usage,
    Warn,
)
from led_review.ui.segments import StreamSegmenter

THEME = Theme(
    {
        "accent": "bright_cyan",
        "muted": "grey62",
        "faint": "grey42",
        # 复合样式也要在主题里注册：style="faint italic" 查不到整串时，
        # Rich 会退化为逐词解析，faint 不是内置色名 → MissingStyle 崩溃
        "reasoning": "grey42 italic",
        "key": "bold bright_cyan",  # 表格左列键名（同因：bold accent 复合名需注册）
        "warning": "yellow",
        "error": "bright_red",
        "success": "green",
    }
)

console = Console(theme=THEME, highlight=False)
# stderr 专用：import 期告警等开发者向消息不污染 stdout 管道
err_console = Console(theme=THEME, highlight=False, stderr=True)

# Markdown Live 重渲染的最小间隔（秒）：流式 chunk 很密，每片都全量重排是 O(n²) 抖动源
LIVE_RENDER_INTERVAL = 0.08
# 工具调用参数值在 ⏺ 行内的回显长度上限
_ARG_VALUE_LIMIT = 60


# ---- 基础消息件 ----


def note(text: str, tag: str | None = None) -> None:
    """暗色系统提示（compact 等内部机制的动作说明）。"""
    console.print(f"[{tag}] {text}" if tag else text, style="faint", markup=False)


def warn(text: str) -> None:
    console.print(f"⚠ {text}", style="warning", markup=False)


def error(text: str) -> None:
    console.print(f"✗ {text}", style="error", markup=False)


def success(text: str) -> None:
    console.print(f"✓ {text}", style="success", markup=False)


def goodbye() -> None:
    console.print("\n[muted]Bye![/]")


# ---- 启动横幅 ----


def banner(model: str, cwd: str, context_tokens: int | None = None) -> None:
    """启动横幅：品牌 + 关键上下文（模型/目录）+ 最小上手提示。"""
    ctx = f" · ctx {format_context_tokens(context_tokens)}" if context_tokens else ""
    if not console.is_terminal:
        console.print(f"led · {model}{ctx}")
        return
    console.print()
    console.print(
        Panel(
            f"[bold accent]✦ mini-agent[/]\n\n"
            f"[muted]模型[/]  {escape(model)}{ctx}\n"
            f"[muted]目录[/]  {escape(cwd)}\n\n"
            f"[faint]输入问题开始对话 · 输入 / 查看命令 · exit / Ctrl+C 退出[/]",
            border_style="faint",
            padding=(0, 2),
        )
    )


# ---- token 仪表盘 ----


def token_line(prompt: int, completion: int, cached: int, total: int) -> None:
    """每轮请求后的消耗行：本轮 prompt/completion/缓存命中 + 会话累计。"""
    console.print(
        f"[faint]tokens · prompt {prompt:,} · completion {completion:,}"
        f"（缓存 {cached:,}）｜累计 {total:,}[/]"
    )


# ---- 工具调用可视化 ----


def _format_args(raw: str) -> str:
    """把工具调用的 arguments JSON 格式化为 key=value 单行，值截断防刷屏。"""
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # max_tokens 截断的不完整 JSON：原样截断回显
        return raw if len(raw) <= 80 else raw[:80] + "…"
    if not isinstance(args, dict):
        return str(args)
    parts = []
    for key, value in args.items():
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > _ARG_VALUE_LIMIT:
            text = text[:_ARG_VALUE_LIMIT] + '…"'
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def tool_call(name: str, raw_arguments: str) -> None:
    """⏺ name(args) —— agent 每一步动作的可视化。"""
    line = Text()
    line.append("⏺ ", style="accent")
    line.append(name or "?", style="bold")
    line.append(f"({_format_args(raw_arguments)})", style="faint")
    console.print()
    console.print(line)


def tool_result(preview: str) -> None:
    """⎿ 结果预览：折叠为单行（换行符显式化），保持时间线紧凑。"""
    one_line = " ⏎ ".join(preview.splitlines())
    line = Text("  ⎿  ", style="faint")
    line.append(one_line, style="muted")
    console.print(line)


# ---- 人工确认（input_utils.confirm 的提示文案，ANSI 字符串形式）----
# prompt_toolkit 的 ANSI() 包装需要转义序列字符串，不走 rich Console

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[90m"
_ANSI_YELLOW = "\033[93m"
_ANSI_RED = "\033[91m"


def confirm_prompt_text(command: str, dangerous: bool, timeout: int) -> str:
    """确认提示：危险命令红色、普通确认黄色，命令本体加粗居中视线。"""
    color = _ANSI_RED if dangerous else _ANSI_YELLOW
    label = "危险命令" if dangerous else "需要确认"
    return (
        f"\n{color}{_ANSI_BOLD}⚠ {label}{_ANSI_RESET}{color} — 即将在项目目录执行:{_ANSI_RESET}\n"
        f"  {_ANSI_BOLD}{command}{_ANSI_RESET}\n"
        f"{_ANSI_DIM}按 y 执行，其余键拒绝（{timeout}s 超时）:{_ANSI_RESET} "
    )


# ---- 流式渲染 ----


class StreamRenderer:
    """一轮 API 响应的渲染管线。

    两种形态：
    - tty：spinner（等首字；思考期间显示计字数作活动信号）→ 正文 Markdown Live 增量重排
    - 非 tty：纯文本直出
    """

    def __init__(self) -> None:
        self._plain = not console.is_terminal
        self._status = None
        self._live: Live | None = None
        self._segments = StreamSegmenter()  # 完成段落卷 + 进行中尾部的唯一切割来源
        self._last_render = 0.0
        self._reasoning_chars = 0
        self._reasoning_noted = False
        self._plain_printed = False  # 管道直出过正文：收尾需补换行，防后续输出粘连

    def __enter__(self) -> StreamRenderer:
        if not self._plain:
            self._status = console.status("[faint]思考中…[/]", spinner="dots")
            self._status.start()
        return self

    def _stop_spinner(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _note_reasoning(self) -> None:
        """思考折叠行：一轮只落一次（首个正文到达时，或整轮无正文时的收尾）。"""
        if self._reasoning_chars and not self._reasoning_noted and not self._plain:
            console.print(f"✻ 思考 {self._reasoning_chars} 字", style="faint", markup=False)
            self._reasoning_noted = True

    def on_reasoning(self, text: str) -> None:
        """思考过程：不流出原文（碎片化的内部独白是 tty 最大乱源），只计数。

        完整推理仍进消息历史/轨迹（streaming.py 负责拼装），终端只留一行折叠摘要。
        管道模式完全静默：stdout 只保留正文答案，重定向结果干净可解析。
        """
        self._reasoning_chars += len(text)
        if self._status is not None:
            self._status.update(f"[faint]思考中…（{self._reasoning_chars} 字）[/]")

    def _land_completed(self) -> None:
        """把已完成的段永久落卷轴，Live 只保留进行中的尾段。

        为什么必须这么做：Live 的重绘是"光标上移 N 行重写"，N = 上次渲染高度；
        内容超过终端高度时，超出的行已滚入 scrollback，光标上移够不到真正的起点，
        每次刷新都在下方留下一份完整副本——长回答会随节流刷新重复几十次。
        落卷后 Live 区域永远只有一个段的高度，从机制上杜绝超高重绘。

        切割规则（fence 闭合 + 空行分界）收敛在 stream_segments.StreamSegmenter。
        Live 运行中的 console.print 会被 rich 渲染到 Live 区域上方（官方支持的模式）。
        """
        done = self._segments.take_completed()
        if not done:
            return
        console.print(Markdown(done))
        if self._live is not None:
            self._live.update(Markdown(self._segments.tail), refresh=False)
        self._last_render = time.monotonic()

    def on_content(self, text: str) -> None:
        """正文：tty Live 增量重排尾段；管道纯文本直出。"""
        if self._plain:
            print(text, end="", flush=True)
            self._plain_printed = True
            return
        if self._live is None:
            # 首个正文 delta：收 spinner、落思考折叠行、起 Live
            self._stop_spinner()
            self._note_reasoning()
            self._live = Live(
                Markdown(""),
                console=console,
                refresh_per_second=1 / LIVE_RENDER_INTERVAL,
                vertical_overflow="visible",
            )
            self._live.start()
        self._segments.feed(text)
        self._land_completed()
        if self._live is not None:
            now = time.monotonic()
            if now - self._last_render >= LIVE_RENDER_INTERVAL:
                self._live.update(Markdown(self._segments.tail), refresh=False)
                self._last_render = now

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_spinner()
        tail = self._segments.finish()
        if self._live is not None:
            # 终稿强制全量渲染一次，收掉节流期间的尾巴（此时只剩最后一个段）
            self._live.update(Markdown(tail), refresh=True)
            self._live.stop()
            self._live = None
            console.print()
        elif self._plain and self._plain_printed:
            print()  # 直出正文后补换行：后续 token 行/提示符不粘在正文末尾
        else:
            self._note_reasoning()  # 整轮无正文（纯工具调用）：折叠行在此落地
        return False


# ---- 事件消费（agent 内核事件流 → 终端渲染的桥）----


def consume(events) -> None:
    """终端消费者：把 agent 内核产出的事件流渲染到终端。

    内核（agent.py / streaming.py）不再 import ui——它是事件的生产者，
    这里是消费者之一（其余消费者：bench 的静默统计、将来的 Web GUI）。
    StreamRenderer 的生命周期由 StreamStart / StreamFinished 事件驱动；
    异常（如 Ctrl+C）也要收掉渲染器，防 Live 区域残留在终端上。

    Usage 事件缓存到 TurnEnd 只打印一次累计行——一轮对话 N 个 API 请求
    打 N 行 token 是 tty 的第二乱源。
    """
    renderer: StreamRenderer | None = None
    last_usage: Usage | None = None
    try:
        for ev in events:
            if isinstance(ev, StreamStart):
                renderer = StreamRenderer()
                renderer.__enter__()
            elif isinstance(ev, ReasoningDelta):
                if renderer:
                    renderer.on_reasoning(ev.text)
            elif isinstance(ev, TextDelta):
                if renderer:
                    renderer.on_content(ev.text)
                else:
                    print(ev.text, end="", flush=True)  # 防御：无渲染器时纯文本直出
            elif isinstance(ev, StreamFinished):
                if renderer:
                    renderer.__exit__(None, None, None)
                    renderer = None
            elif isinstance(ev, ToolCallStart):
                tool_call(ev.name, ev.arguments)
            elif isinstance(ev, ToolCallResult):
                tool_result(ev.preview)
            elif isinstance(ev, Note):
                note(ev.message, tag=ev.tag)
            elif isinstance(ev, Warn):
                warn(ev.message)
            elif isinstance(ev, Usage):
                last_usage = ev
            elif isinstance(ev, TurnEnd):
                if last_usage is not None:
                    token_line(last_usage.prompt, last_usage.completion, last_usage.cached, last_usage.total)
                    last_usage = None
    finally:
        if renderer is not None:
            renderer.__exit__(None, None, None)


def consume_quiet(events) -> None:
    """bench 专用静默消费者：只渲染工具调用与系统旁白，跳过 spinner/推理/Live/每轮 token。

    与 consume() 的区别：不启动 StreamRenderer——TextDelta / ReasoningDelta / Usage
    一律跳过（它们是交互式流式渲染的原料，bench 无人值守场景里是纯噪音）。
    完整轨迹已由 TraceRecorder 落盘 .trace.jsonl，终端只留可快速扫读的摘要。
    """
    for ev in events:
        if isinstance(ev, ToolCallStart):
            tool_call(ev.name, ev.arguments)
        elif isinstance(ev, ToolCallResult):
            tool_result(ev.preview)
        elif isinstance(ev, Note):
            note(ev.message, tag=ev.tag)
        elif isinstance(ev, Warn):
            warn(ev.message)
