"""run_bench 驱动的行为契约：任务发现与缺 verify.py 防护。

约束：不访问网络。discover_tasks / score_task 用 tmp_path 隔离，不依赖真实 bench/tasks。
"""

import bench.run_bench as run_bench


def test_discover_tasks_includes_llm_judge_without_verify(tmp_path, monkeypatch):
    """llm-judge 任务无 verify.py，也应按 PROMPT.md 被发现（deterministic 才有 verify.py）。"""
    det = tmp_path / "a_det"
    det.mkdir()
    (det / "PROMPT.md").write_text("x", encoding="utf-8")
    (det / "verify.py").write_text("x", encoding="utf-8")
    judge = tmp_path / "b_judge"
    judge.mkdir()
    (judge / "PROMPT.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(run_bench, "TASKS_DIR", tmp_path)

    names = [d.name for d in run_bench.discover_tasks()]
    assert names == ["a_det", "b_judge"]


def test_discover_tasks_ignores_dir_without_prompt(tmp_path, monkeypatch):
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "verify.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(run_bench, "TASKS_DIR", tmp_path)

    assert run_bench.discover_tasks() == []


def test_score_task_deterministic_missing_verify_returns_error(tmp_path):
    """deterministic 任务缺 verify.py 应返回明确 error，而非 FileNotFoundError 崩溃。"""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    result = run_bench.score_task({"judge": "deterministic"}, task_dir, sandbox, None)
    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "缺 verify.py" in result["error"]
