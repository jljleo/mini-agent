"""review pipeline 回归测试：findings 解析 / exit code 门禁 / 输出模式隔离（打桩会话，零网络）。"""

import json

import pytest

import mini_agent.review as review
from mini_agent.kernel.events import StreamStart, TurnEnd
from mini_agent.review import parse_findings
from mini_agent.tools import gitdiff

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
        fake_review_env(SAMPLE_OUTPUT)
        monkeypatch.setattr(review.ui, "consume", lambda events: list(events))
        assert review.review("HEAD", fmt="text") == 1
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
        from mini_agent.kernel.events import TextDelta
        return [x for i in range(n) for x in (StreamStart(), TextDelta(f"r{i}"))]

    def test_soft_cap_steers_once(self):
        from mini_agent.kernel.events import TurnControl
        control = TurnControl()
        list(review._round_fuse(iter(self._events(review.SOFT_CAP_ROUNDS + 1)), control))
        assert control.steer.qsize() == 1
        assert "立即按格式输出" in control.steer.get_nowait()
        assert not control.interrupt.is_set()  # SOFT 档不打断

    def test_hard_cap_aborts(self):
        from mini_agent.kernel.events import TurnControl
        control = TurnControl()
        out = list(review._round_fuse(iter(self._events(review.HARD_CAP_ROUNDS + 5)), control))
        assert control.interrupt.is_set()
        assert len(out) < 2 * (review.HARD_CAP_ROUNDS + 5)  # 流被截断
