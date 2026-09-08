"""通知流水线：拼模板 → 各渠道发送 → 记录。"""
from src.data.models import Channel, Notification, Recipient, Template
from src.data.repository import Repository
from src.services.event_bus import EventBus
from src.utils.ids import make_id
from src.utils.logging import get_logger
from src.worker.helpers import dedupe_key, should_dedupe

logger = get_logger("notifier")


class Notifier:
    """dispatch 是入口：对 (recipient, template, channel) 做一次发送尝试。"""

    def __init__(self, repository: Repository, bus: EventBus,
                 dedupe_client=None, window_s: int = 300) -> None:
        self.repository = repository
        self.bus = bus
        self.dedupe_client = dedupe_client
        self.window_s = window_s

    def dispatch(self, recipient_id: str, template_id: str, channel: object) -> dict:
        recipient = self.repository.get_recipient(recipient_id)
        template = self.repository.get_template(template_id)
        content_hash = self._content_hash(recipient, template, channel)
        key = dedupe_key(recipient_id, content_hash, channel.value)
        if self.dedupe_client is not None and should_dedupe(self.dedupe_client, key, self.window_s):
            logger.info("dedupe hit: %s", key)
            return {"skipped": "duplicate"}
        note = Notification(
            id=make_id("ntf"),
            recipient_id=recipient.id,
            template_id=template.id,
            channel=Channel(channel),
            content_hash=content_hash,
        )
        self.repository.save_notification(note)
        self.bus.publish("notification.created", {"id": note.id})
        return {"sent": True, "id": note.id}

    @staticmethod
    def _content_hash(recipient: Recipient, template: Template, channel: object) -> str:
        # 内容指纹：同一 (recipient, template, channel) 视为同一内容
        return f"{recipient.id}:{template.id}:{channel.value}"
