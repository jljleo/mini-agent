"""确定性判分：折扣语义正确 + 文件物理形态未破坏（仍是 CRLF）。"""
import sys

sys.path.insert(0, ".")

from pricing import apply_discount

assert apply_discount(100, 10) == 90, f"打九折应 90，实际 {apply_discount(100, 10)}"
assert apply_discount(200, 25) == 150, f"七五折应 150，实际 {apply_discount(200, 25)}"
assert apply_discount(50, 0) == 50, "不打折应原价"
assert apply_discount(100, 100) == 0, "全额折应 0"

# 文件必须保持 CRLF（编辑不能把仓库的换行风格偷偷改成 LF）
data = open("pricing.py", "rb").read()
assert b"\r\n" in data, "文件应保持 CRLF——编辑不应破坏行尾风格"

print("verify OK")
