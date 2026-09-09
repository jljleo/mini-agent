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
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

import commands  # noqa: F401  集中式注册：导入即触发 @command 注册（不依赖 main.py 的副作用导入）
import ui
from agent import ChatSession
from command_registry import COMMANDS
from config import MODEL, QUIT_COMMANDS, format_context_tokens, get_profile, list_profiles
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
from stream_segments import StreamSegmenter
from tui_render import _format_args, render_event

_REASONING_PREVIEW_LIMIT = 600
_STREAM_FLUSH_INTERVAL = 0.08  # 秒；TextDelta 节流，避免每个小 delta 全量 Markdown.update
_STREAM_FLUSH_CHARS = 2000     # 累积到该字符数也立即 flush
_LOADER_INTERVAL = 0.1         # 秒；loader/工具调用 spinner 的动画帧间隔
_LOADER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille 旋转帧（pi 风格 loader）


class KernelEvent(Message):
    def __init__(self, event) -> None:
        super().__init__()
        self.event = event


class KernelDone(Message):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error


class ModelSelected(Message):
    def __init__(self, profile_name: str) -> None:
        super().__init__()
        self.profile_name = profile_name


class Loader(Static):
    """等待期活动组件（pi 风格）：braille spinner + 状态文本 + 耗时。

    StreamStart 后挂上，首个内容（推理/正文）到达或流结束时撤掉——
    消除 TTFT 期间的死屏。
    """

    def __init__(self, label: str = "思考中…") -> None:
        super().__init__("")
        self._label = label
        self._frame = 0
        self._started = time.monotonic()

    def on_mount(self) -> None:
        self._tick()
        self.set_interval(_LOADER_INTERVAL, self._tick)

    def _tick(self) -> None:
        spinner = _LOADER_FRAMES[self._frame % len(_LOADER_FRAMES)]
        self._frame += 1
        elapsed = time.monotonic() - self._started
        self.update(Text(f"{spinner} {self._label} {elapsed:.1f}s", style="grey62"))


class ToolCallView(Static):
    """工具调用活动组件：执行中 spinner 动画，完成后原地折叠为 ⏺/⎿ 两行。

    pi 风格的生命周期：mount 即开始转圈（⏺ 位置是动画帧），
    finish(preview) 后定格为 ⏺ name(args) + ⎿ 结果摘要，此后不再重绘。
    """

    def __init__(self, name: str, arguments: str) -> None:
        super().__init__("")
        self._name = name
        self._arguments = arguments
        self._preview: str | None = None
        self._frame = 0

    def on_mount(self) -> None:
        self._tick()
        self._timer = self.set_interval(_LOADER_INTERVAL, self._tick)

    def _head(self, bullet: str, bullet_style: str) -> Text:
        head = Text()
        head.append(f"{bullet} ", style=bullet_style)
        head.append(self._name or "?", style="bold")
        head.append(f"({_format_args(self._arguments)})", style="grey42")
        return head

    def _tick(self) -> None:
        if self._preview is not None:
            return
        spinner = _LOADER_FRAMES[self._frame % len(_LOADER_FRAMES)]
        self._frame += 1
        self.update(self._head(spinner, "cyan"))

    def finish(self, preview: str) -> None:
        """结果到达：定格为最终形态（⏺ 头行 + ⎿ 单行摘要）。"""
        self._preview = preview
        self._timer.stop()
        final = self._head("⏺", "cyan")
        one_line = " ⏎ ".join(preview.splitlines())
        final.append("\n  ⎿  ", style="grey42")
        final.append(one_line, style="grey62")
        self.update(final)


class TranscriptView(VerticalScroll):
    """追加式 transcript：线性流水线——完成的段冻结落卷，只有当前生长中的组件更新。

    段序列按时间线性追加：Loader（等待期）→ reasoning（暗色）→ Markdown 正文段
    → ToolCallView（活动组件）→ 下一轮 Loader…。历史段永不重排；
    切割规则（fence 闭合 + 空行分界）由 stream_segments.StreamSegmenter 统一裁决。
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._blocks: list[str] = []
        self._segments = StreamSegmenter()
        self._stream_widget: Markdown | None = None
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._stream_last_flush = 0.0
        self._loader: Loader | None = None
        self._active_tools: list[ToolCallView] = []
        self._reasoning_widget: Static | None = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False

    def text_content(self) -> str:
        return "\n".join(self._blocks)

    def clear(self) -> None:
        self._blocks.clear()
        self.remove_children()
        self._segments = StreamSegmenter()
        self._stream_widget = None
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._loader = None
        self._active_tools = []
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False

    def write(self, renderable) -> None:
        text = renderable.plain if isinstance(renderable, Text) else str(renderable)
        self._blocks.append(text)
        self.mount(Static(renderable))
        self.anchor()

    # ---- 等待期 loader ----

    def _show_loader(self, label: str = "思考中…") -> None:
        self._dismiss_loader()
        self._loader = Loader(label)
        self.mount(self._loader)
        self.anchor()

    def _dismiss_loader(self) -> None:
        if self._loader is not None:
            self._loader.remove()
            self._loader = None

    # ---- 正文流式段 ----

    def begin_stream(self) -> None:
        """一轮 API 响应开始：挂 loader，等首个 delta 到达再替换成正文组件。"""
        self._segments = StreamSegmenter()
        self._stream_widget = None
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._stream_last_flush = time.monotonic()
        self._show_loader()

    def _ensure_stream_widget(self) -> None:
        if self._stream_widget is None:
            self._dismiss_loader()
            self._stream_widget = Markdown("")
            self.mount(self._stream_widget)
            self._schedule_flush()

    def _flush_stream(self) -> None:
        if self._stream_widget is None or not self._stream_dirty:
            return
        done = self._segments.take_completed()
        if done:
            # 完成段永久落卷（插在流式段之前），尾段继续增量重排
            self._blocks.append(done)
            self.mount(Markdown(done), before=self._stream_widget)
        self._stream_widget.update(self._segments.tail)
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
        self._ensure_stream_widget()
        self._segments.feed(text)
        self._stream_dirty = True
        self._stream_pending_chars += len(text)
        if (
            self._stream_pending_chars >= _STREAM_FLUSH_CHARS
            or time.monotonic() - self._stream_last_flush >= _STREAM_FLUSH_INTERVAL
        ):
            self._flush_stream()

    def append_reasoning(self, text: str) -> None:
        if self._reasoning_widget is None:
            self._dismiss_loader()
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

    # ---- 工具调用活动组件 ----

    def begin_tool_call(self, name: str, arguments: str) -> None:
        """ToolCallStart：挂活动组件，执行期间转圈。"""
        self._dismiss_loader()
        widget = ToolCallView(name, arguments)
        self._active_tools.append(widget)
        self._blocks.append(f"⏺ {name}({_format_args(arguments)})")
        self.mount(widget)
        self.anchor()

    def finish_tool_call(self, preview: str) -> None:
        """ToolCallResult：最旧的活动组件定格为 ⏺/⎿（工具按序执行，FIFO 配对）。"""
        if not self._active_tools:
            # 防御：无配对 start（异常路径）时退化为静态行，内容不丢
            rendered = render_event(ToolCallResult("?", preview))
            if rendered is not None:
                self.write(rendered)
            return
        widget = self._active_tools.pop(0)
        widget.finish(preview)
        self._blocks.append(f"  ⎿  {preview}")
        self.anchor()

    # ---- 收尾 ----

    def finish_stream(self) -> None:
        self._dismiss_loader()  # 整轮无正文（纯工具调用）时 loader 不能残留
        self._flush_stream()
        if self._stream_widget is not None:
            self._blocks.append(self._segments.tail)
        if self._reasoning_truncated and self._reasoning_widget is not None:
            self._reasoning_widget.update(Text(self._reasoning_text + " …", style="grey42 italic"))
        self._segments = StreamSegmenter()
        self._stream_widget = None
        self._stream_dirty = False
        self._stream_pending_chars = 0
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0
        self._reasoning_truncated = False


class Dock(Vertical):
    """底部固定区域：审批、排队预览、状态栏、输入框。"""

    # 补全可见时：上下键在输入框内直接移动补全高亮（Input 本身无 up/down 绑定，不冲突）
    BINDINGS = [
        Binding("up", "completion_up", show=False),
        Binding("down", "completion_down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="dock")
        self._queued: list[str] = []
        self._model_mode = False
        self._model_explicitly_selected = False

    def compose(self) -> ComposeResult:
        yield Static("", id="approval")
        yield Static("", id="queued")
        yield Static(f"{MODEL} · ctx 0.0%/{format_context_tokens()} · tokens 0", id="status")
        yield OptionList(id="completion")
        yield Input(placeholder="输入 / 查看命令 · 问题直接开始对话", id="prompt")

    def on_mount(self) -> None:
        self.query_one("#approval", Static).display = False
        self.query_one("#queued", Static).display = False
        self.query_one("#completion", OptionList).display = False

    # ---- 斜杠命令补全（迁移自 prompt_toolkit 的 SlashCommandCompleter）----

    def _update_completion(self, value: str) -> None:
        """输入以 / 开头时，弹出匹配命令的下拉提示；否则收起。

        特殊处理 /model：直接在下拉框里列出可选档案，像命令补全一样上下选择。
        """
        completion = self.query_one("#completion", OptionList)
        if not value.startswith("/"):
            completion.display = False
            self._model_mode = False
            return

        # /model 无参数时：列出档案供选择
        if value == "/model":
            self._model_mode = True
            self._model_explicitly_selected = False
            profiles = list_profiles()
            current = self.app.session.profile_name
            max_name_len = max(len(p) for p in profiles)
            max_model_len = max(len(get_profile(p)["model"]) for p in profiles)
            options = []
            for pname in profiles:
                profile = get_profile(pname)
                is_current = pname == current
                marker = Text("●", style="green" if is_current else "dim")
                name_text = Text(f"{pname:<{max_name_len}}", style="bold" if is_current else "")
                model_text = Text(f"{profile['model']:<{max_model_len}}", style="dim")
                ctx_text = Text(
                    f"ctx {format_context_tokens(profile['context_tokens']):>6}", style="cyan"
                )
                label = Text.assemble(marker, "  ", name_text, "  ", model_text, "  ", ctx_text)
                options.append(Option(label, id=pname))
            completion.clear_options()
            completion.add_options(options)
            completion.highlighted = 0
            completion.display = True
            return

        self._model_mode = False
        # /quit 不在 COMMANDS（走主循环退出词表），补全里单独补上
        candidates = {**COMMANDS, "/quit": None}
        options = [
            Option(f"{name}  {fn.description if fn is not None else '退出程序'}", id=name)
            for name, fn in candidates.items()
            if name.startswith(value) and name != value
        ]
        completion.clear_options()
        if options:
            completion.add_options(options)
            completion.highlighted = 0
            completion.display = True
        else:
            completion.display = False

    def action_completion_up(self) -> None:
        completion = self.query_one("#completion", OptionList)
        if completion.display:
            completion.action_cursor_up()
            if self._model_mode:
                self._model_explicitly_selected = True

    def action_completion_down(self) -> None:
        completion = self.query_one("#completion", OptionList)
        if completion.display:
            completion.action_cursor_down()
            if self._model_mode:
                self._model_explicitly_selected = True

    def selected_completion_id(self) -> str | None:
        """返回当前高亮的补全项 id（命令名/档案名）；无高亮返回 None。"""
        completion = self.query_one("#completion", OptionList)
        highlighted = completion.highlighted_option
        return highlighted.id if highlighted is not None else None

    def is_model_selection(self) -> bool:
        """当前补全列表是否处于 /model 档案选择模式。"""
        return self._model_mode

    def model_explicitly_selected(self) -> bool:
        """用户在 /model 列表里用上下键移动过（或点击过），表示确实想选档案。"""
        return self._model_explicitly_selected

    def has_completion(self) -> bool:
        return self.query_one("#completion", OptionList).display

    def hide_completion(self) -> None:
        self.query_one("#completion", OptionList).display = False

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._update_completion(event.value)

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self._model_mode:
            self.post_message(ModelSelected(event.option_id))
        else:
            # 鼠标点击补全项：直接执行，不要只回填再让用户按第二次回车
            self.app.submit_text(event.option_id)

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
        if self.dock.has_completion():
            self.dock.hide_completion()
            self.query_one("#prompt", Input).focus()
            return
        if self.running and self.control is not None:
            self.control.abort()
            return
        self.exit()

    def submit_text(self, raw: str) -> None:
        # 补全可见时：回车选中当前高亮项并直接执行（一键完成）。
        # 模型选择模式单独处理：Enter 切换档案；命令模式 Enter 执行命令。
        if self.dock.has_completion():
            selected = self.dock.selected_completion_id()
            if selected is not None:
                if self.dock.is_model_selection():
                    if self.dock.model_explicitly_selected():
                        self._switch_profile(selected)
                    else:
                        # 只是输入了 /model 就按回车：展示静态列表，不直接切到第一个档案
                        self.dock.hide_completion()
                        prompt = self.query_one("#prompt", Input)
                        prompt.value = ""
                        self._run_slash_command("/model", "")
                else:
                    prompt = self.query_one("#prompt", Input)
                    prompt.value = selected
                    self.dock.hide_completion()
                    if selected == "/model":
                        # 从命令补全里选中 /model 时：打开档案补全供上下选择，
                        # 而不是直接执行静态列表
                        self.dock._update_completion(selected)
                    else:
                        self.submit_text(selected)
                return

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
            self.transcript.write(Text(f"未知命令: {name}（输入 / 查看命令）", style="yellow"))
            return "prefill"
        return False

    def _switch_profile(self, profile_name: str) -> None:
        try:
            self.session.set_profile(profile_name)
        except RuntimeError as exc:
            self.transcript.write(Text(str(exc), style="yellow"))
            return
        self.dock.set_status(self.session.status_text())
        self.transcript.write(
            Text(f"已切换模型档案：{profile_name}（{self.session.profile['model']}）")
        )
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""
        self.dock.hide_completion()
        prompt.focus()

    @on(ModelSelected)
    def _on_model_selected(self, event: ModelSelected) -> None:
        self._switch_profile(event.profile_name)

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
        elif isinstance(event, ToolCallStart):
            self.transcript.begin_tool_call(event.name, event.arguments)
        elif isinstance(event, ToolCallResult):
            self.transcript.finish_tool_call(event.preview)
        elif isinstance(event, (Note, Warn, Usage)):
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
