"""POST /notify：接收通知请求并进入流水线。"""
from src.utils.logging import get_logger

logger = get_logger("routes.notify")


class NotifyHandler:
    def __init__(self, notifier) -> None:
        self.notifier = notifier

    def post(self, body: dict) -> tuple[int, dict]:
        recipient_id = body.get("recipient_id")
        template_id = body.get("template_id")
        channels = body.get("channels") or ["email"]
        if not recipient_id or not template_id:
            return 400, {"error": "missing recipient_id/template_id"}
        results = [
            self.notifier.dispatch(recipient_id, template_id, ch)
            for ch in channels
        ]
        return 200, {"dispatched": results}
