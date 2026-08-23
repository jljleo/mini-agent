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
        """search_tools 常驻顶层请求，聚合时必须排除（否则 duplicate tool name 400）。"""
        names = [s["function"]["name"] for s in get_all_tool_schemas()]
        assert "search_tools" not in names

    def test_search_tools_executable_returns_json(self):
        result = TOOLS["search_tools"]()
        names = [s["function"]["name"] for s in json.loads(result)]
        assert "read_file" in names  # 业务工具在列

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
