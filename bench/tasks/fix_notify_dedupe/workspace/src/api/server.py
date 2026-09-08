"""应用装配：注册路由。"""
from src.api.auth import check_token
from src.api.routes.notify import NotifyHandler
from src.api.routes.templates import TemplateHandler
from src.api.routes.users import UserHandler


class Server:
    def __init__(self, notifier, templates, users, repository) -> None:
        self.notify = NotifyHandler(notifier)
        self.templates = TemplateHandler(templates)
        self.users = UserHandler(users)
        self.repository = repository

    def handle(self, path: str, method: str, auth: str, body=None):
        if not check_token(auth):
            return 401, {"error": "unauthorized"}
        if path == "/notify" and method == "POST":
            return self.notify.post(body or {})
        if path == "/templates" and method == "GET":
            return self.templates.list_all()
        if path == "/users" and method == "GET":
            return self.users.list_all()
        return 404, {"error": "not found"}
