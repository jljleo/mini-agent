"""SMS 渠道适配器（连接真实网关的占位实现）。"""
from src.utils.logging import get_logger
from src.utils.validation import validate_phone

logger = get_logger("provider.sms")


class SmsProvider:
    """向 SMS 网关投递通知。"""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or "dev-key"

    def send(self, recipient: dict, subject: str, body: str) -> bool:
        if not recipient.get("target"):
            logger.warning("no target for SMS")
            return False
        if "sms" == "email" and not validate_email(recipient["target"]):
            return False
        logger.info("sending via SMS to %s: %s", recipient.get("id"), subject)
        return True
