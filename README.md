# mini-agent

一个 code review CLI：审查一个 diff 范围（PR、分支、工作区改动），输出结构化 findings
（severity / 文件:行号 / 问题描述 / 修复建议），带 CI 门禁语义。

```bash
mini-agent review main...HEAD    # 审查分支改动
mini-agent review                # 审查工作区+暂存（默认）
mini-agent review --format json  # 机器消费（CI/评测）
# 有 high findings 时退出码为 1——任何 CI 都能直接当门禁用，不绑定特定平台
```

不带子命令启动则进入通用 coding agent REPL（辅助形态）。

## 凭什么不同

- **检出率可度量**：自带 benchmark 体系（`bench/`），review 质量用注入 bug 任务量化
  （检出率/误报率），不靠「看起来能 review」
- **代码库感知**：tree-sitter 符号索引（Python/JS/TS/Go/Rust/Java），review 时能定位
  变更的影响面，而不是孤立看 diff
- **规范可沉淀**：项目的 AGENTS.md 与 `skills/` 约定自动注入 system prompt，作为审查依据
- **dogfood**：本仓库的提交由它自己 review——首个版本就抓到过一个真 bug（见提交 565d621）

## 快速开始

要求 Python 3.13+。

```bash
pipx install .
# 配置 API key：默认档案 kimi-code 需要 KIMI_CODE_API_KEY 环境变量
# （也可在项目目录放 .env；更多模型档案见 mini_agent/models.default.json）
cd 你的项目 && mini-agent review HEAD
```

从源码运行：`pip install -r requirements.txt` 后 `.venv/bin/python -m mini_agent review HEAD`。

## 工作原理

review 是一个确定性管道，不是一句 prompt：

1. **diff 提取**（`tools/gitdiff.py`）：纯 Python、只读——文件级概览（状态/增删行）常驻，
   patch 详情按预算分配，超预算文件降级为概览条目
2. **审查**：只读会话（read_file / search_symbols / 只读 bash）执行双轴审查——
   正确性（主）+ 规范（对照 AGENTS.md/skills）；AGENTS.md、repo map、skills 索引
   自动注入 system prompt
3. **输出**：约定格式的 findings 由程序解析成结构（解析不动的由 raw 原文兜底），
   high findings → exit code 1

底座是一个完整的 coding agent 内核：三级上下文压缩、工具分档发现、类型化子 agent、
权限裁决、benchmark 评测（见「项目结构」）。

## CI 集成

review 是宿主无关的批处理命令，任何 CI 都能接入（exit code 门禁 / `--format json` 机器消费）。
本仓库自带的 GitHub Actions 示例见 `.github/workflows/review.yml`：PR 上自动 review
并把 findings 评论到 PR——它同时是本项目的 dogfood 闭环（用 PR 自己的代码 review 自己）。
需要配置 `KIMI_CODE_API_KEY` secret。

## 开发

```bash
.venv/bin/python -m pytest -q        # 全部测试（零网络，OpenAI client 已打桩）
.venv/bin/ruff check .               # lint
.venv/bin/python bench/run_bench.py  # benchmark（访问真实 API，写入 bench/results/）
```

测试约定见 `tests/conftest.py`（socket 被替换为哨兵，任何真实网络请求立即失败）。

## 安全模型

- review 会话只读：不能写文件；bash 只有只读白名单命令直通，其余在非交互环境默认拒绝
- 通用 REPL：文件工具围栏在项目根内，项目外路径需逐次确认；`run_bash` 由
  `permissions.json` 裁决（`deny > allow > ask`）；子 agent 权限随类型收窄

> ⚠️ 这个项目会让 LLM 在你机器上执行命令。请理解 `permissions.json` 的规则后再放权。

## 项目结构

```
mini_agent/
├── main.py           入口（review 子命令分发 + tty/管道单一主循环）
├── review.py         code review 管道（确定性输入→双轴审查→结构化 findings→门禁）
├── skills.py         skills 机制（索引常驻注入 + read_file 惰性加载）
├── config.py         运行时配置（模型档案、子 agent 类型、阈值）
├── repo_map.py       tree-sitter 代码库感知
├── kernel/           事件生产者（agent / streaming / compact / events / bridge）
├── tools/            工具实现与注册表（builtin / registry / gitdiff）
├── commands/         斜杠命令实现与注册表（builtin / registry）
├── ui/               界面层（renderer / input / segments）
└── eval/             评测支撑（judge / trace）
bench/                benchmark 任务包与驱动
tests/                行为契约测试
AGENTS.md             面向 AI 协作者的仓库约定
```

## 许可证

[MIT](LICENSE)
