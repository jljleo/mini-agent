"""agent 对话循环：ChatSession 封装对话状态，把全局 messages 收进对象。

职责：
    - 持有 messages（对话历史）与 OpenAI client
    - chat() 主循环：发请求 → 流式拼装 → 执行工具 → 动态注入声明，循环到出终稿
    - 工具执行的可视化与容错
    - 异常回滚：本轮失败时整体撤销已产生的消息

工具精简后，本模块不再感知具体工具——执行表来自 registry.TOOLS，
动态声明来自 registry.get_all_tool_schemas()，新增工具零改动接入。
"""

import json
import os

from openai import OpenAI
from rich.console import Console

from config import (
    API_KEY_ENV,
    BASE_URL,
    MAX_TOOL_ROUNDS,
    MODEL,
    SYSTEM_MESSAGES,
    TOOL_RESULT_PREVIEW_LEN,
    TRUNCATE_HIGH_TOKENS,
    TRUNCATE_LOW_TOKENS,
)
from compact import (
    apply_slimming,
    apply_truncation,
    calibrate,
    detect_slim_targets,
    detect_truncation_point,
    estimate_total_tokens,
    extract_middle,
    summarize_middle,
)
from tool_registry import SEARCH_TOOLS_SCHEMA, TOOLS, get_all_tool_schemas
from streaming import stream_and_assemble

console = Console()

# 顶层请求常驻的工具：search_tools（动态发现入口）
# NOTE: $web_search（WEB_SEARCH_SCHEMA）暂不常驻——Moonshot 平台 bug：
# kimi-k3 上回传 builtin_function 工具结果必现 400 tokenization failed
# （官方论坛 2026-07-23 已报，未修；kimi-k2.6 正常但 k2.6 不支持动态工具注入机制）。
# 接入代码（agent._execute_tool_call 的 echo 逻辑）已就绪，平台修复后加回 BASE_TOOLS 即可。
BASE_TOOLS = [SEARCH_TOOLS_SCHEMA]


class ChatSession:
    """一轮完整的多轮对话会话：封装 messages 与 client，避免全局状态。"""

    def __init__(self) -> None:
        self.client = OpenAI(api_key=os.environ.get(API_KEY_ENV), base_url=BASE_URL)
        # 拷贝一份 system 模板，避免污染 config 里的原始定义
        self.messages: list[dict] = list(SYSTEM_MESSAGES)
        # token 仪表盘：会话累计消耗
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # ---- 历史管理 ----

    def mark(self) -> int:
        """记录当前历史位置，供失败时回滚。"""
        return len(self.messages)

    def rollback(self, mark: int) -> None:
        """撤销 mark 之后产生的所有消息（异常整体回滚）。"""
        del self.messages[mark:]

    def _tools_already_injected(self) -> bool:
        """是否已注入过动态工具声明（保证幂等，避免重复注入撑胖上下文）。"""
        return any(isinstance(m, dict) and m.get("tools") for m in self.messages)

    # ---- 工具执行 ----

    def _execute_tool_call(self, tool_call: dict) -> tuple[str, str]:
        """执行单个工具调用，返回 (工具名, 结果文本)。任何失败都转为文本结果，不向上抛。"""
        name = tool_call["function"].get("name")
        raw_arguments = tool_call["function"].get("arguments") or "{}"
        # 灰色打印调用过程，让用户看见 agent 在干什么
        print(f"\n\033[90m[调用工具] {name}({raw_arguments})\033[0m", flush=True)
        try:
            if name == "$web_search":
                # Kimi 内置工具：服务端执行搜索，客户端只需把参数原样回传
                result = raw_arguments
            else:
                if name not in TOOLS:
                    raise KeyError(f"Unknown tool: {name}")
                arguments = json.loads(raw_arguments)
                result = str(TOOLS[name](**arguments))  # 兜底：工具可能返回非字符串
        except Exception as e:
            result = f"调用失败: {type(e).__name__}: {e}"
        preview = result if len(result) <= TOOL_RESULT_PREVIEW_LEN else result[:TOOL_RESULT_PREVIEW_LEN] + "..."
        print(f"\033[90m[工具结果] {preview}\033[0m", flush=True)
        return name, result

    # ---- 主循环 ----

    def chat(self, user_input: str) -> None:
        """一轮提问：append → 请求 → 流式 → 工具循环，直到模型给出终稿。"""
        self.messages.append({"role": "user", "content": user_input})

        # 历史管理管道（轮边界检测一次）：先 L3 瘦身，瘦身后估算仍超硬水位才 L1 截断
        slim_targets = detect_slim_targets(self.messages)
        if slim_targets:
            print(f"\033[90m[compact] 已瘦身 {len(slim_targets)} 条超龄工具结果\033[0m")
        # 切点计算也基于瘦身后的投影：占位符只有 ~100 字符，按原始体积装箱会误多切；
        # 瘦身不改消息数量与顺序，下标与原始历史完全对齐，投影可直接复用
        slimmed = apply_slimming(self.messages, slim_targets)
        cut = 0
        note = None
        if estimate_total_tokens(slimmed) >= TRUNCATE_HIGH_TOKENS:
            cut = detect_truncation_point(slimmed, TRUNCATE_LOW_TOKENS)
            if cut:
                # L2 优先：让模型把中段压缩成交接摘要；失败时 note=None 回退 L1 硬切标记
                summary = summarize_middle(extract_middle(slimmed, cut), self.client)
                if summary:
                    note = f"[早期对话历史摘要]\n{summary}"
                    print("\033[90m[compact] 上下文超限，已生成早期历史摘要（L2）\033[0m")
                else:
                    print(f"\033[90m[compact] 上下文超限，已截断早期历史（L1，"
                          f"目标 {TRUNCATE_LOW_TOKENS // 1000}K tokens）\033[0m")

        for _ in range(MAX_TOOL_ROUNDS):
            # 每轮重算投影：工具循环内历史只涨不停（单轮最多 30 次调用×10K 字符），
            # 轮边界的检测看不见"单轮爆炸"，超线时应急升级截断（L1 硬切，不插 L2 调用）
            payload = apply_truncation(apply_slimming(self.messages, slim_targets), cut, note)
            if estimate_total_tokens(payload) >= TRUNCATE_HIGH_TOKENS:
                escalated = detect_truncation_point(
                    apply_slimming(self.messages, slim_targets), TRUNCATE_LOW_TOKENS)
                if escalated > cut:
                    cut, note = escalated, None
                    payload = apply_truncation(apply_slimming(self.messages, slim_targets), cut, note)
                    print("\033[90m[compact] 工具循环内上下文超限，已应急截断（L1）\033[0m")

            # 等待首字到达的间隙显示 spinner，填掉"静默尴尬期"
            with console.status("[dim]思考中...[/dim]", spinner="dots"):
                completion = self.client.chat.completions.create(
                    model=MODEL,
                    # 发送时投影，存储不动；cut 下标对瘦身投影同样有效（瘦身不改消息数量）
                    # note 为 None 时是 L1 硬切标记，为摘要文本时是 L2 保值版
                    messages=payload,
                    tools=BASE_TOOLS,
                    stream=True,
                    stream_options={"include_usage": True}
                )

            assistant_messages, usage = stream_and_assemble(completion)

            if usage:
                calibrate(usage.prompt_tokens, payload)  # 用真实值校准估算系数，观测闭环
                self.total_prompt_tokens += usage.prompt_tokens
                self.total_completion_tokens += usage.completion_tokens
                cached = getattr(usage, "cached_tokens", 0) or 0
                total = self.total_prompt_tokens + self.total_completion_tokens
                print(
                    f"\n\033[90m[tokens] 本轮 prompt={usage.prompt_tokens} completion={usage.completion_tokens}"
                    f"（缓存命中 {cached}）｜累计 {total}\033[0m"
                )

            for assistant_message in assistant_messages:
                finish_reason = assistant_message.pop("_finish_reason", None)
                if finish_reason == "length":
                    if assistant_message.get("tool_calls"):
                        # 截断发生在工具调用轮：arguments JSON 不完整，后续 json 解析失败属预期
                        print("\n\033[93m[警告] 输出被 max_tokens 截断：工具调用参数不完整，若解析失败即为此因\033[0m")
                    else:
                        print("\n\033[93m[警告] 输出被 max_tokens 截断，回答可能不完整\033[0m")
                elif finish_reason == "content_filter":
                    print("\n\033[93m[警告] 内容被安全审查拦截\033[0m")

                self.messages.append(assistant_message)

                tool_calls = assistant_message.get("tool_calls")
                if not tool_calls:
                    # content 已在流式过程中边收边打印，这里只补换行和分隔线
                    print()
                    print("********************************")
                    return

                for tool_call in tool_calls:
                    name, tool_result = self._execute_tool_call(tool_call)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "name": name,  # Moonshot 要求 tool 消息带 name 以便模型配对
                            "content": tool_result,
                        }
                    )

                    # search_tools 被调用后，注入完整工具声明（Moonshot 动态加载机制），幂等
                    if name == "search_tools" and not self._tools_already_injected():
                        self.messages.append(
                            {"role": "system", "tools": get_all_tool_schemas()}
                        )
        else:
            print(f"[Warning] 工具调用超过 {MAX_TOOL_ROUNDS} 轮，已强制结束本轮对话")
