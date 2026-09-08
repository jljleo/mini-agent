"""进程内事件总线。"""
import threading
from typing import Callable

from src.utils.logging import get_logger

logger = get_logger("event_bus")


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[str, dict], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def publish(self, topic: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(topic, payload)
            except Exception:
                logger.exception("subscriber failed on %s", topic)
