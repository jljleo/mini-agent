"""judge.py 回归测试：LLM-as-judge 的解析与调用。零网络（client 打桩）。"""

from types import SimpleNamespace

from judge import _coerce, _parse_score, judge


def _client(content):
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    response = SimpleNamespace(choices=[choice])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))


def test_parse_score_plain_json():
    assert _parse_score('{"score": 0.8, "reason": "基本正确"}') == {"score": 0.8, "reason": "基本正确"}


def test_parse_score_with_fence():
    raw = '```json\n{"score": 0.6, "reason": "部分正确"}\n```'
    assert _parse_score(raw) == {"score": 0.6, "reason": "部分正确"}


def test_parse_score_clamps_out_of_range():
    assert _parse_score('{"score": 1.5, "reason": "x"}')["score"] == 1.0
    assert _parse_score('{"score": -0.2}')["score"] == 0.0


def test_parse_score_fallback_when_garbage():
    result = _parse_score("抱歉我无法评分")
    assert result["score"] == 0.0
    assert "解析失败" in result["reason"]


def test_judge_calls_client_and_parses():
    client = _client('{"score": 0.9, "reason": "达标"}')
    result = judge(client, "产出文本", "标准")
    assert result == {"score": 0.9, "reason": "达标"}


def test_coerce_missing_fields_default():
    assert _coerce({}) == {"score": 0.0, "reason": ""}
    assert _coerce({"score": "0.5"}) == {"score": 0.5, "reason": ""}
