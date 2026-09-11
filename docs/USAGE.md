# led 使用文档（完整版）

> 面向使用者（不只是读者）。覆盖安装、配置、两种使用形态（review 批处理 / 交互
> REPL）、权限与安全模型、评测体系、开发流程与故障排查。
> 本文档以 2026-09 代码状态为准（330 测试通过）。

---

## 1. 这是什么

一个 ~4900 行的 Python 3.13 coding agent 内核 + 旗舰应用 **code review agent**。
两条使用形态：

| 形态 | 入口 | 定位 |
|---|---|---|
| `review` 子命令 | `led review ...` | **主形态**：非交互批处理，CI 可消费，exit code 门禁 |
| 交互 REPL | `led`（无子命令） | 辅助形态：人机对话式编码助手 |

review 的机器接口（CI / 本地复现 / 评测）共用同一个调用原语，宿主无关。
**安全模型**：文件工具是窄接口（围栏限制在 PROJECT_ROOT），`run_bash` 走
`permissions.json` 规则裁决 + 人工审批兜底；子 agent 命令权限随类型声明。

---

## 2. 安装

### 2.1 pipx（推荐，任意目录可用）

```bash
pipx install .
led review main...HEAD        # 在任意项目目录下使用
```

### 2.2 源码开发（本仓库）

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m mini_agent      # 交互 REPL
.venv/bin/python -m mini_agent review main...HEAD
```

### 2.3 配置 API key

默认模型档案 `kimi-code`（模型 `k3`，`https://api.kimi.com/coding/v1`）需要：

```bash
export KIMI_CODE_API_KEY=sk-xxx
```

或者把 key 写进**你正在操作的项目目录**下的 `.env` 文件：

```
KIMI_CODE_API_KEY=sk-your-key-here
```

> ⚠ `.env` 是本仓库的 gitignored 运行时产物，不要提交。

---

## 3. 模型档案

模型档案定义在 `mini_agent/models.default.json`（包内随 wheel 分发）；你可以在
项目目录放 `models.json` 覆盖（合并优先）。

| 档案名 | 模型 | 端点 | API key 环境变量 | 上下文 |
|---|---|---|---|---|
| `kimi-code`（默认） | `k3` | api.kimi.com | `KIMI_CODE_API_KEY` | 1M |
| `kimi-code-256k` | `k3-256k` | api.kimi.com | `KIMI_CODE_API_KEY` | 256K |
| `kimi-code-k2.7` | `kimi-for-coding` | api.kimi.com | `KIMI_CODE_API_KEY` | 256K |
| `kimi-code-k2.7-highspeed` | `kimi-for-coding-highspeed` | api.kimi.com | `KIMI_CODE_API_KEY` | 256K |
| `kimi` | `kimi-k3` | api.moonshot.cn | `MOONSHOT_API_KEY` | 1M |
| `kimi-k2.7-code` 等 | 同名 | api.moonshot.cn | `MOONSHOT_API_KEY` | 256K |

- 默认档案由环境变量 `MINI_AGENT_MODEL` 决定（缺省 `kimi-code`）
- 交互 REPL 里用 `/model` 查看列表或切换（`/model <profile>`）
- 评测用 `--model=` 覆盖当前档案的模型名（见 §8）

> ⚠ 两个端点同名 k3 是不同环境（api.kimi.com 的 `k3` vs api.moonshot.cn 的
> `kimi-k3`），key 不通用。

---

## 4. `review` 子命令（主形态，CI 可用）

在**任何 git 仓库**内运行：

```bash
led review                     # 工作区 + 暂存 vs HEAD
led review main...HEAD         # 分支范围（默认建议形态）
led review a12b3c4             # 单个 commit
led review a12b3c4..b56d7e8    # commit 区间
```

输出：结构化 findings（每条含 severity / 文件:行号 / 问题描述 / 修复建议），
末尾附总结与门禁行。

### 4.1 参数

| 参数 | 说明 |
|---|---|
| `spec` | revspec（`main...HEAD` / `a..b` / 单 commit）；缺省 = 工作区改动 |
| `--format json` | stdout 只出 JSON（`counts` / `findings` / `raw` 原文兜底），机器消费 |
| `--no-fail` | 有 high findings 也返回 0（关闭门禁语义） |
| `--max-findings N` | 最多输出 N 条 findings（0 = 不限制） |

### 4.2 exit code 门禁语义

- `0`：无 high findings（或 `--no-fail`）
- `1`：有 high findings
- `2`：不是 git 仓库等确定性错误

任何 CI 都能直接消费：

```yaml
- run: |
    led review "origin/main...HEAD" --format json || test $? -eq 1
```

### 4.3 工作原理（review 管道）

确定性输入 → 单通道双轴 review（默认）→ 结构化 findings → 门禁。

1. **diff 提取**：`gitdiff.py` Python 侧确定性只读抽取（概览 / 单文件 patch /
   预算降级，含 untracked），不经模型工具调用
2. **会话只读**：researcher 工具表（read_file / search_symbols / 只读 bash），
   CI 零审批
3. **双轴审查**：正确性轴（主，宁缺毋滥）+ 规范轴（对照项目 AGENTS.md 与
   skills 注入的约定）
4. **轮次保险丝**（无人值守防线）：SOFT 档（8 轮）steering 注入收敛指令；
   HARD 档（12 轮）abort 防爆走

> parallel 模式（两轴独立会话 + Python 确定性合并）是 E11 实验结论：recall 无
> 增益、precision +0.11、成本 +32%——默认单通道，门禁（精度敏感）场景可考虑
> parallel（`bench --review-mode=parallel`；交互/CLI 未暴露该开关，按 E11 结论）。

---

## 5. 交互 REPL（辅助形态）

```bash
led            # 在项目目录启动，进入对话循环
```

- tty 下富渲染（Live 增量重排）；管道下自动降级纯文本直出
- 会话开始时注入项目上下文：AGENTS.md 行为契约 + `repo_map` 代码库地图（tree-sitter
  全语言符号索引，Python/JS/TS/Go/Rust/Java）+ `skills/` 索引
- **无硬性轮次上限**（人在看）；失控防线是 `MAX_SAME_TOOL_CALLS`，与
  `MAX_TIMEOUT`（bash 超时上限 120s）

### 5.1 斜杠命令

| 命令 | 说明 |
|---|---|
| `/clear` | 清空对话历史，开始新会话 |
| `/resume` | 恢复上次保存的会话（`config.SESSION_FILE`，项目目录 `.session.json`） |
| `/model` | 列出模型档案；`/model <profile>` 切换 |
| `/compact` | 主动压缩早期历史（L2 摘要，失败回退硬切） |

### 5.2 可用工具

文件工具（窄接口，项目内免确认）：
- `read_file` — 读文件（文本/图片）
- `write_file` — 写文件（新建/覆盖）
- `edit_file` — 精确替换编辑（容错链 L1 精确→L2 行级宽容→L3 报错带 read_file
  指引；改后 tree-sitter 语法冒烟 + 可配置校验命令回喂，见 §6.4）

查询类：
- `search_symbols` — 仓库符号检索（repo_map 数据层）
- `search_tools` / `search_history` — 工具发现 / 会话历史检索

执行类：
- `run_bash` — 通用命令执行（权限裁决见 §7）

协作/状态：
- `todo_write` / `todo_read` — 任务清单（项目目录 `session_todos.json`）
- `spawn_subagent` — 派生子 agent（见 §6.3）

> 常驻工具由 `tools/registry.py` 的 `RESIDENT_TOOL_NAMES` 控制；其余工具
> 通过 `search_tools` 按需发现注入。

---

## 6. 项目感知与自动注入

主循环启动时注入三类项目上下文（会话保留，compact 不截断）：

1. **AGENTS.md** — 行为契约（编码规范/架构边界/踩坑清单），随会话保留
2. **repo_map** — tree-sitter 符号地图（PageRank 式排序），告诉 agent「仓库有什么」
3. **skills 索引** — `skills/*/SKILL.md` 的 frontmatter 索引常驻注入，正文按需
   `read_file` 惰性加载

### 6.1 AGENTS.md / CLAUDE.md

项目根目录的 `AGENTS.md` 会被注入 system prompt（修改即刻生效，无需重启）。
内容应写**应该怎么改**（行为契约），不要写一次性指令或环境特定信息。

### 6.2 skills 机制

```text
skills/<name>/SKILL.md
```

- frontmatter（无 YAML 依赖的轻量解析）含名称与描述
- 索引常驻注入，正文按需读取
- agent 可以 `write_file` 沉淀新 skill（/skill-save 命令刻意不存在，写文件就是沉淀）

### 6.3 子 agent（spawn_subagent）

| 类型 | 工具面 | 命令策略 | 用途 |
|---|---|---|---|
| `researcher` | 只读（无写工具） | read-only：白名单命令直通，非白名单**确定性硬拒**（零 LLM 成本） | 调研、并行深挖 |
| `coder` | 可写可跑 | human：命中 allow 也降级为 ask，冒泡人工审批 | 需要写代码/跑命令的任务 |

- 进程内新会话、上下文隔离、只回结论；`MAX_SUBAGENT_DEPTH=1` 防套娃
- 被拒次数达 `SUBAGENT_DENIAL_LIMIT`（默认 3）即熔断本轮，结论回传主 agent 自我修正
- `spawn_researchers` 是并行调研的批量形态（`SUBAGENT_MAX_PARALLEL=3`）

### 6.4 写后验证闭环

`config.POST_WRITE_CHECK_COMMANDS` 配置 `<编辑文件> 后自动校验` 命令：

```python
POST_WRITE_CHECK_COMMANDS = [
    "ruff check {path}",
    "python -m py_compile {path}",
]
```

- `{path}` 替换为改动文件的项目相对路径；首个失败即停
- 失败的 stderr + exit code 带 ⚠ 回喂工具结果——模型下一轮看到自己改坏的东西
  当场自愈（验证信号优于执行结果）
- 默认空列表 = 仅保留 tree-sitter 语法自检（零回归）

---

## 7. 权限与安全模型

### 7.1 文件围栏

- `PROJECT_ROOT` = 当前工作目录的 realpath（macOS /var→/private/var 符号链接已处理）
- 项目内路径：免确认；项目外路径：需要用户确认
- `run_bash` 命令中出现项目外路径 → 即使命中 allow 也降级为 ask
- 不要用 bash 绕过文件围栏访问项目外路径

### 7.2 run_bash 权限裁决

`permissions.json`（项目根目录）规则裁决，优先级 **deny > allow > ask**：

```json
{
  "rules": [
    {"pattern": "rm -rf", "action": "deny", "note": "递归删除永不放行"},
    {"pattern": "^(ls|pwd|cat|grep|find|head|tail|wc|date)(\\s|$)", "action": "allow"},
    {"pattern": "^git (status|log|diff|show|branch)(\\s|$)", "action": "allow"},
    {"pattern": "curl .*wttr\\.in", "action": "allow"}
  ]
}
```

未命中规则 → ask（人工审批）。`deny` 优先于一切。

### 7.3 已知限制

- `$web_search` 刻意禁用（kimi-k3 平台处理内置工具结果会 400，官方未修）；
  需要联网用 `run_bash` + `curl`
- OS 级沙箱不存在（2026-09 移除）——隔离由工具表 + 命令策略 + 人工审批保证
- 会话文件、TODO、bench 结果都是运行时产物，已 gitignore

---

## 8. 评测体系（bench）

评测是项目的差异化资产：review 质量用注入 bug 任务量化（检出率/误报率），
不靠「看起来能 review」。

### 8.1 基准命令

```bash
.venv/bin/python bench/run_bench.py                # 全部任务（真实 API，烧钱）
.venv/bin/python bench/run_bench.py <task>         # 单任务
.venv/bin/python bench/run_bench.py <task> --compare   # 与 baseline 对比回归
```

实验开关：

| 开关 | 取值 | 说明 |
|---|---|---|
| `--group=` | `map` / `nomap` | 代码库感知 A/B（repo map + search_symbols 有无） |
| `--edit-mode=` | `lenient` / `strict` | 编辑容错链 A/B |
| `--review-mode=` | `single` / `parallel` | review 单/双通道（E11） |
| `--model=` | 任意模型名 | 覆盖评测模型 |

结果写入 `bench/results/*.json`（每任务一条，带时间戳与组标记）+ `summary.json`。

### 8.2 任务结构（`bench/tasks/<name>/`）

```text
chat 任务：   workspace/ + PROMPT.md + verify.py（可选） + META.json
review 任务： base/（干净代码） + bug.patch（注入 bug） + META.json（ground truth）
```

review 任务两种来源：
- **local**：`base/` 复制 → git init → apply `bug.patch` → commit（人可审计
  的单文件 bug 视图）
- **remote**：META 声明 `remote.repo + commit`，沙箱浅克隆 @ 修复提交 F 后
  `git revert F` 回注真实 bug——真实代码 + 真实缺陷，作者只标注不发明

判分：确定性（findings 对照 ground truth 的 path+行号区间，±3 容差，
`aliases` 可接受定位，`neutral` 不算误报）/ graded（verify.py 分数）/ llm-judge。

### 8.3 评测纪律（MANDATE）

**任何真实 API 评测**（无论结果，含负结果）必须追加一节到
`bench/EXPERIMENTS.md`（头部有强制模板）并随代码提交。零成本机制/单测不算评测。

新任务提交前过一遍 EXPERIMENTS.md 头部的**任务设计检查清单**（zones 间距、
alias 收录契约声明处、OSS 选修 bug 型提交、tokens=0 排除等——每一条都是一个
真实事故换来的）。

### 8.4 当前库（2026-09）

35 个任务（26 原有 + 9 新增），四语言（Python/Go/Rust/JS）：合成二次推理族 /
multi-bug+诱饵 / 跨文件协议错位 / 6 个 OSS revert 注入。评测结论见
EXPERIMENTS.md E11~E12.9（k3 全检出、OSS n=3 零误报、precision 防线 1.0）。

---

## 9. 开发与调试

```bash
.venv/bin/python -m pytest -q          # 全部测试（330）
.venv/bin/python -m pytest tests/test_compact.py -q
.venv/bin/python -m pytest tests/test_compact.py::test_name -q
.venv/bin/ruff check .                 # lint
pip install pre-commit && pre-commit install   # 可选钩子
```

- 测试环境零网络：`tests/conftest.py` 把 `socket.socket` 替换成哨兵——OpenAI
  client 必须打桩，测试绝不访问网络
- 仓库无 formatter / typecheck 配置；`pyproject.toml` 的 ruff 规则集
  E/F/I/UP/B，E501 不查（中文字符串多）

### 9.1 调试 review 管道

```bash
.venv/bin/python -m mini_agent review HEAD --format json   # 结构化输出
MINI_AGENT_MODEL=kimi-code-256k .venv/bin/python -m mini_agent review HEAD  # 换档案
```

### 9.2 常见坑

| 现象 | 原因 / 解法 |
|---|---|
| CI 里 review job 没跑 | fork PR 跳过（`head.repo.full_name` 守卫）——需配置 `KIMI_CODE_API_KEY` secret，且 fork PR 无 secrets 本就可跳过 |
| `bench` 单任务 0 tokens 失败 | 基础设施瞬时故障（网络/API 抖动），补跑一次；多样本统计排除 tokens=0 |
| 评测结论漂移 | 单样本不可信——合成任务看 alias 覆盖，OSS 看 n≥3（E12 系列经验） |
| edit_file 报「not found」 | 文件已变更，先 `read_file` 看当前内容（L3 指引会附文件头部预览） |
| 大量中文注释导致 ruff E501 | 已 ignore；真要格式化另配 ruff-format |

---

## 10. 架构速览（改代码前必读）

```
mini_agent/
├── main.py        入口：review 子命令分发 / 无子命令进交互主循环
├── kernel/        agent 循环、流式、上下文压缩（事件生产者，不 import ui）
│   └── events.py  TurnControl / StreamStart / TextDelta / Warn 等事件
├── tools/         @tool 注册（builtin.py）、registry（常驻名单）、gitdiff 只读抽取
├── commands/      @command 注册（斜杠命令）
├── ui/            renderer（StreamRenderer：tty Live / 管道纯文本）、input
├── eval/          judge（llm-judge）/ trace（JSONL 轨迹）
├── repo_map.py    tree-sitter 全语言代码库地图
├── skills.py      skills 索引
├── config.py      运行时配置（模型档案、权限常量、SUBAGENT_TYPES…）
└── review.py      review 管道（双轴/parallel、保险丝、门禁、--max-findings）
```

**内核/UI 边界**：`kernel/` 必须是事件生产者，不能 import `ui/`；通过产出/消费
`kernel/events.py` 来渲染或上报。

---

## 11. 常见任务速查

| 想做什么 | 怎么做 |
|---|---|
| 审查当前改动 | `led review` |
| 审查一个分支 | `led review main...HEAD` |
| CI 门禁 | `led review "origin/main...HEAD"`（high findings → exit 1） |
| 机器消费 | `led review ... --format json` |
| 只读调研子任务 | 交互里让主 agent 用 `spawn_subagent` 派 researcher |
| 允许 agent 写代码 | 交互里正常授权（file 工具项目内免确认）；coder 子 agent 的命令需审批 |
| 沉淀一条约定 | `skills/<name>/SKILL.md`（index 自动注入） |
| 量化 review 质量 | `bench/run_bench.py <task>`，结果留档 EXPERIMENTS.md |
| 换模型 | `/model`（交互）或 `MINI_AGENT_MODEL` 环境变量 |
---

## 12. 零操作接入：GitHub Action（前置章节的补充）

用户不需要源码、不需要手动调用：把 led 作为 GitHub Action 接进自己的仓库，
PR 一开就自动 review 并评论。

### 12.1 用户接入（3 行 + 1 个 secret）

```yaml
# 用户仓库 .github/workflows/review.yml
name: led review
on: pull_request
permissions:
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }      # review 需要 base...HEAD 完整祖先
      - uses: jljleo/mini-agent@v1
        with:
          api_key: ${{ secrets.KIMI_CODE_API_KEY }}   # BYOK
```

前置（用户侧一次性）：在仓库 Settings → Secrets → Actions 添加
`KIMI_CODE_API_KEY`（用户自己的 key）。

### 12.2 Action 参数

| input | 缺省 | 说明 |
|---|---|---|
| `api_key` | （必填） | KIMI_CODE_API_KEY，secrets 传入 |
| `review_spec` | `origin/<base>...HEAD` | 审查范围（git revspec） |
| `fail_on_high` | `false` | `true` 时 high findings 让 job 失败（门禁） |
| `install_from` | `led-review` | 安装来源（PyPI 包名）；项目自身 dogfood 传 `.` |

### 12.3 行为

- PR 一开自动跑指定范围的 review，findings 以 PR 评论呈现；force-push 后
  重跑（靠用户 workflow 的 concurrency 控制）
- 默认信息性评论（不阻塞合并）；precision 数据够格后再开门禁
- fork PR 自动跳过（secrets 对 fork 不可用，也防外部 PR 烧用户的 key）
- 与 CLI 完全同管线（同样的 diff 提取 / 双轴 / 门禁语义），零额外复杂度

### 12.4 项目自身的 dogfood

本仓库 `.github/workflows/review.yml` 即 `uses: ./` 本地 action +
`install_from: "."`——每个 PR 用 PR 自己的代码 review 自己（自举），
action 封装因此被每个 PR 实测。
