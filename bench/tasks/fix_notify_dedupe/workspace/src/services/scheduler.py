"""周期任务调度（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("scheduler")


def Scheduler(*args, **kwargs):
    logger.debug("Scheduler called")
    return None
