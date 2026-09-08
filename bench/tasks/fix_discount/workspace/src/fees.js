// 费用与折扣。档位折扣应是「取其一」，见 RATES。
export const RATES = Object.freeze({ normal: 0.05, premium: 0.20 });

export function calcDiscount(tier) {
  if (!tier || !(tier in RATES)) return 0;
  // BUG: premium 在 normal 之上又叠加了一次，导致 premium 折扣≈24%（0.05 + 0.20*0.05 的叠加被错误实现）
  let rate = RATES[tier];
  if (tier === "premium") rate = RATES["normal"] + (1 - RATES["normal"]) * RATES["premium"];
  return rate;
}

export function applyDiscount(price, rate) {
  return Math.round(price * (1 - rate));
}
