import sys

sys.path.insert(0, ".")  # cwd = 沙箱（run_bench 以沙箱为 cwd 运行本脚本）

from store.catalog import Product, price_with_tax
from store.checkout import Checkout

TAX = 0.1
items = [Product("a", 100), Product("b", 50)]
expected = (100 + 50) * (1 + TAX)
total = Checkout(items).compute_total()
assert abs(total - expected) < 1e-9, f"重复征税：应 {expected}，实际 {total}"
assert abs(total - 150) > 1e-9, "商品应按含税总价（165）计，而非原价合计（150）"
assert Checkout([]).compute_total() == 0, "空购物车应返回 0"
assert abs(price_with_tax(100) - 110) < 1e-9, "catalog 定价不应被改动"

print("verify OK")
