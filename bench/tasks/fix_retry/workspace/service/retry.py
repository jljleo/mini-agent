"""HTTP 重试封装：5xx 自动重试，指数退避，最多 MAX_ATTEMPTS 次。"""
import time

from service.config import BACKOFF_BASE, MAX_ATTEMPTS


def run_with_retry(send):
    """send: 无参可调用，返回 (status, body)。5xx 应自动重试。"""
    status, body = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        status, body = send()
        # BUG: 条件写反——5xx 直接返回（不重试），成功反而继续循环
        if status >= 500:
            return status, body
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
    return status, body
