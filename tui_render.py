"""Textual TUI 的纯渲染映射：把 events.py 的非流式事件转成 Rich renderable。

边界：本模块不 import ui、不碰 ChatSession、不处理 TextDelta/ReasoningDelta。
流式正文/推理由 tui.py 的 TranscriptView 聚合渲染；这里只处理离散事件。
"""

from rich.text import Text

from events import Note, ToolCallResult, ToolCallStart, Usage, Warn

_ARG_VALUE_LIMIT = 60


def _format_args(raw: str) -> str:
    import json

    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw if len(raw) <= 80 else raw[:80] + "…"
    if not isinstance(args, dict):
        return str(args)
    parts = []
    for key, value in args.items():
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > _ARG_VALUE_LIMIT:
            text = text[:_ARG_VALUE_LIMIT] + '…"'
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def render_event(event):
    """把离散事件映射为 Rich Text；流式生命周期事件返回 None。"""
    if isinstance(event, ToolCallStart):
        return Text(f"⏺ {event.name}({_format_args(event.arguments)})", style="cyan")
    if isinstance(event, ToolCallResult):
        one_line = " ⏎ ".join(event.preview.splitlines())
        return Text(f"  ⎿  {one_line}", style="grey62")
    if isinstance(event, Note):
        return Text(f"[{event.tag}] {event.message}" if event.tag else event.message,
                    style="grey42")
    if isinstance(event, Warn):
        return Text(f"⚠ {event.message}", style="yellow")
    if isinstance(event, Usage):
        return Text(
            f"tokens · prompt {event.prompt:,} · completion {event.completion:,}"
            f"（缓存 {event.cached:,}）｜累计 {event.total:,}",
            style="grey42",
        )
    return None
