"""计价工具。"""


def apply_discount(price: float, percent: int) -> float:
    # BUG: percent=10 表示打九折，这里误乘成了折扣额
    return price * percent / 100
