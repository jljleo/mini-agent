"""带重试的抓取器。"""

import time


class FetchError(Exception):
    """抓取最终失败。"""


def fetch_with_retry(fetch, attempts: int = 3, backoff: float = 0.5):
    """失败重试 attempts 次；全部失败抛 FetchError。"""
    last_error = None
    for i in range(attempts):
        try:
            return fetch()
        except Exception as e:  # noqa: BLE001 — 重试语义要求捕获一切
            last_error = e
            time.sleep(backoff * (2 ** i))
    raise FetchError(f"重试 {attempts} 次后仍失败") from last_error
