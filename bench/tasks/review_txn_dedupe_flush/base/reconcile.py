"""支付回调对账。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    amount_cents: int


def reconcile(payment: Payment, ledger: dict[str, int]) -> None:
    """单笔回调即时入账：同一订单多笔支付叠加。"""
    ledger[payment.order_id] = ledger.get(payment.order_id, 0) + payment.amount_cents
