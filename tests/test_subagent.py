"""spawn_subagent / 子 agent 安全模型的回归测试。

零网络：ChatSession 打桩成假事件流。
沙箱机制已移除（2026-09），子 agent 直接操作真实项目，安全由
SUBAGENT_TYPES 工具表 + command_policy + 用户审批保证。
"""

import json

import pytest

import config
import tools
from events import StreamFinished, StreamStart, TurnEnd


@pytest.fixture
def clean_subagent_context():
    """子 agent 上下文是模块级栈：每个测试前后清空，防串味。"""
    tools._context_stack().clear()
    yield
    tools._context_stack().clear()


def _fake_session_cls(messages, events):
    """构造假 ChatSession 类：记录构造参数，chat() 产出固定事件流。"""
    created = {}

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            created["tools"] = tools
            created["depth"] = depth
            created["set_provider"] = set_provider
            self.messages = list(messages)
            self.control = None

        def chat(self, task, control=None):
            self.control = control
            yield from events

    FakeSession.created = created
    return FakeSession


def test_depth_limit_rejects_nested_spawn(clean_subagent_context):
    """嵌套限深：已在子 agent 上下文里再派生，直接拒绝。"""
    tools._context_stack().append({"task": "父任务", "depth": 0})
    result = tools.spawn_subagent("套娃任务", agent_type="coder")
    assert "嵌套已达上限" in result


def test_search_tools_hides_meta_tools_in_subagent(clean_subagent_context):
    """子 agent 的 search_tools 过滤元工具：spawn_subagent 防套娃、todo 防覆盖主清单。"""
    tools._context_stack().append({"task": "x", "depth": 0})
    names = {s["function"]["name"] for s in json.loads(tools.search_tools())}
    assert "spawn_subagent" not in names
    assert "todo_write" not in names
    assert "todo_read" not in names
    assert "search_history" in names  # 检索自己历史无害，保留


def test_search_tools_unfiltered_outside_subagent(clean_subagent_context):
    """主 agent 的 search_tools 不过滤：但 spawn_subagent 已常驻，不再出现在可发现档。"""
    names = {s["function"]["name"] for s in json.loads(tools.search_tools())}
    assert "spawn_subagent" not in names
    assert "search_history" in names  # 其余扩展工具仍可发现


def test_subagent_denied_outside_path(clean_subagent_context):
    """子 agent 访问项目外路径：硬拒绝，不冒泡找人（环境安全层）。"""
    tools._context_stack().append({"task": "x", "depth": 0})
    with pytest.raises(ValueError, match="子 agent 禁止访问项目外路径"):
        tools.read_file("/etc/hosts")


def test_run_bash_in_subagent_human_policy_denied_by_user(clean_subagent_context, monkeypatch):
    """coder（command_policy=human）：ambiguous 命令冒泡给人工审批，用户拒绝即拒绝，带 [子 agent] 前缀标明来源。"""
    tools._context_stack().append({"task": "x", "depth": 0, "type": "coder"})
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")
    confirmed = []
    monkeypatch.setattr(tools, "_confirm", lambda cmd: confirmed.append(cmd) or False)

    result = tools.run_bash("pytest tests/")
    assert "用户拒绝了该命令" in result
    assert confirmed[0].startswith("[子 agent] ")
    assert "pytest" in confirmed[0]


def test_run_bash_in_subagent_human_policy_executes_when_approved(clean_subagent_context, monkeypatch):
    """coder（command_policy=human）：用户批准后命令真实执行。"""
    tools._context_stack().append({"task": "x", "depth": 0, "type": "coder"})
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")
    monkeypatch.setattr(tools, "_confirm", lambda cmd: True)

    result = tools.run_bash("echo hello")
    assert "hello" in result


def test_run_bash_researcher_read_only_hard_denies_non_whitelist(clean_subagent_context, monkeypatch):
    """researcher（command_policy=read_only）：非白名单命令直接硬拒，不冒泡不评审。"""
    tools._context_stack().append({"task": "x", "depth": 0, "type": "researcher"})
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")
    monkeypatch.setattr(tools, "_confirm", lambda cmd: pytest.fail("read_only 子 agent 不应冒泡审批"))

    result = tools.run_bash("sed -i s/a/b/ file")
    assert "只读型子 agent 一律拒绝" in result


def test_run_bash_in_subagent_outside_path_hard_denied(clean_subagent_context, monkeypatch):
    """子 agent 的越界 bash 命令硬拒绝——不冒泡不评审（边界违规是明确事实，不需要评审）。"""
    tools._context_stack().append({"task": "x", "depth": 0})
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "allow")  # 即使规则命中 allow
    monkeypatch.setattr(tools, "_confirm", lambda cmd: pytest.fail("越界路径不应冒泡审批"))

    result = tools.run_bash("cat /etc/hosts")
    assert "子 agent 禁止执行含项目外路径" in result


def test_run_bash_in_subagent_human_policy_downgrades_allow_to_ask(clean_subagent_context, monkeypatch):
    """无沙箱后，coder 子 agent 命中 allow 的命令也降级为 ask，让用户确认。"""
    tools._context_stack().append({"task": "x", "depth": 0, "type": "coder"})
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "allow")
    confirmed = []
    monkeypatch.setattr(tools, "_confirm", lambda cmd: confirmed.append(cmd) or True)

    result = tools.run_bash("echo hello")
    assert "hello" in result
    assert confirmed[0].startswith("[子 agent] ")
    assert "echo" in confirmed[0]


def test_run_bash_in_subagent_researcher_allow_still_passes(clean_subagent_context, monkeypatch):
    """researcher 子 agent 的 allow 命令保持直通（read_only 白名单机制不受影响）。"""
    tools._context_stack().append({"task": "x", "depth": 0, "type": "researcher"})
    monkeypatch.setattr(tools, "_confirm", lambda cmd: pytest.fail("researcher 不应触发人工审批"))

    result = tools.run_bash("echo hello")
    assert "hello" in result


class _FakeControl:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


def test_denial_circuit_breaker_aborts_after_limit(clean_subagent_context):
    """拒绝熔断：连续被拒 SUBAGENT_DENIAL_LIMIT 次即 abort 本轮（codex 思路，防反复试探）。"""
    control = _FakeControl()
    tools._context_stack().append({"task": "x", "depth": 0, "type": "researcher",
                                    "denied": 0, "control": control})
    for _ in range(config.SUBAGENT_DENIAL_LIMIT - 1):
        msg = tools._record_subagent_denial("拒绝")
        assert "熔断" not in msg
    msg = tools._record_subagent_denial("拒绝")
    assert "熔断" in msg
    assert control.aborted


def test_denial_below_limit_returns_reason_unchanged(clean_subagent_context):
    """未到熔断阈值时，拒绝文案原样返回，不触发 abort。"""
    control = _FakeControl()
    tools._context_stack().append({"task": "x", "depth": 0, "type": "researcher",
                                    "denied": 0, "control": control})
    msg = tools._record_subagent_denial("被拒原因")
    assert msg == "被拒原因"
    assert not control.aborted


def test_spawn_subagent_result_reports_denials(clean_subagent_context, monkeypatch):
    """拒绝信息回传：子 agent 运行中被拒的次数附在结论里，主 agent 据此自我修正边界。"""
    import agent as agent_module

    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.messages = [{"role": "assistant", "content": "结论"}]

        def chat(self, task, control=None):
            # researcher read_only：两条非白名单命令被拒
            tools.run_bash("sed -i s/a/b/ f")
            tools.run_bash("sed -i s/c/d/ g")
            yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", FakeSession)
    result = tools.spawn_subagent("x", agent_type="researcher")
    assert "2 条命令/访问被拒" in result
    assert "结论" in result


def test_spawn_subagent_description_has_delegation_discipline():
    """工具描述写死派生纪律（codex 同款）：先规划边界、任务自包含、别乱派。"""
    schema = tools.TOOLS["spawn_subagent"].tool_schema
    desc = schema["function"]["description"]
    assert "self-contained" in desc
    assert "decide the boundaries" in desc


def _stub_chat_network(session, monkeypatch):
    """打桩网络层：create 返回空壳（流由 stream_and_assemble 的桩接管）。"""
    monkeypatch.setattr(session.client.chat.completions, "create", lambda **kwargs: object())


def _drive_search_tools_injection(session, monkeypatch):
    """驱动一轮 chat：第一轮调 search_tools，第二轮出终稿。返回注入的 tools 声明。"""
    import agent as agent_module

    state = {"n": 0}

    def fake_assemble(completion):
        state["n"] += 1
        if state["n"] == 1:
            call = {"id": "c1", "type": "function",
                    "function": {"name": "search_tools", "arguments": "{}"}}
            return iter([StreamFinished([{"role": "assistant", "content": "", "tool_calls": [call]}], None)])
        return iter([StreamFinished([{"role": "assistant", "content": "done"}], None)])

    monkeypatch.setattr(agent_module, "stream_and_assemble", fake_assemble)
    list(session.chat("x"))
    injected = [m for m in session.messages if isinstance(m, dict) and m.get("tools")]
    assert injected, "search_tools 后应注入动态声明"
    return {s["function"]["name"] for s in injected[0]["tools"]}


def test_injection_filters_hidden_tools_for_subagent(clean_subagent_context, monkeypatch):
    """内核动态声明注入也过滤元工具（与 search_tools 返回文本过滤是同一道防线的两层）。"""
    import agent as agent_module

    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    session = agent_module.ChatSession(depth=1)  # 子 agent 会话
    _stub_chat_network(session, monkeypatch)

    names = _drive_search_tools_injection(session, monkeypatch)
    assert "spawn_subagent" not in names
    assert "todo_write" not in names
    assert "todo_read" not in names
    assert "search_history" in names


def test_injection_unfiltered_for_main_agent(monkeypatch):
    """主 agent（depth=0）注入不过滤：spawn_subagent 已常驻，扩展档只包含其余可发现工具。"""
    import agent as agent_module

    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    session = agent_module.ChatSession(depth=0)
    _stub_chat_network(session, monkeypatch)

    names = _drive_search_tools_injection(session, monkeypatch)
    # spawn_subagent 已在常驻 tools= 中，动态注入不应重复出现（duplicate 会 400）
    assert "spawn_subagent" not in names
    assert "search_history" in names
    # 常驻工具里包含 spawn_subagent
    resident_names = {s["function"]["name"] for s in session.tools}
    assert "spawn_subagent" in resident_names


def test_run_bash_outside_subagent_still_asks_human(monkeypatch, clean_subagent_context):
    """主 agent 路径不变：ask 仍然走人工确认。"""
    monkeypatch.setattr(tools, "_check_permission", lambda cmd: "ask")
    monkeypatch.setattr(tools, "_confirm", lambda cmd: False)
    result = tools.run_bash("somecmd")
    assert "用户拒绝" in result


def test_spawn_subagent_returns_only_conclusion(clean_subagent_context, monkeypatch):
    """上下文隔离契约：新会话、受限工具集、depth+1，只回最后 assistant 正文。"""
    import agent as agent_module

    events = [StreamStart(), StreamFinished([{"role": "assistant", "content": "结论：已修复"}], None), TurnEnd()]
    fake_cls = _fake_session_cls(
        [{"role": "assistant", "content": "中间话"}, {"role": "assistant", "content": "结论：已修复"}],
        events,
    )
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)

    result = tools.spawn_subagent("修复 fizzbuzz", agent_type="coder")
    assert "结论：已修复" in result
    assert "中间话" not in result  # 只回最终结论，轨迹不进主上下文
    assert fake_cls.created["depth"] == 1
    assert fake_cls.created["set_provider"] is False  # 子 agent 不覆盖全局 provider
    tool_names = {s["function"]["name"] for s in fake_cls.created["tools"]}
    assert tool_names == set(config.SUBAGENT_TYPES["coder"]["tools"])


def test_spawn_subagent_unknown_type_rejected(clean_subagent_context):
    """未知类型直接拒绝，并列出可用类型与边界（教主 agent 怎么派）。"""
    result = tools.spawn_subagent("x", agent_type="hacker")
    assert "未知子 agent 类型" in result
    assert "researcher" in result and "coder" in result


def test_spawn_subagent_researcher_is_readonly(clean_subagent_context, monkeypatch):
    """researcher 类型：只读工具集，无写工具；bash 保留（grep/find/cat 走 allow 直通，调研刚需）。"""
    import agent as agent_module

    roots_seen = []

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            FakeSession.tools = tools
            self.messages = [{"role": "assistant", "content": "ok"}]

        def chat(self, task, control=None):
            roots_seen.append(tools.PROJECT_ROOT)
            yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", FakeSession)
    real_root = tools.PROJECT_ROOT
    tools.spawn_subagent("调研代码结构", agent_type="researcher")

    tool_names = {s["function"]["name"] for s in FakeSession.tools}
    assert tool_names == set(config.SUBAGENT_TYPES["researcher"]["tools"])
    # 只读边界：无写工具；bash 保留
    assert "write_file" not in tool_names and "edit_file" not in tool_names
    assert "run_bash" in tool_names
    # 沙箱已移除，PROJECT_ROOT 不应被切换
    assert roots_seen[0] == real_root
    assert tools.PROJECT_ROOT == real_root


def test_spawn_subagent_max_turns_aborts(clean_subagent_context, monkeypatch):
    """硬性轮次上限：StreamStart 超过 max_turns 即 abort（优雅收尾防孤儿 tool call）。"""
    import agent as agent_module

    events = [StreamStart()] * 5 + [TurnEnd()]  # 一轮 chat 内多次 API 往返（工具循环）
    fake_cls = _fake_session_cls([{"role": "assistant", "content": "中途结论"}], events)
    monkeypatch.setattr(agent_module, "ChatSession", fake_cls)

    result = tools.spawn_subagent("x", agent_type="coder", max_turns=2)
    assert "已达轮次上限" in result
    assert "中途结论" in result


def test_spawn_subagent_interrupted_marks_incomplete(clean_subagent_context, monkeypatch):
    """被中断（非轮次上限）时：返回标注"被中断、未产出完整结论"，而非把半截话当结论。"""
    import agent as agent_module

    def fake_events(task, control=None):
        control.abort()  # 模拟拒绝熔断/用户中断
        yield StreamStart()
        yield TurnEnd()

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.messages = [{"role": "assistant", "content": "半截话"}]

        def chat(self, task, control=None):
            yield from fake_events(task, control)

    monkeypatch.setattr(agent_module, "ChatSession", FakeSession)
    result = tools.spawn_subagent("x", agent_type="coder")
    assert "被中断" in result
    assert "未产出完整结论" in result


def test_spawn_subagent_uses_context_local_history_provider(clean_subagent_context, monkeypatch):
    """子 agent 不覆盖全局 history provider；search_history 通过子 agent 上下文读取其 session messages。"""
    import agent as agent_module

    sub_messages = [{"role": "assistant", "content": "子结论"}]

    class FakeSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.messages = list(sub_messages)

        def chat(self, task, control=None):
            yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", FakeSession)

    main_provider = lambda: ["主会话消息"]
    tools.set_history_provider(main_provider)

    # 子 agent 运行期间，get_history_provider 应指向子 session
    captured = {}

    class CaptureSession(FakeSession):
        def __init__(self, tools=None, depth=0, set_provider=True):
            super().__init__(tools, depth, set_provider)
            captured["session"] = self

        def chat(self, task, control=None):
            captured["during"] = tools.get_history_provider()()
            yield StreamStart()
            yield TurnEnd()

    monkeypatch.setattr(agent_module, "ChatSession", CaptureSession)
    tools.spawn_subagent("x", agent_type="coder")
    # 子 agent 运行期间，provider 返回的是该子 session 的 messages（同一对象）
    assert captured["during"] is captured["session"].messages
    # 子 agent 结束后回退到主 provider
    assert tools.get_history_provider() is main_provider


def test_spawn_subagent_exception_still_restores_state(clean_subagent_context, monkeypatch):
    """子 agent 崩了也要恢复上下文栈 / history provider。"""
    import agent as agent_module

    class BoomSession:
        def __init__(self, tools=None, depth=0, set_provider=True):
            self.messages = []

        def chat(self, task, control=None):
            raise RuntimeError("炸了")
            yield  # 使其成为生成器

    monkeypatch.setattr(agent_module, "ChatSession", BoomSession)
    real_root = tools.PROJECT_ROOT
    main_provider = lambda: []
    tools.set_history_provider(main_provider)

    result = tools.spawn_subagent("x", agent_type="coder")
    assert "子 agent 执行异常" in result
    assert not tools._context_stack()
    assert tools.PROJECT_ROOT == real_root
    assert tools.get_history_provider() is main_provider


def test_search_history_reads_subagent_session_not_global(clean_subagent_context):
    """search_history 在子 agent 上下文里必须读取子 session 的历史，而不是主 agent 全局历史。"""
    main_messages = [{"role": "user", "content": "主历史内容-main123"}]
    tools.set_history_provider(lambda: main_messages)

    sub_messages = [{"role": "user", "content": "子历史内容-sub456"}]
    fake_session = type("FakeSession", (), {"messages": sub_messages})()
    tools._context_stack().append({
        "task": "子任务", "depth": 0, "type": "researcher",
        "denied": 0, "control": None, "session": fake_session,
    })
    try:
        result = tools.search_history("sub456")
        assert "子历史内容-sub456" in result
        assert "主历史内容-main123" not in result
    finally:
        tools._context_stack().pop()
