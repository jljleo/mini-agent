import sys

sys.path.insert(0, ".")

from calc import add, safe_divide

assert safe_divide(10, 2) == 5
assert safe_divide(1, 0) is None
assert safe_divide(7, 2) == 3.5
assert add(1, 2) == 3  # add 不得被改坏
print("verify OK")
