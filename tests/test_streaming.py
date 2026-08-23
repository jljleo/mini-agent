"""streaming.py 回归测试：SSE chunk 流 → 定稿消息的拼装逻辑。

用 SimpleNamespace 伪造 OpenAI SDK 的 chunk 结构（duck typing 足够，
streaming 本就只用 getattr/属性访问，不依赖真实类型）。
"""

from types import SimpleNamespace

from streaming import stream_and_assemble


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


class RecordingRenderer:
    def __init__(self):
        self.reasoning, self.content = [], []

    def on_reasoning(self, text):
        self.reasoning.append(text)

    def on_content(self, text):
        self.content.append(text)


class TestContentAssembly:
    def test_content_concatenated_across_chunks(self):
        stream = [chunk(choice(delta(role="assistant", content="你"))),
                  chunk(choice(delta(content="好")))]
        msgs, _ = stream_and_assemble(iter(stream))
        assert msgs[0]["content"] == "你好"
        assert msgs[0]["role"] == "assistant"

    def test_none_content_guarded(self):
        """大部分 chunk 的 content 是 None，必须守卫（回归防线）。"""
        stream = [chunk(choice(delta(role="assistant"))),
                  chunk(choice(delta(content="x")))]
        msgs, _ = stream_and_assemble(iter(stream))
        assert msgs[0]["content"] == "x"

    def test_reasoning_preserved_and_rendered(self):
        r = RecordingRenderer()
        stream = [chunk(choice(delta(role="assistant", reasoning="想一"))),
                  chunk(choice(delta(reasoning="想二", content="答")))]
        msgs, _ = stream_and_assemble(iter(stream), r)
        assert msgs[0]["reasoning_content"] == "想一想二"
        assert r.reasoning == ["想一", "想二"]
        assert r.content == ["答"]

    def test_finish_reason_recorded(self):
        stream = [chunk(choice(delta(content="x"), finish_reason="length"))]
        msgs, _ = stream_and_assemble(iter(stream))
        assert msgs[0]["_finish_reason"] == "length"

    def test_multiple_choices_become_multiple_messages(self):
        stream = [chunk(choice(delta(role="assistant", content="零"), index=0),
                        choice(delta(role="assistant", content="一"), index=1))]
        msgs, _ = stream_and_assemble(iter(stream))
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
        msgs, _ = stream_and_assemble(iter(stream))
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
        msgs, _ = stream_and_assemble(iter(stream))
        names = [c["function"]["name"] for c in msgs[0]["tool_calls"]]
        assert names == ["f", "g"]

    def test_index_helper_field_stripped(self):
        """index 是拼装辅助字段，定稿时必须摘掉（不该回传 API）。"""
        stream = [chunk(choice(delta(role="assistant",
                                     tool_calls=[tc(0, id="c1", type="function",
                                                    name="f", arguments="{}")])))]
        msgs, _ = stream_and_assemble(iter(stream))
        assert "index" not in msgs[0]["tool_calls"][0]


class TestUsage:
    def test_usage_returned_not_in_message(self):
        """usage 是账单元数据：走返回值通道，绝不写进消息体（会随历史回传 API）。"""
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        stream = [chunk(choice(delta(content="x"))),
                  chunk(usage=usage)]  # 末 chunk choices 为空
        msgs, got = stream_and_assemble(iter(stream))
        assert got is usage
        assert "usage" not in msgs[0]

    def test_no_usage_returns_none(self):
        _, got = stream_and_assemble(iter([chunk(choice(delta(content="x")))]))
        assert got is None


class TestFallback:
    def test_no_renderer_prints_plaintext(self, capsys):
        """兜底路径：无渲染器时 content 纯文本直出。"""
        stream_and_assemble(iter([chunk(choice(delta(content="直出")))]))
        assert capsys.readouterr().out == "直出"
