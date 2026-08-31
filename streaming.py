"""流式响应拼装：把 SSE chunk 流聚合成完整的 assistant 消息。

模型流式返回时，content 逐字到达、tool_calls 按碎片分片，
本模块负责逐步拼装；拼装过程同时产出事件流（events.py）——
TextDelta / ReasoningDelta 逐 chunk 产出，StreamFinished 收尾带回定稿与 usage。
本模块不感知任何界面：渲染是事件消费者（ui.consume 等）的职责。
"""

import queue as _queue
import threading as _threading

from events import ReasoningDelta, StreamFinished, TextDelta


class StreamAborted(Exception):
    """中断断流：interruptible_stream 检测到中断旗帜时抛出（chat 捕获后走优雅收尾）。"""


def interruptible_stream(open_stream, control):
    """可中断的流式包装（生成器）：把“发请求 + 逐 chunk 读取”全程挪进后台线程，
    主侧 100ms 粒度轮询中断旗帜——任何阶段（连接/TTFT/流式中途）打断延迟都有界。

    为什么需要它：实测 openai/httpx 的流在另一线程阻塞读取时，close() 无法
    唤醒 recv（abort 后 15s 无响应）。那就反过来：让阻塞留在后台线程里，
    消费侧永不长时间阻塞，中断时抬手就走（StreamAborted），后台泵线程由
    daemon + closer 尽力回收（连接句柄随 GC/服务端超时释放）。

    open_stream：无参工厂，调用即发起请求并返回 chunk 迭代器。
    中断时抛 StreamAborted；泵线程内的异常（网络错误等）原样 re-raise。
    """
    q = _queue.Queue()
    done = object()  # 泵结束哨兵（正常或异常）

    def pump() -> None:
        close = None
        try:
            completion = open_stream()
            # 注册 closer：abort() 时尽力断流（加速后台线程退出与连接释放）
            close = getattr(completion, "close", None)
            if close:
                control.register_closer(close)
            for chunk in completion:
                q.put(chunk)
        except BaseException as e:
            q.put(e)
        finally:
            if close:
                control.unregister_closer(close)  # 流结束即注销，防 closer 跨请求累积
            q.put(done)

    _threading.Thread(target=pump, daemon=True).start()

    while True:
        try:
            item = q.get(timeout=0.1)
        except _queue.Empty:
            if control.interrupt.is_set():
                raise StreamAborted()
            continue
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def stream_and_assemble(completion):
    """生成器：遍历流式 chunk，边拼装边产出事件。

    产出序列：ReasoningDelta / TextDelta（任意数量、可交错）→ StreamFinished（恰好一个，收尾）。
    usage 是请求级元数据（账单），只经 StreamFinished 携带，不写进消息体——避免随历史回传 API。
    """
    stream_messages_dict = {}

    stream_usage = None

    for chunk in completion:

        # usage 在最后一个 chunk 的顶层（此时 choices 为空列表），不进 choice 循环
        usage = getattr(chunk, "usage", None)
        if usage:
            stream_usage = usage

        for choice in chunk.choices:

            index = choice.index
            message = stream_messages_dict.setdefault(index, {})

            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                message["_finish_reason"] = finish_reason

            delta = choice.delta

            if delta.role:
                message["role"] = delta.role

            # kimi-k3 始终推理：思考过程逐块到达，拼进消息里保留。
            # 注：早期版本缺 reasoning_content 会 400，2026-08 实测平台已不再强制
            # （占位符/空串/完全缺失均通过校验）——L3.5 清理旧推理的前置障碍已消除
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                message["reasoning_content"] = message.get("reasoning_content", "") + reasoning
                yield ReasoningDelta(reasoning)

            content = delta.content
            if content:  # 大部分 chunk 的 content 是 None，必须守卫
                message["content"] = message.get("content", "") + content
                yield TextDelta(content)

            if delta.tool_calls:
                _merge_tool_calls(message, delta.tool_calls)

    yield StreamFinished(_finalize(stream_messages_dict), stream_usage)


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
