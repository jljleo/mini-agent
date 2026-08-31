"""CLI 入口：主循环与运行调度，业务逻辑下沉到 agent / input_utils / ui / bridge。

运行：python main.py
退出：exit / quit / :q / /quit / Ctrl+C / Ctrl+D（运行中 Ctrl+C = 打断本轮，不退出）

两种形态：
- tty（_tui_loop）：常驻输入框（prompt_toolkit + patch_stdout）——agent 运行时
  输入框不消失：输入回车 = 追加消息（steering 队列，轮边界注入）；
  Esc / Ctrl+C = 立即打断（control.abort() 断流，不等下一个 chunk）；
  确认请求由输入框 y/n 按键应答（ApprovalChannel）
- 管道（_pipe_loop）：回合制读取 + 线程桥（bridge.py），无运行中交互
"""

import queue
import sys
import threading

import tools  # noqa: F401  集中式注册：导入即触发 @tool 注册
import commands  # noqa: F401  集中式注册：导入即触发 @command 注册

from prompt_toolkit.patch_stdout import patch_stdout

import ui
from agent import ChatSession
from bridge import run_in_thread
from command_registry import COMMANDS
from config import MODEL, PROJECT_ROOT, QUIT_COMMANDS
from events import TurnControl
from input_utils import (
    ApprovalChannel,
    abort_pending_approval,
    begin_run,
    current_control,
    end_run,
    is_running,
    read_input,
    set_approval_channel,
    set_status_provider,
    take_prefill,
)


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


# ---- tty：常驻输入框 ----


def _run_turn(session: ChatSession, question: str, control: TurnControl) -> None:
    """运行线程：驱动内核事件流并渲染；成功存档、失败回滚、steering 余量回填。"""
    mark = session.mark()
    try:
        # live=False：patch_stdout 下禁用 Live 光标重绘，正文按块落卷
        ui.consume(session.chat(question, control=control), live=False)
        session.save()  # 每轮成功（含优雅中断收尾）后自动存档
    except Exception as e:
        session.rollback(mark)
        ui.error(f"{type(e).__name__}: {e}")
    finally:
        # 本轮没等到注入时机的 steering（如模型一轮就出终稿）：存为下轮回填，
        # 不吞用户输入（与未知命令报错回填同一约定）
        leftover = []
        while True:
            try:
                leftover.append(control.steer.get_nowait())
            except queue.Empty:
                break
        end_run(leftover)


def _tui_loop(session: ChatSession) -> None:
    set_approval_channel(ApprovalChannel())
    prefill = ""
    # patch_stdout：运行线程的输出被抬升到常驻输入框上方
    with patch_stdout(raw=True):
        while True:
            prefill = prefill or take_prefill()
            try:
                question, forced = read_input(prefill=prefill)
            except KeyboardInterrupt:
                # Ctrl+C：运行中 = 立即打断本轮（断流 + 联动拒绝待确认）；空闲 = 退出
                control = current_control()
                if control is not None:
                    control.abort()
                    abort_pending_approval()
                    continue
                ui.goodbye()
                break
            except EOFError:  # Ctrl+D
                ui.goodbye()
                break
            prefill = ""

            if not question:
                continue
            if is_running():
                # 运行中：一切输入都按追加消息处理（此时执行命令太危险——
                # /clear 之类会拆运行中的会话）
                current_control().steer.put(question)
                ui.note(f"❯ {question}（已排队，将在当前步骤完成后注入）", tag="steer")
                continue

            verdict = _dispatch_command(session, question, forced)
            if verdict == "quit":
                break
            if verdict is True:
                continue
            if verdict == "prefill":
                prefill = question  # 报错但不清空：回填原文，用户修正后重发
                continue

            control = TurnControl()
            begin_run(control)  # 先登记运行态再启动线程，防时序窗口
            threading.Thread(
                target=_run_turn, args=(session, question, control), daemon=True,
            ).start()


# ---- 管道：回合制 + 线程桥 ----


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
    set_status_provider(session.status_text)  # 输入区底部状态栏：模型 · token 累计
    ui.banner(MODEL, PROJECT_ROOT)

    if sys.stdin.isatty():
        _tui_loop(session)
    else:
        _pipe_loop(session)


if __name__ == "__main__":
    main()
