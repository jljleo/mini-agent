"""线程桥：把同步生成器内核升级为队列驱动的双向通道。

事件出口（内核 → 消费者）：worker 线程消费 chat() 生成器，事件进 queue，
主线程阻塞读取——生产者/消费者解耦（斩断生成器的手拉手交替），
主线程得以在 agent 运行时响应用户输入。

指令入口（消费者 → 内核）：TurnControl 随 chat() 传入，内核在合法边界检查。

Ctrl+C 语义：主线程阻塞在 q.get() 时收到 KeyboardInterrupt，本模块替用户
"按下打断键"（置 interrupt 旗帜）并继续等待内核优雅收尾（补孤儿结果、
TurnEnd 随后到达）；短时间内第二次 Ctrl+C 才向上抛出（调用方决定硬退出）。
"""

import queue
import threading
from collections.abc import Callable, Iterator

from events import TurnControl

_DONE = object()  # 队列哨兵：worker 结束标记（无论正常结束还是异常）


def run_in_thread(factory: Callable[[TurnControl], Iterator]) -> tuple[Iterator, TurnControl]:
    """在 worker 线程里驱动事件生成器，返回 (事件迭代器, TurnControl)。

    factory: 接收 control、返回事件生成器的工厂（如 lambda c: session.chat(q, control=c)）。
    内核异常放进队列、在消费者侧原样 re-raise——调用方的 try/except 语义不变。
    """
    control = TurnControl()
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            for ev in factory(control):
                q.put(ev)
        except BaseException as e:  # 含 KeyboardInterrupt：异常也是事件流的一部分
            q.put(e)
        finally:
            q.put(_DONE)

    threading.Thread(target=worker, daemon=True).start()

    def events() -> Iterator:
        interrupts = 0
        while True:
            try:
                item = q.get()
                interrupts = 0
            except KeyboardInterrupt:
                # 主线程 Ctrl+C：消费者被中断 ≠ 流水线死亡。置旗帜让内核优雅收尾；
                # 连续第二次（内核卡住，如长 bash 未结束）才抛出，交给调用方硬退出
                interrupts += 1
                control.interrupt.set()
                if interrupts >= 2:
                    raise
                continue
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    return events(), control
