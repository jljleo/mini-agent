"""led install-hook / uninstall-hook 回归测试：

- 在 tmp git 仓库装 pre-commit/pre-push hook，验证脚本内容锚点（BYOK 跳过分支、
  门禁/信息性参数、spec）
- 卸载干净；非 git 仓库报错
- 安装幂等（覆写不炸）
"""

import os

import pytest

from led_review.hooks import install_hook, uninstall_hook


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


class TestInstallHook:
    def test_installs_pre_push_with_no_fail_by_default(self, git_repo):
        install_hook("pre-push", fail_on_high=False, root=str(git_repo))
        hook = (git_repo / ".git/hooks/pre-push").read_text(encoding="utf-8")
        assert "led review HEAD --no-fail" in hook
        assert "KIMI_CODE_API_KEY" in hook  # BYOK 跳过分支存在
        assert os.access(git_repo / ".git/hooks/pre-push", os.X_OK)  # 可执行位

    def test_fail_on_high_drops_no_fail(self, git_repo):
        """门禁：不加 --no-fail（review 默认 high→exit 1 → hook 阻塞）。"""
        install_hook("pre-commit", fail_on_high=True, root=str(git_repo))
        hook = (git_repo / ".git/hooks/pre-commit").read_text(encoding="utf-8")
        assert "--no-fail" not in hook
        assert "led review  " in hook or "led review" in hook

    def test_pre_commit_spec_is_workspace_default(self, git_repo):
        install_hook("pre-commit", fail_on_high=False, root=str(git_repo))
        hook = (git_repo / ".git/hooks/pre-commit").read_text(encoding="utf-8")
        # pre-commit 的 spec 缺省（工作区 vs HEAD），不应带 HEAD 参数
        assert "led review --no-fail\n" in hook

    def test_reinstall_overwrites_idempotent(self, git_repo):
        install_hook("pre-push", fail_on_high=False, root=str(git_repo))
        install_hook("pre-push", fail_on_high=True, root=str(git_repo))
        hook = (git_repo / ".git/hooks/pre-push").read_text(encoding="utf-8")
        assert "--no-fail" not in hook  # 第二次（门禁）覆盖了第一次

    def test_not_a_git_repo_fails(self, tmp_path):
        plain = tmp_path / "norepo"
        plain.mkdir()
        with pytest.raises(SystemExit, match="不是 git 仓库"):
            install_hook("pre-push", fail_on_high=False, root=str(plain))


class TestUninstallHook:
    def test_uninstall_removes(self, git_repo):
        install_hook("pre-push", fail_on_high=False, root=str(git_repo))
        uninstall_hook("pre-push", root=str(git_repo))
        assert not (git_repo / ".git/hooks/pre-push").exists()

    def test_uninstall_missing_is_noop(self, git_repo):
        uninstall_hook("pre-push", root=str(git_repo))  # 不存在也不抛

    def test_unknown_kind_rejected(self, git_repo):
        with pytest.raises(ValueError, match="未知 hook 类型"):
            install_hook("post-receive", fail_on_high=False, root=str(git_repo))