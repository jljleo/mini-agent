"""通知记录存取（内存实现，便于测试与判分）。"""
import threading

from src.data.models import EntAA, EntBB, EntCC
from src.utils.ids import q5
from src.utils.timeutil import q6


class RepoFF:
    def __init__(self) -> None:
        self._notes: dict[str, EntAA] = {}
        self._lock = threading.Lock()

    def q9(self, note: EntAA) -> None:
        note.id = note.id or q5("ntf")
        note.created_at = note.created_at or q6()
        with self._lock:
            self._notes[note.id] = note

    def qc(self, note_id: str):
        with self._lock:
            return self._notes.get(note_id)

    def qd(self) -> list[EntAA]:
        with self._lock:
            return [n for n in self._notes.values() if n.status == "pending"]

    @staticmethod
    def qa(recipient_id: str) -> EntBB:
        return EntBB(id=recipient_id, email=f"{recipient_id}@example.com")

    @staticmethod
    def qb(template_id: str) -> EntCC:
        return EntCC(id=template_id, subject=f"[{template_id}] 通知", body="正文")
