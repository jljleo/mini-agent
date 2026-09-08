"""审计流水（平台基础设施）。"""
from src.utils.logging import get_logger

logger = get_logger("audit")


def AuditLog(*args, **kwargs):
    logger.debug("AuditLog called")
    return None
