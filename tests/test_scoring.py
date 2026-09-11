"""score_review 纯函数测试：ground truth 命中 / 行号容差 / 误报 / 路径归一化。"""

from bench.scoring import score_review

BUGS = [
    {"id": "bug-a", "path": "src/auth.py", "lines": [40, 45], "description": "空指针"},
    {"id": "bug-b", "path": "src/util.py", "lines": [10, 10], "description": "off-by-one"},
]


def f(severity, path, line):
    return {"severity": severity, "path": path, "line": line, "description": "x"}


class TestScoreReview:
    def test_full_recall_and_precision(self):
        findings = [f("high", "src/auth.py", 42), f("medium", "src/util.py", 10)]
        result = score_review(findings, BUGS)
        assert result["recall"] == 1.0 and result["precision"] == 1.0
        assert result["passed"] and result["score"] == 1.0
        assert result["false_positives"] == 0 and result["missed"] == []

    def test_line_tolerance(self):
        """行号容差 ±3：[40,45] 的命中区间是 [37,48]，48 命中、49 超出。"""
        assert score_review([f("high", "src/auth.py", 49)], BUGS)["hits"] == []
        assert score_review([f("high", "src/auth.py", 48)], BUGS)["hits"] == ["bug-a"]

    def test_false_positive_rate(self):
        """误报 = 不命中任何 bug 的 finding；precision 随之下降。"""
        findings = [f("high", "src/auth.py", 42), f("low", "src/other.py", 1)]
        result = score_review(findings, BUGS)
        assert result["recall"] == 0.5 and result["precision"] == 0.5
        assert result["missed"] == ["bug-b"] and result["false_positives"] == 1

    def test_path_normalization(self):
        """diff 头前缀 a//b/ 与尾缀路径都要能对上 ground truth。"""
        assert score_review([f("high", "b/src/auth.py", 42)], BUGS)["hits"] == ["bug-a"]
        assert score_review([f("high", "repo/src/auth.py", 42)], BUGS)["hits"] == ["bug-a"]

    def test_one_finding_cannot_cover_two_bugs(self):
        """同一 finding 最多命中一个 bug（防一行蒙中全库）。"""
        bugs = [
            {"id": "a", "path": "x.py", "lines": [10, 12], "description": ""},
            {"id": "b", "path": "x.py", "lines": [11, 13], "description": ""},
        ]
        result = score_review([f("high", "x.py", 11)], bugs)
        assert result["recall"] == 0.5 and len(result["hits"]) == 1

    def test_empty_findings_zero_recall(self):
        result = score_review([], BUGS)
        assert result["recall"] == 0.0 and not result["passed"]
