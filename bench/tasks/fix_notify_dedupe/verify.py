"""确定性判分：去重键必须区分渠道（email/push 各自独立去重）。"""
import sys

sys.path.insert(0, ".")

from src.worker.helpers import dedupe_key


# 1. 不同渠道同内容 → 必须不同 key（否则第二个渠道被吞）
assert dedupe_key("u1", "hash-a", "email") != dedupe_key("u1", "hash-a", "push"), \
    "email 与 push 应各自独立去重"

# 2. 同渠道同内容 → 同 key（窗口内去重仍生效）
assert dedupe_key("u1", "hash-a", "email") == dedupe_key("u1", "hash-a", "email")

# 3. 同渠道不同内容 → 不同 key
assert dedupe_key("u1", "hash-a", "email") != dedupe_key("u1", "hash-b", "email")

print("verify OK")
