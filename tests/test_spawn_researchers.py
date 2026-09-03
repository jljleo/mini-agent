"""spawn_researchers：并发只读 researcher 子 agent 的回归测试。

零网络：ChatSession 打桩成假事件流。
设计约束：只读并行、非只读串行；spawn_researchers 仅支持 researcher 类型。
"""

import threading
import time

import pytest

import config
import tools
from events import StreamStart, TurnEnd
from tool_registry import TOOLS


@pytest.fixture
def clean_subagent_context():
    """子 agent 上下文是线程局部栈：每个测试前后清空，防串味。"""
    tools._context_stack().clear()
    yield
    tools._context_stack().clear()


def _fake_session_cls(conclusions=None, on_chat=None):
    """构造假 ChatSession 类：chat() 根据 task 生成结论。"""
    created = []

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.tools = tools
            self.depth = depth
            self.set_provider = set_provider
            self.messages = []
            self.task = None
            created.append(self)

        def chat(self, task, control=None):
            self.task = task
            if on_chat:
                on_chat(self, task)
            conclusion = conclusions.get(task, f"结论：{task}") if conclusions else f"结论：{task}"
            self.messages = [{"role": "assistant", "content": conclusion}]
            yield StreamStart()
            yield TurnEnd()

    FakeSession.created = created
    return FakeSession


def test_spawn_researchers_registered_in_tool_registry(clean_subagent_context):
    """spawn_researchers 应被 @tool 注册到全局 TOOLS。"""
    assert "spawn_researchers" in TOOLS
    schema = TOOLS["spawn_researchers"].tool_schema
    props = schema["function"]["parameters"]["properties"]
    assert "tasks" in props
    assert props["tasks"]["type"] == "array"


def test_spawn_researchers_guided_in_system_prompt():
    """system prompt 应引导 LLM 在调研多个独立文件时优先使用 spawn_researchers。"""
    contents = "\n".join(m.get("content", "") for m in config.SYSTEM_MESSAGES if m.get("role") == "system")
    assert "spawn_researchers" in contents
    assert "并行派生 researcher" in contents


def test_spawn_researchers_runs_multiple_tasks(clean_subagent_context, monkeypatch):
    """多个 researcher 任务并发执行，返回各自结论。"""
    import agent as agent_module

    fake_cls = _fake_session_cls({"taskA": "结论A", "taskB": "结论B"})
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)

    result = tools.spawn_researchers(["taskA", "taskB"])
    assert "结论A" in result
    assert "结论B" in result
    assert len(fake_cls.created) == 2
    for s in fake_cls.created:
        assert s.set_provider is False
        assert s.depth == 1
        tool_names = {t["function"]["name"] for t in s.tools}
        assert tool_names == set(config.SUBAGENT_TYPES["researcher"]["tools"])


def test_spawn_researchers_max_parallel_limits_concurrency(clean_subagent_context, monkeypatch):
    """并发上限由 config.SUBAGENT_MAX_PARALLEL 控制（不对模型暴露参数）。"""
    import agent as agent_module

    current = 0
    max_concurrent = 0
    lock = threading.Lock()

    def on_chat(session, task):
        nonlocal current, max_concurrent
        with lock:
            current += 1
            max_concurrent = max(max_concurrent, current)
        time.sleep(0.05)
        with lock:
            current -= 1

    fake_cls = _fake_session_cls(on_chat=on_chat)
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)
    monkeypatch.setattr(tools, "SUBAGENT_MAX_PARALLEL", 2)

    tools.spawn_researchers(["a", "b", "c", "d"])
    assert max_concurrent <= 2


def test_spawn_researchers_partial_failure_returns_others(clean_subagent_context, monkeypatch):
    """一个 researcher 异常，其他正常结论仍应返回。"""
    import agent as agent_module

    class MixedSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.tools = tools
            self.depth = depth
            self.messages = []

        def chat(self, task, control=None):
            self.messages = [{"role": "assistant", "content": f"结论：{task}"}]
            if task == "boom":
                raise RuntimeError("炸了")
            yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", MixedSession)
    result = tools.spawn_researchers(["ok1", "boom", "ok2"])
    assert "结论：ok1" in result
    assert "结论：ok2" in result
    assert "炸了" in result  # 异常信息应被记录


def test_spawn_researchers_does_not_pollute_global_history_provider(clean_subagent_context, monkeypatch):
    """spawn_researchers 不应覆盖主 agent 的全局 history provider。"""
    import agent as agent_module

    fake_cls = _fake_session_cls()
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)

    def main_provider():
        return ["主会话消息"]

    tools.set_history_provider(main_provider)
    tools.spawn_researchers(["x"])
    assert tools.get_history_provider() is main_provider


def test_spawn_researchers_nested_spawn_rejected(clean_subagent_context, monkeypatch):
    """子 agent 上下文中不能再调用 spawn_researchers（深度限制）。"""
    tools._context_stack().append({"task": "父任务", "depth": 0, "type": "researcher"})
    result = tools.spawn_researchers(["子任务"])
    assert "嵌套已达上限" in result


def test_spawn_researchers_read_only_policy_applies_per_thread(clean_subagent_context, monkeypatch):
    """并发 researcher 的每个线程都走 read_only 策略：非白名单命令硬拒。"""
    import agent as agent_module

    captured = []

    def on_chat(session, task):
        result = tools.run_bash("sed -i s/a/b/ file")
        captured.append((task, result))

    fake_cls = _fake_session_cls(on_chat=on_chat)
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")

    tools.spawn_researchers(["task1", "task2"])
    assert len(captured) == 2
    for _task, result in captured:
        assert "只读型子 agent 一律拒绝" in result


def test_spawn_researchers_rejects_empty_tasks(clean_subagent_context):
    """空任务列表直接拒绝，避免无意义调度。"""
    result = tools.spawn_researchers([])
    assert "至少包含一个任务" in result


def test_spawn_researchers_duplicate_tasks_do_not_collapse(clean_subagent_context, monkeypatch):
    """重复的任务字符串各自独立执行、各自占位，不会因按任务名建 dict 而塌缩。"""
    import agent as agent_module

    fake_cls = _fake_session_cls()
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)

    result = tools.spawn_researchers(["same", "same"])
    assert len(fake_cls.created) == 2  # 两个会话都真实执行了
    assert result.count("### same") == 2  # 输出里两段结论都在


def test_spawn_researchers_max_turns_defaults_and_limits(clean_subagent_context, monkeypatch):
    """max_turns 默认 10，可被显式设置，且钳制在 [1, 50]。"""
    import agent as agent_module

    class TurnCountingSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.tools = tools
            self.depth = depth
            self.messages = [{"role": "assistant", "content": "ok"}]

        def chat(self, task, control=None):
            for _i in range(60):
                if control.interrupt.is_set():
                    break
                yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", TurnCountingSession)

    # 默认 max_turns=10 应被中断
    result = tools.spawn_researchers(["x"])
    assert "已达轮次上限 10" in result

    # 显式 max_turns=5
    result = tools.spawn_researchers(["x"], max_turns=5)
    assert "已达轮次上限 5" in result

    # 边界钳制：0 视为 1
    result = tools.spawn_researchers(["x"], max_turns=0)
    assert "已达轮次上限 1" in result

    # 边界钳制：100 视为 50
    result = tools.spawn_researchers(["x"], max_turns=100)
    assert "已达轮次上限 50" in result
