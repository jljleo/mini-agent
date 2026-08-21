"""CLI 入口：只负责主循环，业务逻辑全部下沉到 agent / input_utils / ui。

运行：python main.py
退出：exit / quit / :q / /quit / Ctrl+C / Ctrl+D
"""

import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册
import commands  # noqa: F401  集中式注册：导入即触发 @command 注册

import ui
from agent import ChatSession
from command_registry import COMMANDS
from config import MODEL, PROJECT_ROOT, QUIT_COMMANDS
from input_utils import read_input, set_status_provider


def main() -> None:
    session = ChatSession()
    set_status_provider(session.status_text)  # 输入区底部状态栏：模型 · token 累计
    ui.banner(MODEL, PROJECT_ROOT)

    while True:
        try:
            question = read_input()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D (EOFError) 或 Ctrl+C (KeyboardInterrupt)：安全退出
            ui.goodbye()
            break

        if not question:
            continue
        if question.lower() in QUIT_COMMANDS:
            ui.goodbye()
            break

        name, _, args = question.partition(" ")
        if name in COMMANDS:
            COMMANDS[name](session, args.strip())  # handler 统一接收 (session, args)
            continue
        if question.startswith("/"):
            # 未注册的斜杠命令：拦截并提示，避免当提问发给模型白烧 token
            ui.warn(f"未知命令: {name}（输入 /help 查看可用命令）")
            continue

        mark = session.mark()  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        try:
            session.chat(question)
        except Exception as e:
            session.rollback(mark)
            ui.error(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
