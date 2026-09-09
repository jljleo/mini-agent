"""CLI 入口：单一主循环，业务逻辑下沉到 agent / input_utils / ui / bridge。

运行：python main.py
退出：exit / quit / :q / /quit / Ctrl+C / Ctrl+D（运行中单击 Ctrl+C = 优雅打断本轮）

tty 与管道共用 `_chat_loop`，差别全部下沉到 read_input / ui 内部自适应：
- 输入：tty = prompt_toolkit（历史/补全/幽灵建议/底部状态栏），管道 = 行读取
- 渲染：tty = Live 增量重排 Markdown，管道 = 纯文本直出（StreamRenderer 自适应）

前端边界是刻意的架构决策（见 ROADMAP 定位）：回合制——agent 运行时终端归渲染器，
本轮结束回到提示符。刻意不做"运行中输入框"（patch_stdout 与 Live 光标重绘互斥，
两者共存必闪烁）；运行中打断走 Ctrl+C（bridge 优雅收尾）。
"""

import sys

import commands  # noqa: F401  集中式注册：导入即触发 @command 注册
import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册
import ui
from agent import ChatSession
from bridge import run_in_thread
from command_registry import COMMANDS
from config import CONTEXT_TOKENS, MODEL, PROJECT_ROOT, QUIT_COMMANDS
from input_utils import read_input, set_status_provider


def _dispatch_command(session: ChatSession, question: str, forced: bool):
    """命令/退出词分发。返回 "quit" / "prefill" / True（已处理）/ False（应进入对话）。"""
    # 前导空格 = 显式逃逸：跳过一切命令分发，强制按消息发给模型
    if not forced and question.lower() in QUIT_COMMANDS:
        ui.goodbye()
        return "quit"
    name, _, args = question.partition(" ")
    if not forced and name in COMMANDS:
        COMMANDS[name](session, args.strip())  # handler 统一接收 (session, args)
        return True
    # 未知斜杠命令拦截——但只在“看起来真的是命令”时：命令名是单个词（/clear），
    # 首个 token 内含其他 / 的是绝对路径（/Users/x.py 提问），应放行给模型
    if not forced and question.startswith("/") and "/" not in name[1:]:
        ui.warn(f"未知命令: {name}（输入 / 查看命令）")
        return "prefill"
    return False


def _chat_loop(session: ChatSession) -> None:
    """唯一主循环：读输入 → 分发命令 → 前台渲染本轮。

    Ctrl+C 语义（bridge 保证）：空闲 = 退出；运行中单击 = 置 interrupt 旗帜，
    内核优雅收尾（补孤儿 tool 结果）照常存档；双击 = 内核卡死时的逃生口，
    放弃本轮不存档（防写入半截状态），tty 回提示符、管道退出进程。
    """
    interactive = sys.stdin.isatty()
    prefill = ""
    while True:
        try:
            question, forced = read_input(prefill=prefill)
        except (EOFError, KeyboardInterrupt):
            ui.goodbye()
            break
        prefill = ""

        if not question:
            continue
        verdict = _dispatch_command(session, question, forced)
        if verdict == "quit":
            break
        if verdict is True:
            continue
        if verdict == "prefill":
            prefill = question  # 报错但不清空：回填原文，用户修正后重发（管道无输入框，静默忽略）
            continue

        mark = session.mark()  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        events, _control = run_in_thread(lambda c, q=question: session.chat(q, control=c))
        try:
            ui.consume(events)  # 非 tty 时 StreamRenderer 自动降级纯文本
            session.save()  # 每轮成功（含优雅中断收尾）后自动存档
        except KeyboardInterrupt:
            session.rollback(mark)
            if interactive:
                ui.warn("已强制打断本轮（未存档）")
                continue
            ui.warn("已强制中断并退出（本轮未存档）")
            ui.goodbye()
            break
        except Exception as e:
            session.rollback(mark)
            ui.error(f"{type(e).__name__}: {e}")


def main() -> None:
    session = ChatSession()
    set_status_provider(session.status_text)  # tty 输入区底部状态栏：模型 · 上下文窗口 · token 累计
    ui.banner(MODEL, PROJECT_ROOT, CONTEXT_TOKENS)
    _chat_loop(session)


if __name__ == "__main__":
    main()
