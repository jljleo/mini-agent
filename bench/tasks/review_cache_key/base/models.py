"""领域模型。"""

from dataclasses import dataclass


@dataclass
class Product:
    id: int
    name: str
    price_cents: int
