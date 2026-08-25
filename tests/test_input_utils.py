"""input_utils.read_input 回归测试（管道分支；tty 分支靠冒烟覆盖）。"""

import io
import sys

import pytest

from input_utils import read_input


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
