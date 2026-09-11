"""滑动窗口限流。"""

import time


class RateLimiter:
    """按用户限流：滑动窗口内最多 limit 次请求。"""

    def __init__(self, limit: int = 100, window_s: int = 60) -> None:
        self._limit = limit
        self._window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, user: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(user, [])
        while hits and hits[0] <= now - self._window_s:
            hits.pop(0)
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True
