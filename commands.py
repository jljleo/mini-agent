"""斜杠命令执行体：handler 统一接收 session，需要状态的命令自取。

输出风格约定：命令名加粗青色，描述灰色（与工具可视化的 \\033[90m 同风格），
两列对齐——所有列表型命令共用 _print_rows。
"""

import unicodedata

from agent import ChatSession
from command_registry import command, COMMANDS
from config import QUIT_COMMANDS, SYSTEM_MESSAGES
from tool_registry import TOOLS

_BOLD_CYAN = "\033[1;36m"
_GRAY = "\033[90m"
_RESET = "\033[0m"


def _display_width(text: str) -> int:
    """终端显示宽度：中文等宽字符占 2 列，len() 会把它们算成 1，直接排版会错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _print_rows(rows: list[tuple[str, str]], indent: str = "  ") -> None:
    """两列对齐打印：左列加粗青色，右列灰色描述。

    先在纯文本上按显示宽度排版，再包颜色码，避免 ANSI 转义序列计入列宽。
    """
    width = max(_display_width(name) for name, _ in rows)
    for name, desc in rows:
        pad = " " * (width - _display_width(name))
        print(f"{indent}{_BOLD_CYAN}{name}{pad}{_RESET}  {_GRAY}{desc}{_RESET}")


@command("/help", "列出所有命令及用法")
def cmd_help(session):
    _print_rows([(name, func.description) for name, func in COMMANDS.items()])
    print(f"\n  {_GRAY}退出: {' / '.join(QUIT_COMMANDS)}（或 Ctrl+C / Ctrl+D）{_RESET}")


@command("/clear", "清空对话历史，开始新会话")
def cmd_clear(session: ChatSession):
    # 重置为 system 模板（含注入的工具声明一并清除，回到全新会话状态）
    session.messages = list(SYSTEM_MESSAGES)
    # token 计数器一并归零：/clear 语义是"全新会话"，累计消耗不应跨会话保留
    session.total_prompt_tokens = 0
    session.total_completion_tokens = 0
    print("会话已清空")


@command("/tokens", "显示 token 消耗明细与上下文规模")
def cmd_tokens(session: ChatSession):
    prompt = session.total_prompt_tokens
    completion = session.total_completion_tokens
    _print_rows([
        ("prompt", f"{prompt} tokens"),
        ("completion", f"{completion} tokens"),
        ("累计", f"{prompt + completion} tokens"),
        ("上下文", f"{len(session.messages)} 条消息"),
    ])


@command("/tools", "列出当前已注册工具（名字+描述）")
def cmd_tools(session: ChatSession):
    schemas = [fn.tool_schema for fn in TOOLS.values() if hasattr(fn, "tool_schema")]
    _print_rows([(s["function"]["name"], s["function"]["description"]) for s in schemas])
