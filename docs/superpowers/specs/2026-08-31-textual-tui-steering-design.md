# Textual TUI 追加消息体验重构设计

> 日期：2026-08-31
> 状态：设计已确认，待实施计划

## 目标

当前 TTY 模式用 `prompt_toolkit + patch_stdout` 维持常驻输入框，模型输出会把输入区位置推着走，导致“追加消息”体验差。目标是把 TTY 改成业界主流结构：**输出区独立滚动，输入/确认/状态固定在底部 dock**，同时保留管道模式。

## 已确认的约束

- 路线：换成独立 TUI 框架，选择 **Textual**。
- 管道模式必须保留：非 TTY/重定向仍可用，输出保持干净可解析。
- 内核边界不变：`agent.py` / `streaming.py` / `compact.py` 继续只产出 `events.py` 事件，不 `import ui`。
- steering 语义不变：运行中追加消息只在轮边界注入，不能切出半个 tool 配对。
- 不长期保留两套 TTY 前端；Textual 稳定后删除 prompt_toolkit 常驻输入框路径。

## 业界参照结论

- opencode：消息区是可滚动 `scrollbox` 且 sticky bottom；输入/确认区在下方固定区域。
- pi：alternate-screen 中使用 `ScrollView(transcript)` + 底部固定 dock（pending/status/editor/footer）。
- codex：`ratatui` 全屏帧 + `bottom_pane` 拥有 composer/审批/状态。

结论：采用 **B 的结构、A 的观感**。视觉保持简洁，但架构上必须是应用拥有的 viewport，而不是依赖终端 scrollback。

## 总体架构

- `main.py` 只做模式分发：
  - TTY：启动 Textual 前端 `tui.py`。
  - 非 TTY：继续走现有 `_pipe_loop` + `ui.py`。
- `ui.py` 保留为管道/轻量输出口径，不再承担复杂常驻输入框形态。
- 新增 `tui.py` 作为 Textual TTY 前端，消费 `session.chat(..., control=TurnControl)` 的事件流。
- `input_utils.py` 逐步退出 TTY 输入主路径，保留管道输入与通用文本清洗能力。

## Textual 布局

```text
┌────────────────────────────────────────────┐
│ TranscriptView（可滚动输出区）             │
│ - assistant 正文 / reasoning 预览          │
│ - tool call / tool result                  │
│ - note / warn / compact 提示               │
├────────────────────────────────────────────┤
│ Dock（固定底部）                           │
│ - ApprovalBar：有确认请求时显示 y/n        │
│ - QueuedPreview：运行中已排队的追加消息    │
│ - StatusBar：模型 / tokens / 运行中状态    │
│ - PromptInput：❯ 输入框                    │
└────────────────────────────────────────────┘
```

组件边界：

- `MiniAgentApp`：Textual 应用壳，持有 `ChatSession`、当前 `TurnControl`、后台事件泵。
- `TranscriptView`：只负责把事件变成可见内容，不直接改会话历史。
- `Dock`：固定底部区域，组合确认、排队预览、状态栏、输入框。
- `PromptInput`：第一版单行输入 + 历史 + 运行中追加；多行编辑后续再升级。

## 数据流与控制流

一轮提问流程：

1. `PromptInput` 提交文本。
2. 空闲时：创建 `TurnControl`，启动后台 worker 消费 `bridge.run_in_thread(...)` 的事件流。
3. 运行中：文本进入 `control.steer`，同时 `QueuedPreview` 显示“已排队”。
4. worker 线程把事件转发进 Textual 消息循环；Textual 只渲染，不改历史。
5. `TurnEnd` 后收尾：保存 session、清运行态、把未注入的 steering 回填到输入框。

控制规则：

- 运行中 Enter = 追加消息进入 steering 队列。
- 运行中 Esc / Ctrl+C = `control.abort()`；有待确认时一并按拒绝放行。
- 空闲时 Ctrl+C / Ctrl+D = 退出。
- 审批请求通过新的 `TextualApprovalChannel` 接入现有 `set_approval_channel(...)`，确认 UI 在 Dock 中显示，`y/n` 只在审批 pending 时消费。

## 渲染与长输出策略

- `TranscriptView` 用 Textual 的滚动容器/RichLog 承载追加式内容。
- 当前轮 assistant 正文聚合成一个 Markdown 块，按节流更新；完成后定稿，不再使用 `rich.Live` 超高重绘。
- reasoning 保留暗色预览，最多 600 字符；完整 reasoning 仍写入历史。
- 工具调用继续显示 `⏺ name(args)` / `⎿ preview`，但渲染在 transcript 内，不影响输入区位置。
- 动态/模型文本默认 `markup=False`；只有明确是 Markdown 正文的块才走 Markdown。
- 滚动策略：默认跟随底部；用户向上翻页时不强行拉回底部，状态栏提示有新输出。
- 第一版不做 transcript 虚拟化。

## 测试策略

- 现有内核测试不变，继续保持零网络。
- 新增 `tests/test_tui.py`，用 Textual headless 测试能力验证 UI 状态：
  - 运行中输入 Enter 后出现在 `QueuedPreview`，轮边界注入后消失；
  - 运行中 `Esc/Ctrl+C` 触发 `control.abort()`；
  - 审批请求出现时 `y/n` 不污染普通输入；
  - `TurnEnd` 后未注入 steering 回填输入框；
  - 管道模式仍走旧路径，输出不受 Textual 影响。
- 不访问真实 API；OpenAI client 继续打桩。

## 迁移与风险

- 引入 Textual 作为 `.venv` 中的 TTY 前端依赖，并更新 `AGENTS.md` 说明；实施时同步补一个最小 `requirements.txt`，避免新环境无法复现安装。
- Textual 路径通过测试后，删除 prompt_toolkit 常驻输入框路径，避免长期维护两套 TTY 前端。
- 风险点集中在：输入历史/补全迁移、审批通道线程等待、Markdown 流式更新节流、滚动跟随策略。每一层都用 headless 测试先钉住行为。

## 非目标

- 不改 agent 内核的事件契约。
- 不做子 agent、长期记忆、MCP。
- 不做 transcript 虚拟化、多栏 sidebar、鼠标选择复制增强。
- 不改变 steering 的轮边界注入语义。
