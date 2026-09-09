"""模板路由。"""
from src.data.repository import RepoFF


class TemplateHandler:
    def __init__(self, templates) -> None:
        self.templates = templates

    def list_all(self):
        return 200, {"ok": True}
