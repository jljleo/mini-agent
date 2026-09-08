"""通知记录存取（内存实现，便于测试与判分）。"""
import threading

from src.data.models import Notification, Recipient, Template
from src.utils.ids import make_id
from src.utils.timeutil import now_ts


class Repository:
    def __init__(self) -> None:
        self._notes: dict[str, Notification] = {}
        self._lock = threading.Lock()

    def save_notification(self, note: Notification) -> None:
        note.id = note.id or make_id("ntf")
        note.created_at = note.created_at or now_ts()
        with self._lock:
            self._notes[note.id] = note

    def get_notification(self, note_id: str):
        with self._lock:
            return self._notes.get(note_id)

    def list_pending(self) -> list[Notification]:
        with self._lock:
            return [n for n in self._notes.values() if n.status == "pending"]

    @staticmethod
    def get_recipient(recipient_id: str) -> Recipient:
        return Recipient(id=recipient_id, email=f"{recipient_id}@example.com")

    @staticmethod
    def get_template(template_id: str) -> Template:
        return Template(id=template_id, subject=f"[{template_id}] 通知", body="正文")
