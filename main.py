"""CLI 入口：主循环与运行调度，业务逻辑下沉到 agent / input_utils / ui / tui。

运行：python main.py
退出：exit / quit / :q / /quit / Ctrl+C / Ctrl+D（运行中 Ctrl+C / Esc = 打断本轮，不退出）

两种形态：
- tty：Textual 全屏前端（tui.py）——输出 viewport 独立滚动，输入/确认/状态固定底部 dock；
  运行中回车 = 追加消息（steering 队列，轮边界注入），Esc / Ctrl+C = 打断本轮
- 管道（_pipe_loop）：回合制读取 + 线程桥（bridge.py），无运行中交互
"""

import sys

import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册
import commands  # noqa: F401  集中式注册：导入即触发 @command 注册

import ui
from agent import ChatSession
from bridge import run_in_thread
from command_registry import COMMANDS
from config import MODEL, PROJECT_ROOT, QUIT_COMMANDS
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
    # 未知斜杠命令拦截——但只在“看起来真的是命令”时：命令名是单个词（/help），
    # 首个 token 内含其他 / 的是绝对路径（/Users/x.py 提问），应放行给模型
    if not forced and question.startswith("/") and "/" not in name[1:]:
        ui.warn(f"未知命令: {name}（输入 /help 查看可用命令）")
        return "prefill"
    return False


def _pipe_loop(session: ChatSession) -> None:
    while True:
        try:
            question, forced = read_input()
        except (EOFError, KeyboardInterrupt):
            ui.goodbye()
            break

        if not question:
            continue
        verdict = _dispatch_command(session, question, forced)
        if verdict == "quit":
            break
        if verdict is True:
            continue
        if verdict == "prefill":
            continue  # 管道无输入框，无法回填

        mark = session.mark()  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        # 线程桥：内核在 worker 线程跑，主线程消费事件。单击 Ctrl+C = 优雅中断
        # （bridge 自动置 interrupt）；双击 = 放弃本轮并退出进程（内核可能卡死，
        # 进程死亡是唯一干净的边界，不存档防写入半截状态，/resume 可恢复上次存档）
        events, _control = run_in_thread(lambda c: session.chat(question, control=c))
        try:
            ui.consume(events)
            session.save()
        except KeyboardInterrupt:
            ui.warn("已强制中断并退出（本轮未存档）")
            ui.goodbye()
            break
        except Exception as e:
            session.rollback(mark)
            ui.error(f"{type(e).__name__}: {e}")


def main() -> None:
    session = ChatSession()
    set_status_provider(session.status_text)  # 管道模式输入区底部状态栏：模型 · token 累计
    ui.banner(MODEL, PROJECT_ROOT)

    if sys.stdin.isatty():
        import tui  # 延迟导入：管道模式不加载 Textual

        tui.run(session)
    else:
        _pipe_loop(session)


if __name__ == "__main__":
    main()
