"""ID 生成。"""
import uuid


def make_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:16]
    return f"{prefix}-{uid}" if prefix else uid
