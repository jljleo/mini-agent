"""agent.py 回归测试：会话状态管理（回滚/存档/注入检测）、死循环保险丝与事件契约。

chat() 主循环是事件流生成器：API 调用用 monkeypatch 打桩（stream_and_assemble
返回罐头 StreamFinished 事件，client.create 返回空壳）——测的是循环控制逻辑，不是网络。
"""

import json
import os

import agent
from agent import ChatSession, load_saved_session
from events import StreamFinished, TurnEnd, Warn


class TestMarkRollback:
    def test_rollback_restores_history(self, session):
        mark = session.mark()
        session.messages.append({"role": "user", "content": "x"})
        session.messages.append({"role": "assistant", "content": "y"})
        session.rollback(mark)
        assert len(session.messages) == mark

    def test_rollback_never_touches_old_messages(self, session):
        """回滚只删新增：mark 之前的历史一个字节不能动（原子性约定）。"""
        original = list(session.messages)
        mark = session.mark()
        session.messages.append({"role": "user", "content": "x"})
        session.rollback(mark)
        assert session.messages == original


class TestSessionArchive:
    def test_save_and_load_roundtrip(self, session):
        session.messages.append({"role": "user", "content": "记住我"})
        session.total_prompt_tokens = 123
        session.save()
        data = load_saved_session()
        assert data["messages"][-1]["content"] == "记住我"
        assert data["total_prompt_tokens"] == 123

    def test_corrupt_archive_returns_none(self, session):
        """存档是增强不是依赖：损坏时返回 None 而非崩溃。"""
        with open(agent.SESSION_FILE, "w") as f:
            f.write("{broken")
        assert load_saved_session() is None

    def test_missing_archive_returns_none(self, session):
        assert load_saved_session() is None

    def test_save_is_atomic(self, session):
        """原子写：落盘后不留 tmp 残文件。"""
        session.save()
        assert os.path.exists(agent.SESSION_FILE)
        assert not os.path.exists(agent.SESSION_FILE + ".tmp")


class TestToolInjection:
    def test_tools_injection_detection(self, session):
        assert not session._tools_already_injected()
        session.messages.append({"role": "system", "tools": [{"x": 1}]})
        assert session._tools_already_injected()


class TestExecuteToolCall:
    def test_unknown_tool_becomes_error_text(self, session):
        """任何失败都转为文本结果，不向上抛（工具循环不能被异常打断）。"""
        name, result = session._execute_tool_call(
            {"function": {"name": "no_such_tool", "arguments": "{}"}})
        assert "调用失败" in result

    def test_bad_arguments_become_error_text(self, session, monkeypatch):
        monkeypatch.setitem(agent.TOOLS, "needs_args", lambda x: x)
        _, result = session._execute_tool_call(
            {"function": {"name": "needs_args", "arguments": "{bad json"}})
        assert "调用失败" in result


def stub_chat_network(session, monkeypatch):
    """打桩网络层：create 返回空壳（流由 stream_and_assemble 的桩接管）。

    测试绝不能发真实 API 请求——测的是循环控制逻辑，不是网络。
    """
    monkeypatch.setattr(session.client.chat.completions, "create",
                        lambda **kwargs: object())


def canned(messages, usage=None):
    """罐头事件流：stream_and_assemble 的桩，直接吐出 StreamFinished。"""
    return lambda completion: iter([StreamFinished(messages, usage)])


class TestDeadLoopFuse:
    def test_same_call_three_times_intercepted(self, session, monkeypatch):
        """行为保险丝：同一 (工具名, 参数) 连续 3 次判死循环，强制结束并补拦截结果。"""
        stub_chat_network(session, monkeypatch)
        monkeypatch.setitem(agent.TOOLS, "fake_tool", lambda: "ok")

        call = {"id": "c1", "type": "function",
                "function": {"name": "fake_tool", "arguments": "{}"}}
        monkeypatch.setattr(
            agent, "stream_and_assemble",
            canned([{"role": "assistant", "content": "", "tool_calls": [dict(call)]}]))

        events = list(session.chat("测试死循环"))

        intercepts = [m for m in session.messages
                      if "[系统] 同一调用连续重复" in str(m.get("content", ""))]
        assert intercepts, "死循环未被保险丝拦截"
        # 拦截消息后不应再有新的工具执行结果（循环已强制结束）
        last_tool_idx = max(i for i, m in enumerate(session.messages)
                            if m.get("role") == "tool")
        assert session.messages[last_tool_idx]["content"].startswith("[系统]")
        assert isinstance(events[-1], TurnEnd), "熔断结束也必须产出 TurnEnd 收尾"
        assert any(isinstance(e, Warn) for e in events)

    def test_normal_calls_not_misjudged(self, session, monkeypatch):
        """正常任务每次调用参数不同，不得误伤。"""
        stub_chat_network(session, monkeypatch)
        monkeypatch.setitem(agent.TOOLS, "fake_tool", lambda: "ok")

        state = {"n": 0}

        def fake_assemble(completion):
            state["n"] += 1
            if state["n"] <= 3:
                # 每次参数不同
                call = {"id": f"c{state['n']}", "type": "function",
                        "function": {"name": "fake_tool",
                                     "arguments": json.dumps({"i": state["n"]})}}
                return iter([StreamFinished(
                    [{"role": "assistant", "content": "", "tool_calls": [call]}], None)])
            return iter([StreamFinished([{"role": "assistant", "content": "完成"}], None)])

        monkeypatch.setattr(agent, "stream_and_assemble", fake_assemble)
        events = list(session.chat("正常任务"))
        assert session.messages[-1]["content"] == "完成"
        assert not any("[系统] 同一调用连续重复" in str(m.get("content", ""))
                       for m in session.messages)
        assert isinstance(events[-1], TurnEnd), "终稿结束必须产出 TurnEnd 收尾"


class TestTruncatedToolCallsVoided:
    def test_length_truncated_tool_calls_voided(self, session, monkeypatch):
        """stopReason=length 时整批 tool call 作废（pi 同款加固）：
        arguments JSON 可能不完整，执行半个参数比不执行更危险。"""
        stub_chat_network(session, monkeypatch)
        executed = []
        monkeypatch.setitem(agent.TOOLS, "fake_tool",
                            lambda: executed.append(1) or "ok")
        call = {"id": "c1", "type": "function",
                "function": {"name": "fake_tool", "arguments": '{"path": "/etc'}}
        monkeypatch.setattr(
            agent, "stream_and_assemble",
            canned([{"role": "assistant", "content": "", "tool_calls": [dict(call)],
                     "_finish_reason": "length"}]))

        events = list(session.chat("截断测试"))

        assert not executed, "截断轮的工具调用不应被执行"
        voided = [m for m in session.messages if "已作废" in str(m.get("content", ""))]
        assert len(voided) == 1, "每个 tool_call 都必须补作废结果（孤儿 tool_call 必 400）"
        assert voided[0]["tool_call_id"] == "c1"
        assert isinstance(events[-1], TurnEnd)
        assert any(isinstance(e, Warn) and "截断" in e.message for e in events)
