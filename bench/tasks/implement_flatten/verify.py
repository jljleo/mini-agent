import sys

sys.path.insert(0, ".")

from flatten import flatten

assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5], f"嵌套拍平错误: {flatten([1, [2, [3, 4]], 5])}"
assert flatten([]) == [], f"空列表应返回 []，实际 {flatten([])}"
assert flatten([[1], [2, [3]]]) == [1, 2, 3], f"多层嵌套错误: {flatten([[1], [2, [3]]])}"
assert flatten([1, 2, 3]) == [1, 2, 3], f"已平列表应原样返回: {flatten([1, 2, 3])}"

print("verify OK")
