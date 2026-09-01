"""可观测性核心：TraceRecorder 消费事件流，产出结构化 JSONL 轨迹。

零内核改动：它是 events.py 的又一个消费者（同 ui.consume / bench 消费者）。
每次 StreamStart 开启一个新 turn span，所有事件记录 task_id + turn_id + 事件类型 +
字段摘要 + 相对上一事件的耗时，落成 JSONL 供"复盘哪一步走岔了"。
"""

import json
import time

from events import (
    Note,
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnEnd,
    Usage,
    Warn,
)


class TraceRecorder:
    """事件流 → 结构化轨迹记录器（纯消费者，透传事件不打断下游）。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._records: list[dict] = []
        self._turn_id = 0
        self._last_ts: float | None = None

    def wrap(self, events):
        """包一层事件流：逐事件记录后原样透传，下游（ui.consume）不受影响。"""
        self._last_ts = time.monotonic()
        for ev in events:
            self._record(ev)
            yield ev

    def _record(self, ev) -> None:
        now = time.monotonic()
        name = type(ev).__name__
        if name == "StreamStart":
            self._turn_id += 1
        elapsed_ms = round((now - self._last_ts) * 1000, 1) if self._last_ts is not None else 0.0
        self._records.append({
            "task_id": self.task_id,
            "turn_id": self._turn_id,
            "event": name,
            "elapsed_ms": elapsed_ms,
            "detail": self._detail(ev),
        })
        self._last_ts = now

    def _detail(self, ev) -> dict:
        if isinstance(ev, TextDelta):
            return {"chars": len(ev.text)}
        if isinstance(ev, ReasoningDelta):
            return {"chars": len(ev.text)}
        if isinstance(ev, StreamFinished):
            return {"messages": len(ev.messages)}
        if isinstance(ev, ToolCallStart):
            return {"name": ev.name, "args": ev.arguments}
        if isinstance(ev, ToolCallResult):
            return {"name": ev.name, "preview": ev.preview}
        if isinstance(ev, Usage):
            return {"prompt": ev.prompt, "completion": ev.completion,
                    "cached": ev.cached, "total": ev.total}
        if isinstance(ev, Note):
            return {"message": ev.message, "tag": ev.tag}
        if isinstance(ev, Warn):
            return {"message": ev.message}
        return {}

    def records(self) -> list[dict]:
        return list(self._records)

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in self._records)
