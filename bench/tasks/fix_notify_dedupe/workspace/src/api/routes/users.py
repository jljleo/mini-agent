"""用户路由。"""
from src.data.repository import Repository


class UserHandler:
    def __init__(self, users) -> None:
        self.users = users

    def list_all(self):
        return 200, {"ok": True}
