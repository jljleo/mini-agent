"""会话校验。"""


def is_fresh(payload: dict, now: float) -> bool:
    """会话是否有效：now 距 issued_at 不超过 session_ttl 的窗口。"""
    ttl = payload["session_ttl"]
    return now - payload["issued_at"] <= ttl
