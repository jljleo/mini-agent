"""CLI 入口：只负责主循环，业务逻辑全部下沉到 agent / input_utils。

运行：python main.py
退出：exit / quit / :q / Ctrl+C / Ctrl+D
"""

from agent import ChatSession
from input_utils import read_input

QUIT_COMMANDS = ("exit", "quit", ":q")


def main() -> None:
    session = ChatSession()

    while True:
        try:
            question = read_input("Input your question (exit/quit to quit): ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D (EOFError) 或 Ctrl+C (KeyboardInterrupt)：安全退出
            print("\nBye!")
            break

        if not question:
            continue
        if question.lower() in QUIT_COMMANDS:
            print("Bye!")
            break

        mark = session.mark()  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        try:
            session.chat(question)
        except Exception as e:
            session.rollback(mark)
            print(f"[Error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
