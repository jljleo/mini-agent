"""文件读写工具（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("files")


def read_json(*args, **kwargs):
    logger.debug("read_json called")
    return None
