"""A/B 对照分析：repo map 实验组(map) vs 对照组(nomap)。

读取 bench/results/*-{map,nomap}.json，按任务对齐多次运行，输出指标对比：
    通过率 / 平均轮数 / 平均 prompt tokens / 首轮 read_file 命中目标文件率

用法:
    .venv/bin/python bench/compare_ab.py          # 全部任务
    .venv/bin/python bench/compare_ab.py fix_checkout
"""
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# 各任务的「目标文件」（首轮 read_file 命中它的比例 = 定位效率的直接证据）
TARGETS = {
    "fix_checkout": "store/checkout.py",
    "fix_discount": "src/fees.js",
    "fix_retry": "service/retry.py",
    "fix_notify_dedupe": "src/worker/helpers.py",
}


def load_group(group: str) -> dict[str, list[dict]]:
    by_task: dict[str, list[dict]] = {}
    for f in sorted(RESULTS.glob(f"*-{group}.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        by_task.setdefault(rec.get("task", "?"), []).append(rec)
    return by_task


def first_read_file(rec: dict) -> str:
    """会话中第一次 read_file 调用的 path 参数（'' = 从未 read_file）。"""
    for m in rec.get("messages", []):
        for tool in m.get("tool_calls") or []:
            fn = tool.get("function", {}) or {}
            if fn.get("name") == "read_file":
                try:
                    return json.loads(fn.get("arguments") or "{}").get("path", "")
                except json.JSONDecodeError:
                    return ""
    return ""


def turns(rec: dict) -> int:
    """轮数 = assistant 消息数（每条助理消息即一轮模型输出）。"""
    return sum(1 for m in rec.get("messages", [])
               if m.get("role") == "assistant" and (m.get("content") or m.get("tool_calls")))


def avg(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def main() -> None:
    only = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    model = next((a.split("=", 1)[1] for a in sys.argv[1:]
                  if a.startswith("--model=")), None)
    map_g, nomap_g = load_group("map"), load_group("nomap")

    tasks = sorted(t for t in set(map_g) | set(nomap_g) if only is None or t == only)

    print(f"{'任务':<15}{'组':<7}{'N':<3}{'通过':<7}{'轮均':<7}{'prompt均':<9}"
          f"{'token均':<10}{'首读命中':<9}")
    print("-" * 72)
    agg = {"map": dict(n=0, ok=0, turns=0, tokens=0, hit=0, reads=0, reads_any=0),
           "nomap": dict(n=0, ok=0, turns=0, tokens=0, hit=0, reads=0, reads_any=0)}
    for t in tasks:
        for group in ("map", "nomap"):
            src = map_g if group == "map" else nomap_g
            recs = [r for r in src.get(t, []) if model is None or r.get("model", "kimi-k3") == model]
            if not recs:
                continue
            n = len(recs)
            ok = sum(r["passed"] for r in recs)
            t_avg = avg([turns(r) for r in recs])
            pt = avg([r.get("prompt_tokens", 0) for r in recs])
            tok = avg([r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in recs])
            target = TARGETS.get(t)
            hits = reads = 0
            for r in recs:
                p = first_read_file(r).replace("\\", "/")
                if p:
                    reads += 1
                    if target and p.rstrip("/").endswith(target.rstrip("/")):
                        hits += 1
            hit_str = f"{hits}/{reads}" if target else "-"
            a = agg[group]
            a["n"] += n
            a["ok"] += ok
            a["turns"] += t_avg * n
            a["tokens"] += tok * n
            a["hit"] += hits
            a["reads"] += reads
            a["reads_any"] += n
            print(f"{t:<15}{group:<7}{n:<3}{ok}/{n:<6}{t_avg:<7.1f}{pt:<9.0f}"
                  f"{tok:<10.0f}{hit_str:<9}")
    print("-" * 72)
    for group in ("map", "nomap"):
        a = agg[group]
        if not a["n"]:
            continue
        hit_str = f"{a['hit']}/{a['reads']}" if a["reads"] else "-"
        print(f"{'合计':<15}{group:<7}{a['n']:<3}{a['ok']}/{a['n']:<6}"
              f"{a['turns'] / a['n']:<7.1f}{'-':<9}{a['tokens'] / a['n']:<10.0f}{hit_str:<9}")


if __name__ == "__main__":
    main()