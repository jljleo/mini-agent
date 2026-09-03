# AGENTS.md

## 项目形态

- 扁平的 Python 3.13 CLI agent；有最小 `requirements.txt`（openai / python-dotenv / prompt_toolkit / rich / textual），没有构建步骤、lint 配置或代码生成。使用仓库内虚拟环境：`.venv/bin/python`；新环境先 `.venv/bin/pip install -r requirements.txt`。
- 入口是 `main.py`；根据 `sys.stdin.isatty()` 自动选择 TTY 模式（`tui.py` 的 Textual 全屏前端）或管道模式（`_pipe_loop`）。
- 运行时配置在 `config.py`，会加载 `.env`；真实运行需要 `MOONSHOT_API_KEY`。不要读取或提交 `.env`。
- 内核/UI 边界很重要：`agent.py`、`streaming.py`、`compact.py` 必须保持为事件生产者，不能 `import ui`。通过产出/消费 `events.py` 事件来渲染或上报（`ui.consume`、bench 消费者）。

## 命令

- 运行应用：`.venv/bin/python main.py`
- 运行全部测试：`.venv/bin/python -m pytest -q`
- 运行单个测试文件：`.venv/bin/python -m pytest tests/test_compact.py -q`
- 运行单个测试：`.venv/bin/python -m pytest tests/test_compact.py::test_name -q`
- 运行 benchmark：`.venv/bin/python bench/run_bench.py [task_name]`；会访问真实 API，在每个任务独立的临时沙箱里自动批准，并写入 `bench/results/*.json`。
- 仓库没有配置 lint/typecheck/format 命令；不要臆造必须的命令顺序。

## 测试规则

- `pytest.ini` 只设置 `testpaths = tests`。
- `tests/conftest.py` 会把仓库根目录插入 `sys.path`，重置 `compact._chars_per_token`，并把 `socket.socket` 替换成哨兵类：任何真实网络请求都会立即失败。OpenAI client 调用必须打桩；测试绝不能访问网络。
- 使用 `session` fixture 获得隔离的 `ChatSession`：假 API key + `tmp_path` 会话文件。
- 优先写行为契约回归测试，而不是实现细节断言；文件系统和全局状态隔离用 `tmp_path`/`monkeypatch`。

## 仓库特有约定与坑

- 工具只在 `tools.py` 里用 `@tool` 注册；工具 schema 和实现共用这一个事实来源。常驻工具只由 `tool_registry.RESIDENT_TOOL_NAMES` 控制；新工具默认通过 `search_tools` 可发现，除非显式加入常驻名单。
- 斜杠命令在 `commands.py` 里用 `@command` 注册；`main.py` 导入 `tools` 和 `commands` 是为了触发注册副作用。
- 上下文压缩只做发送时投影：`compact.py` 变换的是消息副本，绝不能改存储历史。必须保留 assistant `tool_calls` 与 tool 消息配对；截断不能制造孤儿 tool 消息。
- 如果流式响应 `finish_reason == "length"` 且带 tool calls，要整批拒绝执行并补上明确的 tool 结果；半截 arguments 不安全，孤儿 tool calls 会导致 API 400。
- 交互循环刻意没有硬性最大轮次限制。保险丝是 `config.py` 的 `MAX_SAME_TOOL_CALLS`；无人值守场景的轮次限制应放在调用侧（如 bench、子 agent 的 `max_turns`），不要污染核心循环。
- 子 agent（`spawn_subagent`）：类型化派生（`config.SUBAGENT_TYPES`——researcher 只读 / coder 可写可跑），进程内新 `ChatSession(tools=类型工具表, depth+1)`，上下文隔离只回结论；子 agent 直接操作真实项目，安全由工具表 + command_policy + 用户审批保证（沙箱机制已移除）。子 agent 的 bash 命令：researcher 一律硬拒、coder 即使命中 allow 也降级为 ask 冒泡给人工审批（带 `[子 agent]` 前缀）；越界路径硬拒绝（不评审不冒泡）。防套娃双保险：类型工具表不含 spawn_subagent + `MAX_SUBAGENT_DEPTH=1`。
- 命令权限随类型声明（`SUBAGENT_TYPES[*].command_policy`）：researcher=read_only（白名单直通，非白名单确定性硬拒，零 LLM 成本）；coder=human（allow 也降级为 ask，与 ambiguous 一起冒泡给人工审批，带 `[子 agent]` 前缀）。限制的唯一可信来源是 config，不是模型生成的参数。
- 防乱派生：`spawn_subagent` 工具描述写死派生纪律（先规划边界、任务自包含、别乱派）；子 agent 运行中被拒的命令/访问计入 `_subagent_context`，达到 `SUBAGENT_DENIAL_LIMIT`（默认 3）即 `control.abort()` 熔断本轮，拒绝次数附在结论里回传主 agent 自我修正。
- 文件工具是窄接口，围栏限制在 `PROJECT_ROOT`；项目外路径需要确认。`run_bash` 由 `permissions.json` 裁决：`deny > allow > ask`；如果命令包含项目外路径，即使命中 allow 也会降级为 ask。不要用 bash 绕过文件围栏访问项目外路径。
- `$web_search` 在 `config.py` 中被刻意禁用，因为 kimi-k3 当前处理内置工具结果会失败；需要联网时用 `run_bash` + `curl`，并先告诉用户要访问的 URL。
- UI 输出统一走语义化 helper；TTY 使用 `tui.py`（Textual 全屏：输出 viewport + 底部固定 dock，离散事件渲染映射在 `tui_render.py` 纯函数里），管道模式继续用 `ui.py`。动态/模型文本必须用 `Text`/`markup=False`，避免 `[brackets]` 被当成 Rich markup 解析。
- `.session.json`、`.chat_history`、`session_todos.json`、`bench/results/` 都是运行时产物，已 gitignore。
