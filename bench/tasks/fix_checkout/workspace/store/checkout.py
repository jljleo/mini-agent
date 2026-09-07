"""结算。"""

from store.catalog import TAX_RATE, price_with_tax


class Checkout:
    def __init__(self, items: list) -> None:
        self.items = items

    def compute_total(self) -> float:
        # BUG：每个商品已按含税价计，合计又乘一次税率 → 重复征税
        subtotal = sum(price_with_tax(i.price) for i in self.items)
        return subtotal * (1 + TAX_RATE)
