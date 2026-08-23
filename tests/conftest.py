"""pytest 共享基础设施。

- sys.path 插入项目根：tests/ 不是包，pytest 默认只把测试文件所在目录加进
  sys.path，被测模块（compact/tools/agent...）在项目根，需显式引入。
- reset_chars_per_token：compact 的估算系数是模块级全局且会被 calibrate 漂移，
  每个测试前重置回初值 2.0，防测试间互相污染（顺序敏感是测试体系的慢性病）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compact  # noqa: E402


@pytest.fixture(autouse=True)
def reset_chars_per_token():
    compact._chars_per_token = 2.0
    yield


@pytest.fixture
def session(monkeypatch, tmp_path):
    """隔离的 ChatSession：假 API key（构造 client 用，不发请求）、存档指向 tmp。"""
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    import agent
    monkeypatch.setattr(agent, "SESSION_FILE", str(tmp_path / "session.json"))
    return agent.ChatSession()
