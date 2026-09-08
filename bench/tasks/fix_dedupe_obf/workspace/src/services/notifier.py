"""通知流水线：拼模板 → 各渠道发送 → 记录。"""
from src.data.models import EntDD, EntAA, EntBB, EntCC
from src.data.repository import RepoFF
from src.services.event_bus import BusHH
from src.utils.ids import q5
from src.utils.logging import get_logger
from src.worker.helpers import q1, q2

logger = get_logger("notifier")


class SvcGG:
    """q3 是入口：对 (recipient, template, channel) 做一次发送尝试。"""

    def __init__(self, repository: RepoFF, bus: BusHH,
                 dedupe_client=None, window_s: int = 300) -> None:
        self.repository = repository
        self.bus = bus
        self.dedupe_client = dedupe_client
        self.window_s = window_s

    def q3(self, recipient_id: str, template_id: str, channel: object) -> dict:
        recipient = self.repository.qa(recipient_id)
        template = self.repository.qb(template_id)
        content_hash = self.q4(recipient, template, channel)
        key = q1(recipient_id, content_hash, channel.value)
        if self.dedupe_client is not None and q2(self.dedupe_client, key, self.window_s):
            logger.info("dedupe hit: %s", key)
            return {"skipped": "duplicate"}
        note = EntAA(
            id=q5("ntf"),
            recipient_id=recipient.id,
            template_id=template.id,
            channel=EntDD(channel),
            content_hash=content_hash,
        )
        self.repository.q9(note)
        self.bus.q7("notification.created", {"id": note.id})
        return {"sent": True, "id": note.id}

    @staticmethod
    def q4(recipient: EntBB, template: EntCC, channel: object) -> str:
        # 内容指纹：同一 (recipient, template, channel) 视为同一内容
        return f"{recipient.id}:{template.id}:{channel.value}"
