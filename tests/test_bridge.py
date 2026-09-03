"""bridge.py 回归测试：线程桥的事件保序、异常传播与 Ctrl+C 打断语义。

bridge 是内核生成器与主线程之间的队列通道——它的契约是：
事件一个不少、顺序不乱、异常原样传播、Ctrl+C 自动转为 interrupt 旗帜。
"""

import queue

import pytest

from bridge import run_in_thread
from events import Note, TurnEnd


class TestEventDelivery:
    def test_events_delivered_in_order(self):
        """事件一个不少、顺序不乱（队列通道的基本契约）。"""
        def factory(control):
            yield Note("一")
            yield Note("二")
            yield TurnEnd()

        events, _ = run_in_thread(factory)
        got = list(events)
        assert [e.message for e in got if isinstance(e, Note)] == ["一", "二"]
        assert isinstance(got[-1], TurnEnd)

    def test_kernel_exception_propagates_to_consumer(self):
        """内核异常在消费者侧原样 re-raise——调用方的 try/except 语义不变。"""
        def factory(control):
            yield Note("出事前")
            raise ValueError("boom")

        events, _ = run_in_thread(factory)
        try:
            list(events)
            pytest.fail("异常应传播到消费者")
        except ValueError as e:
            assert "boom" in str(e)


class TestCtrlCBecomesInterrupt:
    def test_first_ctrl_c_sets_interrupt_and_continues(self, monkeypatch):
        """单击 Ctrl+C：自动置 interrupt 旗帜，流水线继续等内核优雅收尾。"""
        real_get = queue.Queue.get
        state = {"raised": False}

        def flaky_get(self, *args, **kwargs):
            if not state["raised"]:
                state["raised"] = True
                raise KeyboardInterrupt  # 模拟主线程阻塞在 q.get 时收到 Ctrl+C
            return real_get(self, *args, **kwargs)

        monkeypatch.setattr(queue.Queue, "get", flaky_get)

        def factory(control):
            # 内核模拟：下一检查点看到 interrupt 就优雅收尾
            while not control.interrupt.is_set():
                yield Note("工作中")
            yield Note("已收尾")
            yield TurnEnd()

        events, control = run_in_thread(factory)
        got = list(events)  # 不抛 KeyboardInterrupt——已被 bridge 吸收为 interrupt
        assert control.interrupt.is_set()
        assert any(isinstance(e, Note) and e.message == "已收尾" for e in got)
        assert isinstance(got[-1], TurnEnd)

    def test_second_ctrl_c_raises(self, monkeypatch):
        """双击 Ctrl+C：内核卡住不收尾时，向上抛出（调用方决定硬退出）。"""
        monkeypatch.setattr(queue.Queue, "get",
                            lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

        def factory(control):
            yield Note("永远等不到我")

        events, control = run_in_thread(factory)
        try:
            next(events)
            pytest.fail("双击 Ctrl+C 应抛出 KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
        assert control.interrupt.is_set()
