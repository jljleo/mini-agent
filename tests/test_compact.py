"""compact.py 回归测试：L3 瘦身 / token 估算与校准 / L1 截断 / L2 摘要。

这些测试固化的是血泪知识（见 DESIGN_NOTES）：切点不拆 tool 配对、保护窗口
按角色计数、[:0] 切片陷阱、收益门槛保缓存、EMA 校准钳制……注释不阻止回归，
断言才阻止。
"""

import json
from types import SimpleNamespace

import pytest

import compact
from compact import (
    _count_chars,
    _prefix_indices,
    apply_slimming,
    apply_truncation,
    calibrate,
    detect_slim_targets,
    detect_truncation_point,
    estimate_tokens,
    extract_middle,
    summarize_middle,
)


# ---- 消息构造辅助 ----

def user(text="u" * 100):
    return {"role": "user", "content": text}


def asst(text="a" * 100, reasoning=None, tool_calls=None):
    m = {"role": "assistant", "content": text}
    if reasoning is not None:
        m["reasoning_content"] = reasoning
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


def tool_msg(call_id="c1", name="read_file", content="t" * 100):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def make_tool_pair(call_id, name, args, result):
    """assistant(tool_calls) + tool 结果的原子块。"""
    return [
        asst("", tool_calls=[{"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": args}}]),
        tool_msg(call_id, name, result),
    ]


# ============ L3 瘦身：detect ============

class TestDetectSlimTargets:
    def test_below_trigger_no_action(self):
        """总字符数低于触发阈值时完全不动作（保 prompt cache）。"""
        msgs = [user()] + make_tool_pair("c1", "read_file", "{}", "x" * 5000)
        assert detect_slim_targets(msgs, trigger_chars=1_000_000) == set()

    def test_aged_tool_results_slimmed_recent_protected(self):
        """超龄 tool 结果入选，最近 keep_recent 条受保护。"""
        msgs = [user()]
        for i in range(6):
            msgs += make_tool_pair(f"c{i}", "read_file", "{}", "x" * 600)
        targets = detect_slim_targets(
            msgs, keep_recent=5, min_len=500, trigger_chars=0, min_savings=0)
        tool_indices = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
        assert targets == {tool_indices[0]}  # 6 条 tool 只留最近 5 条，第 1 条超龄

    def test_short_results_not_slimmed(self):
        """原文短于 min_len 不瘦身：占位符本身 ~80 字符，太短是负收益。"""
        msgs = [user()]
        for i in range(6):
            msgs += make_tool_pair(f"c{i}", "read_file", "{}", "短")
        assert detect_slim_targets(
            msgs, keep_recent=0, min_len=500, trigger_chars=0, min_savings=0) == set()

    def test_savings_below_threshold_no_action(self):
        """收益门槛：省的量配不上缓存失效代价就不动。"""
        msgs = [user()]
        for i in range(3):
            msgs += make_tool_pair(f"c{i}", "read_file", "{}", "x" * 600)
        # 可省 = 2 条超龄 × 600 = 1200 < min_savings
        assert detect_slim_targets(
            msgs, keep_recent=1, min_len=500, trigger_chars=0, min_savings=2000) == set()

    def test_keep_recent_zero_no_slice_trap(self):
        """keep_recent=0 时所有超龄 tool 都可选（[:-0] 是空列表的切片陷阱，回归防线）。"""
        msgs = [user()] + make_tool_pair("c1", "read_file", "{}", "x" * 600)
        targets = detect_slim_targets(
            msgs, keep_recent=0, min_len=500, trigger_chars=0, min_savings=0)
        assert targets == {2}

    def test_aged_reasoning_slimmed_per_role_window(self):
        """L3.5：超龄 assistant 的旧推理入选；保护窗口按角色各自计数，
        不应被成簇的 tool 消息挤偏。"""
        msgs = [user(), asst("结论", reasoning="r" * 600)]
        msgs += make_tool_pair("c1", "read_file", "{}", "x" * 600)
        msgs.append(asst("最终结论", reasoning="r" * 600))
        # tool 只 1 条（在保护窗内），assistant 2 条（第 1 条超龄）
        targets = detect_slim_targets(
            msgs, keep_recent=1, min_len=500, trigger_chars=0, min_savings=0)
        assert targets == {1}  # 只有第一条 assistant 的 reasoning


# ============ L3 瘦身：apply ============

class TestApplySlimming:
    def test_empty_targets_identity(self):
        """targets 为空恒等返回原列表（同一对象）：未触发时零打扰。"""
        msgs = [user()]
        assert apply_slimming(msgs, set()) is msgs

    def test_projection_does_not_mutate_storage(self):
        """发送时投影：存储的原始消息一个字节都不能动。"""
        msgs = [user()] + make_tool_pair("c1", "run_bash", '{"command": "ls"}', "x" * 600)
        apply_slimming(msgs, {2})
        assert msgs[2]["content"] == "x" * 600

    def test_tool_placeholder_carries_recovery_clues(self):
        """占位符必须带：工具名 + 参数回显 + 原长度 + 恢复引导。"""
        msgs = [user()] + make_tool_pair("c1", "read_file", '{"path": "a.py"}', "x" * 600)
        out = apply_slimming(msgs, {2})
        ph = out[2]["content"]
        assert "read_file" in ph and '"path": "a.py"' in ph and "600" in ph

    def test_reasoning_placeholder_keeps_content_and_tool_calls(self):
        """L3.5 只换 reasoning_content：content（结论）与 tool_calls（配对）原样保留。"""
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        msgs = [asst("结论", reasoning="r" * 600, tool_calls=tc)]
        out = apply_slimming(msgs, {0})
        assert out[0]["content"] == "结论"
        assert out[0]["tool_calls"] is tc
        assert "600" in out[0]["reasoning_content"]
        assert msgs[0]["reasoning_content"] == "r" * 600  # 存储不动

    def test_out_of_range_index_downgrades_to_noop(self):
        """错误降级方向：下标越界只许漏处理，不许抛错或换错消息。"""
        msgs = [user()]
        assert apply_slimming(msgs, {99}) == msgs


# ============ token 估算与校准 ============

class TestEstimation:
    def test_count_chars_covers_all_payload(self):
        """同一把尺子：content + reasoning_content + tool_calls 参数都算负载。"""
        m = asst("c" * 10, reasoning="r" * 20,
                 tool_calls=[{"function": {"arguments": "a" * 30}}])
        assert _count_chars(m) == 60

    def test_estimate_uses_shared_coefficient(self):
        assert estimate_tokens(user("u" * 100)) == 50  # 系数 2.0（fixture 已重置）


class TestCalibrate:
    def test_small_samples_skipped(self):
        calibrate(real_tokens=500, messages=[user("u" * 4000)])
        assert compact._chars_per_token == 2.0  # real_tokens < 1000 不校准
        calibrate(real_tokens=5000, messages=[user("u" * 500)])
        assert compact._chars_per_token == 2.0  # chars < 1000 不校准

    def test_ema_moves_toward_observed(self):
        calibrate(real_tokens=1000, messages=[user("u" * 4000)])  # observed = 4.0
        assert compact._chars_per_token == pytest.approx(0.7 * 2.0 + 0.3 * 4.0)

    def test_clamped(self):
        calibrate(real_tokens=1000, messages=[user("u" * 100_000)])  # observed = 100
        assert compact._chars_per_token == 6.0



# ============ L1 截断：切点计算 ============

class TestDetectTruncationPoint:
    def test_fits_returns_zero(self):
        msgs = [{"role": "system", "content": "s"}, user(), asst()]
        assert detect_truncation_point(msgs, budget_tokens=10_000) == 0

    def test_packs_from_tail(self):
        """反向装箱：装得下最新消息，优先丢最老的。"""
        msgs = [{"role": "system", "content": "s" * 10},  # prefix: 5 tokens
                user("u" * 100), asst("a" * 100)]          # prefix: 首轮 100 tokens
        # 中段 4 条非保留区消息，各 100 字符 = 50 tokens
        msgs += [user("m" * 100), asst("m" * 100), user("n" * 100), asst("n" * 100)]
        prefix_tokens = (10 + 100 + 100) // 2  # 105
        cut = detect_truncation_point(msgs, budget_tokens=prefix_tokens + 100)
        assert cut == 5  # 只装得下最新 2 条（idx 5,6... 实际 tail = msgs[5:]）
        assert msgs[cut]["content"] == "n" * 100 or msgs[cut]["role"] == "user"

    def test_never_orphans_tool_message(self):
        """切点吸附安全边界：切点落在 tool 消息上必须前移，否则配对拆散 API 必 400。"""
        msgs = [{"role": "system", "content": "s" * 10}, user("u" * 100), asst("a" * 100)]
        msgs.append(user("m" * 100))                                      # idx 3：第二轮起点
        msgs += make_tool_pair("c1", "read_file", "{}", "t" * 100)          # idx 4,5
        msgs += [user("n" * 100), asst("n" * 100)]                          # idx 6,7
        prefix_tokens = (10 + 100 + 100) // 2  # 保留区 = {0,1,2}，105 tokens
        # 预算装 2 条尾部（idx 7,6）→ 自然切点 5 是 tool 消息，必须吸附到 6
        cut = detect_truncation_point(msgs, budget_tokens=prefix_tokens + 100)
        assert msgs[cut]["role"] != "tool"
        assert cut == 6

    def test_tiny_budget_keeps_last_user(self):
        """兜底：预算连最近一轮都装不下时，至少保留最后一个 user 起的尾部。"""
        msgs = [{"role": "system", "content": "s"}, user("u" * 100), asst("a" * 100),
                user("last")]
        cut = detect_truncation_point(msgs, budget_tokens=1)
        assert msgs[cut]["content"] == "last"

    def test_tools_declaration_survives(self):
        """结构性保留：历史中段带 tools 键的 system 消息（动态注入声明）必须在保留区。"""
        msgs = [{"role": "system", "content": "s"},
                user("first"), asst("a"),
                {"role": "system", "tools": [{"x": 1}]},  # idx 3：动态注入
                user("second"), asst("b")]
        prefix = _prefix_indices(msgs)
        assert 3 in prefix
        cut = detect_truncation_point(msgs, budget_tokens=1)
        if cut:  # 发生截断时注入声明仍在投影头部
            out = apply_truncation(msgs, cut)
            assert any(m.get("tools") for m in out)


# ============ L1 截断：apply ============

class TestApplyTruncation:
    def test_cut_zero_identity(self):
        msgs = [user()]
        assert apply_truncation(msgs, 0) is msgs

    def test_head_marker_tail_structure(self):
        msgs = [{"role": "system", "content": "s"}, user("first"), asst("a"),
                user("drop-me"), asst("drop-me-too"), user("tail"), asst("tail-a")]
        out = apply_truncation(msgs, cut=5)
        roles = [m["role"] for m in out]
        # head(system + 首轮) + marker(system) + tail
        assert roles == ["system", "user", "assistant", "system", "user", "assistant"]
        assert "截断" in out[3]["content"]
        assert out[4]["content"] == "tail"

    def test_note_replaces_marker(self):
        """L2 摘要复用同一切点：note 传入时标记位换成摘要文本。"""
        msgs = [user("first"), asst("a"), user("drop"), asst("drop-a"), user("tail")]
        # 保留区 = 首轮 {0,1}；cut=3 → tail = {3,4}，dropped=1 → 标记位在 out[2]
        out = apply_truncation(msgs, cut=3, note="[早期对话历史摘要]\nxxx")
        assert out[2]["content"].startswith("[早期对话历史摘要]")

    def test_no_marker_when_nothing_dropped(self):
        """空中段（实际没丢消息）不插标记，避免误导模型。"""
        msgs = [{"role": "system", "content": "s"}, user("only-round"), asst("a")]
        # 全部在保留区（首轮）：head=全部，tail=空，dropped=0
        out = apply_truncation(msgs, cut=3)
        assert all("截断" not in str(m.get("content", "")) for m in out)
        assert len(out) == 3


# ============ L2 摘要 ============

class FakeCompletions:
    def __init__(self, content="摘要内容", exc=None):
        self._content, self._exc = content, exc
        self.last_messages = None

    def create(self, model, messages, stream):
        self.last_messages = messages
        if self._exc:
            raise self._exc
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self._content))])


class TestSummarize:
    def test_empty_middle_returns_none(self):
        assert summarize_middle([], FakeCompletions()) is None

    def test_success_returns_summary(self):
        fake = FakeCompletions(content="四段摘要")
        assert summarize_middle([user("中段")], SimpleNamespace(chat=SimpleNamespace(completions=fake))) == "四段摘要"

    def test_failure_falls_back_silently(self):
        """摘要失败返回 None（上层回退 L1 硬切）——绝不能让压缩动作拖崩主流程。"""
        fake = FakeCompletions(exc=RuntimeError("API down"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        assert summarize_middle([user("中段")], client) is None

    def test_oversized_middle_truncated_from_tail(self):
        """中段超长时只取靠后部分（更贴近当前任务），且保留截断提示。"""
        fake = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        middle = [user("x" * (compact.SUMMARIZE_MAX_CHARS + 10_000))]
        summarize_middle(middle, client)
        sent = fake.last_messages[1]["content"]
        assert "更早部分略" in sent
        assert len(sent) < len(json.dumps(middle, ensure_ascii=False))


# ============ 中段提取 ============

class TestExtractMiddle:
    def test_excludes_prefix_and_tail(self):
        msgs = [{"role": "system", "content": "s"}, user("first"), asst("a"),
                user("mid"), asst("mid-a"), user("tail")]
        # 保留区 = {0,1,2}（首轮到第二个 user 之前）；cut=5 → 中段 = {3,4}，tail = {5}
        middle = extract_middle(msgs, cut=5)
        contents = [m["content"] for m in middle]
        assert contents == ["mid", "mid-a"]
