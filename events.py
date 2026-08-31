"""事件定义：agent 内核与界面层之间的唯一契约。

内核（agent / streaming / compact）只产出事件，不感知任何界面；
终端（ui.consume）、bench（统计消费者）、将来的 Web GUI（SSE 序列化）
都是事件的消费者。新增前端 = 新写一个消费者，内核零改动。

设计对齐 pi 的 EventStream<AgentEvent> 与 codex 的 EventMsg：
流式片段（TextDelta / ReasoningDelta）逐 chunk 产出，
请求级元数据（定稿消息、usage）收在 StreamFinished 里——
usage 是账单数据，绝不进入消息体（避免随历史回传 API）。
"""

from dataclasses import dataclass
from typing import Any


# ---- 流式请求生命周期 ----


@dataclass
class StreamStart:
    """一次 API 流式请求开始（消费者借此启动 spinner 等等待态）。"""


@dataclass
class TextDelta:
    """正文流式片段。"""
    text: str


@dataclass
class ReasoningDelta:
    """思考过程流式片段。"""
    text: str


@dataclass
class StreamFinished:
    """一次流式请求的收尾：带回拼装定稿的 assistant 消息列表与 usage。"""
    messages: list[dict]
    usage: Any  # openai.types.CompletionUsage | None


# ---- 工具执行 ----


@dataclass
class ToolCallStart:
    """即将执行一次工具调用。arguments 是原始 JSON 字符串（可能因 max_tokens 截断不完整）。"""
    name: str
    arguments: str


@dataclass
class ToolCallResult:
    """工具执行完毕。preview 是截断后的预览（完整结果在消息历史里）。"""
    name: str
    preview: str


# ---- 系统旁白 ----


@dataclass
class Note:
    """内部机制的动作说明（compact 等），消费者通常用暗色呈现。"""
    message: str
    tag: str | None = None


@dataclass
class Warn:
    message: str


@dataclass
class Usage:
    """一轮请求的 token 消耗（prompt/completion/缓存命中）+ 会话累计 total。"""
    prompt: int
    completion: int
    cached: int
    total: int


@dataclass
class TurnEnd:
    """一轮提问结束（终稿 / 死循环熔断 / 截断作废都算）。"""
