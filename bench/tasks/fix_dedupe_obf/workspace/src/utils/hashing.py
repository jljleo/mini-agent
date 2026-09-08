"""哈希工具（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("hashing")


def sha256_short(*args, **kwargs):
    logger.debug("sha256_short called")
    return None
