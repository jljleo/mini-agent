"""输入处理：常驻输入框（tty）+ 管道读取 + 文本清洗 + 确认通道。

全程序只此一套输入体系（prompt_toolkit），避免多处 input()/select 混用
导致 stdin 缓冲冲突（正是"输 y 卡死"的根因）。

tty 形态（main._tui_loop）：常驻 ❯ 输入框 + patch_stdout——agent 运行时
输入框不消失，运行输出自动抬升到输入框上方：
    - 空闲时 Enter = 发起新一轮；运行中 Enter = 追加消息（steering 队列）
    - Esc / Ctrl+C = 立即打断（control.abort()：置旗帜 + 关闭活动流，零延迟）
    - 确认请求（危险命令/越界文件）由输入框 y/n 按键应答（ApprovalChannel）
管道形态（main._pipe_loop）：回合制原始读取，无运行中交互。

所有输入统一 sanitize：NFC 规范化 + 控制字符过滤。
界面元素（❯ 提示符样式、底部状态栏、确认框文案）的配色与文案定义在 ui.py，
本模块只负责装配 prompt_toolkit 部件。
"""

import sys
import threading
import unicodedata

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

import ui
from command_registry import COMMANDS
from config import HISTORY_FILE

# prompt_toolkit 会话（惰性创建）：历史记录存项目目录，↑ 键可翻出历史提问（跨会话保留）
# 惰性原因：模块级创建在非 tty 环境（管道输入）会打印警告
_prompt_session: PromptSession | None = None

# 底部状态栏内容提供者（main 启动时注入 ChatSession.status_text）：
# 每次按键重绘，显示 模型 · token 累计 等会话状态
_status_provider = None

_PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "ansicyan bold",
        "bottom-toolbar": "bg:#262626 #808080",
        "auto-suggest": "#585858",
        "completion-menu": "bg:#1c1c1c #d0d0d0",
        "completion-menu.completion.current": "bg:#005f5f #ffffff",
        "completion-menu.meta": "bg:#1c1c1c #808080",
    }
)


def set_status_provider(fn) -> None:
    """注入底部状态栏的内容回调（返回一行纯文本）。"""
    global _status_provider
    _status_provider = fn


# ---- 运行状态（常驻输入框与运行线程共享）----


class ApprovalChannel:
    """常驻输入框模式的确认通道：确认请求打印到滚动区，y/n 由按键绑定应答。

    替代旧的"独立确认弹窗"——常驻输入框下同时只能有一个 prompt 会话，
    确认不再另起会话，而是复用输入框的按键（filter=有待确认时才拦截 y/n）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: tuple[str, bool] | None = None
        self._answered = threading.Event()
        self._result = False

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def ask(self, command: str, dangerous: bool, timeout: int) -> bool:
        """运行线程（工具执行深处）调用：挂起等待输入框 y/n，超时默认拒绝。"""
        with self._lock:
            self._pending = (command, dangerous)
            self._answered.clear()
            self._result = False
        # 确认文案走 print：patch_stdout 会把它抬升到输入框上方
        print(ui.confirm_prompt_text(command, dangerous, timeout), flush=True)
        answered = self._answered.wait(timeout)
        with self._lock:
            self._pending = None
            result = self._result
        if not answered:
            # 超时兜底（假 tty / 用户离开）：拒绝
            ui.error(f"{timeout}s 内未收到确认，已默认拒绝")
            return False
        print("y" if result else "n")  # 单键无回显，补一行答案留痕
        return result

    def answer(self, yes: bool) -> None:
        """输入线程按键绑定调用。无待确认时不应被触发（按键 filter 保证）。"""
        with self._lock:
            if self._pending is None:
                return
            self._result = yes
            self._answered.set()


_approval_channel: ApprovalChannel | None = None


def set_approval_channel(channel: ApprovalChannel | None) -> None:
    """main（tty）启动时登记确认通道；confirm() 优先走它，缺省回退旧单键弹窗。"""
    global _approval_channel
    _approval_channel = channel


def abort_pending_approval() -> None:
    """打断时联动：待确认的请求按拒绝放行，防内核在确认上等满超时。"""
    if _approval_channel is not None and _approval_channel.has_pending:
        _approval_channel.answer(False)


_run_lock = threading.Lock()
_run_control = None          # 运行中轮次的 TurnControl（None = 空闲）
_next_prefill = ""           # 上轮未注入的 steering，回填下一个输入框


def is_running() -> bool:
    with _run_lock:
        return _run_control is not None


def current_control():
    with _run_lock:
        return _run_control


def begin_run(control) -> None:
    """运行开始前登记（main 线程，启动运行线程之前——防时序窗口）。"""
    global _run_control
    with _run_lock:
        _run_control = control


def end_run(leftover_steer: list[str]) -> None:
    """运行结束（运行线程）：清除运行态；未注入的 steering 存为下轮回填。"""
    global _run_control, _next_prefill
    with _run_lock:
        _run_control = None
        if leftover_steer:
            _next_prefill = "\n".join(leftover_steer)


def take_prefill() -> str:
    """主循环取走上轮回填（一次性）。"""
    global _next_prefill
    with _run_lock:
        prefill, _next_prefill = _next_prefill, ""
    return prefill


def _bottom_toolbar() -> HTML:
    if is_running():
        return HTML("<bottom-toolbar> ● 运行中 · 输入回车=追加 · Esc/Ctrl+C=打断 · 确认按 y/n </bottom-toolbar>")
    if _status_provider is None:
        return HTML("<bottom-toolbar> </bottom-toolbar>")
    try:
        text = _status_provider()
    except Exception:
        # 状态栏是装饰件：任何异常都不许影响输入主流程
        text = ""
    return HTML(f"<bottom-toolbar> {text} </bottom-toolbar>")


class SlashCommandCompleter(Completer):
    """斜杠命令补全：仅在行首以 / 开头时弹出候选（带命令描述）。

    普通输入不弹补全菜单——避免补全菜单打开时回车被"采纳候选"抢占，
    导致正常提问发不出去。
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # /quit 不在 COMMANDS（走主循环退出词表），补全里单独补上
        candidates = {**COMMANDS, "/quit": None}
        for name, fn in candidates.items():
            if name.startswith(text):
                meta = fn.description if fn is not None else "退出程序"
                yield Completion(name, start_position=-len(text), display_meta=meta)


def _build_keybindings() -> KeyBindings:
    """常驻输入框的按键绑定：确认应答（y/n，仅有待确认时拦截）+ Esc 立即打断。"""
    kb = KeyBindings()

    def approval_pending() -> bool:
        return _approval_channel is not None and _approval_channel.has_pending

    @kb.add("y", filter=Condition(approval_pending))
    @kb.add("Y", filter=Condition(approval_pending))
    def _yes(event):
        _approval_channel.answer(True)

    @kb.add("n", filter=Condition(approval_pending))
    @kb.add("N", filter=Condition(approval_pending))
    def _no(event):
        _approval_channel.answer(False)

    @kb.add("escape", eager=True)  # eager：不等转义序列超时，Esc 即按即断
    def _esc(event):
        if approval_pending():
            _approval_channel.answer(False)  # 有待确认：Esc = 拒绝
            return
        control = current_control()
        if control is not None:
            control.abort()  # 运行中：立即打断（置旗帜 + 断流，零延迟）
            abort_pending_approval()  # 联动：待确认的确认框一并按拒绝放行

    return kb


def _get_prompt_session() -> PromptSession:
    global _prompt_session
    if _prompt_session is None:
        _prompt_session = PromptSession(
            history=FileHistory(HISTORY_FILE),
            completer=SlashCommandCompleter(),
            auto_suggest=AutoSuggestFromHistory(),  # 历史幽灵建议：灰色尾随，→ 键采纳
            style=_PROMPT_STYLE,
            bottom_toolbar=_bottom_toolbar,
            key_bindings=_build_keybindings(),
        )
    return _prompt_session


def sanitize(text: str) -> str:
    """清洗输入：NFC 规范化 + 剥掉控制字符（保留 \n \t）。"""
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return text.strip()


def read_input(prompt: str = "", prefill: str = "") -> tuple[str, bool]:
    """读取用户输入：终端下用 prompt_toolkit（历史/行编辑），管道模式退回原始读法。

    返回 (清洗后的文本, 是否前导空格逃逸)：
    - prefill：预填输入框（Codex 式——未知命令报错后恢复原文，用户修正即可重发）；
      非交互环境无法预填，静默忽略；
    - 前导空格 = 显式逃逸：强制按消息发送，跳过斜杠命令分发（Codex 同款）。
      注意必须在 sanitize 之前检测——sanitize 会 strip 掉前导空格。

    可能抛出 EOFError（Ctrl+D / 管道耗尽）或 KeyboardInterrupt（Ctrl+C），
    由调用方决定如何收尾。
    """
    if sys.stdin.isatty():
        # ❯ 提示符：品牌色加粗；prompt_toolkit 遇到 Ctrl+C/Ctrl+D 抛 KeyboardInterrupt/EOFError
        raw = _get_prompt_session().prompt(HTML("<prompt>❯</prompt> "), default=prefill)
    else:
        # 非交互环境（管道/重定向）：prompt_toolkit 不适用，退回字节读取
        print(prompt, end="", flush=True)
        raw_b = sys.stdin.buffer.readline()
        if raw_b == b"":
            raise EOFError
        raw = raw_b.decode("utf-8", errors="replace")

    forced = raw.startswith(" ")
    return sanitize(raw), forced


# 确认等待的最长秒数：防"假 tty"（isatty 为 True 但无人应答）导致永久阻塞
CONFIRM_TIMEOUT = 60


def _single_key_confirm(message: str) -> bool:
    """prompt_toolkit 单键确认（旧形态回退路径，非常驻输入框模式用）。"""
    kb = KeyBindings()
    state = {"answer": False}

    @kb.add("y")
    @kb.add("Y")
    def _yes(event):
        state["answer"] = True
        event.app.exit(result=True)

    @kb.add("n")
    @kb.add("N")
    @kb.add("escape")
    @kb.add("c-c")  # Ctrl+C 视为拒绝而非崩溃
    @kb.add("enter")  # 直接回车 = 默认拒绝（贴合 "(y/N)" 语义）
    def _no(event):
        state["answer"] = False
        event.app.exit(result=False)

    # 用独立 session（不带历史），避免污染主输入的历史记录
    session: PromptSession = PromptSession(key_bindings=kb)
    # ANSI() 包装：prompt_toolkit 对纯字符串不解析转义序列（安全设计），
    # 需显式声明"此文本含 ANSI 颜色码"，否则 \033 会以 ^[ 形式原样显示
    try:
        return bool(session.prompt(ANSI(message)))
    except (EOFError, KeyboardInterrupt):
        return False


def confirm(command: str, dangerous: bool) -> bool:
    """命令执行前的用户确认：y 放行，其余拒绝；带超时与假 tty 防护。

    通道选择：
      - 常驻输入框模式（_approval_channel 已登记）：打印确认文案，按键绑定应答；
      - 否则回退旧的独立单键弹窗（子线程 + join 超时）。
    三层防线不变：非 tty 默认拒绝；超时默认拒绝；只有明确按 y 才放行。
    """
    if not sys.stdin.isatty():
        ui.error("非交互环境无法确认，已默认拒绝执行该命令")
        return False

    if _approval_channel is not None:
        return _approval_channel.ask(command, dangerous, CONFIRM_TIMEOUT)

    message = ui.confirm_prompt_text(command, dangerous, CONFIRM_TIMEOUT)

    # 单键确认放子线程跑，主线程 join 超时：超时即拒绝。
    # 直接调用会阻塞主线程，无法对 prompt_toolkit 自身施加超时。
    result = {"answer": False}

    def _run() -> None:
        result["answer"] = _single_key_confirm(message)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(CONFIRM_TIMEOUT)

    if worker.is_alive():
        # 超时：prompt_toolkit 还在等键，判定为假 tty，拒绝
        ui.error(f"{CONFIRM_TIMEOUT}s 内未收到确认，已默认拒绝")
        return False

    return result["answer"]
