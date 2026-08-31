"""事件定义：agent 内核与界面层之间的唯一契约。

内核（agent / streaming / compact）只产出事件，不感知任何界面；
终端（ui.consume）、bench（统计消费者）、将来的 Web GUI（SSE 序列化）
都是事件的消费者。新增前端 = 新写一个消费者，内核零改动。

设计对齐 pi 的 EventStream<AgentEvent> 与 codex 的 EventMsg：
流式片段（TextDelta / ReasoningDelta）逐 chunk 产出，
请求级元数据（定稿消息、usage）收在 StreamFinished 里——
usage 是账单数据，绝不进入消息体（避免随历史回传 API）。

反向通道（消费者 → 内核）是 TurnControl：interrupt 旗帜 + steering 队列，
对齐 codex 的 Op。内核只在合法边界检查（事件间隙 / 工具间隙 / 轮边界），
保证中断与插话都不会切出半个 tool 配对。
"""

import queue
import threading
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
    """一轮提问结束（终稿 / 死循环熔断 / 截断作废 / 用户中断都算）。"""


# ---- 控制通道（消费者 → 内核方向，对齐 codex 的 Op）----


class TurnControl:
    """一轮对话的控制通道：interrupt 旗帜 + steering 队列 + 断流钩子。

    与事件流方向相反——事件是内核 → 消费者，控制是消费者 → 内核。
    - interrupt：threading.Event，置位即请求中断；内核在下一个检查点响应
      （正在执行的单个工具不打断——强杀 bash 子进程是另一档工程）。
      只置旗帜不断流时，内核阻塞在读 chunk 上，要等下一个 chunk 才检查旗帜
    - steer：queue.Queue[str]，运行中插话；内核在轮边界 drain，
      作为 user 消息注入（pi 的 PendingMessageQueue 同款 drain 语义）
    - abort()：立即中断——置旗帜 + 关闭活动 SSE 流（closer 由内核注册）。
      断流让阻塞读取立刻抛异常，零延迟收尾
    """

    def __init__(self) -> None:
        self.interrupt = threading.Event()
        self.steer: queue.Queue[str] = queue.Queue()
        self._closers: list = []

    def register_closer(self, fn) -> None:
        """内核注册活动流的关闭函数（每次请求一个）。"""
        self._closers.append(fn)

    def unregister_closer(self, fn) -> None:
        try:
            self._closers.remove(fn)
        except ValueError:
            pass

    def abort(self) -> None:
        """立即中断：置旗帜 + 关闭所有活动流。幂等，可重复调用。"""
        self.interrupt.set()
        for fn in self._closers[:]:  # 拷贝遍历：closer 可能触发注销
            try:
                fn()
            except Exception:
                pass  # 断流是尽力而为：关不掉就等内核下一个检查点兜底
