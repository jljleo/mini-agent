"""配置加载：环境变量覆盖默认值。"""


def get_timeout(config: dict) -> int:
    """超时秒数：0 表示不超时（合法配置），缺省 30。"""
    value = config.get("timeout")
    if value is None:
        return 30
    return value


def get_retries(config: dict) -> int:
    """重试次数，缺省 3。"""
    value = config.get("retries")
    if value is None:
        return 3
    return value
