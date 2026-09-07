"""商品与定价。price_with_tax 已含税，调用方不应再额外计税。"""

TAX_RATE = 0.1


class Product:
    def __init__(self, name: str, price: float) -> None:
        self.name = name
        self.price = price


def price_with_tax(price: float) -> float:
    return price * (1 + TAX_RATE)
