"""benchmark 驱动：批量任务 → 沙箱执行 → 分层判分 → 轨迹导出 → 回归对比。

用法（在项目根目录）：
    python bench/run_bench.py                 # 跑全部任务
    python bench/run_bench.py fix_fizzbuzz    # 只跑指定任务
    python bench/run_bench.py --compare       # 跑完后与 baseline 对比回归

任务结构（bench/tasks/<name>/）：
    workspace/   agent 的工作区，会被复制到独立临时目录
    PROMPT.md    发给 agent 的任务描述
    META.json    任务元数据：judge 类型（deterministic/graded/llm-judge）、rubric 等
    verify.py    判分脚本（deterministic/graded 任务；llm-judge 任务不需要）

评分三层：
    deterministic：verify.py exit 0 = pass
    graded：verify.py 输出 score=0.8 之类，解析为 0~1 分
    llm-judge：无确定性判据时，judge.py 用 LLM + rubric 打分

可观测：TraceRecorder 挂在事件流上，每任务产出 .trace.jsonl 轨迹。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
BENCH_DIR = PROJECT_ROOT / "bench"
TASKS_DIR = BENCH_DIR / "tasks"
RESULTS_DIR = BENCH_DIR / "results"

import tools  # noqa: E402
import ui  # noqa: E402
from agent import ChatSession  # noqa: E402
from bench.scoring import (  # noqa: E402
    build_summary,
    compare_summaries,
    load_manifest,
    load_meta,
    parse_verify_score,
)
from judge import judge, make_client  # noqa: E402
from trace import TraceRecorder  # noqa: E402


def discover_tasks(only: str | None = None) -> list[Path]:
    """发现任务目录：含 PROMPT.md 的即算任务。

    verify.py 按需存在：deterministic / graded 任务必须有，llm-judge 任务
    （META.json 里 judge=llm-judge）没有 verify.py——产出是对话正文，判分走 judge.py。
    """
    tasks = sorted(
        d for d in TASKS_DIR.iterdir()
        if d.is_dir() and (d / "PROMPT.md").exists()
    )
    if only:
        tasks = [d for d in tasks if d.name == only]
        if not tasks:
            sys.exit(f"找不到任务: {only}（可用: {[d.name for d in discover_tasks()]}）")
    return tasks


def last_assistant_text(session) -> str:
    """取会话最后一条 assistant 正文，作为 llm-judge 的产出输入。"""
    if not session:
        return ""
    for m in reversed(session.messages):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return ""


def score_task(meta: dict, task_dir: Path, sandbox: Path, session) -> dict:
    """按 META.judge 分层判分，返回 {passed, score, method, ...}。"""
    judge_type = meta.get("judge", "deterministic")

    if judge_type == "llm-judge":
        result = judge(make_client(), last_assistant_text(session), meta.get("rubric", ""))
        return {
            "passed": result["score"] >= 0.5,
            "score": result["score"],
            "method": "llm-judge",
            "judge_reason": result["reason"],
        }

    verify = task_dir / "verify.py"
    if not verify.exists():
        return {
            "passed": False,
            "score": 0.0,
            "method": judge_type,
            "error": f"{judge_type} 任务缺 verify.py（llm-judge 任务才不需要）",
        }

    proc = subprocess.run(
        [sys.executable, str(verify)],
        cwd=sandbox, capture_output=True, text=True, timeout=60,
    )
    verify_stdout, verify_stderr = proc.stdout[-2000:], proc.stderr[-2000:]

    if judge_type == "graded":
        score = parse_verify_score(proc.stdout)
        if score is not None:
            return {
                "passed": proc.returncode == 0 or score >= 0.5,
                "score": score,
                "method": "graded",
                "verify_stdout": verify_stdout,
                "verify_stderr": verify_stderr,
            }

    return {
        "passed": proc.returncode == 0,
        "score": 1.0 if proc.returncode == 0 else 0.0,
        "method": "deterministic",
        "verify_stdout": verify_stdout,
        "verify_stderr": verify_stderr,
    }


def run_task(task_dir: Path, meta: dict) -> tuple[dict, TraceRecorder]:
    """单任务全流程：复制工作区 → 沙箱内跑 agent（带 trace）→ 分层判分 → 返回记录。"""
    sandbox = Path(tempfile.mkdtemp(prefix=f"bench_{task_dir.name}_"))
    shutil.copytree(task_dir / "workspace", sandbox, dirs_exist_ok=True)
    prompt = (task_dir / "PROMPT.md").read_text(encoding="utf-8").strip()

    saved_root = tools.PROJECT_ROOT
    saved_confirm = tools.confirm
    tools.PROJECT_ROOT = str(sandbox)
    tools.confirm = lambda *args, **kwargs: True
    recorder = TraceRecorder(task_dir.name)
    session = None
    try:
        session = ChatSession()
        ui.consume_quiet(recorder.wrap(session.chat(prompt)))
    except Exception as e:
        print(f"[bench] agent 异常中断: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        tools.PROJECT_ROOT = saved_root
        tools.confirm = saved_confirm

    scoring = score_task(meta, task_dir, sandbox, session)
    record = {
        "task": task_dir.name,
        **scoring,
        "prompt_tokens": session.total_prompt_tokens if session else 0,
        "completion_tokens": session.total_completion_tokens if session else 0,
        "sandbox": str(sandbox),
        "messages": session.messages if session else [],
    }
    return record, recorder


def main() -> None:
    compare = "--compare" in sys.argv[1:]
    only = next((a for a in sys.argv[1:] if not a.startswith("--")), None)

    manifest = load_manifest(TASKS_DIR / "manifest.json")
    tasks = discover_tasks(only)
    RESULTS_DIR.mkdir(exist_ok=True)

    summary_path = RESULTS_DIR / "summary.json"
    prev_summary = None
    if compare and summary_path.exists():
        prev_summary = json.loads(summary_path.read_text(encoding="utf-8"))

    print(f"[bench] {len(tasks)} 个任务待跑")
    records = []
    for task_dir in tasks:
        meta = load_meta(task_dir)
        judge_type = meta.get("judge", "deterministic")
        print(f"[bench] ▶ {task_dir.name} [{judge_type}]")
        started = time.time()
        record, recorder = run_task(task_dir, meta)
        record["elapsed_s"] = round(time.time() - started, 1)
        records.append(record)

        ts = time.strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"{task_dir.name}-{ts}.json"
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        trace_out = RESULTS_DIR / f"{task_dir.name}-{ts}.trace.jsonl"
        trace_out.write_text(recorder.to_jsonl(), encoding="utf-8")

        mark = "✅ PASS" if record["passed"] else "❌ FAIL"
        tokens = record["prompt_tokens"] + record["completion_tokens"]
        print(f"[bench] {mark} · score={record['score']:.2f} · {tokens:,} tokens · {record['elapsed_s']}s")

    summary = build_summary(records, manifest.get("version", 1))
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(r["passed"] for r in records)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in records)
    print(f"\n[bench] 通过率 {passed}/{len(records)} · 均分 {summary['aggregate']['avg_score']} · 总消耗 {total_tokens:,} tokens")

    if compare:
        print("\n[bench] 回归对比（vs baseline）：")
        for line in compare_summaries(prev_summary, summary):
            print(line)
        threshold = manifest.get("regression_threshold", 0.1)
        regressions = []
        prev_tasks = (prev_summary or {}).get("tasks", {})
        for name, t in summary["tasks"].items():
            p = prev_tasks.get(name)
            if p and (p["score"] - t["score"]) > threshold:
                regressions.append(f"{name}（{p['score']}→{t['score']}）")
        if regressions:
            print(f"\n[bench] ⚠ 回归告警：{', '.join(regressions)}")
        else:
            print("\n[bench] 无回归")


if __name__ == "__main__":
    main()
