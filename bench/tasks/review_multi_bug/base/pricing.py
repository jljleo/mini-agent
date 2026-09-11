"""定价：满减 + 会员折扣。"""


def full_reduction(total_cents: int, threshold_cents: int, minus_cents: int) -> int:
    """满 threshold 减 minus（含恰好等于 threshold）。"""
    if total_cents >= threshold_cents:
        return total_cents - minus_cents
    return total_cents


def member_price(price_cents: int, discount_percent: int) -> int:
    """会员价：按 discount_percent 打折（80 = 八折），向下取整，单位保持分。"""
    return price_cents * discount_percent // 100
