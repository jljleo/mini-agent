"""tool_registry / command_registry 回归测试：注册表的单一事实来源语义。"""

import json

import pytest

import command_registry
import tool_registry
import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册（与 main.py 同一机制）
from tool_registry import TOOLS, get_all_tool_schemas, tool


@pytest.fixture
def clean_tools():
    """注册测试工具会污染全局 TOOLS：快照-恢复，防测试间串味。"""
    snapshot = dict(TOOLS)
    yield
    TOOLS.clear()
    TOOLS.update(snapshot)


class TestToolRegistry:
    def test_decorator_registers_schema_and_fn(self, clean_tools):
        @tool("demo_tool", "演示", {"x": {"type": "string"}}, ["x"])
        def demo(x):
            return x

        assert TOOLS["demo_tool"] is demo
        schema = demo.tool_schema["function"]
        assert schema["name"] == "demo_tool"
        assert schema["parameters"]["required"] == ["x"]

    def test_required_defaults_empty(self, clean_tools):
        @tool("no_args", "无参", {})
        def f():
            return "ok"

        assert f.tool_schema["function"]["parameters"]["required"] == []

    def test_get_all_excludes_search_tools(self, clean_tools):
        """search_tools 的声明由 SEARCH_TOOLS_SCHEMA 常驻，不参与聚合（防自我引用）。"""
        names = [s["function"]["name"] for s in get_all_tool_schemas()]
        assert "search_tools" not in names

    def test_resident_default_true(self, clean_tools):
        """默认常驻：8 个基本工具直接进 tools= 参数，无需先检索。"""
        @tool("demo_resident", "演示", {})
        def f():
            return "ok"

        assert f.tool_resident is True
        names = [s["function"]["name"] for s in get_all_tool_schemas()]
        assert "demo_resident" in names

    def test_extended_split(self, clean_tools):
        """resident=False 进可发现档：不常驻 tools=，由 search_tools 检索后注入。"""
        @tool("demo_extended", "演示", {}, resident=False)
        def f():
            return "ok"

        resident_names = [s["function"]["name"] for s in get_all_tool_schemas()]
        extended_names = [s["function"]["name"] for s in tool_registry.get_extended_tool_schemas()]
        assert "demo_extended" not in resident_names
        assert "demo_extended" in extended_names

    def test_search_tools_empty_extended_gives_guidance(self, clean_tools):
        """没有可发现工具时返回明确指引而非空列表（空列表会让模型以为检索失败）。"""
        # 真实注册表常驻 3 个可发现工具（search_history/todo × 2），先移除模拟空档
        for name in ("search_history", "todo_write", "todo_read"):
            TOOLS.pop(name)
        assert "没有额外的可发现工具" in TOOLS["search_tools"]()

    def test_search_tools_returns_only_extended(self, clean_tools):
        """核心互斥：search_tools 绝不能返回常驻工具（常驻 + 注入 = duplicate 400）。"""
        @tool("demo_extended2", "演示", {}, resident=False)
        def f():
            return "ok"

        names = {s["function"]["name"] for s in json.loads(TOOLS["search_tools"]())}
        assert names == {"search_history", "todo_write", "todo_read", "demo_extended2"}
        assert "read_file" not in names  # 常驻四件套绝不在可发现档

    def test_core_four_resident_others_discoverable(self):
        """分档契约：read/write/edit_file + run_bash 常驻，其余走 search_tools 发现。"""
        resident = {s["function"]["name"] for s in get_all_tool_schemas()}
        extended = {s["function"]["name"] for s in tool_registry.get_extended_tool_schemas()}
        assert resident == {"read_file", "write_file", "edit_file", "run_bash"}
        assert extended == {"search_history", "todo_write", "todo_read"}

    def test_business_tools_registered(self):
        """导入 tools 后核心业务工具全部注册（防装饰器被误删）。"""
        for name in ("read_file", "write_file", "edit_file", "run_bash",
                     "todo_write", "todo_read", "search_tools"):
            assert name in TOOLS


class TestCommandRegistry:
    def test_duplicate_registration_warns(self, capsys):
        """重名静默覆盖容易藏 bug：必须显式告警。"""
        @command_registry.command("/dup_test", "第一次")
        def a(session, args=""):
            pass

        @command_registry.command("/dup_test", "第二次")
        def b(session, args=""):
            pass

        try:
            assert "重复注册" in capsys.readouterr().err
            assert command_registry.COMMANDS["/dup_test"] is b  # 后者覆盖前者
        finally:
            del command_registry.COMMANDS["/dup_test"]

    def test_handler_signature_contract(self):
        """所有已注册命令必须接受 (session, args) 签名。"""
        import inspect

        import commands  # noqa: F401  触发注册
        for name, fn in command_registry.COMMANDS.items():
            params = list(inspect.signature(fn).parameters)
            assert len(params) >= 2, f"{name} 签名不符合 (session, args) 约定"

    def test_core_commands_registered(self):
        import commands  # noqa: F401
        for name in ("/help", "/clear", "/tokens", "/tools", "/resume", "/compact"):
            assert name in command_registry.COMMANDS
