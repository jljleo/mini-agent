"""会话令牌签发。会话有效期默认 1 小时。"""

MAX_TTL_SECONDS = 60 * 60


def issue_session(ttl_seconds: int = MAX_TTL_SECONDS) -> dict:
    """签发会话载荷；session_ttl 字段单位：秒，issued_at 单位：秒（epoch）。"""
    return {"issued_at": 0.0, "session_ttl": ttl_seconds}
