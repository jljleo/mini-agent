"""L3 工具结果瘦身：超龄 tool 消息在发送时投影为占位符，存储不动。

- detect_slim_targets：轮边界（chat 开头）检测一次，返回需瘦身的消息下标集
- apply_slimming：纯函数投影，每次发请求时应用；只给目标消息生成新 dict，不改存储
- 占位符自带恢复线索（工具名 + 参数回显 + 原长度），模型需要时可自助重取
"""

from config import (
    SLIM_TRIGGER_CHARS,
    TOOL_ARG_ECHO_LEN,
    TOOL_RESULT_KEEP_RECENT,
    TOOL_RESULT_MIN_SLIM_LEN,
)


def detect_slim_targets(
    messages: list[dict],
    keep_recent: int = TOOL_RESULT_KEEP_RECENT,
    min_len: int = TOOL_RESULT_MIN_SLIM_LEN,
    trigger_chars: int = SLIM_TRIGGER_CHARS,
) -> set[int]:
    """检测需瘦身的 tool 消息下标：超龄（不在最近 keep_recent 条 tool 消息内）且原文够长。

    保护窗口按 tool 消息计数而非全部消息：agent 循环中 tool 消息成簇出现，
    按总消息数保护会让窗口随簇大小漂移。
    总字符数低于 trigger_chars 时返回空集：不动作 = prompt cache 完全无损。
    """
    total_chars = sum(len(str(m.get("content") or "")) for m in messages)
    if total_chars < trigger_chars:
        return set()

    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    # 切片写法兼容 keep_recent=0（[:-0] 会得到空列表，与语义相反）
    aged = tool_indices[: max(0, len(tool_indices) - keep_recent)]
    return {i for i in aged if len(str(messages[i].get("content") or "")) >= min_len}


def apply_slimming(messages: list[dict], targets: set[int]) -> list[dict]:
    """发送时投影：目标下标的消息换占位符（生成新 dict），其余共享原引用。

    返回值可能是原列表（targets 为空时），调用方必须只读。
    下标越界与 role 校验是双保险：detect 侧的错误只降级为"漏处理"，不会"换错消息"。
    """
    if not targets:
        return messages
    out = list(messages)  # 浅拷贝列表壳，dict 仍共享；只给被改的消息造新 dict
    for index in targets:
        if 0 <= index < len(out) and out[index].get("role") == "tool":
            out[index] = {**out[index], "content": _make_placeholder(messages, index)}
    return out


def _make_placeholder(messages: list[dict], index: int) -> str:
    """生成占位符：工具名 + 参数回显 + 原长度 + 恢复引导（按可再生性分两类文案）。"""
    msg = messages[index]
    name = msg.get("name", "unknown")
    original_len = len(str(msg.get("content") or ""))
    args_echo = _find_args_echo(messages, index)
    if name == "run_bash":
        # bash 结果可能是 curl 联网快照等不可再生观测，重跑拿到的是最新状态而非原文
        hint = "可重跑该命令获取最新结果（原结果为历史快照）"
    else:
        hint = f"如需内容，可重新调用 {name} 获取"
    return f"[历史工具结果已瘦身] {name}{args_echo}，原 {original_len} 字符。{hint}。"


def _find_args_echo(messages: list[dict], index: int) -> str:
    """参数回显：向前找配对 assistant 消息中同 tool_call_id 的 arguments，压空白并截断。"""
    tool_call_id = messages[index].get("tool_call_id")
    for msg in reversed(messages[:index]):
        if msg.get("role") != "assistant":
            continue
        for tool_call in msg.get("tool_calls") or []:
            if tool_call.get("id") == tool_call_id:
                raw = " ".join((tool_call.get("function", {}).get("arguments") or "").split())
                if len(raw) > TOOL_ARG_ECHO_LEN:
                    raw = raw[:TOOL_ARG_ECHO_LEN] + "..."
                return f"({raw})"
    return ""
