工作区是一个小型 Python 服务（`service/` 包）：

- `service/config.py`：重试配置
- `service/http_client.py`：底层 HTTP 请求（返回 (status, body)）
- `service/retry.py`：提供 run_with_retry，用于对 5xx 响应自动重试
- `service/run.py`：入口

线上反馈：调用方拿到 5xx 时，agent 应该自动重试到成功或耗尽次数，但实际表现是：
1. 5xx 一到手就返回，从不重试
2. 成功的响应反而被反复调用（浪费请求）

请定位并修复 `run_with_retry`。要求：
- 5xx（status >= 500）应自动重试，指数退避，最多 MAX_ATTEMPTS 次
- 非 5xx（status < 500）应立即返回，绝不重试
- 不要修改任何函数签名与配置
