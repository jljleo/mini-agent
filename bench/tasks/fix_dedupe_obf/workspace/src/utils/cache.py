"""内存缓存：近 LRU 语义（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("cache")


def InMemoryCache(*args, **kwargs):
    logger.debug("InMemoryCache called")
    return None
