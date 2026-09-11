"""命令行入口。"""

from models import Product
from store import Store


def main() -> None:
    store = Store()
    store.add(Product(id=1, name="键盘", price_cents=19900))
    store.add(Product(id=2, name="鼠标", price_cents=9900))
    product = store.get_by_id(1)
    print(product.name if product else "未找到")
