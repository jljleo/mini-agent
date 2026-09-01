# mini-agent 评测 + 可观测体系设计

> 日期：2026-08-31
> 状态：待确认

## 目标

把 mini-agent 从"能跑"升级到"可度量、可复盘"，补齐 agent 工程市场最稀缺的两项能力——**评测（Evals）** 与 **可观测性（Observability）**。这既是 mini-agent 自身的地基，也是后续多 agent 项目共享的基础设施（两个项目复用同一套评测与追踪）。

## 已确认的约束

- 内核边界不变：`agent.py` / `streaming.py` / `compact.py` 继续只产出 `events.py` 事件，不 `import` 任何评测/观测模块。评测与观测都作为**事件消费者**接入（同 `ui.consume`、bench 消费者）。
- 尽量零内核改动：能靠"消费者读事件流"解决的问题，就不动内核。
- 真实 API 只出现在 bench 运行时；测试仍走 socket 哨兵零网络。
- 不引入重量级平台（LangSmith/Langfuse/Braintrust 一律不装），全部自己实现——这是"系统思维"的展示点，也是本项目区别于"会点框架"的分水岭。

## 现状盘点

已有（`bench/run_bench.py`，125 行）：

- 任务目录 `bench/tasks/<name>/`：`workspace/` + `PROMPT.md` + `verify.py`
- 沙箱隔离：任务工作区复制进 `tempfile` 临时目录，`tools.PROJECT_ROOT` 指向沙箱
- 判分：`verify.py` 在沙箱内运行，`exit 0 = pass`
- 结果落盘：`bench/results/<name>-<timestamp>.json`，含 pass/fail、token 汇总、messages 轨迹

缺口（本设计要补的）：

1. **评测集无版本化**：任务只是一堆目录，没有 manifest、没有版本、没有分类。
2. **判分只有二元 pass/fail**：没有分档评分、没有开放题（无确定性判据）的 LLM 判分。
3. **无回归追踪**：每次跑完的结果是孤立的 JSON，无法对比"这版比上版差在哪"。
4. **无可观测性**：只有 token 汇总，没有 trace ID、没有结构化日志、没有逐步的"哪个工具调用花了多久、消耗多少 token、返回了什么"。

## 总体架构

新增两个独立模块，均作为事件消费者挂在现有事件流上：

```text
                    events.py 事件流
                    │
        ┌───────────┼───────────────┬─────────────┐
        ▼           ▼               ▼             ▼
   ui.consume   bench 消费者    TraceRecorder   Judge
   （终端渲染）  （沙箱+判分）   （可观测，新增） （评测判分，新增）
```

- `trace.py`：`TraceRecorder`，可观测性核心。消费事件流，产出结构化轨迹（JSONL）。
- `judge.py`：LLM-as-judge，评测判分。对无确定性判据的任务，用 LLM + rubric 打分。
- `bench/run_bench.py`：扩展评分分层、回归对比、manifest 加载。

## 一、可观测性（TraceRecorder）

### 设计要点

`TraceRecorder` 是纯事件消费者，**零内核改动**：

- 构造函数接收 `task_id`（如 bench 任务名）作为 trace 根标识。
- 每遇到 `StreamStart` 生成一个 `turn_id`（一轮 = StreamStart → TurnEnd），遇 `TurnEnd` 关闭。
- 每个事件产出一条结构化记录（JSON 行），携带 `task_id` + `turn_id` + 时间戳。

### 记录字段

| 字段 | 来源 | 用途 |
|------|------|------|
| `task_id` | 构造入参 | 关联到具体任务 |
| `turn_id` | `StreamStart` 时自增 | 一轮一个 span |
| `ts` | 时钟 | 时间线 |
| `event` | 事件类型名 | 可过滤 |
| `detail` | 事件字段 | tool 名/args/result 摘要、text 长度等 |
| `tokens` | `Usage` 事件 | 逐步 token 消耗 |

对 `ToolCallStart` / `ToolCallResult` 特别记录 **name、arguments、result 摘要、相对上一事件的耗时**——这是面试里"复盘某一步工具挂了"的原始材料。

### 产出

- bench 模式：轨迹写入 `bench/results/<task>-<ts>.trace.jsonl`，与判分结果并列。
- 交互模式（可选）：`--trace` 开关，落盘到会话旁，不污染 stdout。

## 二、评测体系（Evals）

### 1. 评测集版本化

- 新增 `bench/tasks/manifest.json`：声明任务清单，含版本号、分类（如 `debug` / `implement` / `search`）、判分类型。
- 每任务新增 `META.json`：`version`、`category`、`difficulty`、`judge`（`deterministic` / `graded` / `llm-judge`）、`rubric`（llm-judge 任务的评分标准文本）。

### 2. 评分三层

| 层 | 判据 | 适用 | 状态 |
|----|------|------|------|
| deterministic | `verify.py` exit code | 有确定性答案的任务 | ✅ 已有 |
| graded | `verify.py` 输出 0~1 分数 | 有部分正确概念 | 🆕 |
| llm-judge | `judge.py` LLM + rubric | 开放题（无确定性判据） | 🆕 |

`verify.py` 升级：除了 `exit 0`，可 `print` 一个 0~1 分数（`score=0.8`），runner 解析作为 graded 分。不输出分数则回退二进制 pass/fail。

### 3. LLM-as-judge（judge.py）

- 输入：任务的最终产出（沙箱内结果文件 / assistant 终稿）+ `META.json` 的 rubric。
- 输出：0~1 分数 + 一段中文理由。
- 独立于 agent 会话，复用 `config.py` 的 API 配置发起一次 judge 请求，不污染主会话上下文。
- 判分结果与轨迹一并落盘，便于人工抽查 judge 质量（judge 的 judge）。

### 4. 回归追踪

- 每次全量跑完，汇总成 `bench/results/summary.json`：通过率、均 token、均耗时、各任务分数。
- 保留上一版 summary 为 baseline，`run_bench.py --compare` 输出 delta（哪些任务分数下降、token 上涨）。
- 回归告警阈值写在 `manifest.json`（如"任一分层任务分数较 baseline 下降 >0.1 即告警"）。

## 数据流

```text
run_bench.py
  ├─ 加载 manifest.json（版本/分类/阈值）
  ├─ 对每任务：
  │    ├─ 沙箱复制 + PROJECT_ROOT 替换（现有）
  │    ├─ TraceRecorder(task_id) 挂上事件流  ← 可观测
  │    ├─ ChatSession.chat() 产出事件
  │    ├─ 事件 → ui.consume（可见）+ TraceRecorder（落轨迹）
  │    ├─ 判分：deterministic / graded（verify.py）或 llm-judge（judge.py）
  │    └─ 结果 + 轨迹落盘
  ├─ 汇总 summary.json
  └─ --compare 对比 baseline 输出 delta
```

## 测试策略

- `tests/test_trace.py`：喂一个 mock 事件流，断言 TraceRecorder 产出的 JSONL 字段完整、turn_id 正确分段、tool 调用带耗时与 token。
- `tests/test_judge.py`：mock LLM client，断言 judge 解析出分数与理由、rubric 正确传入。
- `tests/test_bench_meta.py`：manifest/META 加载、缺失字段报错、graded 分数解析（含"无分数回退二进制"）。
- 全部零网络：socket 哨兵 + OpenAI client 打桩（沿用 conftest 现有约定）。

## 非目标

- 不装 LangSmith / Langfuse / Braintrust / Arize 等外部评测平台。
- 不做 prompt 版本管理、A/B 测试、在线评测。
- 不做 trace 的可视化 UI（第一版只出 JSONL + 控制台摘要）。
- 不做 async 化、MCP、多 agent——这些归后续项目。

## 与多 agent 项目的关系（预告）

- 多 agent 采用业界主流的 **subagent 模型**（主 agent + `task` 工具 spawn 子 agent，非重编排层）。`TraceRecorder` 的 `trace_id` 设计为可跨 agent 传递的普通字符串：主 agent spawn 子 agent 时把 `trace_id` 传入，各自轨迹能按同一 trace 串联。本设计先立好"trace 是字符串、可传递"的约定，不做实现。
- 评测体系直接服务多 agent 项目的测试闭环：developer/tester 的"测试失败→修复→回归"就是用 graded/llm-judge 来度量。
