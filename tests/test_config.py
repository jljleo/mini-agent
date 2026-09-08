"""模型档案（config.MODEL_PROFILES）行为契约测试。

多模型适配的唯一事实来源是 config 的档案表：选择器（MINI_AGENT_MODEL）决定
MODEL/BASE_URL/API_KEY_ENV/CONTEXT_TOKENS，L1 截断水位随档案的上下文窗口走。
重载 config 后必须在 fixture 收尾恢复默认档案，防污染同进程的其他测试。
"""

import importlib

import pytest

import config


@pytest.fixture
def reload_config(monkeypatch):
    """按档案名重载 config（None = 未设环境变量的默认路径），收尾恢复默认。"""
    def _reload(name: str | None):
        if name is None:
            monkeypatch.delenv("MINI_AGENT_MODEL", raising=False)
        else:
            monkeypatch.setenv("MINI_AGENT_MODEL", name)
        importlib.reload(config)

    yield _reload
    monkeypatch.delenv("MINI_AGENT_MODEL", raising=False)
    importlib.reload(config)


def test_default_profile_is_kimi(reload_config):
    reload_config(None)
    assert config.MODEL_PROFILE == "kimi"
    assert config.MODEL == "kimi-k3"
    assert config.BASE_URL == "https://api.moonshot.cn/v1"
    assert config.API_KEY_ENV == "MOONSHOT_API_KEY"


def test_profile_switch(reload_config):
    reload_config("deepseek")
    assert config.MODEL == "deepseek-chat"
    assert config.BASE_URL == "https://api.deepseek.com/v1"
    assert config.API_KEY_ENV == "DEEPSEEK_API_KEY"
    assert config.CONTEXT_TOKENS == 64_000


def test_truncation_watermarks_follow_context_window(reload_config):
    # kimi 128K 窗口 → 100K/60K，与历史调参一致（回归保护：推导式不能漂移默认值）
    reload_config("kimi")
    assert (config.TRUNCATE_HIGH_TOKENS, config.TRUNCATE_LOW_TOKENS) == (100_000, 60_000)
    # 小窗口模型水位必须同比下移，否则 compact 的防爆兜底失效
    reload_config("deepseek")
    assert config.TRUNCATE_HIGH_TOKENS == config.CONTEXT_TOKENS - 28_000
    assert 0 < config.TRUNCATE_LOW_TOKENS < config.TRUNCATE_HIGH_TOKENS


def test_unknown_profile_fails_fast(reload_config):
    # 拼错的档案名必须在启动时炸出来（带可选名单），而不是静默落到某个模型上
    with pytest.raises(SystemExit, match="未知模型档案"):
        reload_config("kimi-typo")
