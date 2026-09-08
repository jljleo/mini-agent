"""webhook 路由（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("webhooks")


def WebhookHandler(*args, **kwargs):
    logger.debug("WebhookHandler called")
    return None
