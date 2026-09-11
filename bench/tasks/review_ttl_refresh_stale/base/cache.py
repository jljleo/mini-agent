"""带 TTL 的内存缓存。"""

import time


class TTLCache:
    """键值缓存：条目带过期时间，get 时惰性失效。"""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self._ttl_s = ttl_s
        self._data: dict[str, tuple[str, float]] = {}

    def set(self, key: str, value: str) -> None:
        self._data[key] = (value, time.monotonic() + self._ttl_s)

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value
