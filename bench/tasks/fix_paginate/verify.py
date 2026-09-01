import sys

sys.path.insert(0, ".")  # cwd = 沙箱（run_bench 以沙箱为 cwd 运行本脚本）

from paginate import paginate

items = list(range(10))

assert paginate(items, 1, 3) == [0, 1, 2], f"page=1 应返回前 3 个元素，实际 {paginate(items, 1, 3)}"
assert paginate(items, 2, 3) == [3, 4, 5], f"page=2 应返回 [3,4,5]，实际 {paginate(items, 2, 3)}"
assert paginate(items, 4, 3) == [9], f"最后一页不完整应返回 [9]，实际 {paginate(items, 4, 3)}"
assert paginate(items, 99, 3) == [], f"越界页应返回空列表，实际 {paginate(items, 99, 3)}"
assert paginate([], 1, 3) == [], f"空列表应返回空列表，实际 {paginate([], 1, 3)}"

print("verify OK")
