"""确定性判分：去重键必须区分渠道（email/push 各自独立去重）。
注意：本源符号名经过混淆（无意义标识符），判分只依赖行为。"""
import sys

sys.path.insert(0, ".")

from src.worker.helpers import q1


# 1. 不同渠道同内容 → 必须不同 key
assert q1("u1", "hash-a", "email") != q1("u1", "hash-a", "push"), \
    "email 与 push 应各自独立去重"

# 2. 同渠道同内容 → 同 key
assert q1("u1", "hash-a", "email") == q1("u1", "hash-a", "email")

# 3. 同渠道不同内容 → 不同 key
assert q1("u1", "hash-a", "email") != q1("u1", "hash-b", "email")

print("verify OK")
