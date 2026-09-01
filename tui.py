"""Textual TTY 前端：输出 viewport + 底部固定 dock。

边界：本文件是 events.py 的消费者；不改 agent 内核，不把业务逻辑搬进 UI。
非 TTY 管道模式仍在 main.py / ui.py，不走这里。
"""

import io
import os
import threading
import time
from queue import Empty

# iTerm2 + Kitty 键盘协议的已知 IME 缺陷：中文候选选择会插入数字而非汉字。
# 禁用 Kitty 协议后回退到传统转义序列，中文/日文/带重音字符在 iTerm2 可正常输入。
# 须在 import textual 前生效（constants.py 模块级读取该开关）。
# 本应用按键绑定只有 ctrl+c/ctrl+d/esc 等标准键，不依赖 Kitty 的按键消歧。
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

from rich.console import Console
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Markdown, Static

import ui
import commands  # noqa: F401  集中式注册：导入即触发 @command 注册（不依赖 main.py 的副作用导入）
from agent import ChatSession
from command_registry import COMMANDS
from config import MODEL, PROJECT_ROOT, QUIT_COMMANDS
from events import (
    Note,
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnControl,
    TurnEnd,
    Usage,
    Warn,
)
from input_utils import sanitize
from tui_render import render_event

_REASONING_PREVIEW_LIMIT = 600
_STREAM_FLUSH_INTERVAL = 0.08  # 秒；TextDelta 节流，避免每个小 delta 全量 Markdown.update
_STREAM_FLUSH_CHARS = 2000     # 累积到该字符数也立即 flush


def _split_complete(text: str) -> tuple[str, str]:
    """把已完成块从流式缓冲里切出来，返回 (完成部分, 尾部)。

    只有 ``` 成对（偶数个）且存在空行分界时才切割：未闭合 fence 的 Markdown
    单独渲染会错乱，必须等它闭合。完成块落卷后永不重排，只剩尾部参与增量渲染，
    从机制上杜绝"每 chunk 全量重排长回答"的 O(n²) 抖动（同 StreamRenderer 落卷）。
    """
    if text.count("```") % 2 == 1:
        return "", text
    idx = text.rfind("\n\n")
    if idx <= 0:
        return "", text
    return text[:idx + 2], text[idx + 2:]


class KernelEvent(Message):
    def __init__(self, event) -> None:
        super().__init__()
        self.event = event


class KernelDone(Message):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error


class TranscriptView(VerticalScroll):
    """追加式 transcript：离散事件写 Static；流式正文聚合到一个 Markdown 块。"""

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._blocks: list[str] = []
        self._stream_widget: Markdown | None = None
        self._stream_parts: list[str] = []
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._stream_last_flush = 0.0
        self._reasoning_widget: Static | None = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False

    def text_content(self) -> str:
        return "\n".join(self._blocks)

    def clear(self) -> None:
        self._blocks.clear()
        self.remove_children()
        self._stream_widget = None
        self._stream_parts = []
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False

    def write(self, renderable) -> None:
        text = renderable.plain if isinstance(renderable, Text) else str(renderable)
        self._blocks.append(text)
        self.mount(Static(renderable))
        self.anchor()

    def begin_stream(self) -> None:
        self._stream_parts = []
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._stream_last_flush = time.monotonic()
        self._stream_widget = Markdown("")
        self.mount(self._stream_widget)
        self.anchor()

    def _flush_stream(self) -> None:
        if self._stream_widget is None or not self._stream_dirty:
            return
        full = "".join(self._stream_parts)
        done, tail = _split_complete(full)
        if done:
            # 完成块永久落卷（插在流式块之前），尾部继续增量重排
            self._blocks.append(done)
            self.mount(Markdown(done), before=self._stream_widget)
            self._stream_parts = [tail]
            self._stream_widget.update(tail)
        else:
            self._stream_widget.update(full)
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._stream_last_flush = time.monotonic()
        self.anchor()

    def _schedule_flush(self) -> None:
        def flush_and_reschedule() -> None:
            self._flush_stream()
            # 流活跃期间始终续命（而非只在 dirty 时）：否则定时器触发瞬间若
            # dirty 已被 append_text 的立即 flush 清掉，链条即断——后续慢速
            # 流只能靠 2000 字符阈值兜底，尾部会滞留。无新内容时的 flush 是
            # 空操作（_flush_stream 首行 early return），代价可忽略。
            if self._stream_widget is not None:
                self._schedule_flush()

        self.set_timer(_STREAM_FLUSH_INTERVAL, flush_and_reschedule)

    def append_text(self, text: str) -> None:
        if self._stream_widget is None:
            self.begin_stream()
            self._schedule_flush()
        self._stream_parts.append(text)
        self._stream_dirty = True
        self._stream_pending_chars += len(text)
        if (
            self._stream_pending_chars >= _STREAM_FLUSH_CHARS
            or time.monotonic() - self._stream_last_flush >= _STREAM_FLUSH_INTERVAL
        ):
            self._flush_stream()

    def append_reasoning(self, text: str) -> None:
        if self._reasoning_widget is None:
            self._reasoning_widget = Static("", classes="reasoning")
            self.mount(self._reasoning_widget)
        remaining = max(0, _REASONING_PREVIEW_LIMIT - self._reasoning_chars)
        chunk = text[:remaining]
        self._reasoning_chars += len(chunk)
        if len(text) > remaining:
            self._reasoning_truncated = True
        if chunk:
            self._reasoning_text += chunk
            self._reasoning_widget.update(Text(self._reasoning_text, style="grey42 italic"))
        self.anchor()

    def finish_stream(self) -> None:
        self._flush_stream()
        if self._stream_widget is not None:
            self._blocks.append("".join(self._stream_parts))
        if self._reasoning_truncated and self._reasoning_widget is not None:
            self._reasoning_widget.update(Text(self._reasoning_text + " …", style="grey42 italic"))
        self._stream_widget = None
        self._stream_parts = []
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False


class Dock(Vertical):
    """底部固定区域：审批、排队预览、状态栏、输入框。"""

    def __init__(self) -> None:
        super().__init__(id="dock")
        self._queued: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="approval")
        yield Static("", id="queued")
        yield Static(f"{MODEL} · tokens 0", id="status")
        yield Input(placeholder="输入问题开始对话 · /help 查看命令", id="prompt")

    def on_mount(self) -> None:
        self.query_one("#approval", Static).display = False
        self.query_one("#queued", Static).display = False

    def queued_text(self) -> str:
        if not self._queued:
            return ""
        first = self._queued[0]
        return f"已排队 {len(self._queued)} 条：{first[:60]}{'…' if len(first) > 60 else ''}"

    def set_queued(self, items: list[str]) -> None:
        self._queued = list(items)
        widget = self.query_one("#queued", Static)
        widget.display = bool(items)
        widget.update(Text(self.queued_text(), style="grey62"))

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text, style="grey62"))

    def show_approval(self, command: str, dangerous: bool) -> None:
        widget = self.query_one("#approval", Static)
        style = "bright_red" if dangerous else "yellow"
        label = "危险命令" if dangerous else "需要确认"
        widget.update(Text(f"⚠ {label} — {command}（按 y 执行，其余键拒绝）", style=style))
        widget.display = True

    def hide_approval(self) -> None:
        self.query_one("#approval", Static).display = False


class TextualApprovalChannel:
    """input_utils.set_approval_channel 的 Textual 实现：工具线程阻塞等待，UI 按键应答。"""

    def __init__(self, app: "MiniAgentApp") -> None:
        self.app = app
        self._answered = threading.Event()
        self._result = False
        self._pending = False
        self._lock = threading.Lock()

    @property
    def has_pending(self) -> bool:
        return self._pending

    def ask(self, command: str, dangerous: bool, timeout: int) -> bool:
        with self._lock:  # 串行化，避免重入
            self._pending = True
            self._result = False
            self._answered.clear()
            self.app.call_from_thread(self.app.show_approval, command, dangerous)
            answered = self._answered.wait(timeout)
            self._pending = False
            self.app.call_from_thread(self.app.hide_approval)
            return self._result if answered else False

    def answer(self, yes: bool) -> None:
        if not self._pending:
            return
        self._result = yes
        self._answered.set()


class MiniAgentApp(App):
    CSS = """
    Screen { layout: vertical; }
    #transcript { height: 1fr; padding: 0 1; }
    #dock { height: auto; padding: 0 1; }
    #approval { padding: 1 0; }
    #queued { padding: 0 0 1 0; }
    #status { color: grey; padding: 0 0 1 0; }
    #prompt { border: none; }
    .reasoning { color: grey; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_exit", show=False),
        Binding("ctrl+d", "exit", show=False),
        Binding("escape", "interrupt_or_exit", show=False),
    ]

    def __init__(self, session: ChatSession) -> None:
        super().__init__()
        self.session = session
        self.control: TurnControl | None = None
        self.running = False
        self._worker: threading.Thread | None = None
        self._mark = 0
        self._queued: list[str] = []
        self.approval = TextualApprovalChannel(self)

    def compose(self) -> ComposeResult:
        self.transcript = TranscriptView()
        self.dock = Dock()
        yield self.transcript
        yield self.dock

    def on_mount(self) -> None:
        from input_utils import set_approval_channel

        set_approval_channel(self.approval)
        self.query_one("#prompt", Input).focus()
        self.dock.set_status(self.session.status_text())

    def show_approval(self, command: str, dangerous: bool) -> None:
        self.dock.show_approval(command, dangerous)
        self.query_one("#prompt", Input).disabled = True

    def hide_approval(self) -> None:
        self.dock.hide_approval()
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()

    def on_key(self, event) -> None:
        # 仅审批 pending 时消费 y/n；此时 prompt 已 disabled，按键可达 app 层。
        if self.approval.has_pending and event.key.lower() in ("y", "n"):
            self.approval.answer(event.key.lower() == "y")
            event.stop()

    def action_approval_yes(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(True)

    def action_approval_no(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(False)

    def on_unmount(self) -> None:
        # 释放可能挂起的审批等待者，避免 ask() 等满 timeout。
        self.approval.answer(False)

    def action_interrupt_or_exit(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(False)
            return
        if self.running and self.control is not None:
            self.control.abort()
            return
        self.exit()

    def submit_text(self, raw: str) -> None:
        question = sanitize(raw)
        if not question:
            return
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""

        if self.running and self.control is not None:
            self.control.steer.put(question)
            self._queued.append(question)
            self.dock.set_queued(self._queued)
            return

        verdict = self._dispatch_command(question)
        if verdict == "quit":
            self.exit()
            return
        if verdict is True:
            return
        if verdict == "prefill":
            prompt.value = question
            return

        self._start_turn(question)

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        self.submit_text(event.value)

    def _dispatch_command(self, question: str):
        if question.lower() in QUIT_COMMANDS:
            return "quit"
        name, _, args = question.partition(" ")
        if name in COMMANDS:
            self._run_slash_command(name, args.strip())
            return True
        if question.startswith("/") and "/" not in name[1:]:
            self.transcript.write(Text(f"未知命令: {name}（输入 /help 查看可用命令）", style="yellow"))
            return "prefill"
        return False

    def _run_slash_command(self, name: str, args: str) -> None:
        buffer = io.StringIO()
        old_console = ui.console
        ui.console = Console(
            file=buffer,
            force_terminal=False,
            width=max(40, self.size.width - 2),
            theme=ui.THEME,
            highlight=False,
        )
        try:
            COMMANDS[name](self.session, args)
        finally:
            ui.console = old_console
        output = buffer.getvalue().rstrip()
        if output:
            self.transcript.write(Text(output))
        if name == "/clear":
            self.transcript.clear()
            self._queued.clear()
            self.dock.set_queued([])
        self.dock.set_status(self.session.status_text())

    def _start_turn(self, question: str) -> None:
        self.running = True
        self.control = TurnControl()
        self._mark = self.session.mark()
        self.transcript.write(Text(f"❯ {question}", style="bold bright_cyan"))
        self._worker = threading.Thread(target=self._pump, args=(question, self.control), daemon=True)
        self._worker.start()

    def _pump(self, question: str, control: TurnControl) -> None:
        try:
            for event in self.session.chat(question, control=control):
                self.call_from_thread(self.post_message, KernelEvent(event))
            self.call_from_thread(self.post_message, KernelDone())
        except Exception as error:
            self.call_from_thread(self.post_message, KernelDone(error))

    @on(KernelEvent)
    def _on_kernel_event(self, message: KernelEvent) -> None:
        event = message.event
        if isinstance(event, StreamStart):
            self.transcript.begin_stream()
        elif isinstance(event, ReasoningDelta):
            self.transcript.append_reasoning(event.text)
        elif isinstance(event, TextDelta):
            self.transcript.append_text(event.text)
        elif isinstance(event, StreamFinished):
            self.transcript.finish_stream()
        elif isinstance(event, (ToolCallStart, ToolCallResult, Note, Warn, Usage)):
            rendered = render_event(event)
            if rendered is not None:
                self.transcript.write(rendered)
            if isinstance(event, Usage):
                self.dock.set_status(self.session.status_text())
        elif isinstance(event, TurnEnd):
            pass

    @on(KernelDone)
    def _on_kernel_done(self, message: KernelDone) -> None:
        self.running = False
        self.transcript.finish_stream()
        if message.error is None:
            self.session.save()
        else:
            self.session.rollback(self._mark)
            self.transcript.write(Text(f"✗ {type(message.error).__name__}: {message.error}", style="bright_red"))

        leftover = []
        if self.control is not None:
            while True:
                try:
                    leftover.append(self.control.steer.get_nowait())
                except Empty:
                    break
        self.control = None
        self._queued.clear()
        self.dock.set_queued([])
        if leftover:
            self.query_one("#prompt", Input).value = " ".join(leftover)
        self.query_one("#prompt", Input).focus()
        self.dock.set_status(self.session.status_text())


def run(session: ChatSession) -> None:
    MiniAgentApp(session).run()
