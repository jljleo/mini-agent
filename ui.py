"""TUI 组件库：全程序唯一的界面层，所有终端渲染统一走这里。

设计：
    - 单一 Console + 语义化 Theme：业务代码只调 banner/warn/tool_call 等语义接口，
      不再散落 \\033 转义码；调整配色只改 THEME 一处
    - StreamRenderer：一轮 API 响应的渲染管线——spinner（等首字）→
      思考过程暗色预览 → 正文 Markdown 流式渲染（Live 增量重排）
    - 工具调用可视化采用 Claude Code 风格 ⏺ / ⎿：美观之外仍是"幻觉测谎仪"
    - 非 tty（管道/重定向）自动降级为纯文本直出：rich 自动去色，
      Live/spinner 不启用，管道输出保持干净可解析

动态文本一律用 Text/markup=False 渲染，杜绝模型输出里的 "[xxx]" 被当 markup 解析。
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

# 思考过程（reasoning）在终端最多展示的字符数，超出以 " …" 收尾。
# 仅影响显示：写入消息历史的完整推理不受影响（streaming.py 负责拼装）
REASONING_PREVIEW_CHARS = 600
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


def banner(model: str, cwd: str) -> None:
    """启动横幅：品牌 + 关键上下文（模型/目录）+ 最小上手提示。"""
    if not console.is_terminal:
        console.print(f"mini-agent · {model}")
        return
    console.print()
    console.print(
        Panel(
            f"[bold accent]✦ mini-agent[/]\n\n"
            f"[muted]模型[/]  {escape(model)}\n"
            f"[muted]目录[/]  {escape(cwd)}\n\n"
            f"[faint]输入问题开始对话 · /help 查看命令 · exit / Ctrl+C 退出[/]",
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
    """一轮 API 响应的渲染管线：spinner → 思考暗色预览 → 正文 Markdown Live。

    用法（agent.chat 工具循环内，每轮请求一个实例）：
        with StreamRenderer() as renderer:
            completion = client.chat.completions.create(..., stream=True)
            messages, usage = stream_and_assemble(completion, renderer)

    spinner 覆盖 connect + TTFT 的"静默尴尬期"；非 tty 自动降级为纯文本直出。
    """

    def __init__(self) -> None:
        self._plain = not console.is_terminal
        self._status = None
        self._live: Live | None = None
        self._markdown = ""
        self._last_render = 0.0
        self._reasoning_shown = 0
        self._reasoning_truncated = False

    def __enter__(self) -> "StreamRenderer":
        if not self._plain:
            self._status = console.status("[faint]思考中…[/]", spinner="dots")
            self._status.start()
        return self

    def _stop_spinner(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # streaming.py 的回调（chunk 到达即触发）

    def on_reasoning(self, text: str) -> None:
        """思考过程：暗色斜体流式预览，超上限截断（完整内容仍进消息历史）。

        管道模式下不输出：stdout 只保留正文答案，重定向结果干净可解析。
        """
        self._stop_spinner()
        if self._plain:
            return
        if self._reasoning_shown >= REASONING_PREVIEW_CHARS:
            self._reasoning_truncated = True
            return
        remaining = REASONING_PREVIEW_CHARS - self._reasoning_shown
        chunk = text[:remaining]
        self._reasoning_shown += len(chunk)
        if len(chunk) < len(text):
            self._reasoning_truncated = True
        console.print(chunk, end="", style="reasoning", markup=False, soft_wrap=True)

    def on_content(self, text: str) -> None:
        """正文：tty 下 Markdown Live 增量渲染；管道下纯文本直出。"""
        self._stop_spinner()
        if self._plain:
            print(text, end="", flush=True)
            return
        if self._live is None:
            if self._reasoning_shown:
                # 思考与正文之间留白；被截断的思考以省略号收尾
                console.print(" …" if self._reasoning_truncated else "")
            self._live = Live(
                Markdown(""),
                console=console,
                refresh_per_second=1 / LIVE_RENDER_INTERVAL,
                vertical_overflow="visible",
            )
            self._live.start()
        self._markdown += text
        now = time.monotonic()
        if now - self._last_render >= LIVE_RENDER_INTERVAL:
            self._live.update(Markdown(self._markdown), refresh=False)
            self._last_render = now

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_spinner()
        if self._live is not None:
            # 终稿强制全量渲染一次，收掉节流期间的尾巴
            self._live.update(Markdown(self._markdown), refresh=True)
            self._live.stop()
            self._live = None
            console.print()
        elif self._reasoning_shown:
            console.print(" …" if self._reasoning_truncated else "")
        return False
