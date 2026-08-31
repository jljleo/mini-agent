# Textual TUI 追加消息体验重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TTY 前端从 prompt_toolkit 常驻输入框迁移到 Textual 全屏布局，让输出区独立滚动、输入/确认/状态固定在底部 dock，同时保留管道模式。

**Architecture:** 内核 `agent.py`/`streaming.py`/`compact.py` 不改事件契约。TTY 新增 `tui.py` 作为 Textual 事件消费者；`tui_render.py` 放可单测的纯渲染映射；`main.py` 只按 `sys.stdin.isatty()` 分发到 Textual 或现有管道循环。斜杠命令继续复用 `commands.py`，在 Textual 里用临时 Rich Console 捕获输出后写入 transcript。

**Tech Stack:** Python 3.13、Textual、Rich、pytest、现有 OpenAI-compatible agent kernel。

---

## 文件结构

- Create: `requirements.txt` — 最小运行依赖清单；补齐 openai / python-dotenv / prompt_toolkit / rich / textual，保证新环境可复现。
- Create: `tui_render.py` — 纯函数：把非流式 `events.py` 事件映射成 Rich renderable，便于无 Textual 单测。
- Create: `tui.py` — Textual TTY 前端：`MiniAgentApp`、`TranscriptView`、`Dock`、`TextualApprovalChannel`。
- Modify: `main.py` — TTY 分支改为启动 `tui.run(session)`；删除 prompt_toolkit 常驻输入框路径；保留 `_pipe_loop`。
- Modify: `AGENTS.md` — 更新“没有包清单”的旧说法，记录 Textual 依赖与安装命令。
- Test: `tests/test_tui.py` — Textual headless UI 测试；不访问网络，不启动真实终端。

注意：本计划不包含 `git commit` 步骤；如要提交，必须由用户明确要求后单独执行。

### Task 1: 引入 Textual 依赖并更新仓库说明

**Files:**
- Create: `requirements.txt`
- Modify: `AGENTS.md`
- Test: `tests/test_tui.py`

- [x] **Step 1: 写一个会失败的依赖冒烟测试**

Create `tests/test_tui.py`:

```python
"""Textual TUI 前端测试。

约束：不启动真实终端、不访问网络；Textual headless run_test 只验证 UI 状态。
"""

import asyncio


def run(coro):
    return asyncio.run(coro)


def test_textual_dependency_available():
    import textual

    assert textual.__version__
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'textual'`

- [x] **Step 3: 添加最小依赖并安装**

Create `requirements.txt`:

```text
openai
python-dotenv
prompt_toolkit
rich
textual>=0.86
```

Run: `.venv/bin/pip install -r requirements.txt`
Expected: exit 0；随后 `.venv/bin/python -c "import textual; print(textual.__version__)"` 能打印版本号

- [x] **Step 4: 更新 AGENTS.md 的项目形态说明**

Replace this line in `AGENTS.md`:

```markdown
- 扁平的 Python 3.13 CLI agent；没有包清单、构建步骤、lint 配置或代码生成。使用仓库内虚拟环境：`.venv/bin/python`。
```

with:

```markdown
- 扁平的 Python 3.13 CLI agent；有最小 `requirements.txt`（openai / python-dotenv / prompt_toolkit / rich / textual），没有构建步骤、lint 配置或代码生成。使用仓库内虚拟环境：`.venv/bin/python`；新环境先 `.venv/bin/pip install -r requirements.txt`。
```

- [x] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: `1 passed`

### Task 2: 新增 `tui_render.py`，把非流式事件渲染逻辑做成纯函数

**Files:**
- Create: `tui_render.py`
- Test: `tests/test_tui.py`

- [x] **Step 1: 先写渲染契约测试**

Append to `tests/test_tui.py`:

```python
from events import Note, ToolCallResult, ToolCallStart, Usage, Warn
from tui_render import render_event


def test_render_tool_call_start_and_result():
    start = render_event(ToolCallStart("read_file", '{"path": "agent.py"}'))
    result = render_event(ToolCallResult("read_file", "ok"))

    assert "⏺ read_file" in start.plain
    assert "path" in start.plain
    assert "⎿" in result.plain
    assert "ok" in result.plain


def test_render_note_warn_usage_as_plain_text():
    assert render_event(Note("已瘦身 2 条", tag="compact")).plain == "[compact] 已瘦身 2 条"
    assert render_event(Warn("中断")).plain == "⚠ 中断"
    usage = render_event(Usage(10, 5, 3, 18))
    assert "prompt 10" in usage.plain
    assert "累计 18" in usage.plain
```

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tui_render'`

- [x] **Step 2: 实现 `tui_render.py`**

Create `tui_render.py`:

```python
"""Textual TUI 的纯渲染映射：把 events.py 的非流式事件转成 Rich renderable。

边界：本模块不 import ui、不碰 ChatSession、不处理 TextDelta/ReasoningDelta。
流式正文/推理由 tui.py 的 TranscriptView 聚合渲染；这里只处理离散事件。
"""

from rich.text import Text

from events import Note, ToolCallResult, ToolCallStart, Usage, Warn

_ARG_VALUE_LIMIT = 60


def _format_args(raw: str) -> str:
    import json

    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
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


def render_event(event):
    """把离散事件映射为 Rich Text；流式生命周期事件返回 None。"""
    if isinstance(event, ToolCallStart):
        return Text(f"⏺ {event.name}({_format_args(event.arguments)})", style="cyan")
    if isinstance(event, ToolCallResult):
        one_line = " ⏎ ".join(event.preview.splitlines())
        return Text(f"  ⎿  {one_line}", style="grey62")
    if isinstance(event, Note):
        return Text(f"[{event.tag}] {event.message}" if event.tag else event.message,
                    style="grey42")
    if isinstance(event, Warn):
        return Text(f"⚠ {event.message}", style="yellow")
    if isinstance(event, Usage):
        return Text(
            f"tokens · prompt {event.prompt:,} · completion {event.completion:,}"
            f"（缓存 {event.cached:,}）｜累计 {event.total:,}",
            style="grey42",
        )
    return None
```

- [x] **Step 3: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: `3 passed`

### Task 3: 新增 `tui.py` 基础布局、事件泵与 steering 排队预览

**Files:**
- Create: `tui.py`
- Test: `tests/test_tui.py`

- [x] **Step 1: 写 UI 行为测试（先失败）**

Append to `tests/test_tui.py`:

```python
from events import TurnControl


class FakeSession:
    def __init__(self):
        self.saved = False
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.messages = []

    def status_text(self):
        return "kimi-k3 · tokens 0"

    def mark(self):
        return len(self.messages)

    def rollback(self, mark):
        del self.messages[mark:]

    def save(self):
        self.saved = True

    def chat(self, question, control=None):
        raise AssertionError("UI 测试不应启动真实 chat")


def test_running_submit_queues_steering_and_shows_preview():
    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test() as pilot:
            app.running = True
            app.control = TurnControl()
            app.submit_text("追加一句")
            await pilot.pause()

            assert app.control.steer.get_nowait() == "追加一句"
            assert app.dock.queued_text() == "已排队 1 条：追加一句"
            assert app.query_one("#prompt").value == ""

    run(scenario())


def test_idle_submit_starts_turn_and_echoes_user_message():
    from tui import MiniAgentApp

    class ChatFakeSession(FakeSession):
        def chat(self, question, control=None):
            from events import TurnEnd
            self.messages.append({"role": "user", "content": question})
            yield TurnEnd()

    async def scenario():
        session = ChatFakeSession()
        app = MiniAgentApp(session=session)
        async with app.run_test() as pilot:
            app.submit_text("原始提问")
            await pilot.pause(0.1)

            assert session.messages[0]["content"] == "原始提问"
            assert session.saved is True
            assert app.running is False
            assert "❯ 原始提问" in app.transcript.text_content()

    run(scenario())
```

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tui'`

- [x] **Step 2: 实现 `tui.py` 基础版本**

Create `tui.py`:

```python
"""Textual TTY 前端：输出 viewport + 底部固定 dock。

边界：本文件是 events.py 的消费者；不改 agent 内核，不把业务逻辑搬进 UI。
非 TTY 管道模式仍在 main.py / ui.py，不走这里。
"""

import io
import threading

from rich.console import Console
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Markdown, Static

import ui
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
        self._reasoning_widget: Static | None = None
        self._reasoning_text = ""
        self._reasoning_chars = 0

    def text_content(self) -> str:
        return "\n".join(self._blocks)

    def clear(self) -> None:
        self._blocks.clear()
        self.remove_children()
        self._stream_widget = None
        self._stream_parts = []
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0

    def write(self, renderable) -> None:
        text = renderable.plain if isinstance(renderable, Text) else str(renderable)
        self._blocks.append(text)
        self.mount(Static(renderable))
        self.anchor()

    def begin_stream(self) -> None:
        self._stream_parts = []
        self._stream_widget = Markdown("")
        self.mount(self._stream_widget)
        self.anchor()

    def append_text(self, text: str) -> None:
        if self._stream_widget is None:
            self.begin_stream()
        self._stream_parts.append(text)
        self._stream_widget.update("".join(self._stream_parts))
        self.anchor()

    def append_reasoning(self, text: str) -> None:
        if self._reasoning_widget is None:
            self._reasoning_widget = Static("", classes="reasoning")
            self.mount(self._reasoning_widget)
        remaining = max(0, 600 - self._reasoning_chars)
        chunk = text[:remaining]
        self._reasoning_chars += len(chunk)
        if chunk:
            self._reasoning_text += chunk
            self._reasoning_widget.update(Text(self._reasoning_text, style="grey42 italic"))
        self.anchor()

    def finish_stream(self) -> None:
        if self._stream_widget is not None:
            self._blocks.append("".join(self._stream_parts))
        if self._reasoning_chars > 600 and self._reasoning_widget is not None:
            self._reasoning_widget.update(Text(self._reasoning_text + " …", style="grey42 italic"))
        self._stream_widget = None
        self._stream_parts = []
        self._reasoning_widget = None
        self._reasoning_text = ""
        self._reasoning_chars = 0


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

    @property
    def has_pending(self) -> bool:
        return self._pending

    def ask(self, command: str, dangerous: bool, timeout: int) -> bool:
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
        Binding("y", "approval_yes", show=False),
        Binding("n", "approval_no", show=False),
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

    def action_approval_yes(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(True)

    def action_approval_no(self) -> None:
        if self.approval.has_pending:
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
                except Exception:
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
```

- [x] **Step 3: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: `5 passed`

### Task 4: 审批通道与打断行为的 UI 测试

**Files:**
- Modify: `tui.py`
- Test: `tests/test_tui.py`

- [x] **Step 1: 写审批与打断测试（先失败）**

Append to `tests/test_tui.py`:

```python
import threading
import time


def test_approval_yes_unblocks_tool_thread():
    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test() as pilot:
            result = {}

            def asker():
                result["ok"] = app.approval.ask("ls", dangerous=False, timeout=2)

            thread = threading.Thread(target=asker)
            thread.start()
            for _ in range(100):
                if app.approval.has_pending:
                    break
                time.sleep(0.01)
            await pilot.pause()

            assert app.query_one("#approval").display is True
            app.action_approval_yes()
            thread.join(timeout=2)
            await pilot.pause()

            assert result["ok"] is True
            assert app.query_one("#approval").display is False

    run(scenario())


def test_ctrl_c_running_aborts_control():
    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test():
            app.running = True
            app.control = TurnControl()
            app.action_interrupt_or_exit()
            assert app.control.interrupt.is_set()

    run(scenario())
```

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: FAIL（`#approval` 的 display 初始不是 False，或 `action_interrupt_or_exit` 对已拒绝审批处理不符合预期）

- [x] **Step 2: 修正 `tui.py` 的审批显示与打断细节**

In `Dock.on_mount`, keep approval hidden but do not rely on CSS default:

```python
    def on_mount(self) -> None:
        approval = self.query_one("#approval", Static)
        approval.display = False
        queued = self.query_one("#queued", Static)
        queued.display = False
```

In `MiniAgentApp.action_interrupt_or_exit`, make pending approval rejection explicit and non-sticky:

```python
    def action_interrupt_or_exit(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(False)
            return
        if self.running and self.control is not None:
            self.control.abort()
            return
        self.exit()
```

In `MiniAgentApp.action_approval_no`, ensure any non-y key path only answers when pending:

```python
    def action_approval_no(self) -> None:
        if self.approval.has_pending:
            self.approval.answer(False)
```

- [x] **Step 3: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: `7 passed`

### Task 5: 接入 `main.py`，保留管道模式并删除旧 TTY 常驻输入框路径

**Files:**
- Modify: `main.py`
- Test: `tests/test_tui.py`

- [x] **Step 1: 写模式分发测试（先失败）**

Append to `tests/test_tui.py`:

```python
def test_main_dispatches_tty_to_textual(monkeypatch):
    import main

    called = {}

    class FakeTui:
        @staticmethod
        def run(session):
            called["tui"] = session

    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main, "ChatSession", lambda: object())
    monkeypatch.setattr(main.ui, "banner", lambda *args: None)
    monkeypatch.setattr(main, "set_status_provider", lambda fn: None)
    monkeypatch.setitem(__import__("sys").modules, "tui", FakeTui)

    main.main()

    assert "tui" in called
```

Run: `.venv/bin/python -m pytest tests/test_tui.py::test_main_dispatches_tty_to_textual -q`
Expected: FAIL（当前 `main.main()` 仍走 `_tui_loop`）

- [x] **Step 2: 修改 `main.py` 的模式分发**

Keep imports, but replace prompt_toolkit TTY imports and loops. The final `main.py` should keep `_dispatch_command` for pipe mode and simplify `main()`:

```python
"""CLI 入口：主循环与运行调度，业务逻辑下沉到 agent / input_utils / ui / tui。

运行：python main.py
退出：exit / quit / :q / /quit / Ctrl+C / Ctrl+D（运行中 Ctrl+C = 打断本轮，不退出）

两种形态：
- tty：Textual 全屏前端（tui.py）——输出 viewport 独立滚动，输入/确认/状态固定底部 dock
- 管道（_pipe_loop）：回合制读取 + 线程桥（bridge.py），无运行中交互
"""

import sys

import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册
import commands  # noqa: F401  集中式注册：导入即触发 @command 注册

import ui
from agent import ChatSession
from bridge import run_in_thread
from command_registry import COMMANDS
from config import MODEL, PROJECT_ROOT, QUIT_COMMANDS
from input_utils import read_input, set_status_provider


def _dispatch_command(session: ChatSession, question: str, forced: bool):
    """命令/退出词分发。返回 "quit" / "prefill" / True（已处理）/ False（应进入对话）。"""
    if not forced and question.lower() in QUIT_COMMANDS:
        ui.goodbye()
        return "quit"
    name, _, args = question.partition(" ")
    if not forced and name in COMMANDS:
        COMMANDS[name](session, args.strip())
        return True
    if not forced and question.startswith("/") and "/" not in name[1:]:
        ui.warn(f"未知命令: {name}（输入 /help 查看可用命令）")
        return "prefill"
    return False


def _pipe_loop(session: ChatSession) -> None:
    while True:
        try:
            question, forced = read_input()
        except (EOFError, KeyboardInterrupt):
            ui.goodbye()
            break

        if not question:
            continue
        verdict = _dispatch_command(session, question, forced)
        if verdict == "quit":
            break
        if verdict is True:
            continue
        if verdict == "prefill":
            continue

        mark = session.mark()
        events, _control = run_in_thread(lambda c: session.chat(question, control=c))
        try:
            ui.consume(events)
            session.save()
        except KeyboardInterrupt:
            ui.warn("已强制中断并退出（本轮未存档）")
            ui.goodbye()
            break
        except Exception as e:
            session.rollback(mark)
            ui.error(f"{type(e).__name__}: {e}")


def main() -> None:
    session = ChatSession()
    set_status_provider(session.status_text)
    ui.banner(MODEL, PROJECT_ROOT)

    if sys.stdin.isatty():
        import tui

        tui.run(session)
    else:
        _pipe_loop(session)


if __name__ == "__main__":
    main()
```

Delete from `main.py`: `patch_stdout` import, `ApprovalChannel` import, `abort_pending_approval` import, `begin_run` import, `current_control` import, `end_run` import, `is_running` import, `set_approval_channel` import, `take_prefill` import, `_run_turn`, `_tui_loop`.

- [x] **Step 3: 运行新增测试与全量回归**

Run: `.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: `8 passed`

Run: `.venv/bin/python -m pytest -q`
Expected: `158 passed`

### Task 6: 收尾一致性与文档同步

**Files:**
- Modify: `AGENTS.md`
- Modify: `ROADMAP.md`
- Test: `tests/test_tui.py`

- [x] **Step 1: 更新 AGENTS.md 的 TTY 描述**

Replace this line in `AGENTS.md`:

```markdown
- UI 输出统一走 `ui.py` 的语义化 helper；动态/模型文本必须用 `Text`/`markup=False`，避免 `[brackets]` 被当成 Rich markup 解析。TTY 输入用 `prompt_toolkit` + `patch_stdout`；该模式下用 `ui.consume(..., live=False)` 渲染。
```

with:

```markdown
- UI 输出统一走语义化 helper；TTY 使用 `tui.py`（Textual 全屏：输出 viewport + 底部固定 dock），管道模式继续用 `ui.py`。动态/模型文本必须用 `Text`/`markup=False`，避免 `[brackets]` 被当成 Rich markup 解析。
```

- [x] **Step 2: 更新 ROADMAP.md 的运行中输入条目**

In `ROADMAP.md`, replace the “运行中输入（2026-08-29）” bullet with:

```markdown
- [x] **运行中输入（2026-08-31 重构为 Textual）**：TTY 从 prompt_toolkit 常驻输入框迁移到 Textual 全屏布局——输出 transcript 独立滚动，审批/排队预览/状态/输入固定在底部 dock；运行中 Enter 仍入 steering 队列并在轮边界注入，Esc/Ctrl+C 打断，未注入 steering 回填输入框；管道模式保持原路径
```

- [x] **Step 3: 跑最终回归**

Run: `.venv/bin/python -m pytest -q`
Expected: `158 passed`

Run: `.venv/bin/python main.py </dev/null`
Expected: 打印 banner 后进入管道模式并正常退出，不报 `textual` 或 `prompt_toolkit` 相关错误

## 自审记录

- Spec 覆盖：依赖、布局、事件泵、steering 排队、审批、打断、斜杠命令、管道回归、AGENTS/ROADMAP 同步均有任务。
- 无占位符：每个实现步骤给出文件、代码或精确替换文本。
- 类型一致：`MiniAgentApp(session=...)`、`FakeSession.chat(question, control=None)`、`TextualApprovalChannel.ask(command, dangerous, timeout)` 与 `input_utils.set_approval_channel` 的鸭子类型一致。
