"""流式响应拼装：把 SSE chunk 流聚合成完整的 assistant 消息。

模型流式返回时，content 逐字到达、tool_calls 按碎片分片，
本模块负责边收边打印（content）+ 逐步拼装（tool_calls），输出定稿消息。
"""

from openai.types import CompletionUsage


def stream_and_assemble(completion) -> tuple[list[dict], CompletionUsage | None]:
    """遍历流式 chunk：content 边收边打印，tool_calls 碎片逐步拼装。

    返回 (组装定稿的 assistant 消息列表, usage 对象或 None)。
    usage 是请求级元数据（账单），只走返回值通道，不写进消息体——避免随历史回传 API。
    """
    stream_messages_dict = {}

    stream_usage: CompletionUsage | None = None

    for chunk in completion:

        # usage 在最后一个 chunk 的顶层（此时 choices 为空列表），不进 choice 循环
        usage = getattr(chunk, "usage", None)
        if usage:
            stream_usage = usage

        for choice in chunk.choices:

            index = choice.index
            message = stream_messages_dict.setdefault(index, {})

            delta = choice.delta

            if delta.role:
                message["role"] = delta.role

            # kimi-k3 始终推理：思考过程逐块到达，必须拼进消息里——
            # 带 tool_calls 的 assistant 消息缺 reasoning_content 会被 API 拒绝（400）
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                message["reasoning_content"] = message.get("reasoning_content", "") + reasoning

            content = delta.content
            if content:  # 大部分 chunk 的 content 是 None，必须守卫
                print(content, end="", flush=True)
                message["content"] = message.get("content", "") + content

            if delta.tool_calls:
                _merge_tool_calls(message, delta.tool_calls)

    return _finalize(stream_messages_dict), stream_usage


def _merge_tool_calls(message: dict, delta_tool_calls) -> None:
    """把 tool_calls 碎片合并进 message：一次性字段直接赋值，分片字段累加。"""
    tool_calls = message.setdefault("tool_calls", [])
    for tool_call in delta_tool_calls:
        tool_call_index = tool_call.index
        # 惰性扩容：缺几个补几个，保证下标可访问
        if len(tool_calls) < tool_call_index + 1:
            tool_calls.extend([{}] * (tool_call_index + 1 - len(tool_calls)))
        tool_call_object = tool_calls[tool_call_index]
        tool_call_object["index"] = tool_call_index

        # 一次性字段：直接赋值
        if tool_call.id:
            tool_call_object["id"] = tool_call.id
        if tool_call.type:
            tool_call_object["type"] = tool_call.type

        if tool_call.function:
            function = tool_call_object.setdefault("function", {})
            if tool_call.function.name:
                function["name"] = tool_call.function.name
            # 分片字段：拼接累加
            if tool_call.function.arguments:
                function["arguments"] = function.get("arguments", "") + tool_call.function.arguments


def _finalize(stream_messages_dict: dict) -> list[dict]:
    """定稿：补 role、摘掉组装辅助字段 index。"""
    for message in stream_messages_dict.values():
        message.setdefault("role", "assistant")
        for tc in message.get("tool_calls", []):
            tc.pop("index", None)
    return list(stream_messages_dict.values())
