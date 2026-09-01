"""bench 评测的纯函数：manifest/META 加载、graded 分数解析、summary 对比。"""

import json

from bench.scoring import (
    build_summary,
    compare_summaries,
    load_manifest,
    load_meta,
    parse_verify_score,
)


def test_parse_verify_score_extracts_graded():
    assert parse_verify_score("verify OK score=0.8\n") == 0.8
    assert parse_verify_score("score = 0.5") == 0.5


def test_parse_verify_score_returns_none_without_score():
    assert parse_verify_score("verify OK\n") is None


def test_parse_verify_score_clamps():
    assert parse_verify_score("score=1.7") == 1.0


def test_load_manifest_default_when_missing(tmp_path):
    m = load_manifest(tmp_path / "nope.json")
    assert m["version"] == 1
    assert m["regression_threshold"] == 0.1


def test_load_manifest_reads_file(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"version": 3, "regression_threshold": 0.05}))
    assert load_manifest(p)["version"] == 3


def test_load_meta_default_when_missing(tmp_path):
    meta = load_meta(tmp_path)
    assert meta["judge"] == "deterministic"


def test_build_summary_aggregates():
    records = [
        {"task": "a", "passed": True, "score": 1.0, "prompt_tokens": 100, "completion_tokens": 10, "elapsed_s": 5.0},
        {"task": "b", "passed": False, "score": 0.4, "prompt_tokens": 50, "completion_tokens": 5, "elapsed_s": 3.0},
    ]
    s = build_summary(records, version=1)
    assert s["aggregate"]["pass_rate"] == 0.5
    assert s["aggregate"]["avg_score"] == 0.7
    assert s["aggregate"]["total_tokens"] == 165


def test_compare_summaries_detects_regression():
    prev = {"tasks": {"a": {"score": 1.0, "passed": True}, "b": {"score": 0.9, "passed": True}}}
    curr = {"tasks": {"a": {"score": 0.8, "passed": True}, "c": {"score": 1.0, "passed": True}}}
    text = "\n".join(compare_summaries(prev, curr))
    assert "a" in text and "回归" in text
    assert "c" in text and "新增" in text
    assert "b" in text and "移除" in text


def test_compare_summaries_no_baseline():
    assert compare_summaries(None, {"tasks": {}}) == ["（无 baseline，跳过对比）"]
