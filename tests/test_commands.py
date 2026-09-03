"""commands.py 回归测试：斜杠命令执行体的行为契约。

SESSION_FILE / TODO_FILE 全部指向 tmp：命令会删文件，绝不碰真实存档。
"""

import os

import pytest

import agent
import commands
import tools
from commands import cmd_clear, cmd_compact, cmd_resume, cmd_tokens


@pytest.fixture(autouse=True)
def isolate_files(tmp_path, monkeypatch):
    """commands 和 agent 各自 import 了 SESSION_FILE，两处都要指走。"""
    session_file = str(tmp_path / "session.json")
    monkeypatch.setattr(agent, "SESSION_FILE", session_file)
    monkeypatch.setattr(commands, "SESSION_FILE", session_file)
    monkeypatch.setattr(tools, "TODO_FILE", str(tmp_path / "todos.json"))


class TestClear:
    def test_resets_messages_counters_and_files(self, session):
        session.messages.append({"role": "user", "content": "x"})
        session.total_prompt_tokens = 999
        session.save()  # 造出存档文件
        tools.todo_write([{"content": "t", "status": "pending"}])

        cmd_clear(session)

        assert len(session.messages) == len(commands.SYSTEM_MESSAGES)
        assert session.total_prompt_tokens == 0
        assert not os.path.exists(agent.SESSION_FILE)
        assert not os.path.exists(tools.TODO_FILE)

    def test_clear_on_fresh_session_no_crash(self, session):
        cmd_clear(session)  # 无存档无 todo：幂等不炸


class TestTokens:
    def test_renders_dashboard(self, session, capsys):
        session.total_prompt_tokens = 1000
        cmd_tokens(session)
        out = capsys.readouterr().out
        assert "1,000" in out and "tokens" in out


class TestResume:
    def test_no_archive(self, session, capsys):
        cmd_resume(session)
        assert "没有可恢复的会话存档" in capsys.readouterr().out

    def test_restores_messages_and_counters(self, session, capsys):
        session.messages.append({"role": "user", "content": "上次的话题"})
        session.total_prompt_tokens = 42
        session.save()
        session.messages = []  # 模拟新会话
        session.total_prompt_tokens = 0

        cmd_resume(session)

        assert session.messages[-1]["content"] == "上次的话题"
        assert session.total_prompt_tokens == 42
        assert "最近话题" in capsys.readouterr().out


class TestCompact:
    def test_healthy_session_no_action(self, session, capsys):
        cmd_compact(session)
        assert "无需压缩" in capsys.readouterr().out

    def test_compacts_with_summary_and_archives(self, session, monkeypatch, capsys):
        """手动压缩：中段换成摘要、原文归档、落盘。"""
        # 造一个超过 LOW 水位（60K tokens ≈ 120K 字符）的历史
        session.messages.append({"role": "user", "content": "最初任务"})
        session.messages.append({"role": "assistant", "content": "首次回应"})
        for _i in range(25):
            session.messages.append({"role": "user", "content": "x" * 5000})
            session.messages.append({"role": "assistant", "content": "y" * 5000})

        monkeypatch.setattr(commands, "summarize_middle",
                            lambda middle, client, on_note=None: "假摘要")
        original = list(session.messages)

        cmd_compact(session)

        assert session.compact_archive == original  # 突变前的后悔药
        assert any("假摘要" in str(m.get("content", "")) for m in session.messages)
        assert len(session.messages) < len(original)
        assert "已压缩" in capsys.readouterr().out
        assert os.path.exists(agent.SESSION_FILE)  # 突变后立即落盘

    def test_summary_failure_falls_back_to_marker(self, session, monkeypatch):
        session.messages.append({"role": "user", "content": "最初任务"})
        session.messages.append({"role": "assistant", "content": "首次回应"})
        for _i in range(25):
            session.messages.append({"role": "user", "content": "x" * 5000})
            session.messages.append({"role": "assistant", "content": "y" * 5000})

        monkeypatch.setattr(commands, "summarize_middle",
                            lambda middle, client, on_note=None: None)
        cmd_compact(session)
        assert any("截断" in str(m.get("content", "")) for m in session.messages)
