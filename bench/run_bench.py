"""benchmark 驱动：批量任务 → 沙箱执行 → verify 判分 → 轨迹导出。

用法（在项目根目录）：
    python bench/run_bench.py                 # 跑全部任务
    python bench/run_bench.py fix_fizzbuzz    # 只跑指定任务

任务结构（bench/tasks/<name>/）：
    workspace/   agent 的工作区，会被复制到独立临时目录
    PROMPT.md    发给 agent 的任务描述
    verify.py    判分脚本：在沙箱内运行，exit 0 = pass

设计要点：
- 工作区隔离：tools.PROJECT_ROOT 指向任务沙箱，agent 的文件工具和 bash cwd
  都锚定在沙箱里——bench 的核心缺口（PROJECT_ROOT 写死）用测试体系趟平的
  同款手法（monkeypatch 式全局替换）解决。
- 自动批准：bench 是无人值守场景，非交互环境 confirm 默认拒绝会把 agent 饿死；
  沙箱即边界（tmp 目录、跑完即焚），箱内一切操作自动放行。
- 轨迹导出：每任务的 messages + token 消耗 + 判分结果落盘 bench/results/，
  benchmark 的价值不只在 pass/fail，更在能复盘"哪一步走岔了"。
"""

import json
import shutil
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


def discover_tasks(only: str | None = None) -> list[Path]:
    """发现任务目录：含 PROMPT.md 与 verify.py 的才算完整任务。"""
    tasks = sorted(
        d for d in TASKS_DIR.iterdir()
        if d.is_dir() and (d / "PROMPT.md").exists() and (d / "verify.py").exists()
    )
    if only:
        tasks = [d for d in tasks if d.name == only]
        if not tasks:
            sys.exit(f"找不到任务: {only}（可用: {[d.name for d in discover_tasks()]}）")
    return tasks


def run_task(task_dir: Path) -> dict:
    """单任务全流程：复制工作区 → 沙箱内跑 agent → verify 判分 → 返回结果记录。"""
    sandbox = Path(tempfile.mkdtemp(prefix=f"bench_{task_dir.name}_"))
    shutil.copytree(task_dir / "workspace", sandbox, dirs_exist_ok=True)
    prompt = (task_dir / "PROMPT.md").read_text(encoding="utf-8").strip()

    # 沙箱替换：PROJECT_ROOT 指向任务工作区 + bench 模式自动批准（跑完恢复原值）
    saved_root = tools.PROJECT_ROOT
    saved_confirm = tools.confirm
    tools.PROJECT_ROOT = str(sandbox)
    tools.confirm = lambda *args, **kwargs: True
    session = None
    try:
        session = ChatSession()
        # chat() 是事件流生成器：bench 复用终端消费者（输出保持可见，便于观察轨迹）
        ui.consume(session.chat(prompt))
    except Exception as e:
        # agent 崩溃 ≠ 任务失败：记录崩溃原因，仍走 verify（可能部分完成）
        print(f"[bench] agent 异常中断: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        tools.PROJECT_ROOT = saved_root
        tools.confirm = saved_confirm

    # 判分：verify.py 在沙箱内运行，exit 0 = pass
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(task_dir / "verify.py")],
        cwd=sandbox, capture_output=True, text=True, timeout=60,
    )
    passed = proc.returncode == 0

    record = {
        "task": task_dir.name,
        "passed": passed,
        "prompt_tokens": session.total_prompt_tokens if session else 0,
        "completion_tokens": session.total_completion_tokens if session else 0,
        "sandbox": str(sandbox),
        "verify_stdout": proc.stdout[-2000:],  # 判分输出留档（截断防爆）
        "verify_stderr": proc.stderr[-2000:],
        "messages": session.messages if session else [],
    }
    if not passed:
        record["fail_hint"] = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["(无输出)"]
    return record


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    tasks = discover_tasks(only)
    RESULTS_DIR.mkdir(exist_ok=True)

    print(f"[bench] {len(tasks)} 个任务待跑")
    records = []
    for task_dir in tasks:
        print(f"[bench] ▶ {task_dir.name}")
        started = time.time()
        record = run_task(task_dir)
        record["elapsed_s"] = round(time.time() - started, 1)
        records.append(record)

        out = RESULTS_DIR / f"{task_dir.name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        mark = "✅ PASS" if record["passed"] else "❌ FAIL"
        total = record["prompt_tokens"] + record["completion_tokens"]
        print(f"[bench] {mark} · {total:,} tokens · {record['elapsed_s']}s → {out.name}")

    passed = sum(r["passed"] for r in records)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in records)
    print(f"\n[bench] 通过率 {passed}/{len(records)} · 总消耗 {total_tokens:,} tokens")


if __name__ == "__main__":
    main()
