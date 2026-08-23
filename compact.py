"""历史管理：L3 工具结果瘦身 + 单条体积上限 + L1 历史截断，均为发送时投影，存储不动。

执行顺序（重要）：先 L3 瘦身（无损、便宜），瘦身后估算仍超硬水位才 L1 截断（有损、兜底）。

- detect_slim_targets / apply_slimming：超龄 tool 消息换占位符（带恢复线索）
- apply_message_cap：单条消息体积上限（中段截断保首尾）——补上 MAX_OUTPUT_LEN
  没盖住的那半边：user 输入无天然上限，首条 user 超大会让保留区自身爆窗
- detect_truncation_point：反向装箱算截断点——从尾部往回装，装满低水位就停，
  再吸附到安全边界（不制造孤儿 tool 消息），O(n) 一趟无试错
- apply_truncation：头部保留区（system 模板 + 动态注入的 tools 声明）+ 截断标记 + 尾部
- extract_middle / summarize_middle：L2 摘要——被移出窗口的中段先让模型压缩成交接摘要
  插回投影；摘要失败回退 L1 的截断标记（L1 是 L2 的 fallback）
"""

import json

import ui
from config import (
    MODEL,
    SINGLE_MSG_CAP_CHARS,
    SLIM_MIN_SAVINGS_CHARS,
    SLIM_TRIGGER_CHARS,
    SUMMARIZE_MAX_CHARS,
    TOOL_ARG_ECHO_LEN,
    TOOL_RESULT_KEEP_RECENT,
    TOOL_RESULT_MIN_SLIM_LEN,
)


def detect_slim_targets(
    messages: list[dict],
    keep_recent: int = TOOL_RESULT_KEEP_RECENT,
    min_len: int = TOOL_RESULT_MIN_SLIM_LEN,
    trigger_chars: int = SLIM_TRIGGER_CHARS,
    min_savings: int = SLIM_MIN_SAVINGS_CHARS,
) -> set[int]:
    """检测需瘦身的消息下标：超龄 tool 结果（L3）+ 超龄 assistant 的长推理（L3.5）。

    保护窗口按角色各自计数（默认最近 5 条不动）：agent 循环中 tool 消息成簇出现，
    按总消息数保护会让窗口随簇大小漂移。
    总字符数低于 trigger_chars 时返回空集：不动作 = prompt cache 完全无损。
    """
    # 与 L1 共用同一份负载口径（_count_chars：含 reasoning_content 与 tool_calls 参数）
    total_chars = sum(_count_chars(m) for m in messages)
    if total_chars < trigger_chars:
        return set()

    # 切片写法兼容 keep_recent=0（[:-0] 会得到空列表，与语义相反）
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    aged_tools = tool_indices[: max(0, len(tool_indices) - keep_recent)]
    targets = {i for i in aged_tools if len(str(messages[i].get("content") or "")) >= min_len}

    # L3.5：超龄 assistant 的旧推理——价值在产生当轮已兑现（结论在 content 里），
    # 是典型“低价值大体积”。已实测占位符/缺失均过 API 校验（2026-08-20）
    asst_indices = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    aged_assts = asst_indices[: max(0, len(asst_indices) - keep_recent)]
    targets |= {i for i in aged_assts
                if len(str(messages[i].get("reasoning_content") or "")) >= min_len}

    # 收益门槛：省下总量太小就不动——瘦身会顶掉被改位置之后的缓存前缀，
    # 省几百字符赔几千 tokens 的重算是净亏损
    savings = sum(
        len(str(messages[i].get("content") or "")) if messages[i].get("role") == "tool"
        else len(str(messages[i].get("reasoning_content") or ""))
        for i in targets
    )
    if savings < min_savings:
        return set()
    return targets


def apply_slimming(messages: list[dict], targets: set[int]) -> list[dict]:
    """发送时投影：目标下标的消息换占位符（生成新 dict），其余共享原引用。

    返回值可能是原列表（targets 为空时），调用方必须只读。
    下标越界与 role 校验是双保险：detect 侧的错误只降级为"漏处理"，不会"换错消息"。
    """
    if not targets:
        return messages
    out = list(messages)  # 浅拷贝列表壳，dict 仍共享；只给被改的消息造新 dict
    for index in targets:
        if not 0 <= index < len(out):
            continue
        role = out[index].get("role")
        if role == "tool":
            out[index] = {**out[index], "content": _make_placeholder(messages, index)}
        elif role == "assistant" and out[index].get("reasoning_content"):
            # L3.5：只换 reasoning_content，content/tool_calls 原样保留（配对结构不动）
            original_len = len(str(out[index]["reasoning_content"]))
            out[index] = {**out[index],
                          "reasoning_content": f"[历史推理已省略，原 {original_len} 字符]"}
    return out


def _make_placeholder(messages: list[dict], index: int) -> str:
    """生成占位符：工具名 + 参数回显 + 原长度 + 恢复引导（按可再生性分两类文案）。"""
    msg = messages[index]
    name = msg.get("name", "unknown")
    original_len = len(str(msg.get("content") or ""))
    args_echo = _find_args_echo(messages, index)
    if name == "run_bash":
        # bash 结果可能是 curl 联网快照等不可再生观测，重跑拿到的是最新状态而非原文
        hint = "可重跑该命令获取最新结果（原结果为历史快照），或经 search_tools 发现 search_history 后检索原文"
    else:
        hint = f"如需内容，可重新调用 {name} 获取，或经 search_tools 发现 search_history 后检索原文"
    return f"[历史工具结果已瘦身] {name}{args_echo}，原 {original_len} 字符。{hint}。"


# ---- 单条体积上限 ----


def apply_message_cap(messages: list[dict], cap: int = SINGLE_MSG_CAP_CHARS) -> list[dict]:
    """单条消息体积上限（投影）：content 超 cap 时中段截断保首尾，存储不动。

    分层防御的“单条体积”层。MAX_OUTPUT_LEN 只盖住工具产出，user 输入无天然上限：
    首条 user 超大会让保留区自身爆窗（它无条件保留，会话永久报废，只能 /clear）；
    末条 user 超大会让 L1 兜底失效（保留尾部本身超窗口，API 必 400）。
    对全 role 一视同仁：位置语义（保留区/尾部）由截断层管，这层只管体积。
    保首尾的依据：任务目标在开头、最新补充在结尾，中段截断损失最小。
    """
    out = None
    for i, m in enumerate(messages):
        content = str(m.get("content") or "")
        if len(content) <= cap:
            continue
        if out is None:
            out = list(messages)  # 惰性拷贝：无超标消息时恒等返回原列表（零打扰）
        head = cap // 2
        tail = cap - head
        omitted = len(content) - head - tail
        out[i] = {**m, "content": (
            content[:head]
            + f"\n...[中间省略 {omitted} 字符，可经 search_tools 发现 search_history 后检索完整内容]...\n"
            + content[-tail:]
        )}
    return out if out is not None else messages


# ---- token 估算 ----

# 字符/token 换算系数：初值 2.0 是中英代码混合语料的经验值，
# 之后每轮用 API 返回的真实 prompt_tokens 做 EMA 校准（观测形成闭环）。
_chars_per_token = 2.0


def current_chars_per_token() -> float:
    """当前校准后的换算系数（供 /tokens 等观测入口展示）。"""
    return _chars_per_token


def calibrate(real_tokens: int, messages: list[dict]) -> None:
    """用一次真实请求的 prompt_tokens 校准估算系数（指数滑动平均）。

    偏离越多调整越多；EMA 平滑单轮噪声。
    注意 real_tokens 包含工具声明等消息外开销，会使估算略偏保守（安全方向）。
    """
    global _chars_per_token
    chars = sum(_count_chars(m) for m in messages)
    if real_tokens < 1_000 or chars < 1_000:
        return  # 小样本噪声大，不校准
    observed = chars / real_tokens  # 该轮真实的字符/token
    _chars_per_token = min(6.0, max(0.8, 0.7 * _chars_per_token + 0.3 * observed))


# ---- L1 截断 ----

# 截断后在拼接处插入的标记：让模型知道历史被切过，而不是对话本来就这么多；
# 附恢复指引——被切部分在存储里完好。search_history 是可发现工具（resident=False），
# 指引需走两跳：先 search_tools 发现，再调用检索（承诺必须可兑现）
_TRUNCATION_MARKER = {"role": "system", "content": "[早期对话历史已截断，仅保留近期内容；如需被截断部分的细节，可经 search_tools 发现 search_history 后检索]"}


def _count_chars(msg: dict) -> int:
    """单条消息的负载字符数：content + reasoning_content + tool_calls 参数。

    估算与校准共用的唯一口径——“什么算负载”只在这定义，防两处漏改。
    reasoning_content 必须计入：kimi-k3 始终推理且随消息回传（缺了 400），是真实负载；
    tool_calls 的 arguments 同理：write_file 的整个文件内容在参数里。
    """
    chars = len(str(msg.get("content") or ""))
    chars += len(str(msg.get("reasoning_content") or ""))
    for tc in msg.get("tool_calls") or []:
        chars += len(tc.get("function", {}).get("arguments") or "")
    return chars


def estimate_tokens(msg: dict) -> int:
    """单条消息的 token 估算：负载字符数 ÷ 动态校准的换算系数。"""
    return int(_count_chars(msg) / _chars_per_token)


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


def apply_truncation(messages: list[dict], cut: int, note: str | None = None) -> list[dict]:
    """L1 投影：头部保留区 + 中间标记 + messages[cut:] 尾部。cut=0 时恒等返回原列表。

    note 为 None 时是硬切标记（纯 L1）；传入 L2 摘要文本时标记变为摘要，同一切点保值。
    """
    if cut <= 0:
        return messages
    prefix = _prefix_indices(messages)
    head = [m for i, m in enumerate(messages) if i in prefix]
    tail = [m for i, m in enumerate(messages) if i >= cut and i not in prefix]
    # 实际没丢任何消息时不插标记（如首轮全在保留区导致的空中段），避免误导模型
    dropped = len(messages) - len(head) - len(tail)
    if dropped == 0:
        return head + tail
    marker = {"role": "system", "content": note} if note else _TRUNCATION_MARKER
    return head + [marker] + tail


# ---- L2 摘要 ----

# 摘要模板：业界收敛的 anchored 四段结构（intent/changes/decisions/next steps）。
# 要点：先求全再求精（Anthropic：maximize recall first）；技术细节逐字保留（失真高发区）；
# 下一步要带停止条件（JetBrains：摘要会模糊自然停止信号，拖长轨迹）。
SUMMARIZE_SYSTEM_PROMPT = (
    "你正在执行上下文压缩：一段对话历史即将从模型的上下文窗口中移除。\n"
    "注意：这是对话的中段而非开头——更早的内容（如最初任务）仍保留在上下文中，"
    "不要把中段的第一条当作对话的起点。\n"
    "你要把它压缩成一份给接手模型看的交接摘要。\n"
    "要求：\n"
    "1. 先求全再求精：宁多勿漏，高保真优先于简短。\n"
    "2. 严格按四段输出：\n"
    "   【原始意图】用户最初的目标\n"
    "   【已完成】已做的改动/得出的结论（文件路径、命令、报错等关键细节逐字保留，不要改写）\n"
    "   【关键决策】技术决策及原因\n"
    "   【下一步】接下来要做什么，写明停止条件\n"
    "3. 只输出摘要正文，不要任何客套话。"
)


def extract_middle(messages: list[dict], cut: int) -> list[dict]:
    """提取将被移出窗口的中段（L2 摘要的输入）：cut 之前且不属于头部保留区的消息。"""
    prefix = _prefix_indices(messages)
    return [m for i, m in enumerate(messages) if i < cut and i not in prefix]


def summarize_middle(middle: list[dict], client, model: str = MODEL) -> str | None:
    """调用模型把中段历史压缩成交接摘要；任何失败返回 None（上层回退 L1 硬切）。

    L2 被触发的时刻正是上下文将爆的压力时刻，摘要请求自身失败概率偏高，
    所以这里必须静默容错，绝不能让压缩动作把主流程拖崩。
    """
    if not middle:
        return None
    serialized = json.dumps(middle, ensure_ascii=False, default=str)
    if len(serialized) > SUMMARIZE_MAX_CHARS:
        serialized = "...（更早部分略）\n" + serialized[-SUMMARIZE_MAX_CHARS:]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": "请压缩以下对话历史：\n\n" + serialized},
            ],
            stream=False,
        )
        return resp.choices[0].message.content
    except Exception as e:
        ui.note(f"L2 摘要失败（{type(e).__name__}），回退 L1 硬切", tag="compact")
        return None


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
