工作区是一个小型商城项目，多文件结构：

- `store/catalog.py`：商品模型与定价，`price_with_tax(price)` 已返回含 10% 税的价格
- `store/checkout.py`：`Checkout` 结算类
- `main.py`：命令行入口

用户投诉：结算金额总是偏贵。请定位问题并修复——问题出在 `Checkout.compute_total`：
它先用含税单价汇总，随后对合计又乘了一次税率，税被重复计算。

要求：
- `compute_total` 应返回「商品原价合计 × (1 + TAX_RATE)」这一遍税的结果
- 不要修改任何函数签名，不要改动 `store/catalog.py` 的定价逻辑
- 保持其他行为不变（空购物车应返回 0）
