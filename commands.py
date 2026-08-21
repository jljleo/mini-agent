"""斜杠命令执行体：handler 统一接收 session，需要状态的命令自取。

输出风格约定：命令名加粗青色，描述灰色（与工具可视化的 \\033[90m 同风格），
两列对齐——所有列表型命令共用 _print_rows。
"""

import unicodedata

from agent import ChatSession
from command_registry import command, COMMANDS
from compact import detect_slim_targets, apply_slimming, estimate_total_tokens, detect_truncation_point, \
    summarize_middle, extract_middle, apply_truncation, current_chars_per_token
from config import QUIT_COMMANDS, SYSTEM_MESSAGES, TRUNCATE_LOW_TOKENS
from tool_registry import TOOLS
from tools import clear_todo_file

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
    clear_todo_file()
    print("会话已清空")


@command("/tokens", "显示 token 消耗明细与上下文规模")
def cmd_tokens(session: ChatSession):
    prompt = session.total_prompt_tokens
    completion = session.total_completion_tokens
    _print_rows([
        ("prompt", f"{prompt} tokens"),
        ("completion", f"{completion} tokens"),
        ("累计", f"{prompt + completion} tokens"),
        ("上下文", f"{len(session.messages)} 条消息（估算 {estimate_total_tokens(session.messages)} tokens）"),
        ("估算系数", f"{current_chars_per_token():.2f} 字符/token（随真实 usage 动态校准）"),
    ])


@command("/tools", "列出当前已注册工具（名字+描述）")
def cmd_tools(session: ChatSession):
    schemas = [fn.tool_schema for fn in TOOLS.values() if hasattr(fn, "tool_schema")]
    _print_rows([(s["function"]["name"], s["function"]["description"]) for s in schemas])

@command("/compact", "主动压缩早期历史（L2 摘要，失败回退硬切）")
def cmd_compact(session: ChatSession):
    # 手动压缩语义：用户下令即执行，不受自动水位线限制——直接向 LOW 水位切；
    # 瘦身的触发/收益门槛同理跳过（trigger_chars=0, min_savings=0）
    slimmed = apply_slimming(
        session.messages,
        detect_slim_targets(session.messages, trigger_chars=0, min_savings=0),
    )
    before = estimate_total_tokens(slimmed)
    cut = detect_truncation_point(slimmed, TRUNCATE_LOW_TOKENS)
    if not cut:
        print(f"当前历史约 {before} tokens，规模健康，无需压缩")
        return

    session.compact_archive = list(session.messages)  # 归档原文：突变前的后悔药
    # L2 优先：让模型把中段压缩成交接摘要；失败时 note=None 回退 L1 硬切标记
    summary = summarize_middle(extract_middle(slimmed, cut), session.client)
    note = f"[早期对话历史摘要]\n{summary}" if summary else None
    session.messages = apply_truncation(slimmed, cut, note)

    kind = "L2 摘要" if summary else "L1 硬切"
    after = estimate_total_tokens(session.messages)
    print(f"会话已压缩（{kind}）：约 {before} → {after} tokens，原文已归档到 session.compact_archive")
