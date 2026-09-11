"""双索引注册表。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    id: int
    name: str


class Registry:
    """按 id 与按 name 双索引；两个索引必须原子更新。"""

    def __init__(self) -> None:
        self._by_id: dict[int, Item] = {}
        self._by_name: dict[str, Item] = {}

    def register(self, item: Item) -> None:
        self._by_id[item.id] = item
        self._by_name[item.name] = item

    def get_by_id(self, item_id: int) -> Item | None:
        return self._by_id.get(item_id)

    def get_by_name(self, name: str) -> Item | None:
        return self._by_name.get(name)
