"""健康检查路由（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("health")


def HealthHandler(*args, **kwargs):
    logger.debug("HealthHandler called")
    return None
