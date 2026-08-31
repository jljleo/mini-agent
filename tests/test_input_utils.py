"""input_utils 回归测试：read_input 管道分支 + ApprovalChannel 确认通道。

常驻输入框的按键绑定/工具栏靠 pty 冒烟覆盖，这里测通道逻辑本身。
"""

import io
import sys
import threading
import time

import pytest

from input_utils import ApprovalChannel, read_input


class FakeStdin:
    """模拟非 tty stdin（管道）：isatty False + buffer 字节流。"""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False


def feed(monkeypatch, data: bytes):
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))


class TestReadInputPipe:
    def test_normal_input(self, monkeypatch):
        feed(monkeypatch, b"hello\n")
        assert read_input() == ("hello", False)

    def test_leading_space_escapes_command_dispatch(self, monkeypatch):
        """前导空格 = 显式逃逸（Codex 同款）：强制按消息发送，跳过斜杠命令分发。"""
        feed(monkeypatch, b" /help\n")
        text, forced = read_input()
        assert text == "/help"  # 文本照常 strip
        assert forced is True

    def test_tab_is_not_escape(self, monkeypatch):
        """只有空格是逃逸符，tab 不是（Codex 语义：space 才禁用命令解析）。"""
        feed(monkeypatch, b"\t/help\n")
        _, forced = read_input()
        assert forced is False

    def test_eof_raises(self, monkeypatch):
        feed(monkeypatch, b"")
        with pytest.raises(EOFError):
            read_input()

    def test_sanitize_still_applies(self, monkeypatch):
        """逃逸检测在 sanitize 之前：strip 后的文本不带前导空格，但 forced 仍为真。"""
        feed(monkeypatch, b"  /quit  \n")
        text, forced = read_input()
        assert text == "/quit" and forced is True


class TestApprovalChannel:
    def test_yes_answer_approves(self):
        """ask 挂起，另一个线程 answer(True) 后放行。"""
        channel = ApprovalChannel()
        result = {}

        def asker():
            result["ok"] = channel.ask("ls -la", dangerous=False, timeout=5)

        t = threading.Thread(target=asker)
        t.start()
        # 等 ask 进入 pending 状态再应答（模拟用户在输入框按 y）
        for _ in range(100):
            if channel.has_pending:
                break
            time.sleep(0.01)
        assert channel.has_pending
        channel.answer(True)
        t.join(timeout=2)
        assert result["ok"] is True

    def test_no_answer_rejects(self):
        channel = ApprovalChannel()
        result = {}

        def asker():
            result["ok"] = channel.ask("rm -rf x", dangerous=True, timeout=5)

        t = threading.Thread(target=asker)
        t.start()
        for _ in range(100):
            if channel.has_pending:
                break
            time.sleep(0.01)
        channel.answer(False)
        t.join(timeout=2)
        assert result["ok"] is False

    def test_timeout_defaults_to_reject(self):
        """无人应答超时：默认拒绝（假 tty 防线）。"""
        channel = ApprovalChannel()
        assert channel.ask("ls", dangerous=False, timeout=0.1) is False
        assert not channel.has_pending  # 超时后 pending 已清理

    def test_answer_without_pending_is_noop(self):
        channel = ApprovalChannel()
        channel.answer(True)  # 不应抛异常
        assert not channel.has_pending
