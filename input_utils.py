"""输入处理：终端交互读取 + 管道读取 + 文本清洗 + 命令确认。

全程序只此一套输入体系（prompt_toolkit），避免多处 input()/select 混用
导致 stdin 缓冲冲突（正是"输 y 卡死"的根因）。

终端下用 prompt_toolkit（历史/行编辑/单键确认），管道/重定向退回原始读法。
所有输入统一 sanitize：NFC 规范化 + 控制字符过滤。
"""

import sys
import threading
import unicodedata

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from command_registry import COMMANDS
from config import HISTORY_FILE

# prompt_toolkit 会话（惰性创建）：历史记录存项目目录，↑ 键可翻出历史提问（跨会话保留）
# 惰性原因：模块级创建在非 tty 环境（管道输入）会打印警告
_prompt_session: PromptSession | None = None


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


def _get_prompt_session() -> PromptSession:
    global _prompt_session
    if _prompt_session is None:
        _prompt_session = PromptSession(
            history=FileHistory(HISTORY_FILE),
            completer=SlashCommandCompleter(),
        )
    return _prompt_session


def sanitize(text: str) -> str:
    """清洗输入：NFC 规范化 + 剥掉控制字符（保留 \\n \\t）。"""
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return text.strip()


def read_input(prompt: str = "") -> str:
    """读取用户输入：终端下用 prompt_toolkit（历史/行编辑），管道模式退回原始读法。

    可能抛出 EOFError（Ctrl+D / 管道耗尽）或 KeyboardInterrupt（Ctrl+C），
    由调用方决定如何收尾。
    """
    if sys.stdin.isatty():
        # prompt_toolkit 遇到 Ctrl+C/Ctrl+D 会抛 KeyboardInterrupt/EOFError
        return sanitize(_get_prompt_session().prompt(prompt))

    # 非交互环境（管道/重定向）：prompt_toolkit 不适用，退回字节读取
    print(prompt, end="", flush=True)
    raw = sys.stdin.buffer.readline()

    if raw == b"":
        raise EOFError

    return sanitize(raw.decode("utf-8", errors="replace"))


# 确认等待的最长秒数：防"假 tty"（isatty 为 True 但无人应答）导致永久阻塞
CONFIRM_TIMEOUT = 60


def _single_key_confirm(message: str) -> bool:
    """prompt_toolkit 单键确认：按 y 放行，n / Esc / Ctrl+C / Enter 拒绝，无需回车。

    用一次性 PromptSession + 自定义按键绑定：拦截所有可打印字符，
    只认 y / n，其余键忽略；绑定 Enter 为"默认拒绝"，避免空等。
    """
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
    # prompt 返回 event.app.exit 的 result；异常（如 Ctrl+D）一律视为拒绝。
    # ANSI() 包装：prompt_toolkit 对纯字符串不解析转义序列（安全设计），
    # 需显式声明“此文本含 ANSI 颜色码”，否则 \033 会以 ^[ 形式原样显示
    try:
        return bool(session.prompt(ANSI(message)))
    except (EOFError, KeyboardInterrupt):
        return False


def confirm(command: str, dangerous: bool) -> bool:
    """命令执行前的用户确认：y 放行，其余拒绝；带超时与假 tty 防护。

    三层防线：
      1. 非 tty（管道/重定向）：无法交互，默认拒绝；
      2. tty 但超时无人应答（假 tty）：默认拒绝；
      3. 只有明确按 y 才放行。
    """
    if not sys.stdin.isatty():
        print("\n\033[91m[拒绝] 非交互环境无法确认，已默认拒绝执行该命令\033[0m", flush=True)
        return False

    color = "\033[91m" if dangerous else "\033[93m"  # 危险=红，普通=黄
    label = "危险命令" if dangerous else "需要确认"
    message = (
        f"\n{color}[{label}] 即将在项目目录执行: {command}\n"
        f"确认执行? (y/N，单键生效，{CONFIRM_TIMEOUT}s 超时): \033[0m"
    )

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
        print(f"\n\033[91m[拒绝] {CONFIRM_TIMEOUT}s 内未收到确认，已默认拒绝\033[0m", flush=True)
        return False

    return result["answer"]
