"""agent 对话循环：ChatSession 封装对话状态，把全局 messages 收进对象。

职责：
    - 持有 messages（对话历史）与 OpenAI client
    - chat() 主循环（生成器）：发请求 → 流式拼装 → 执行工具 → 动态注入声明，
      循环到出终稿；全程只产出事件流（events.py），不感知任何界面
    - 工具执行的容错（任何失败转为文本结果，不向上抛）
    - 异常回滚：本轮失败时整体撤销已产生的消息

界面边界：本模块不 import ui——内核是事件的生产者，渲染是消费者的职责
（ui.consume / bench / 将来的 GUI）。
工具精简后，本模块不再感知具体工具——执行表来自 registry.TOOLS，
动态声明来自 registry.get_resident_tool_schemas()，新增工具零改动接入。
"""

import json
import os
import queue

from openai import OpenAI

import config
from compact import (
    apply_message_cap,
    apply_slimming,
    apply_truncation,
    calibrate,
    detect_slim_targets,
    detect_truncation_point,
    estimate_total_tokens,
    extract_middle,
    summarize_middle,
)
from config import (
    MAX_SAME_TOOL_CALLS,
    REPO_MAP_MAX_CHARS,
    SESSION_FILE,
    SUBAGENT_HIDDEN_TOOLS,
    SYSTEM_MESSAGES,
    TOOL_RESULT_PREVIEW_LEN,
    format_context_tokens,
    format_tokens,
    get_profile,
)
from events import (
    Note,
    StreamFinished,
    StreamStart,
    ToolCallResult,
    ToolCallStart,
    TurnControl,
    TurnEnd,
    Usage,
    Warn,
)
from repo_map import build_repo_map_cached
from streaming import interruptible_stream, stream_and_assemble
from tool_registry import TOOLS, get_extended_tool_schemas, get_resident_tool_schemas
from tools import set_history_provider

# AGENTS.md 自动注入：仓库行为约定（编码规范/架构边界/踩坑清单）作为一条 system 消息
# 注入在模板后、repo map 前——它是「怎么改代码」的交规，repo map 是「仓库有什么」的
# 地图，两者同一注入边界。compact 头部保留区会保留开头连续的 system，契约不会截断。
_AGENTS_MD_HEADER = "[AGENTS.md 仓库行为约定（随会话保留）：]"


def _agents_md_text() -> str | None:
    """读仓库根的 AGENTS.md；文件缺失/读失败时静默返回 None（契约是增强，不是依赖）。"""
    try:
        with open(os.path.join(config.PROJECT_ROOT, "AGENTS.md"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None

# 常驻请求的工具声明：search_tools（发现入口）+ 核心四件套（RESIDENT_TOOL_NAMES）。
# 模块级算一次即可——本行执行时 tools.py 已完成导入注册（上方 from tools import），
# 常驻档是静态集合，无需每轮重算。可发现工具（名单外）由 search_tools 检索后注入。
BASE_TOOLS = get_resident_tool_schemas()

def load_saved_session() -> dict | None:
    """读取会话存档；不存在或损坏返回 None（存档是增强，不是依赖）。"""
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data.get("messages"), list) else None
    except (json.JSONDecodeError, OSError):
        return None


class ChatSession:
    """一轮完整的多轮对话会话：封装 messages 与 client，避免全局状态。

    tools: 工具声明列表（默认 None = 用全局 BASE_TOOLS 常驻名单）。子 agent 场景
    传入受限工具集实现权限收窄。depth: 嵌套深度（0 = 主 agent，>0 = 子 agent）。
    """

    def __init__(self, tools: list[dict] | None = None, depth: int = 0,
                 set_provider: bool = True, profile_name: str | None = None) -> None:
        self.profile_name = profile_name or config.MODEL_PROFILE
        self.profile = get_profile(self.profile_name)
        self._init_client()
        # 拷贝一份 system 模板，避免污染 config 里的原始定义
        self.messages: list[dict] = list(SYSTEM_MESSAGES)
        # AGENTS.md 行为契约注入（与 repo map 同构：失败静默，随会话保留）
        agents_md = _agents_md_text()
        if agents_md:
            self.messages.append(
                {"role": "system", "content": f"{_AGENTS_MD_HEADER}\n{agents_md}"}
            )
        # 代码库地图注入：项目结构/核心符号一览（aider 式）。作为一条 system 消息
        # 追加在模板后——compact 的头部保留区会保留它，不会被截断。生成失败静默
        # 跳过（地图是增强，不是依赖）；模块级缓存避免每个会话重复扫描。
        try:
            repo_map_text = build_repo_map_cached(max_chars=REPO_MAP_MAX_CHARS)
            if repo_map_text:
                self.messages.append({"role": "system", "content": repo_map_text})
        except Exception:
            pass
        # 历史检索工具的数据源：存储（而非投影）——被瘦身/截断/摘要/截中的原文都可检索
        if set_provider:
            set_history_provider(lambda: self.messages)
        # token 仪表盘：会话累计消耗 + 当前上下文占用（最近一次请求的 prompt tokens）
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_prompt_tokens = 0
        # 子 agent 支持：可注入的工具集与嵌套深度
        self.tools = tools if tools is not None else BASE_TOOLS
        self.depth = depth

    def _init_client(self) -> None:
        """根据当前档案创建 OpenAI client；key 缺失时立即报错。"""
        api_key = os.environ.get(self.profile["api_key_env"])
        if not api_key:
            raise RuntimeError(
                f"档案 {self.profile_name!r} 缺少 API key："
                f"环境变量 {self.profile['api_key_env']} 未设置"
            )
        self.client = OpenAI(api_key=api_key, base_url=self.profile["base_url"])

    def set_profile(self, name: str) -> None:
        """运行时切换模型档案；失败时保留原档案与 client。"""
        old_profile, old_name = self.profile, self.profile_name
        try:
            self.profile = get_profile(name)
            self.profile_name = name
            self._init_client()
        except Exception:
            self.profile, self.profile_name = old_profile, old_name
            raise

    def status_text(self) -> str:
        """输入区底部状态栏的内容（input_utils 底栏回调，每次按键重绘）。

        ctx 占比 = 当前历史消息占用的窗口比例（pi 同款比例样式）。
        用 estimate_total_tokens(self.messages) 而不是累计消耗，是因为累计 tokens
        包含已被截断/丢弃的旧消息，会虚高；当前消息列表才是真实窗口占用。
        """
        prompt = self.total_prompt_tokens
        completion = self.total_completion_tokens
        total = prompt + completion
        used = estimate_total_tokens(self.messages)
        pct = used / self.profile["context_tokens"] * 100
        return (
            f"{self.profile['model']} · ctx {pct:.1f}%/"
            f"{format_context_tokens(self.profile['context_tokens'])} · "
            f"prompt {format_tokens(prompt)} · completion {format_tokens(completion)} · total {format_tokens(total)}"
        )

    # ---- 历史管理 ----

    def mark(self) -> int:
        """记录当前历史位置，供失败时回滚。"""
        return len(self.messages)

    def rollback(self, mark: int) -> None:
        """撤销 mark 之后产生的所有消息（异常整体回滚）。"""
        del self.messages[mark:]

    # ---- 会话存档（/resume 的存储层）----

    def save(self) -> None:
        """会话存档：原子写（tmp + replace），进程崩溃不留半截文件。"""
        data = {
            "messages": self.messages,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
        }
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SESSION_FILE)

    def _tools_already_injected(self) -> bool:
        """是否已注入过动态工具声明（保证幂等，避免重复注入撑胖上下文）。"""
        return any(isinstance(m, dict) and m.get("tools") for m in self.messages)

    # ---- 工具执行 ----

    @staticmethod
    def _interrupted(control: TurnControl | None) -> bool:
        """中断旗帜是否已置位（control 缺省 = 无控制通道，永不中断）。"""
        return bool(control and control.interrupt.is_set())

    def _drain_steering(self, control: TurnControl):
        """轮次间隙拉取 steering 队列（生成器）：运行中插话只注入在合法消息边界。

        pi 的 PendingMessageQueue 同款 drain 语义：消息作为 user 角色注入历史，
        下次请求生效。绝不在流式/工具执行中途插入（会切出半个 tool 配对）。
        """
        while True:
            try:
                text = control.steer.get_nowait()
            except queue.Empty:
                return
            self.messages.append({"role": "user", "content": text})
            yield Note(f"已注入运行中消息：{text[:60]}{'…' if len(text) > 60 else ''}",
                       tag="steer")

    def _execute_tool_call(self, tool_call: dict) -> tuple[str, str]:
        """执行单个工具调用，返回 (工具名, 结果文本)。任何失败都转为文本结果，不向上抛。"""
        name = tool_call["function"].get("name")
        raw_arguments = tool_call["function"].get("arguments") or "{}"
        try:
            if name not in TOOLS:
                raise KeyError(f"Unknown tool: {name}")
            arguments = json.loads(raw_arguments)
            result = str(TOOLS[name](**arguments))  # 兜底：工具可能返回非字符串
        except Exception as e:
            result = f"调用失败: {type(e).__name__}: {e}"
        return name, result

    # ---- 主循环 ----

    def chat(self, user_input: str, control: TurnControl | None = None):
        """一轮提问（生成器）：append → 请求 → 流式 → 工具循环，直到模型给出终稿。

        全程只产出事件流（events.py），不感知界面。调用方必须消费完整个流
        （ui.consume / for 循环），生成器不消费就不执行。
        control：可选控制通道（interrupt / steering），缺省时行为与纯交互一致。
        """
        self.messages.append({"role": "user", "content": user_input})

        # 历史管理管道（轮边界检测一次）：先 L3 瘦身 + 单条体积上限（均不改消息数量，
        # 下标与原始历史对齐），瘦身后估算仍超硬水位才 L1 截断
        slim_targets = detect_slim_targets(self.messages)
        if slim_targets:
            yield Note(f"已瘦身 {len(slim_targets)} 条超龄工具结果", tag="compact")
        # 切点计算也基于瘦身后的投影：占位符只有 ~100 字符，按原始体积装箱会误多切
        slimmed = apply_message_cap(apply_slimming(self.messages, slim_targets))
        cut = 0
        note = None
        high = self.profile["truncate_high_tokens"]
        low = self.profile["truncate_low_tokens"]
        if estimate_total_tokens(slimmed) >= high:
            cut = detect_truncation_point(slimmed, low)
            if cut:
                # L2 优先：让模型把中段压缩成交接摘要；失败时 note=None 回退 L1 硬切标记
                deferred_notes: list[str] = []  # 摘要内部的消息先收着，统一作为事件产出
                summary = summarize_middle(extract_middle(slimmed, cut), self.client,
                                           model=self.profile["model"],
                                           on_note=deferred_notes.append)
                for msg in deferred_notes:
                    yield Note(msg, tag="compact")
                if summary:
                    note = f"[早期对话历史摘要]\n{summary}"
                    yield Note("上下文超限，已生成早期历史摘要（L2）", tag="compact")
                else:
                    yield Note(f"上下文超限，已截断早期历史（L1，目标 {low // 1000}K tokens）",
                               tag="compact")

        # 死循环保险丝状态：跟踪连续重复的 (工具名, 参数) 签名
        last_call_sig = None
        same_call_count = 0

        # 无硬性轮次上限（与 pi/Codex/Claude Code 交互模式一致）：交互场景人在看，
        # 硬上限只会误杀合法长任务。失控防线是行为保险丝（同签名连续重复熔断）。
        # bench 等无人值守场景若需要上限，应加在调用侧而非污染交互循环
        while True:
            # 控制通道检查点①（轮边界）：interrupt 优先于 steering——
            # 用户叫停了就不要再注入新话
            if self._interrupted(control):
                yield Warn("本轮已被用户中断")
                yield TurnEnd()
                return
            if control:
                yield from self._drain_steering(control)

            # 每轮重算投影：工具循环内历史只涨不停（单轮最多 30 次调用×10K 字符），
            # 轮边界的检测看不见"单轮爆炸"，超线时应急升级截断（L1 硬切，不插 L2 调用）
            # 瘦身与体积上限均不改消息数量与顺序，cut 下标对投影同样有效
            def project(msgs):
                return apply_message_cap(apply_slimming(msgs, slim_targets))

            payload = apply_truncation(project(self.messages), cut, note)
            if estimate_total_tokens(payload) >= high:
                escalated = detect_truncation_point(project(self.messages), low)
                if escalated > cut:
                    cut, note = escalated, None
                    payload = apply_truncation(project(self.messages), cut, note)
                    yield Note("工具循环内上下文超限，已应急截断（L1）", tag="compact")

            yield StreamStart()
            def open_stream(payload=payload):  # 显式绑定当轮投影，防闭包捕获漂移
                return self.client.chat.completions.create(
                    model=self.profile["model"],
                    # 发送时投影，存储不动；cut 下标对瘦身投影同样有效（瘦身不改消息数量）
                    # note 为 None 时是 L1 硬切标记，为摘要文本时是 L2 保值版
                    messages=payload,
                    tools=self.tools,  # 当前会话的工具声明（子 agent 可注入受限集）
                    stream=True,
                    stream_options={"include_usage": True}
                )

            if control is not None:
                # 可中断包装：请求+读取全程在后台泵线程，主侧 100ms 轮询旗帜——
                # 连接/TTFT/流式中途任何阶段的打断延迟都有界（closer 断流对
                # 跨线程阻塞读不可靠，实测 abort 后 15s 未唤醒，故改为此方案）
                source = interruptible_stream(open_stream, control)
            else:
                source = open_stream()
            # 流式事件原样透传给消费者；StreamFinished 是内核自留的收尾事件
            # （定稿消息与 usage 从它身上取回，继续驱动工具循环）
            assistant_messages, usage = [], None
            try:
                for ev in stream_and_assemble(source):
                    # 检查点②（事件间隙）：中断即弃流——部分响应尚未 append 进历史，
                    # 不存在配对问题，是干净的放弃点
                    if self._interrupted(control):
                        yield Warn("本轮已被用户中断，未完成的部分输出已丢弃")
                        yield TurnEnd()
                        return
                    if isinstance(ev, StreamFinished):
                        assistant_messages, usage = ev.messages, ev.usage
                    else:
                        yield ev
            except Exception:
                # StreamAborted / abort 断流导致的读取异常——预期内的中断路径
                if self._interrupted(control):
                    yield Warn("本轮已被用户中断，未完成的部分输出已丢弃")
                    yield TurnEnd()
                    return
                raise

            if usage:
                calibrate(usage.prompt_tokens, payload)  # 用真实值校准估算系数，观测闭环
                self.total_prompt_tokens += usage.prompt_tokens
                self.total_completion_tokens += usage.completion_tokens
                self.last_prompt_tokens = usage.prompt_tokens  # 当前上下文占用（状态栏百分比）
                cached = getattr(usage, "cached_tokens", 0) or 0
                yield Usage(usage.prompt_tokens, usage.completion_tokens, cached,
                            self.total_prompt_tokens + self.total_completion_tokens)

            for assistant_message in assistant_messages:
                finish_reason = assistant_message.pop("_finish_reason", None)
                tool_calls = assistant_message.get("tool_calls")

                if finish_reason == "length" and tool_calls:
                    # 截断轮的工具调用整批作废（pi 同款）：arguments JSON 不完整，
                    # 执行半个参数比不执行更危险。必须为每个 tool_call 补作废结果——
                    # 缺响应的 tool_calls 是孤儿，下次请求必 400
                    self.messages.append(assistant_message)
                    for tc in tool_calls:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": tc["function"].get("name"),
                            "content": "[系统] 输出被 max_tokens 截断，工具调用参数不完整，整批调用已作废。请缩小单次动作（如分次写入）后重试。",
                        })
                    yield Warn("输出被 max_tokens 截断：工具调用参数不完整，已作废整批调用")
                    yield TurnEnd()
                    return
                if finish_reason == "length":
                    yield Warn("输出被 max_tokens 截断，回答可能不完整")
                elif finish_reason == "content_filter":
                    yield Warn("内容被安全审查拦截")

                self.messages.append(assistant_message)

                if not tool_calls:
                    # 终稿：内容已由 TextDelta 事件流式送出，本轮结束
                    yield TurnEnd()
                    return

                # 本轮是否调用了 search_tools（循环结束后统一注入声明，见下方注释）
                search_tools_called = False
                for i, tool_call in enumerate(tool_calls):
                    # 检查点③（工具间隙）：剩余调用补 interrupted 结果防孤儿 400。
                    # 正在执行的单个工具不打断——强杀 bash 子进程是另一档工程（v1 等它自然结束）
                    if self._interrupted(control):
                        for tc in tool_calls[i:]:
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc.get("id"),
                                "name": tc["function"].get("name"),
                                "content": "[系统] 本轮已被用户中断，该调用未执行。",
                            })
                        yield Warn("已中断：剩余工具调用未执行")
                        yield TurnEnd()
                        return

                    # 死循环保险丝（行为识别）：同一 (工具名, 参数) 连续出现 N 次即判定卡死。
                    # 正常任务每次调用参数不同不会误伤；真卡死的模型几轮内被揪出
                    sig = (
                        tool_call["function"].get("name"),
                        tool_call["function"].get("arguments") or "{}",
                    )
                    same_call_count = same_call_count + 1 if sig == last_call_sig else 1
                    last_call_sig = sig
                    if same_call_count >= MAX_SAME_TOOL_CALLS:
                        yield Warn(f"同一工具调用连续重复 {same_call_count} 次（{sig[0]}），"
                                   f"判定死循环，已强制结束本轮")
                        # 为当前及剩余 tool_calls 补拦截结果：缺响应的 tool_calls 是孤儿，
                        # 下次请求必 400
                        for tc in tool_calls[i:]:
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc.get("id"),
                                "name": tc["function"].get("name"),
                                "content": "[系统] 同一调用连续重复，已拦截。请停止重试，基于已有信息给出结论。",
                            })
                        yield TurnEnd()
                        return

                    # 工具调用事件（⏺ 面板的数据源）：让用户看见 agent 在干什么，
                    # 兼作"幻觉测谎仪"
                    yield ToolCallStart(sig[0], sig[1])
                    name, tool_result = self._execute_tool_call(tool_call)
                    preview = (tool_result if len(tool_result) <= TOOL_RESULT_PREVIEW_LEN
                               else tool_result[:TOOL_RESULT_PREVIEW_LEN] + "…")
                    yield ToolCallResult(name, preview)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "name": name,  # Moonshot 要求 tool 消息带 name 以便模型配对
                            "content": tool_result,
                        }
                    )
                    if name == "search_tools":
                        search_tools_called = True

                # search_tools 被调用后，注入可发现工具声明（Moonshot 动态加载机制），幂等。
                # 必须在全部 tool 响应 append 之后注入：若插在并行 tool_call 的响应中间
                # （assistant 一次发两个调用，先执行 search_tools），Moonshot 会把后一个
                # tool_calls 判为孤儿返回 400（"did not have response messages"）——
                # 评测在 fix_notify_dedupe nomap 组实拍到此现场。
                # 当前没有可发现工具时不注入（空声明消息只会白占上下文）。
                # 子 agent（depth>0）注入时过滤元工具（spawn/todo），与 tools.search_tools
                # 的返回文本过滤是同一道防线的两层：少了这层，声明一旦入史，元工具即可被调用。
                if search_tools_called and not self._tools_already_injected():
                    schemas = get_extended_tool_schemas()
                    if self.depth > 0:
                        schemas = [s for s in schemas
                                   if s["function"]["name"] not in SUBAGENT_HIDDEN_TOOLS]
                    if schemas:
                        self.messages.append({"role": "system", "tools": schemas})
