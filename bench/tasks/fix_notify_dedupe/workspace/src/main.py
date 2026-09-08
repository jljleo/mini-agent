"""入口：装配并启动。"""
from src.api.server import Server
from src.data.repository import Repository
from src.services.event_bus import EventBus
from src.services.notifier import Notifier
from src.worker.helpers import should_dedupe
from src.worker.queue import WorkerQueue


def build_app(dedupe_client=None) -> Server:
    repository = Repository()
    bus = EventBus()
    notifier = Notifier(repository, bus, dedupe_client=dedupe_client)
    queue = WorkerQueue()
    server = Server(notifier, None, None, repository)
    server.repository = repository
    return server


if __name__ == "__main__":
    app = build_app()
    print("notify service listening (demo)")
