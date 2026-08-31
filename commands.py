"""斜杠命令执行体：handler 统一接收 (session, args)，无参数命令忽略 args。

输出风格约定：列表型命令共用 _render_rows（rich Table 两列排版——
左列命令名 accent 色，右列描述暗色；CJK 宽度由 rich 自动处理，
取代旧版手工 \\033 + east_asian_width 对齐）。
"""

import os

from rich.table import Table

import ui
from agent import ChatSession, load_saved_session
from command_registry import command, COMMANDS
from compact import apply_message_cap, detect_slim_targets, apply_slimming, estimate_total_tokens, detect_truncation_point, \
    summarize_middle, extract_middle, apply_truncation, current_chars_per_token
from config import QUIT_COMMANDS, SESSION_FILE, SYSTEM_MESSAGES, TRUNCATE_LOW_TOKENS
from tool_registry import TOOLS
from tools import clear_todo_file


def _render_rows(rows: list[tuple[str, str]]) -> None:
    """两列对齐渲染：左列加粗 accent，右列暗色描述。"""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    table.add_column(style="key", no_wrap=True)
    table.add_column(style="muted")
    for name, desc in rows:
        table.add_row(name, desc)
    ui.console.print(table)


@command("/help", "列出所有命令及用法")
def cmd_help(session: ChatSession, args: str = ""):
    _render_rows([(name, func.description) for name, func in COMMANDS.items()])
    ui.console.print(f"\n[faint]退出: {' / '.join(QUIT_COMMANDS)}（或 Ctrl+C / Ctrl+D）[/]")


@command("/clear", "清空对话历史，开始新会话")
def cmd_clear(session: ChatSession, args: str = ""):
    # 重置为 system 模板（含注入的工具声明一并清除，回到全新会话状态）
    session.messages = list(SYSTEM_MESSAGES)
    # token 计数器一并归零：/clear 语义是"全新会话"，累计消耗不应跨会话保留
    session.total_prompt_tokens = 0
    session.total_completion_tokens = 0
    clear_todo_file()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)  # 存档一并清除：/clear 后 /resume 不应复活旧会话
    ui.success("会话已清空，开始新的对话")


@command("/tokens", "显示 token 消耗明细与上下文规模")
def cmd_tokens(session: ChatSession, args: str = ""):
    prompt = session.total_prompt_tokens
    completion = session.total_completion_tokens
    _render_rows([
        ("prompt", f"{prompt:,} tokens"),
        ("completion", f"{completion:,} tokens"),
        ("累计", f"{prompt + completion:,} tokens"),
        ("上下文", f"{len(session.messages)} 条消息（估算 {estimate_total_tokens(session.messages):,} tokens）"),
        ("估算系数", f"{current_chars_per_token():.2f} 字符/token（随真实 usage 动态校准）"),
    ])


@command("/tools", "列出当前已注册工具（名字+描述）")
def cmd_tools(session: ChatSession, args: str = ""):
    schemas = [fn.tool_schema for fn in TOOLS.values() if hasattr(fn, "tool_schema")]
    _render_rows([(s["function"]["name"], s["function"]["description"]) for s in schemas])


@command("/resume", "恢复上次保存的会话")
def cmd_resume(session: ChatSession, args: str = ""):
    data = load_saved_session()
    if not data:
        ui.note("没有可恢复的会话存档")
        return
    session.messages = data["messages"]
    session.total_prompt_tokens = data.get("total_prompt_tokens", 0)
    session.total_completion_tokens = data.get("total_completion_tokens", 0)
    ui.success(f"已恢复会话：{len(session.messages)} 条消息"
               f"（估算 {estimate_total_tokens(session.messages):,} tokens）")
    # 最近一条用户消息作为话题提示，帮助用户回忆起上下文
    last_user = next(
        (str(m.get("content", "")) for m in reversed(session.messages) if m.get("role") == "user"),
        "",
    )
    if last_user:
        ui.note(f"最近话题：{last_user[:50]}{'...' if len(last_user) > 50 else ''}")


@command("/compact", "主动压缩早期历史（L2 摘要，失败回退硬切）")
def cmd_compact(session: ChatSession, args: str = ""):
    # 手动压缩语义：用户下令即执行，不受自动水位线限制——直接向 LOW 水位切；
    # 瘦身的触发/收益门槛同理跳过（trigger_chars=0, min_savings=0）
    slimmed = apply_message_cap(apply_slimming(
        session.messages,
        detect_slim_targets(session.messages, trigger_chars=0, min_savings=0),
    ))
    before = estimate_total_tokens(slimmed)
    cut = detect_truncation_point(slimmed, TRUNCATE_LOW_TOKENS)
    if not cut:
        ui.note(f"当前历史约 {before:,} tokens，规模健康，无需压缩")
        return

    session.compact_archive = list(session.messages)  # 归档原文：突变前的后悔药
    # L2 优先：让模型把中段压缩成交接摘要；失败时 note=None 回退 L1 硬切标记
    summary = summarize_middle(extract_middle(slimmed, cut), session.client,
                               on_note=lambda m: ui.note(m, tag="compact"))
    note = f"[早期对话历史摘要]\n{summary}" if summary else None
    session.messages = apply_truncation(slimmed, cut, note)

    kind = "L2 摘要" if summary else "L1 硬切"
    after = estimate_total_tokens(session.messages)
    ui.success(f"会话已压缩（{kind}）：约 {before:,} → {after:,} tokens，原文已归档到 session.compact_archive")
    session.save()  # 突变后立即落盘，保持存档与内存一致
