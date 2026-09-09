"""指标收集（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("metrics")


def Metrics(*args, **kwargs):
    logger.debug("Metrics called")
    return None
