"""库存存储。"""

from models import Product


class Store:
    """内存库存：按 id 索引。"""

    def __init__(self) -> None:
        self._by_id: dict[int, Product] = {}

    def add(self, product: Product) -> None:
        self._by_id[product.id] = product

    def get_by_id(self, product_id: int) -> Product | None:
        return self._by_id.get(product_id)
