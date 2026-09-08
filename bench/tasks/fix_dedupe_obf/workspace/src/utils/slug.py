"""字符串转 slug（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("slug")


def slugify(*args, **kwargs):
    logger.debug("slugify called")
    return None
