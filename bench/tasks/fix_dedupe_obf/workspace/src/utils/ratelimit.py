"""全局限流器（进程内实现）。"""
import time
import threading

from src.utils.logging import get_logger

logger = get_logger("ratelimit")


class RateLimiter:
    def __init__(self, limit: int = 100, window_s: int = 60) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._hits = [t for t in self._hits if now - t < self.window_s]
            if len(self._hits) >= self.limit:
                logger.warning("rate limit exceeded: %d in %ds", self.limit, self.window_s)
                return False
            self._hits.append(now)
            return True
