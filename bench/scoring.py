"""bench 评测的纯函数：manifest/META 加载、graded 分数解析、summary 汇总与对比。

与 run_bench.py 的驱动逻辑分离：本模块零 IO 副作用（除了读文件），可单测。
"""

import json
import re
import time
from pathlib import Path


def load_manifest(path: Path) -> dict:
    """加载 bench/tasks/manifest.json；不存在时返回默认（版本 1、阈值 0.1）。"""
    if not path.exists():
        return {"version": 1, "regression_threshold": 0.1}
    return json.loads(path.read_text(encoding="utf-8"))


def load_meta(task_dir: Path) -> dict:
    """加载单任务的 META.json；缺失时返回默认 deterministic 元数据。"""
    meta_path = task_dir / "META.json"
    if not meta_path.exists():
        return {"version": 1, "category": "uncategorized", "difficulty": "unknown",
                "judge": "deterministic", "rubric": ""}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def parse_verify_score(stdout: str) -> float | None:
    """从 verify.py 输出解析 graded 分数（如 "score=0.8"）；无分数返回 None。"""
    m = re.search(r"score\s*=\s*([0-9]+(?:\.[0-9]+)?)", stdout)
    if not m:
        return None
    try:
        return max(0.0, min(1.0, float(m.group(1))))
    except ValueError:
        return None


def build_summary(records: list[dict], version: int) -> dict:
    """把任务结果记录汇总成 summary：每任务分数 + 聚合指标。"""
    tasks = {}
    for r in records:
        tasks[r["task"]] = {
            "score": r.get("score", 1.0 if r.get("passed") else 0.0),
            "passed": r.get("passed", False),
            "method": r.get("method", "deterministic"),
            "tokens": r.get("prompt_tokens", 0) + r.get("completion_tokens", 0),
            "elapsed_s": r.get("elapsed_s", 0.0),
        }
    scores = [t["score"] for t in tasks.values()]
    return {
        "version": version,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
        "aggregate": {
            "pass_rate": round(sum(1 for t in tasks.values() if t["passed"]) / len(tasks), 3) if tasks else 0.0,
            "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "total_tokens": sum(t["tokens"] for t in tasks.values()),
        },
    }


def compare_summaries(prev: dict | None, curr: dict) -> list[str]:
    """对比 prev 与 curr 两个 summary，产出一行行 delta 文本（回归标记）。"""
    if prev is None:
        return ["（无 baseline，跳过对比）"]
    lines = []
    prev_tasks = prev.get("tasks", {})
    for name, t in curr.get("tasks", {}).items():
        p = prev_tasks.get(name)
        if p is None:
            lines.append(f"  + {name}: 新增任务，score={t['score']}")
            continue
        delta = round(t["score"] - p["score"], 3)
        flag = "" if delta >= 0 else "  ⚠ 回归"
        lines.append(f"  {name}: {p['score']} → {t['score']}（{delta:+.3f}）{flag}")
    for name in prev_tasks:
        if name not in curr.get("tasks", {}):
            lines.append(f"  - {name}: 已移除")
    return lines
