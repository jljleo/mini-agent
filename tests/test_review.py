"""review pipeline 回归测试：findings 解析 / exit code 门禁 / 输出模式隔离（打桩会话，零网络）。"""

import json
import sys

import pytest

import led_review.review as review
from led_review.kernel.events import StreamStart, TurnEnd
from led_review.review import cap_findings, parse_findings
from led_review.tools import gitdiff

SAMPLE_OUTPUT = """- [high] src/auth.py:42 — 空指针：user 可能为 None 时直接访问 user.id
  建议：先判空再取 id
- [low] src/util.py:7 — 魔法数字 86400 建议命名常量
这是不该被解析的散文行。
## 总结
整体可以，有一处必须修。"""


class FakeSession:
    """罐头会话：chat 返回空事件流，messages 提供终稿。"""

    def __init__(self, final_text, **kwargs):
        self.messages = [{"role": "assistant", "content": final_text}]

    def chat(self, prompt, control=None):
        return iter([StreamStart(), TurnEnd()])


@pytest.fixture
def fake_review_env(monkeypatch):
    """隔离 diff 与会话：collect 返回固定 diff，ChatSession 返回罐头终稿。"""
    monkeypatch.setattr(review.gitdiff, "collect", lambda spec, root=None: "diff 内容")
    holder = {}

    def make(final_text):
        holder["final"] = final_text
        monkeypatch.setattr(review, "ChatSession", lambda **kw: FakeSession(final_text))

    return make


class TestParseFindings:
    def test_standard(self):
        findings = parse_findings(SAMPLE_OUTPUT)
        assert len(findings) == 2
        assert findings[0].severity == "high" and findings[0].path == "src/auth.py"
        assert findings[0].line == 42 and findings[0].suggestion == "先判空再取 id"
        assert findings[1].suggestion is None  # 散文行不附着

    def test_no_findings(self):
        assert parse_findings("无 findings\n## 总结\n挺好。") == []

    def test_garbage_tolerated(self):
        assert parse_findings("完全自由格式的回答，没有任何 finding 行") == []


class TestReviewExitCode:
    def test_high_findings_fail_gate(self, fake_review_env, capsys):
        fake_review_env(SAMPLE_OUTPUT)
        code = review.review("HEAD", fmt="json")
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["counts"]["high"] == 1 and len(out["findings"]) == 2
        assert out["raw"] == SAMPLE_OUTPUT  # 原文保留，解析丢失可兜底

    def test_clean_review_passes(self, fake_review_env):
        fake_review_env("无 findings\n## 总结\n挺好。")
        assert review.review("HEAD", fmt="json") == 0

    def test_no_fail_flag_disables_gate(self, fake_review_env):
        fake_review_env(SAMPLE_OUTPUT)
        assert review.review("HEAD", fmt="json", fail_on_high=False) == 0

    def test_empty_diff_skips(self, monkeypatch, capsys):
        monkeypatch.setattr(review.gitdiff, "collect", lambda spec, root=None: "（无变更）")
        assert review.review(None) == 0
        assert "跳过" in capsys.readouterr().out

    def test_text_mode_renders_and_prints_gate(self, fake_review_env, monkeypatch, capsys):
        """tty 分支：render=True（工具流渲染）且输出门禁行。

        led review 在 PR #4 指正：capsys 下 isatty() 恒 False，此测试原先静默
        走管道分支（render=False），tty 分支零覆盖、ui.consume 桩永不触发——
        显式钉住 isatty=True 让测试名副其实。
        """
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)  # 真 tty 分支
        fake_review_env(SAMPLE_OUTPUT)
        calls = []
        monkeypatch.setattr(review.ui, "consume",
                            lambda events: calls.append(1) or list(events))
        assert review.review("HEAD", fmt="text") == 1
        assert calls == [1]                        # consume 被调用（渲染过）
        assert "1 个 high findings" in capsys.readouterr().out


class TestCli:
    def test_not_a_repo_exits_2(self, monkeypatch, capsys):
        def boom(spec, root=None):
            raise gitdiff.GitRepoError("不是 git 仓库")

        monkeypatch.setattr(review.gitdiff, "collect", boom)
        assert review.cli(["main...HEAD"]) == 2
        assert "不是 git 仓库" in capsys.readouterr().err

    def test_build_prompt_contains_axes_and_diff(self):
        prompt = review.build_prompt("DIFF_BODY")
        assert "正确性" in prompt and "规范" in prompt and "DIFF_BODY" in prompt


class TestRoundFuse:
    """两档轮次保险丝：SOFT 档 steering 注入收敛指令，HARD 档才 abort。"""

    def _events(self, n):
        from led_review.kernel.events import TextDelta
        return [x for i in range(n) for x in (StreamStart(), TextDelta(f"r{i}"))]

    def test_soft_cap_steers_once(self):
        from led_review.kernel.events import TurnControl
        control = TurnControl()
        list(review._round_fuse(iter(self._events(review.SOFT_CAP_ROUNDS + 1)), control))
        assert control.steer.qsize() == 1
        assert "立即按格式输出" in control.steer.get_nowait()
        assert not control.interrupt.is_set()  # SOFT 档不打断

    def test_hard_cap_aborts(self):
        from led_review.kernel.events import TurnControl
        control = TurnControl()
        out = list(review._round_fuse(iter(self._events(review.HARD_CAP_ROUNDS + 5)), control))
        assert control.interrupt.is_set()
        assert len(out) < 2 * (review.HARD_CAP_ROUNDS + 5)  # 流被截断


class TestParallelMode:
    """parallel 模式：两轴独立会话 + Python 确定性合并（E11 实验组）。"""

    def test_merge_dedupes_by_path_line_keeps_higher_severity(self):
        a = [review.Finding("low", "x.py", 10, "轴A的描述")]
        b = [review.Finding("high", "x.py", 10, "轴B的同位置报告"),
             review.Finding("medium", "y.py", 3, "独有发现")]
        merged = review._merge_findings([a, b])
        assert len(merged) == 2
        spot = next(f for f in merged if f.path == "x.py")
        assert spot.severity == "high" and spot.description == "轴B的同位置报告"
        # severity 排序：high 在前
        assert merged[0].severity == "high"

    def test_parallel_dispatches_two_axes_and_merges(self, monkeypatch):
        monkeypatch.setattr(review.gitdiff, "collect", lambda spec, root=None: "diff")
        calls = {}

        class FakeSession:
            total_prompt_tokens = total_completion_tokens = 1
            messages = []

        def fake_run_one(prompt, render):
            axis = "correctness" if "【正确性】" in prompt else "standards"
            calls[axis] = True
            text = "- [high] a.py:1 — 轴发现" if axis == "correctness" else "无 findings"
            return text, FakeSession()

        monkeypatch.setattr(review, "_run_one", fake_run_one)
        raw, findings, sessions = review.run_review("HEAD", mode="parallel")
        assert calls == {"correctness": True, "standards": True}
        assert len(sessions) == 2
        assert len(findings) == 1 and findings[0].path == "a.py"
        assert "【correctness 轴】" in raw and "【standards 轴】" in raw


class TestCapFindings:
    """--max-findings 截断（E11 事故带入的 cap_findings off-by-one 回归测试）。

    上游 bug：findings[:limit - 1] 使 limit=5 只返回 4 条；且此前零测试覆盖，
    326 全绿也拦不住。杀青前修复 + 从此有回归线。
    """

    def _f(self, n):
        return [review.Finding("low", "f.py", i, f"f{i}") for i in range(n)]

    def test_limit_zero_unlimited(self):
        fs = self._f(8)
        assert cap_findings(fs, 0) is fs  # 0 = 不限制（原样返回，不拷贝）

    def test_exact_limit_returns_exact_count(self):
        fs = self._f(8)
        assert len(cap_findings(fs, 5)) == 5  # 回归点：曾经是 4

    def test_limit_exceeding_list_ok(self):
        assert len(cap_findings(self._f(3), 10)) == 3  # 不会越界

    def test_limit_one(self):
        assert len(cap_findings(self._f(2), 1)) == 1


class TestPipeOutput:
    """非 tty（管道/CI）的 text 模式输出必须干净可解析（dogfood 真 bug 回归）。

    上游 bug：text 模式一律 render=True → `led review > review.md` 把整套
    ⏺/⎿ 工具调用终端渲染卷进 PR 评论（评论乱码/工具过程泄露）。修复后
    管道模式只出终稿原文 + 门禁行。
    """

    def test_pipe_mode_prints_raw_not_render(self, fake_review_env, monkeypatch, capsys):
        """非 tty：不调 ui.consume（无工具流渲染），输出含终稿原文与门禁行。"""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)  # 模拟管道
        fake_review_env(SAMPLE_OUTPUT)
        calls = []
        monkeypatch.setattr(review.ui, "consume",
                            lambda events: calls.append(1) or list(events))
        code = review.review("HEAD", fmt="text")
        out = capsys.readouterr().out
        assert calls == []                    # 未渲染（无工具流）
        assert "空指针" in out                # 终稿原文在
        assert "1 个 high findings" in out    # 门禁行在
        assert "⏺" not in out and "⎿" not in out  # 无渲染标记
        assert code == 1

    def test_no_change_pipe_skips(self, monkeypatch, capsys):
        monkeypatch.setattr(review.gitdiff, "collect", lambda spec, root=None: "（无变更）")
        assert review.review(None, fmt="text") == 0
        assert "跳过" in capsys.readouterr().out
