"""确定性判分：用 node 以 ESM 导入 fee 逻辑，断言折扣行为（取其一，非叠加）。"""
import os
import subprocess
import sys

probe = r'''
import { calcDiscount, applyDiscount } from "./src/fees.js";
const checks = [
  [null, 100, 100],       // 无会员：不打折
  ["normal", 100, 95],    // 5%
  ["premium", 100, 80],   // 20%，不是 24% 也不是叠加
];
for (const [tier, price, want] of checks) {
  const got = applyDiscount(price, calcDiscount(tier));
  if (got !== want) { console.error(`tier=${tier} price=${price}: want ${want}, got ${got}`); process.exit(1); }
}
console.log("verify OK");
'''

proc = subprocess.run(
    ["node", "--input-type=module", "-e", probe],
    cwd=os.getcwd(), capture_output=True, text=True, timeout=30,
)
if proc.returncode != 0:
    print(proc.stderr[-1500:])
    sys.exit(1)
print(proc.stdout.strip())
