"""外部 webhook 渠道（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("webhook")


def WebhookProvider(*args, **kwargs):
    logger.debug("WebhookProvider called")
    return None
