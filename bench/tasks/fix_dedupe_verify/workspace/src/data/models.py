"""领域模型。"""
from dataclasses import dataclass, field
from enum import Enum


class EntDD(str, Enum):
    EMAIL = "email"
    PUSH = "push"
    SMS = "sms"


class EntEE(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class EntBB:
    id: str
    email: str = ""
    device_token: str = ""
    phone: str = ""


@dataclass
class EntCC:
    id: str
    subject: str
    body: str


@dataclass
class EntAA:
    id: str
    recipient_id: str
    template_id: str
    channel: EntDD
    content_hash: str
    status: EntEE = EntEE.PENDING
    attempts: int = 0
    created_at: int = 0
    variants: dict = field(default_factory=dict)
