"""入口：演示 run_with_retry 的用法。"""
from service.retry import run_with_retry


def fake_send():
    return 200, "ok"


if __name__ == "__main__":
    print(run_with_retry(fake_send))
