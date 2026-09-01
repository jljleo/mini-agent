import sys

sys.path.insert(0, ".")

from binary_search import binary_search

arr = [1, 3, 5, 7, 9, 11]
assert binary_search(arr, 5) == 2, f"查找 5 应返回 2，实际 {binary_search(arr, 5)}"
assert binary_search(arr, 1) == 0, f"查找 1 应返回 0，实际 {binary_search(arr, 1)}"
assert binary_search(arr, 11) == 5, f"查找 11 应返回 5，实际 {binary_search(arr, 11)}"
assert binary_search(arr, 3) == 1, f"查找 3 应返回 1，实际 {binary_search(arr, 3)}"
assert binary_search(arr, 100) == -1, f"不存在应返回 -1，实际 {binary_search(arr, 100)}"

print("verify OK")
