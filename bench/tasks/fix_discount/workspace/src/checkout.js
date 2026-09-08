import { applyDiscount, calcDiscount } from "./fees.js";
import { items } from "./lineItems.js";

export function checkoutTotal(tier) {
  return items().reduce((sum, it) => sum + applyDiscount(it.price * it.qty, calcDiscount(tier)), 0);
}
