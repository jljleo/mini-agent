"""Textual TUI 前端测试。

约束：不启动真实终端、不访问网络；Textual headless run_test 只验证 UI 状态。
"""

import asyncio


def run(coro):
    return asyncio.run(coro)


def test_textual_dependency_available():
    import textual

    assert textual.__version__


from events import Note, ToolCallResult, ToolCallStart, Usage, Warn
from tui_render import render_event


def test_split_complete_freezes_completed_blocks_only():
    from tui import _split_complete

    assert _split_complete("完整段落\n\n尾部") == ("完整段落\n\n", "尾部")
    assert _split_complete("无空行分隔") == ("", "无空行分隔")
    assert _split_complete("```\n半截代码\n") == ("", "```\n半截代码\n")  # 未闭合 fence 不切
    assert _split_complete("```\n代码\n```\n\n正文") == ("```\n代码\n```\n\n", "正文")


def test_flush_timer_survives_dirty_clearing():
    # 回归：定时器触发瞬间 dirty 已被立即 flush 清掉时，链条不能断。
    # 否则后续慢速流（间隔 < 80ms）只能等 2000 字符阈值，尾部滞留。
    from textual.app import App

    from tui import TranscriptView

    class BareApp(App):
        def compose(self):
            yield TranscriptView()

    async def scenario():
        app = BareApp()
        async with app.run_test() as pilot:
            v = app.query_one(TranscriptView)
            v.append_text("A")
            await pilot.pause(0.1)  # 定时器触发 flush "A"，dirty 清空
            v.append_text("B")      # 间隔 < 80ms，不会立即 flush
            await pilot.pause(0.1)  # 修复后定时器仍在，会 flush "B"
            assert "B" in v._stream_widget._markdown

    run(scenario())


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


def test_yn_keys_not_swallowed_when_no_approval_pending():
    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test() as pilot:
            keys = {b.key for b in app.BINDINGS}
            assert "y" not in keys
            assert "n" not in keys

            prompt = app.query_one("#prompt")
            prompt.value = "yes"
            await pilot.pause()
            assert prompt.value == "yes"
            assert app.approval.has_pending is False

    run(scenario())


def test_error_path_rolls_back_and_finishes_stream():
    from events import StreamStart, TextDelta
    from tui import MiniAgentApp

    class BoomSession(FakeSession):
        def chat(self, question, control=None):
            self.messages.append({"role": "user", "content": question})
            yield StreamStart()
            yield TextDelta("半截")
            raise RuntimeError("boom")

    async def scenario():
        session = BoomSession()
        app = MiniAgentApp(session=session)
        async with app.run_test() as pilot:
            app.submit_text("会失败的问题")
            await pilot.pause(0.2)

            assert session.messages == []
            assert session.saved is False
            assert "半截" in app.transcript.text_content()
            assert app.transcript._stream_widget is None
            assert "boom" in app.transcript.text_content()

    run(scenario())


def test_leftover_steering_backfills_prompt():
    from tui import MiniAgentApp

    class QuickSession(FakeSession):
        def chat(self, question, control=None):
            yield TurnEnd()

    async def scenario():
        session = QuickSession()
        app = MiniAgentApp(session=session)
        async with app.run_test() as pilot:
            app.submit_text("原始")
            app.submit_text("排队话")
            await pilot.pause(0.2)

            assert app.query_one("#prompt").value == "排队话"

    run(scenario())


def test_main_dispatches_tty_to_textual(monkeypatch):
    import sys

    import main

    called = {}

    class FakeTui:
        @staticmethod
        def run(session):
            called["tui"] = session

    class BareSession:
        def status_text(self):
            return "fake · tokens 0"

    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main, "ChatSession", BareSession)
    monkeypatch.setattr(main.ui, "banner", lambda *args: None)
    monkeypatch.setattr(main, "set_status_provider", lambda fn: None)
    monkeypatch.setitem(sys.modules, "tui", FakeTui)

    main.main()

    assert "tui" in called


def test_slash_command_help_renders_into_transcript():
    # 回归：tui 必须自行 import commands 触发注册，不能依赖 main.py 的副作用导入
    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test() as pilot:
            app.submit_text("/help")
            await pilot.pause()

            content = app.transcript.text_content()
            assert "/compact" in content
            assert "未知命令" not in content

    run(scenario())


def test_approval_yes_unblocks_tool_thread():
    import threading
    import time

    from tui import MiniAgentApp

    async def scenario():
        app = MiniAgentApp(session=FakeSession())
        async with app.run_test() as pilot:
            result = {}

            def asker():
                result["ok"] = app.approval.ask("ls", dangerous=False, timeout=5)

            thread = threading.Thread(target=asker)
            thread.start()
            for _ in range(500):
                if app.approval.has_pending:
                    break
                time.sleep(0.01)
            await pilot.pause()

            assert app.query_one("#approval").display is True
            assert app.query_one("#prompt").disabled is True
            await pilot.press("y")
            thread.join(timeout=5)
            await pilot.pause()

            assert result.get("ok") is True
            assert app.query_one("#approval").display is False
            assert app.query_one("#prompt").disabled is False

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
