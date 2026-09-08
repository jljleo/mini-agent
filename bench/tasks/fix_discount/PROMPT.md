工作区是一个 Node.js 商城项目（`src/` 目录）：

- `src/lineItems.js`：行项目数据
- `src/fees.js`：费用与折扣计算
- `src/checkout.js`：结算流程，依赖 fees.js
- `src/index.js`：入口

用户投诉：结算页的折扣总是不对。顾客购买 100 元的商品：
- 普通会员（tier "normal")应得 5% 折扣 → 应支付 95
- 高级会员（tier "premium")应得 20% 折扣 → 应支付 80

但实际 premium 会员常常只拿到比 normal 少一点的折扣。请定位并修复。

要求：
- 两个档位的折扣是取其一，不是叠加（premium 应 20%，不是 5%+20% 叠加也不是 5% 后再打 20%）
- 不要修改函数签名和返回值的形状
- 不影响 non-member（无折扣）与 lineItems 的正常输出
