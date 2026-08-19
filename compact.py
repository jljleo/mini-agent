"""历史管理：L3 工具结果瘦身 + L1 历史截断，均为发送时投影，存储不动。

执行顺序（重要）：先 L3 瘦身（无损、便宜），瘦身后估算仍超硬水位才 L1 截断（有损、兜底）。

- detect_slim_targets / apply_slimming：超龄 tool 消息换占位符（带恢复线索）
- detect_truncation_point：反向装箱算截断点——从尾部往回装，装满低水位就停，
  再吸附到安全边界（不制造孤儿 tool 消息），O(n) 一趟无试错
- apply_truncation：头部保留区（system 模板 + 动态注入的 tools 声明）+ 截断标记 + 尾部
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


# ---- L1 截断 ----

# 截断后在拼接处插入的标记：让模型知道历史被切过，而不是对话本来就这么多
_TRUNCATION_MARKER = {"role": "system", "content": "[早期对话历史已截断，仅保留近期内容]"}


def estimate_tokens(msg: dict) -> int:
    """单条消息的 token 粗估（chars//2）。

    tool_calls 的 arguments 必须计入：write_file 的整个文件内容在参数里，
    是 assistant 消息的体积大头。
    """
    chars = len(str(msg.get("content") or ""))
    for tc in msg.get("tool_calls") or []:
        chars += len(tc.get("function", {}).get("arguments") or "")
    return chars // 2


def estimate_total_tokens(messages: list[dict]) -> int:
    """整个消息列表的 token 粗估，用于 L1 触发判断。"""
    return sum(estimate_tokens(m) for m in messages)


def _prefix_indices(messages: list[dict]) -> set[int]:
    """头部保留区下标：结构性保留 + 语义性保留。

    结构性（少了会崩）：开头连续的 system 模板 + 动态注入的 tools 声明消息
    （role=system 且带 tools 键，可能在历史中段；被截掉模型就看不到业务工具声明，
    工具循环直接残废）。
    语义性（少了不崩但会丢目标）：首轮完整对话（首条 user 起到第二条 user 之前）。
    参考 Claude Code 保留头部若干条的策略：意图与模型的首次回应（最初的约定/计划）
    一起保留，避免“只见任务书、不知做到哪”。mini-agent 面向长任务场景，
    截断后模型仍需知道最初要做什么，防任务漂移。
    首轮若含大工具结果，已被管道前一级的 L3 瘦身压成占位符，不会灌爆保留区。
    """
    indices = set()
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            break
        indices.add(i)
    for i, m in enumerate(messages):
        if m.get("role") == "system" and "tools" in m:
            indices.add(i)
    first_user = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
    if first_user is not None:
        second_user = next(
            (i for i in range(first_user + 1, len(messages)) if messages[i].get("role") == "user"),
            len(messages),
        )
        indices.update(range(first_user, second_user))
    return indices


def detect_truncation_point(messages: list[dict], budget_tokens: int) -> int:
    """反向装箱计算截断点：返回下标 cut，messages[cut:] 为保留尾部；0 表示无需截断。

    从最新往最老累计估算 token，装满预算（扣除头部保留区的开销）即停，
    再向前吸附到安全边界。O(n) 一趟完成，不需要循环试错。
    """
    prefix = _prefix_indices(messages)
    remaining = budget_tokens - sum(estimate_tokens(messages[i]) for i in prefix)

    acc = 0
    cut = None
    for i in range(len(messages) - 1, -1, -1):
        if i in prefix:
            continue  # 保留区不占装箱预算（已提前扣除）
        acc += estimate_tokens(messages[i])
        if acc > remaining:
            cut = i + 1
            break
    if cut is None:
        return 0  # 全部装得下

    # 边界吸附：截断点不能落在 tool 消息上（会与它的 assistant(tool_calls) 拆散，API 必 400）
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1

    # 兜底：预算连最近一轮都装不下时，至少保留最后一个 user 消息起的尾部，不切出空上下文
    if cut >= len(messages):
        for j in range(len(messages) - 1, -1, -1):
            if messages[j].get("role") == "user":
                cut = j
                break
    return cut


def apply_truncation(messages: list[dict], cut: int) -> list[dict]:
    """L1 投影：头部保留区 + 截断标记 + messages[cut:] 尾部。cut=0 时恒等返回原列表。"""
    if cut <= 0:
        return messages
    prefix = _prefix_indices(messages)
    head = [m for i, m in enumerate(messages) if i in prefix]
    tail = [m for i, m in enumerate(messages) if i >= cut and i not in prefix]
    return head + [_TRUNCATION_MARKER] + tail


def _find_args_echo(messages: list[dict], index: int) -> str:
    """参数回显：向前找配对 assistant 消息中同 tool_call_id 的 arguments，压空白并截断。"""
    tool_call_id = messages[index].get("tool_call_id")
    # 一次性有多个tool_calls所以不能直接查上一个
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
