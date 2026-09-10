"""pytest 共享基础设施。

- sys.path 插入项目根：tests/ 不是包，pytest 默认只把测试文件所在目录加进
  sys.path，被测模块（compact/tools/agent...）在项目根，需显式引入。
- reset_chars_per_token：compact 的估算系数是模块级全局且会被 calibrate 漂移，
  每个测试前重置回初值 2.0，防测试间互相污染（顺序敏感是测试体系的慢性病）。
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_agent.kernel.compact as compact  # noqa: E402


class _GuardedSocket(socket.socket):
    """禁网哨兵：connect/connect_ex 抛错，其余行为不变。"""

    def connect(self, *args, **kwargs):
        raise RuntimeError("测试禁止真实网络请求（是否漏打桩 client.create？）")

    def connect_ex(self, *args, **kwargs):
        raise RuntimeError("测试禁止真实网络请求（是否漏打桩 client.create？）")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """制度化零网络：socket.socket 换哨兵类，任何漏打桩的真实请求当场失败。

    零网络不该靠每个测试作者自觉——曾漏桩 client.create 发真请求撞 401。
    注意不能 patch socket.socket.connect（C 类型属性不可改），只能换模块属性；
    主流库（httpx/openai）运行时查找 socket.socket，哨兵可拦截。
    """
    monkeypatch.setattr(socket, "socket", _GuardedSocket)


@pytest.fixture(autouse=True)
def _fake_api_keys(monkeypatch):
    """测试不依赖真实 .env：所有档案的 key 设假值（dotenv 不覆盖已存在变量）。

    CI/干净机器没有 .env，ChatSession._init_client 缺 key 会 RuntimeError——
    本地全绿纯属 load_dotenv 把真 key 装进了环境变量（2026-09-10 CI 抓出：
    移走 .env 后 5 failed + 33 errors）。
    """
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("KIMI_CODE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def reset_chars_per_token():
    compact._chars_per_token = 2.0
    yield


@pytest.fixture
def session(monkeypatch, tmp_path):
    """隔离的 ChatSession：假 API key（构造 client 用，不发请求）、存档指向 tmp。"""
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    import mini_agent.kernel.agent as agent
    monkeypatch.setattr(agent, "SESSION_FILE", str(tmp_path / "session.json"))
    return agent.ChatSession()


@pytest.fixture
def small_context_profile(monkeypatch):
    """注册一个 64K 窗口的测试用 Kimi 档案，用于触发截断/百分比等边界测试。"""
    import mini_agent.config as config
    profile = {
        "model": "kimi-test-64k",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "context_tokens": 64_000,
    }
    config.MODEL_PROFILES["kimi-test-64k"] = profile
    config.USER_PROFILES.pop("kimi-test-64k", None)
    yield "kimi-test-64k"
    config.MODEL_PROFILES.pop("kimi-test-64k", None)
