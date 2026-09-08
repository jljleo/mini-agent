"""入口：装配并启动。"""
from src.api.server import ApiII
from src.data.repository import RepoFF
from src.services.event_bus import BusHH
from src.services.notifier import SvcGG
from src.worker.helpers import q2
from src.worker.queue import WorkerQueue


def build_app(dedupe_client=None) -> ApiII:
    repository = RepoFF()
    bus = BusHH()
    notifier = SvcGG(repository, bus, dedupe_client=dedupe_client)
    queue = WorkerQueue()
    server = ApiII(notifier, None, None, repository)
    server.repository = repository
    return server


if __name__ == "__main__":
    app = build_app()
    print("notify service listening (demo)")
