"""stream_segments 分段器单测：流式切割契约（ui.py StreamRenderer 与落卷渲染共用）。

契约：完成段（fence 闭合 + 空行分界）一旦吐出即冻结落卷；
未闭合 ``` 或没有空行时一切留在生长中的尾段。
"""

from led_review.ui.segments import StreamSegmenter, split_complete


class TestSplitComplete:
    def test_blank_line_boundary(self):
        assert split_complete("段一\n\n段二") == ("段一\n\n", "段二")

    def test_no_boundary_keeps_everything_in_tail(self):
        assert split_complete("只有一段\n两行") == ("", "只有一段\n两行")

    def test_unclosed_fence_blocks_split(self):
        # 未闭合 fence 的 Markdown 单独渲染会错乱，必须等它闭合
        assert split_complete("```py\ncode\n\n还在块内") == ("", "```py\ncode\n\n还在块内")

    def test_closed_fence_resumes_split(self):
        assert split_complete("```py\ncode\n```\n\n正文") == ("```py\ncode\n```\n\n", "正文")


class TestStreamSegmenter:
    def test_feed_then_take_completed(self):
        seg = StreamSegmenter()
        seg.feed("第一段。")
        assert seg.take_completed() == ""  # 无空行分界，不落卷
        seg.feed("\n\n第二段")
        assert seg.take_completed() == "第一段。\n\n"
        assert seg.tail == "第二段"

    def test_incremental_fence_close(self):
        seg = StreamSegmenter()
        seg.feed("```python\ncode\n\n")  # fence 未闭合
        assert seg.take_completed() == ""
        seg.feed("```\n\n收尾")
        assert seg.take_completed() == "```python\ncode\n\n```\n\n"
        assert seg.tail == "收尾"

    def test_take_completed_is_idempotent_when_nothing_new(self):
        seg = StreamSegmenter()
        seg.feed("段\n\n尾")
        assert seg.take_completed() == "段\n\n"
        assert seg.take_completed() == ""  # 再取为空，不重复吐

    def test_finish_takes_remaining_tail(self):
        seg = StreamSegmenter()
        seg.feed("半截尾部")
        assert seg.finish() == "半截尾部"
        assert seg.tail == ""
        assert seg.take_completed() == ""
