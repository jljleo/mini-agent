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


def _path_match(finding_path: str, bug_path: str) -> bool:
    """findings 里的路径与 ground truth 对齐：剥 diff 头可能带入的 a//b/ 前缀，
    允许尾缀匹配（模型可能输出仓库相对路径而 ground truth 是子目录相对）。"""
    norm = finding_path.removeprefix("a/").removeprefix("b/")
    return norm == bug_path or norm.endswith("/" + bug_path)


def _in_zone(path: str, line: int, zone: dict, line_tolerance: int) -> bool:
    lo, hi = zone["lines"]
    return _path_match(path, zone["path"]) and lo - line_tolerance <= line <= hi + line_tolerance


def score_review(findings: list[dict], bugs: list[dict], line_tolerance: int = 3,
                 neutral: list[dict] | None = None) -> dict:
    """review 任务判分（纯函数）：findings 对照 ground truth bugs。

    命中 = 路径匹配且行号落在 bug 行区间 ±line_tolerance（模型定位允许小偏差）；
    同一 finding 最多命中一个 bug（先匹配先得）。

    bug 可带 aliases：同一 bug 的其它可接受定位（如缓存 key 定义处 vs 使用处）。
    neutral（顶层传入）：有效但非 ground truth 的区间——命中 neutral 的 finding
    既不算检出也不算误报（E8 教训：docstring 未同步/死代码这类派生观察是 review 的
    正当产出，precision 不该惩罚它；但它是 bug 的影子，也不该记检出）。

    返回 score=recall（主指标），precision=命中/(命中+真误报)（neutral 不进分母）。
    """
    neutral = neutral or []
    used: set[int] = set()
    neutral_count = 0
    hits, missed = [], []
    for bug in bugs:
        zones = [bug, *bug.get("aliases", [])]
        hit_idx = None
        for i, f in enumerate(findings):
            if i in used:
                continue
            if any(_in_zone(f["path"], f["line"], z, line_tolerance) for z in zones):
                hit_idx = i
                break
        if hit_idx is None:
            missed.append(bug["id"])
        else:
            used.add(hit_idx)
            hits.append(bug["id"])
    bug_zones = [z for bug in bugs for z in [bug, *bug.get("aliases", [])]]
    false_positives = []
    for i, f in enumerate(findings):
        if i in used:
            continue
        # 落在任一 bug 区间（含已被其他 finding 命中的）或 neutral 区间的 finding
        # 不算误报：指向的位置确实有注入缺陷——同区多条至多是冗余，不是错误
        # （已知可被「在同一区域刷屏」钻空子，模型真开始刷时再收紧，E9 留档）
        if any(_in_zone(f["path"], f["line"], z, line_tolerance) for z in bug_zones + neutral):
            neutral_count += 1
        else:
            false_positives.append(f)
    recall = len(hits) / len(bugs) if bugs else 1.0
    scored = len(findings) - neutral_count
    precision = len(hits) / scored if scored else 1.0
    return {
        "score": recall,
        "passed": recall >= 0.5,
        "method": "review-ground-truth",
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "hits": hits,
        "missed": missed,
        "false_positives": len(false_positives),
        "neutral": neutral_count,
    }

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
