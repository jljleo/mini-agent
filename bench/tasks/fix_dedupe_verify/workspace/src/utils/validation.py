"""输入校验。"""
import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr))


def validate_phone(num: str) -> bool:
    return num.isdigit() and len(num) >= 6
