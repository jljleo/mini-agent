"""鉴权中间件（占位实现）。"""
from src.utils.logging import get_logger

logger = get_logger("auth")

VALID_TOKENS = {"t-1", "t-2"}


def check_token(authorization: str) -> bool:
    token = authorization.removeprefix("Bearer ").strip()
    ok = token in VALID_TOKENS
    if not ok:
        logger.warning("invalid token: %r", token)
    return ok
