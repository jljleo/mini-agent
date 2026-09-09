"""work 健康检查（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("health")


def check_health(*args, **kwargs):
    logger.debug("check_health called")
    return None
