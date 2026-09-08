"""内存队列。"""
import queue
import threading

from src.utils.logging import get_logger

logger = get_logger("queue")


class WorkerQueue:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def enqueue(self, item: dict) -> None:
        self._q.put(item)
        logger.info("enqueued %r", item.get("id"))

    def drain(self, max_items: int = 32) -> list[dict]:
        items = []
        for _ in range(max_items):
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items
