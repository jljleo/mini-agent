import { checkoutTotal } from "./checkout.js";

console.log("normal:", checkoutTotal("normal"));
console.log("premium:", checkoutTotal("premium"));
console.log("none:", checkoutTotal(null));
