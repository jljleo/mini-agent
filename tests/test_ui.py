"""ui.StreamRenderer 回归测试：分段落卷逻辑（长回答 Live 超高重绘重复的修复）。"""

import ui
from ui import StreamRenderer


class DummyLive:
    """替代 rich Live：记录每次 update 的 renderable，不做真实终端渲染。"""

    instances = []

    def __init__(self, renderable, **kwargs):
        self.renderable = renderable
        self.updates = []
        DummyLive.instances.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def update(self, renderable, refresh=False):
        self.renderable = renderable
        self.updates.append(renderable)


def make_renderer(monkeypatch):
    """构造强制走 tty 渲染路径的 StreamRenderer（Live 打桩）。"""
    DummyLive.instances.clear()
    monkeypatch.setattr(ui, "Live", DummyLive)
    r = StreamRenderer()
    r._plain = False   # 绕过 is_terminal 检测，强制走 Live 路径
    r._live_ok = True  # _live_ok 在 __init__ 时按 is_terminal 计算，需同步强制
    return r


class TestSegmentedFinalization:
    def test_complete_blocks_land_in_scrollback(self, monkeypatch, capsys):
        """空行前的完整块永久落卷轴，Live 只渲染进行中的尾部。"""
        r = make_renderer(monkeypatch)
        r.on_content("第一段。\n\n第二段进行中")
        assert "第一段" in capsys.readouterr().out
        assert r._tail == "第二段进行中"

    def test_unclosed_code_fence_blocks_finalization(self, monkeypatch, capsys):
        """代码块未闭合（``` 奇数）时暂不落卷：半拉 fence 单独渲染会错乱。"""
        r = make_renderer(monkeypatch)
        r.on_content("```python\ncode line\n\n还在代码块里")
        assert capsys.readouterr().out == ""  # 什么都没落卷
        assert r._tail == "```python\ncode line\n\n还在代码块里"

    def test_fence_close_resumes_finalization(self, monkeypatch, capsys):
        r = make_renderer(monkeypatch)
        r.on_content("```python\ncode\n\n")
        r.on_content("```\n\n收尾段落")
        out = capsys.readouterr().out
        assert "code" in out  # 闭合后整块落卷
        assert r._tail == "收尾段落"

    def test_no_blank_line_no_finalize(self, monkeypatch, capsys):
        """没有空行 = 只有一个进行中的块，全部留在 Live。"""
        r = make_renderer(monkeypatch)
        r.on_content("一行\n两行\n三行")
        assert capsys.readouterr().out == ""
        assert "三行" in r._tail

    def test_exit_renders_remaining_tail(self, monkeypatch, capsys):
        """收尾：节流期间没渲染的尾巴在 __exit__ 强制全量渲染。"""
        r = make_renderer(monkeypatch)
        r.on_content("唯一的段落没有空行")
        r.__exit__(None, None, None)
        live = DummyLive.instances[0]
        assert "唯一的段落" in live.renderable.markup  # 终稿进入 Live 定稿

    def test_long_answer_no_duplication_source(self, monkeypatch, capsys):
        """回归核心：超长回答的 Live 区域始终只有一个块（杜绝超高重绘重复）。"""
        r = make_renderer(monkeypatch)
        for i in range(100):  # 模拟 100 块的长回答，远超终端高度
            r.on_content(f"第 {i} 块的内容。\n\n")
        live = DummyLive.instances[0]
        # Live 从未持有超过一个块的内容
        assert all(len(str(u.markup)) < 50 for u in live.updates)
        out = capsys.readouterr().out
        assert "第 0 块" in out and "第 99 块" in out  # 全部落卷且只出现一次
        assert out.count("第 50 块") == 1
