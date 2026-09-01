"""trace.py 回归测试：TraceRecorder 把事件流记录成结构化轨迹。

约束：不访问网络；TraceRecorder 是纯消费者（透传事件），用真实 events.py 事件对象喂。
"""

import json

from events import (
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnEnd,
    Usage,
)
from trace import TraceRecorder


def _finish():
    return StreamFinished([{"role": "assistant", "content": "ok"}], None)


def test_wrap_passthrough_events_unchanged():
    """wrap 必须原样透传事件（下游 ui.consume 依赖完整流）。"""
    events = [StreamStart(), TextDelta("你好"), _finish(), TurnEnd()]
    recorder = TraceRecorder("t1")
    assert list(recorder.wrap(iter(events))) == events


def test_turn_id_increments_on_stream_start():
    """每个 StreamStart 开启新 turn，事件按 turn 分段。"""
    recorder = TraceRecorder("t1")
    events = [StreamStart(), TextDelta("a"), TurnEnd(), StreamStart(), TextDelta("b"), TurnEnd()]
    list(recorder.wrap(iter(events)))
    turn_ids = [r["turn_id"] for r in recorder.records()]
    assert turn_ids[0] == 1
    assert turn_ids[1] == 1
    assert turn_ids[3] == 2
    assert turn_ids[4] == 2


def test_tool_calls_record_name_args_preview():
    """工具调用记录 name/args/result 摘要——复盘失败的原始材料。"""
    recorder = TraceRecorder("t1")
    list(recorder.wrap(iter([
        StreamStart(),
        ToolCallStart("read_file", '{"path": "a.py"}'),
        ToolCallResult("read_file", "hello"),
        TurnEnd(),
    ])))
    details = [r["detail"] for r in recorder.records() if r["event"] in ("ToolCallStart", "ToolCallResult")]
    assert details[0] == {"name": "read_file", "args": '{"path": "a.py"}'}
    assert details[1] == {"name": "read_file", "preview": "hello"}


def test_usage_and_delta_recorded():
    """Usage 记录 token 分项；TextDelta 记录字符数。"""
    recorder = TraceRecorder("t1")
    list(recorder.wrap(iter([
        StreamStart(),
        TextDelta("你好"),
        Usage(100, 10, 5, 110),
        TurnEnd(),
    ])))
    text_detail = next(r["detail"] for r in recorder.records() if r["event"] == "TextDelta")
    usage_detail = next(r["detail"] for r in recorder.records() if r["event"] == "Usage")
    assert text_detail == {"chars": 2}
    assert usage_detail == {"prompt": 100, "completion": 10, "cached": 5, "total": 110}


def test_to_jsonl_every_record_has_task_id():
    """JSONL 每条都带 task_id，可跨任务合并。"""
    recorder = TraceRecorder("task_x")
    list(recorder.wrap(iter([StreamStart(), TurnEnd()])))
    lines = recorder.to_jsonl().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["task_id"] == "task_x"
