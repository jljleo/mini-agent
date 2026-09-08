"""领域模型。"""
from dataclasses import dataclass, field
from enum import Enum


class Channel(str, Enum):
    EMAIL = "email"
    PUSH = "push"
    SMS = "sms"


class Status(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class Recipient:
    id: str
    email: str = ""
    device_token: str = ""
    phone: str = ""


@dataclass
class Template:
    id: str
    subject: str
    body: str


@dataclass
class Notification:
    id: str
    recipient_id: str
    template_id: str
    channel: Channel
    content_hash: str
    status: Status = Status.PENDING
    attempts: int = 0
    created_at: int = 0
    variants: dict = field(default_factory=dict)
