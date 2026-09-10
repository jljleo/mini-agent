# mini-agent

一个扁平的 Python CLI coding agent，内核加评测设施约 4,700 行。
实现了现代 coding agent 的核心机制：上下文压缩、工具分档发现、类型化子 agent、
权限裁决、代码库感知、benchmark 评测。

## 特性

- **三级上下文压缩**：L3 工具结果瘦身 → L2 摘要 → L1 反向装箱硬切，发送时投影、绝不动存储历史
- **工具分档**：常驻工具直接声明，其余经 `search_tools` 检索后动态注入（MCP 思想的进程内简化版）
- **类型化子 agent**：`spawn_subagent`（researcher 只读 / coder 可写）+ `spawn_researchers` 并行只读调研，
  上下文隔离只回结论，嵌套限深 + 命令策略 + 拒绝熔断
- **代码库感知**：tree-sitter 多语言符号索引（Python/JS/TS/Go/Rust/Java），
  repo map 注入 system prompt + `search_symbols` 工具
- **权限裁决**：`run_bash` 由 `permissions.json` 规则表裁决（`deny > allow > ask`），文件工具围栏在项目根内
- **benchmark 体系**：`bench/` 任务包 + 三层评分（deterministic / graded / llm-judge）+ 回归对比

## 快速开始

要求 Python 3.13+。

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入 API key：默认档案 kimi-code 用 KIMI_CODE_API_KEY；
                       # 或设 MINI_AGENT_MODEL=kimi，用 MOONSHOT_API_KEY
.venv/bin/python main.py
```

- 终端直接运行 → 回合制 REPL（流式 Markdown 渲染）
- 管道/重定向运行 → 纯文本输出，如 `echo "你好" | .venv/bin/python main.py`

## 开发

```bash
.venv/bin/pip install pytest ruff
.venv/bin/python -m pytest -q        # 全部测试（零网络，OpenAI client 已打桩）
.venv/bin/ruff check .               # lint
.venv/bin/python bench/run_bench.py  # benchmark（访问真实 API，写入 bench/results/）
```

测试约定见 `tests/conftest.py`（socket 被替换为哨兵，任何真实网络请求立即失败）。

## 安全模型

- 文件工具（read/write/edit）围栏限制在项目根内，项目外路径需逐次确认
- `run_bash` 由 `permissions.json` 裁决：`deny`（硬拒）> `allow`（直通）> `ask`（人工确认）；
  命令含项目外路径时即使命中 allow 也降级为 ask
- 子 agent 直接操作真实项目，隔离由类型工具表 + 命令策略 + 用户审批保证；越界路径硬拒绝

> ⚠️ 这个项目会让 LLM 在你机器上执行命令。请理解 `permissions.json` 的规则后再放权，
> 建议先在测试仓库里体验。

## 项目结构

```
main.py           入口（tty/管道单一主循环）
agent.py          会话内核（事件生产者）
events.py         事件定义（内核/UI 解耦）
bridge.py         线程桥（打断/优雅收尾）
streaming.py      流式组装
compact.py        三级上下文压缩
repo_map.py       tree-sitter 代码库感知
tools.py          工具实现（@tool 注册）
tool_registry.py  工具分档与 schema
commands.py       斜杠命令
config.py         运行时配置（模型档案、子 agent 类型、阈值）
bench/            benchmark 任务包与驱动
trace.py          逐事件轨迹记录（JSONL）
judge.py          LLM-as-judge 评分
tests/            行为契约测试
AGENTS.md         面向 AI 协作者的仓库约定
```

## 许可证

[MIT](LICENSE)
