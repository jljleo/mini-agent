"""模型档案（config.MODEL_PROFILES + models.json）行为契约测试。

多模型适配的唯一事实来源是 config 的档案表：选择器（MINI_AGENT_MODEL）决定
MODEL/BASE_URL/API_KEY_ENV/CONTEXT_TOKENS，L1 截断水位随档案的上下文窗口走。
重载 config 后必须在 fixture 收尾恢复默认档案，防污染同进程的其他测试。
"""

import importlib
import json

import pytest

import config


@pytest.fixture
def reload_config(monkeypatch, tmp_path):
    """按档案名重载 config（None = 未设环境变量的默认路径），收尾恢复默认。"""
    def _reload(name: str | None, user_profiles: dict | None = None):
        # 重载时先用安全的内置档案，避免自定义档案还没写进文件就触发 module-level apply
        if name is None:
            monkeypatch.delenv("MINI_AGENT_MODEL", raising=False)
        else:
            monkeypatch.setenv("MINI_AGENT_MODEL", "kimi")
        importlib.reload(config)
        # 隔离用户自定义档案：避免测试机上的 models.json 污染
        config._USER_MODELS_JSON = str(tmp_path / "models.json")
        if user_profiles is not None:
            with open(config._USER_MODELS_JSON, "w", encoding="utf-8") as f:
                json.dump(user_profiles, f)
        config.refresh_user_profiles()
        if name is not None:
            monkeypatch.setenv("MINI_AGENT_MODEL", name)
        try:
            config.apply_profile(name or "kimi")
        except KeyError as exc:
            available = ", ".join(config.list_profiles())
            raise SystemExit(
                f"未知模型档案 {name!r}（MINI_AGENT_MODEL），"
                f"可选：{available}；新模型请在 models.default.json 或 models.json 添加"
            ) from exc

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
    reload_config("kimi-code-256k")
    assert config.MODEL == "k3-256k"
    assert config.BASE_URL == "https://api.kimi.com/coding/v1"
    assert config.API_KEY_ENV == "KIMI_CODE_API_KEY"
    assert config.CONTEXT_TOKENS == 256_000


def test_truncation_watermarks_follow_context_window(reload_config):
    # 默认 kimi-k3 现在是 1M 窗口：水位按公式走
    reload_config("kimi")
    assert config.TRUNCATE_HIGH_TOKENS == config.CONTEXT_TOKENS - 28_000
    assert 0 < config.TRUNCATE_LOW_TOKENS < config.TRUNCATE_HIGH_TOKENS
    # 用 128K 用户档案验证历史调参值不变（公式回归保护）
    reload_config("custom128", user_profiles={
        "custom128": {
            "model": "custom-128k",
            "base_url": "https://example.com/v1",
            "api_key_env": "CUSTOM_KEY",
            "context_tokens": 128_000,
        }
    })
    assert (config.TRUNCATE_HIGH_TOKENS, config.TRUNCATE_LOW_TOKENS) == (100_000, 60_000)
    # 小窗口模型水位必须同比下移，否则 compact 的防爆兜底失效
    reload_config("kimi-code-256k")
    assert config.TRUNCATE_HIGH_TOKENS == config.CONTEXT_TOKENS - 28_000
    assert 0 < config.TRUNCATE_LOW_TOKENS < config.TRUNCATE_HIGH_TOKENS


def test_unknown_profile_fails_fast(reload_config):
    # 拼错的档案名必须在启动时炸出来（带可选名单），而不是静默落到某个模型上
    with pytest.raises(SystemExit, match="未知模型档案"):
        reload_config("kimi-typo")


def test_user_profiles_override_builtin(reload_config):
    # 项目根 models.json 里的档案覆盖内置同名档案，且新增档案可用
    reload_config("kimi", user_profiles={
        "kimi": {
            "model": "kimi-override",
            "base_url": "https://override.cn/v1",
            "api_key_env": "OVERRIDE_KEY",
            "context_tokens": 256_000,
        },
        "custom": {
            "model": "custom-model",
            "base_url": "https://custom.example.com/v1",
            "api_key_env": "CUSTOM_KEY",
            "context_tokens": 100_000,
        },
    })
    assert config.MODEL == "kimi-override"
    assert config.BASE_URL == "https://override.cn/v1"
    assert config.API_KEY_ENV == "OVERRIDE_KEY"
    assert config.CONTEXT_TOKENS == 256_000
    assert "custom" in config.list_profiles()


def test_get_profile_returns_normalized_dict(reload_config):
    reload_config("kimi-code-256k")
    p = config.get_profile("kimi-code-256k")
    assert p["model"] == "k3-256k"
    assert p["truncate_high_tokens"] == p["context_tokens"] - 28_000
    assert p["truncate_low_tokens"] == int(p["truncate_high_tokens"] * 0.6)


def test_format_context_tokens():
    assert config.format_context_tokens(128_000) == "128K"
    assert config.format_context_tokens(131_072) == "131K"
    assert config.format_context_tokens(1_000_000) == "1.0M"
    assert config.format_context_tokens() == "1.0M"  # 缺省 = 当前默认档案窗口（kimi-k3 1M）
