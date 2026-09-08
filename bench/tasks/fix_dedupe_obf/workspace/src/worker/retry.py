"""指数退避重试装饰器。"""
import time

from src.utils.logging import get_logger

logger = get_logger("retry")


def with_retry(attempts: int = 3, backoff_s: float = 0.05):
    def deco(fn):
        def wrapper(*args, **kwargs):
            last = None
            for i in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    if i < attempts:
                        time.sleep(backoff_s * (2 ** (i - 1)))
            raise last
        return wrapper
    return deco
