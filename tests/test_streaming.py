"""streaming.py 回归测试：SSE chunk 流 → 事件流 + 定稿消息的拼装逻辑。

用 SimpleNamespace 伪造 OpenAI SDK 的 chunk 结构（duck typing 足够，
streaming 本就只用 getattr/属性访问，不依赖真实类型）。

事件契约：stream_and_assemble 是生成器，产出 ReasoningDelta / TextDelta
（任意数量、可交错）→ StreamFinished（恰好一个，收尾，带回定稿消息与 usage）。
"""

from types import SimpleNamespace

from events import ReasoningDelta, StreamFinished, TextDelta, TurnControl
from streaming import interruptible_stream, stream_and_assemble


def tc(index, id=None, type=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, type=type, function=fn)


def delta(role=None, content=None, reasoning=None, tool_calls=None):
    return SimpleNamespace(role=role, content=content,
                           reasoning_content=reasoning, tool_calls=tool_calls)


def choice(delta, index=0, finish_reason=None):
    return SimpleNamespace(index=index, delta=delta, finish_reason=finish_reason)


def chunk(*choices, usage=None):
    return SimpleNamespace(choices=list(choices), usage=usage)


def run(stream):
    """消费事件流，返回 (定稿消息列表, usage, 全部事件)。"""
    events = list(stream_and_assemble(iter(stream)))
    finished = [e for e in events if isinstance(e, StreamFinished)]
    assert len(finished) == 1, "事件流必须以恰好一个 StreamFinished 收尾"
    assert isinstance(events[-1], StreamFinished), "StreamFinished 必须是最后一个事件"
    return finished[0].messages, finished[0].usage, events


class TestContentAssembly:
    def test_content_concatenated_across_chunks(self):
        stream = [chunk(choice(delta(role="assistant", content="你"))),
                  chunk(choice(delta(content="好")))]
        msgs, _, events = run(stream)
        assert msgs[0]["content"] == "你好"
        assert msgs[0]["role"] == "assistant"
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["你", "好"]

    def test_none_content_guarded(self):
        """大部分 chunk 的 content 是 None，必须守卫（回归防线）。"""
        stream = [chunk(choice(delta(role="assistant"))),
                  chunk(choice(delta(content="x")))]
        msgs, _, events = run(stream)
        assert msgs[0]["content"] == "x"
        assert len([e for e in events if isinstance(e, TextDelta)]) == 1

    def test_reasoning_preserved_and_emitted(self):
        stream = [chunk(choice(delta(role="assistant", reasoning="想一"))),
                  chunk(choice(delta(reasoning="想二", content="答")))]
        msgs, _, events = run(stream)
        assert msgs[0]["reasoning_content"] == "想一想二"
        assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["想一", "想二"]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["答"]

    def test_finish_reason_recorded(self):
        stream = [chunk(choice(delta(content="x"), finish_reason="length"))]
        msgs, _, _ = run(stream)
        assert msgs[0]["_finish_reason"] == "length"

    def test_multiple_choices_become_multiple_messages(self):
        stream = [chunk(choice(delta(role="assistant", content="零"), index=0),
                        choice(delta(role="assistant", content="一"), index=1))]
        msgs, _, _ = run(stream)
        assert [m["content"] for m in msgs] == ["零", "一"]


class TestToolCallMerging:
    def test_arguments_fragments_concatenated(self):
        """arguments 分片到达：必须拼接而非覆盖。"""
        stream = [
            chunk(choice(delta(role="assistant",
                               tool_calls=[tc(0, id="c1", type="function",
                                              name="read_file", arguments='{"pa')]))),
            chunk(choice(delta(tool_calls=[tc(0, arguments='th": "a.py"}')]))),
        ]
        msgs, _, _ = run(stream)
        call = msgs[0]["tool_calls"][0]
        assert call["id"] == "c1"
        assert call["function"]["name"] == "read_file"
        assert call["function"]["arguments"] == '{"path": "a.py"}'

    def test_multiple_tool_calls_by_index(self):
        """一次多个 tool_calls：按下标归位，惰性扩容。"""
        stream = [chunk(choice(delta(
            role="assistant",
            tool_calls=[tc(0, id="c1", type="function", name="f", arguments="{}"),
                        tc(1, id="c2", type="function", name="g", arguments="{}")])))]
        msgs, _, _ = run(stream)
        names = [c["function"]["name"] for c in msgs[0]["tool_calls"]]
        assert names == ["f", "g"]

    def test_index_helper_field_stripped(self):
        """index 是拼装辅助字段，定稿时必须摘掉（不该回传 API）。"""
        stream = [chunk(choice(delta(role="assistant",
                                     tool_calls=[tc(0, id="c1", type="function",
                                                    name="f", arguments="{}")])))]
        msgs, _, _ = run(stream)
        assert "index" not in msgs[0]["tool_calls"][0]


class TestUsage:
    def test_usage_carried_by_stream_finished_not_in_message(self):
        """usage 是账单元数据：只经 StreamFinished 携带，绝不写进消息体（会随历史回传 API）。"""
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        stream = [chunk(choice(delta(content="x"))),
                  chunk(usage=usage)]  # 末 chunk choices 为空
        msgs, got, _ = run(stream)
        assert got is usage
        assert "usage" not in msgs[0]

    def test_no_usage_returns_none(self):
        _, got, _ = run([chunk(choice(delta(content="x")))])
        assert got is None


class TestEventContract:
    def test_streaming_itself_prints_nothing(self, capsys):
        """streaming 不感知界面：不准直接 print——渲染是事件消费者（ui.consume）的事。"""
        _, _, events = run([chunk(choice(delta(content="直出")))])
        assert capsys.readouterr().out == ""
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["直出"]


class TestInterruptibleStream:
    def test_chunks_pass_through_in_order(self):
        """无中断时：chunk 原样按序透传（包装不改变正常路径）。"""
        control = TurnControl()
        source = interruptible_stream(lambda: iter(["a", "b", "c"]), control)
        assert list(source) == ["a", "b", "c"]

    def test_interrupt_raises_promptly_even_when_stream_stuck(self):
        """流卡死（TTFT/长推理无 chunk）时，中断也有界延迟（≤0.1s 轮询粒度）。

        回归防线：closer 断流对跨线程阻塞读不可靠（实测 15s 未唤醒），
        所以阻塞被挪进后台泵线程，消费侧轮询旗帜——此测试卡住泵线程验证。
        """
        import time as _time

        from streaming import StreamAborted

        control = TurnControl()

        def stuck():
            _time.sleep(30)  # 泵线程卡死（daemon，测试结束即回收）
            yield "永远到不了"

        source = interruptible_stream(stuck, control)
        t0 = _time.monotonic()
        control.interrupt.set()  # 主侧置旗帜
        try:
            next(source)
            assert False, "应抛 StreamAborted"
        except StreamAborted:
            pass
        assert _time.monotonic() - t0 < 5, "中断响应应在轮询粒度内，而非等卡死的流"

    def test_pump_exception_propagates(self):
        """泵线程里的异常（网络错误）原样 re-raise，不吞。"""
        control = TurnControl()

        def broken():
            raise ConnectionError("network down")
            yield

        source = interruptible_stream(broken, control)
        try:
            next(source)
            assert False
        except ConnectionError:
            pass
