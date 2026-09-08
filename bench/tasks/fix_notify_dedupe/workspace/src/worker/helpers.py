"""worker 通用工具：发送去重（同内容同渠道在窗口内只发一次）。"""


def dedupe_key(recipient_id: str, content_hash: str, provider: str) -> str:
    """去重键应区分渠道：email 与 push 各自独立去重。

    引用方（src/services/notifier.py）保证 content_hash 已含 recipient+template+channel。
    """
    # BUG: 去重键漏掉了 provider——email 先发后，push 同内容被误判「已发」而吞掉
    return f"notif:{recipient_id}:{content_hash}"


def should_dedupe(client, key: str, window_s: int) -> bool:
    """client: 提供 exists(key) 与 set(key, ttl) 的存储（判分用不上，走内存）。"""
    if client is None:
        return False
    if client.exists(key):
        return True
    client.set(key, 1, ttl=window_s)
    return False
