"""序列化工具（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("serializer")


def dumps(*args, **kwargs):
    logger.debug("dumps called")
    return None
