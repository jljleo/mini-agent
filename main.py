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

    prefill = ""  # 未知命令报错后的回填文本（Codex 式：不吞用户输入）
    while True:
        try:
            question, forced = read_input(prefill=prefill)
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D (EOFError) 或 Ctrl+C (KeyboardInterrupt)：安全退出
            ui.goodbye()
            break
        prefill = ""

        if not question:
            continue
        # 前导空格 = 显式逃逸：跳过一切命令分发，强制按消息发给模型
        if not forced and question.lower() in QUIT_COMMANDS:
            ui.goodbye()
            break

        name, _, args = question.partition(" ")
        if not forced and name in COMMANDS:
            COMMANDS[name](session, args.strip())  # handler 统一接收 (session, args)
            continue
        # 未知斜杠命令拦截——但只在“看起来真的是命令”时：命令名是单个词（/help），
        # 首个 token 内含其他 / 的是绝对路径（/Users/x.py 提问），应放行给模型
        if not forced and question.startswith("/") and "/" not in name[1:]:
            ui.warn(f"未知命令: {name}（输入 /help 查看可用命令）")
            prefill = question  # 报错但不清空：回填原文，用户修正后重发
            continue

        mark = session.mark()  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        try:
            ui.consume(session.chat(question))  # 内核产出事件流，终端消费渲染
            session.save()  # 每轮成功后自动存档：崩溃也不丢进度（/resume 恢复）
        except Exception as e:
            session.rollback(mark)
            ui.error(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
