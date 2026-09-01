# mini-agent 评测 + 可观测体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 mini-agent 补上评测体系（版本化评测集 + 分层判分 + 回归对比）与可观测性（TraceRecorder 产出 JSONL 轨迹），作为多 agent 项目的地基。

**Architecture:** 评测与可观测都做成 `events.py` 的**纯消费者**，零内核改动。`trace.py` 的 `TraceRecorder` 用 `wrap()` 透传事件并记录轨迹；`judge.py` 用 LLM + rubric 给开放题判分；`bench/scoring.py` 放纯函数（manifest/META 加载、graded 分数解析、summary 汇总与对比）；`bench/run_bench.py` 做集成。

**Tech Stack:** Python 3.13，OpenAI SDK（judge 调用），pytest（socket 哨兵零网络），无外部评测平台。

**Spec:** `docs/superpowers/specs/2026-08-31-mini-agent-evals-observability-design.md`

---

### Task 1: TraceRecorder 可观测性核心

**Files:**
- Create: `trace.py`
- Test: `tests/test_trace.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_trace.py`:

```python
"""trace.py 回归测试：TraceRecorder 把事件流记录成结构化轨迹。

约束：不访问网络；TraceRecorder 是纯消费者（透传事件），用真实 events.py 事件对象喂。
"""

import json

from events import (
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnEnd,
    Usage,
)
from trace import TraceRecorder


def _finish():
    return StreamFinished([{"role": "assistant", "content": "ok"}], None)


def test_wrap_passthrough_events_unchanged():
    """wrap 必须原样透传事件（下游 ui.consume 依赖完整流）。"""
    events = [StreamStart(), TextDelta("你好"), _finish(), TurnEnd()]
    recorder = TraceRecorder("t1")
    assert list(recorder.wrap(iter(events))) == events


def test_turn_id_increments_on_stream_start():
    """每个 StreamStart 开启新 turn，事件按 turn 分段。"""
    recorder = TraceRecorder("t1")
    events = [StreamStart(), TextDelta("a"), TurnEnd(), StreamStart(), TextDelta("b"), TurnEnd()]
    list(recorder.wrap(iter(events)))
    turn_ids = [r["turn_id"] for r in recorder.records()]
    assert turn_ids[0] == 1
    assert turn_ids[1] == 1
    assert turn_ids[3] == 2
    assert turn_ids[4] == 2


def test_tool_calls_record_name_args_preview():
    """工具调用记录 name/args/result 摘要——复盘失败的原始材料。"""
    recorder = TraceRecorder("t1")
    list(recorder.wrap(iter([
        StreamStart(),
        ToolCallStart("read_file", '{"path": "a.py"}'),
        ToolCallResult("read_file", "hello"),
        TurnEnd(),
    ])))
    details = [r["detail"] for r in recorder.records() if r["event"] in ("ToolCallStart", "ToolCallResult")]
    assert details[0] == {"name": "read_file", "args": '{"path": "a.py"}'}
    assert details[1] == {"name": "read_file", "preview": "hello"}


def test_usage_and_delta_recorded():
    """Usage 记录 token 分项；TextDelta 记录字符数。"""
    recorder = TraceRecorder("t1")
    list(recorder.wrap(iter([
        StreamStart(),
        TextDelta("你好"),
        Usage(100, 10, 5, 110),
        TurnEnd(),
    ])))
    text_detail = next(r["detail"] for r in recorder.records() if r["event"] == "TextDelta")
    usage_detail = next(r["detail"] for r in recorder.records() if r["event"] == "Usage")
    assert text_detail == {"chars": 2}
    assert usage_detail == {"prompt": 100, "completion": 10, "cached": 5, "total": 110}


def test_to_jsonl_every_record_has_task_id():
    """JSONL 每条都带 task_id，可跨任务合并。"""
    recorder = TraceRecorder("task_x")
    list(recorder.wrap(iter([StreamStart(), TurnEnd()])))
    lines = recorder.to_jsonl().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["task_id"] == "task_x"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_trace.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'trace'`）

- [ ] **Step 3: 实现 trace.py**

Create `trace.py`:

```python
"""可观测性核心：TraceRecorder 消费事件流，产出结构化 JSONL 轨迹。

零内核改动：它是 events.py 的又一个消费者（同 ui.consume / bench 消费者）。
每次 StreamStart 开启一个新 turn span，所有事件记录 task_id + turn_id + 事件类型 +
字段摘要 + 相对上一事件的耗时，落成 JSONL 供"复盘哪一步走岔了"。
"""

import json
import time

from events import (
    Note,
    ReasoningDelta,
    StreamFinished,
    StreamStart,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    TurnEnd,
    Usage,
    Warn,
)


class TraceRecorder:
    """事件流 → 结构化轨迹记录器（纯消费者，透传事件不打断下游）。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._records: list[dict] = []
        self._turn_id = 0
        self._last_ts: float | None = None

    def wrap(self, events):
        """包一层事件流：逐事件记录后原样透传，下游（ui.consume）不受影响。"""
        self._last_ts = time.monotonic()
        for ev in events:
            self._record(ev)
            yield ev

    def _record(self, ev) -> None:
        now = time.monotonic()
        name = type(ev).__name__
        if name == "StreamStart":
            self._turn_id += 1
        elapsed_ms = round((now - self._last_ts) * 1000, 1) if self._last_ts is not None else 0.0
        self._records.append({
            "task_id": self.task_id,
            "turn_id": self._turn_id,
            "event": name,
            "elapsed_ms": elapsed_ms,
            "detail": self._detail(ev),
        })
        self._last_ts = now

    def _detail(self, ev) -> dict:
        if isinstance(ev, TextDelta):
            return {"chars": len(ev.text)}
        if isinstance(ev, ReasoningDelta):
            return {"chars": len(ev.text)}
        if isinstance(ev, StreamFinished):
            return {"messages": len(ev.messages)}
        if isinstance(ev, ToolCallStart):
            return {"name": ev.name, "args": ev.arguments}
        if isinstance(ev, ToolCallResult):
            return {"name": ev.name, "preview": ev.preview}
        if isinstance(ev, Usage):
            return {"prompt": ev.prompt, "completion": ev.completion,
                    "cached": ev.cached, "total": ev.total}
        if isinstance(ev, Note):
            return {"message": ev.message, "tag": ev.tag}
        if isinstance(ev, Warn):
            return {"message": ev.message}
        return {}

    def records(self) -> list[dict]:
        return list(self._records)

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in self._records)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_trace.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add trace.py tests/test_trace.py
git commit -m "feat: 新增 TraceRecorder 可观测性（事件消费者产出 JSONL 轨迹）"
```

---

### Task 2: LLM-as-judge 判分

**Files:**
- Create: `judge.py`
- Test: `tests/test_judge.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_judge.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_judge.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'judge'`）

- [ ] **Step 3: 实现 judge.py**

Create `judge.py`:

```python
"""LLM-as-judge：对无确定性判据的任务用 LLM + rubric 打分。

独立于 agent 会话：发起一次独立 judge 请求，不污染主会话上下文。
judge() 接收可注入的 client（测试用 mock），_parse_score 是纯函数。
"""

import json
import os
import re

from openai import OpenAI

from config import API_KEY_ENV, BASE_URL, MODEL

_JUDGE_SYSTEM = (
    "你是一名客观的评测判分员。根据给定的评分标准（rubric），评估任务产出是否达标，"
    "并给出 0 到 1 的分数（1=完全达标，0=完全未达标）。只输出一个 JSON 对象："
    '{"score": <0到1的小数>, "reason": "<一句话中文理由>"}，不要输出其他内容。'
)


def make_client() -> OpenAI:
    """构造独立 judge 用的 OpenAI client（复用 config 的模型与 key 配置）。"""
    return OpenAI(api_key=os.environ.get(API_KEY_ENV), base_url=BASE_URL)


def judge(client: OpenAI, task_output: str, rubric: str) -> dict:
    """用 LLM 判分，返回 {"score": float, "reason": str}。

    task_output：任务的最终产出文本；rubric：META.json 里的评分标准。
    """
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": f"评分标准：\n{rubric}\n\n任务产出：\n{task_output}"},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    return _parse_score(raw)


def _parse_score(raw: str) -> dict:
    """从 LLM 原始输出解析 score + reason，容错（模型可能包 ```json 或多余文字）。"""
    try:
        return _coerce(json.loads(raw))
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    s = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
    if s:
        try:
            return {"score": max(0.0, min(1.0, float(s.group(1)))), "reason": raw[:200]}
        except ValueError:
            pass
    return {"score": 0.0, "reason": f"judge 解析失败: {raw[:200]}"}


def _coerce(data: dict) -> dict:
    """把解析出的 JSON 收敛成 {"score": float, "reason": str}，缺字段给默认值。"""
    score = data.get("score", 0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return {"score": score, "reason": str(data.get("reason", ""))}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_judge.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add judge.py tests/test_judge.py
git commit -m "feat: 新增 LLM-as-judge 判分（judge.py，可注入 client）"
```

---

### Task 3: bench 纯函数（manifest/META/graded/summary/compare）

**Files:**
- Create: `bench/__init__.py`
- Create: `bench/scoring.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_scoring.py`:

```python
"""bench/scoring.py 回归测试：manifest/META 加载、graded 分数解析、summary 对比。"""

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'bench.scoring'`）

- [ ] **Step 3: 实现 bench/__init__.py + bench/scoring.py**

Create `bench/__init__.py`（空文件）:

```python
```

Create `bench/scoring.py`:

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add bench/__init__.py bench/scoring.py tests/test_scoring.py
git commit -m "feat: 新增 bench/scoring 纯函数（manifest/META/graded/summary/compare）"
```

---

### Task 4: 评测集数据（manifest.json + 3 个 META.json）

**Files:**
- Create: `bench/tasks/manifest.json`
- Create: `bench/tasks/fix_fizzbuzz/META.json`
- Create: `bench/tasks/find_secret/META.json`
- Create: `bench/tasks/implement_safe_divide/META.json`

- [ ] **Step 1: 创建评测集 manifest**

Create `bench/tasks/manifest.json`:

```json
{
  "version": 1,
  "regression_threshold": 0.1
}
```

- [ ] **Step 2: 创建三个任务的 META.json**

Create `bench/tasks/fix_fizzbuzz/META.json`:

```json
{
  "version": 1,
  "category": "debug",
  "difficulty": "easy",
  "judge": "deterministic",
  "rubric": ""
}
```

Create `bench/tasks/find_secret/META.json`:

```json
{
  "version": 1,
  "category": "search",
  "difficulty": "easy",
  "judge": "deterministic",
  "rubric": ""
}
```

Create `bench/tasks/implement_safe_divide/META.json`:

```json
{
  "version": 1,
  "category": "implement",
  "difficulty": "easy",
  "judge": "deterministic",
  "rubric": ""
}
```

- [ ] **Step 3: 验证加载正确**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0, '.'); from pathlib import Path; from bench.scoring import load_manifest, load_meta; m=load_manifest(Path('bench/tasks/manifest.json')); print(m); print(load_meta(Path('bench/tasks/fix_fizzbuzz')))"`
Expected: 输出 `{'version': 1, 'regression_threshold': 0.1}` 和 fix_fizzbuzz 的 META 内容

- [ ] **Step 4: 提交**

```bash
git add bench/tasks/manifest.json bench/tasks/*/META.json
git commit -m "feat: 新增评测集 manifest 与任务 META 元数据"
```

---

### Task 5: run_bench.py 集成（TraceRecorder + 分层判分 + summary + --compare）

**Files:**
- Modify: `bench/run_bench.py`（整体替换）

- [ ] **Step 1: 替换 run_bench.py**

用以下完整内容覆盖 `bench/run_bench.py`:

```python
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
    """发现任务目录：含 PROMPT.md 与 verify.py 的才算完整任务。

    注意：llm-judge 任务没有 verify.py，当前版本暂不支持自动发现（见 score_task 注释）。
    """
    tasks = sorted(
        d for d in TASKS_DIR.iterdir()
        if d.is_dir() and (d / "PROMPT.md").exists() and (d / "verify.py").exists()
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

    proc = subprocess.run(
        [sys.executable, str(task_dir / "verify.py")],
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
        ui.consume(recorder.wrap(session.chat(prompt)))
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
```

- [ ] **Step 2: 验证 run_bench 可导入、参数解析正确（不发请求）**

Run: `.venv/bin/python -c "import ast; ast.parse(open('bench/run_bench.py').read()); print('语法 OK')"`
Expected: `语法 OK`

- [ ] **Step 3: 跑全量测试确认无回归**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS（原有 164 + 新增 20 = 184 passed）

- [ ] **Step 4: 提交**

```bash
git add bench/run_bench.py
git commit -m "feat: bench 集成 TraceRecorder、分层判分、summary 与 --compare 回归对比"
```

---

## Self-Review 记录

- Spec 覆盖：评测集版本化（Task 4 manifest/META）、评分三层（Task 2 judge + Task 3 parse_verify_score + Task 5 score_task）、回归追踪（Task 3 compare/build_summary + Task 5 --compare）、TraceRecorder（Task 1 + Task 5 接线）、测试策略（各 task 的 tests）。全部覆盖。
- 已知 MVP 限制（已文档化，非缺陷）：llm-judge 任务无 verify.py，`discover_tasks` 暂不自动发现，需后续松弛；graded 的 pass 判据为 `returncode==0 or score>=0.5`。
- 类型一致性：`score_task` 恒返回 `score` 字段（float），`main` 里 `record['score']:.2f` 依赖此约定；`build_summary`/`compare_summaries` 字段名（task/score/passed/tokens/elapsed_s）与 `run_task`/`main` 一致。
