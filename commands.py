import json

from agent import ChatSession
from command_registry import command, COMMANDS
from config import SYSTEM_MESSAGES
from tool_registry import TOOLS


@command("/help", "列出所有命令及用法")
def cmd_help(session):
    for name, func in COMMANDS.items():
        print(f"{name}: {func.description}")


@command("/quit", "退出程序")
def cmd_quit(session):
    print("Bye!")
    exit(0)

@command("/clear", "清空对话历史，开始新会话")
def cmd_clear(session: ChatSession):
    # 重置为 system 模板（含注入的工具声明一并清除，回到全新会话状态）
    session.messages = list(SYSTEM_MESSAGES)
    print("会话已清空")
@command("/tokens", "显示当前会话已消耗的tokens数")
def cmd_tokens(session: ChatSession):
    tokens = session.total_completion_tokens + session.total_prompt_tokens
    print(f"当前会话已消耗 {tokens} 个tokens")

@command("/tools", "列出当前已注册工具（名字+描述）")
def cmd_tools(session: ChatSession):
    tools = [fn.tool_schema for fn in TOOLS.values() if hasattr(fn, "tool_schema")]
    for tool in tools:
        # tool_dict = json.loads(tool)
        print(tool["function"]["name"], tool["function"]["description"])
