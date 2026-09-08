"""确定性判分：5xx 重试到成功 / 成功不重试 / 耗尽返回最后结果。"""
import sys

sys.path.insert(0, ".")

from service.retry import run_with_retry

# 1. 前两次 5xx → 应重试到第 3 次成功（共 3 次调用）
calls = []
def flaky():
    calls.append(1)
    return (500, "err") if len(calls) < 3 else (200, "ok")

status, body = run_with_retry(flaky)
assert status == 200 and body == "ok", f"5xx 应重试到成功，得到 {status} {body}"
assert len(calls) == 3, f"应恰好调用 3 次，实际 {len(calls)}"


# 2. 成功路径：只调用一次，绝不重试
calls2 = []
def ok():
    calls2.append(1)
    return (200, "ok")

run_with_retry(ok)
assert len(calls2) == 1, f"非 5xx 不应重试，实际调用 {len(calls2)} 次"


# 3. 全部 5xx：耗尽后返回最后一次结果（不崩溃、不无限循环）
def always_500():
    return (503, "down")

status, _ = run_with_retry(always_500)
assert status == 503

print("verify OK")
