"""gitdiff 回归测试：tmp 目录造真实 git 仓库，验证 diff 提取层的结构与降级契约。

subprocess 调 git 是确定性本地操作（不经网络，不受 conftest socket 哨兵影响）。
"""

import subprocess

import pytest

import mini_agent.tools.gitdiff as gitdiff
from mini_agent.tools.gitdiff import GitRepoError


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """一个带初始提交的 git 仓库。"""
    git(tmp_path, "init")
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    return tmp_path


class TestListChanges:
    def test_modified_working_tree(self, repo):
        (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (c,) = gitdiff.list_changes(root=str(repo))
        assert c.path == "app.py" and c.status == "modified"
        assert (c.added, c.deleted) == (1, 1)

    def test_added_deleted_status(self, repo):
        """numstat 只有行数：A/D 语义必须靠 name-status 补齐（回归核心）。"""
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "app.py").unlink()
        git(repo, "add", "-A")
        changes = {c.path: c for c in gitdiff.list_changes(root=str(repo))}
        assert changes["new.py"].status == "added"
        assert changes["app.py"].status == "deleted"

    def test_rename_detected(self, repo):
        git(repo, "mv", "app.py", "renamed.py")
        (c,) = gitdiff.list_changes(root=str(repo))
        assert c.status == "renamed" and c.old_path == "app.py" and c.path == "renamed.py"

    def test_binary_file(self, repo):
        (repo / "logo.bin").write_bytes(b"\x89PNG\x00\x01\x02")
        git(repo, "add", ".")
        (c,) = [c for c in gitdiff.list_changes(root=str(repo)) if c.path == "logo.bin"]
        assert c.added is None and c.deleted is None  # 二进制无行数

    def test_untracked_only_in_working_mode(self, repo):
        """untracked 文件：工作区模式必须列出（review 漏新文件是大坑），commit range 不含。"""
        (repo / "draft.py").write_text("y = 1\n", encoding="utf-8")
        paths_working = {c.path: c.status for c in gitdiff.list_changes(root=str(repo))}
        assert paths_working.get("draft.py") == "untracked"
        git(repo, "add", ".")
        git(repo, "commit", "-m", "add draft")
        paths_range = {c.path for c in gitdiff.list_changes("HEAD~1..HEAD", root=str(repo))}
        assert "draft.py" in paths_range  # 已提交后按 range 正常出现

    def test_range_and_single_rev_specs(self, repo):
        (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "change")
        assert {c.path for c in gitdiff.list_changes("HEAD~1..HEAD", root=str(repo))} == {"app.py"}
        # 单个 rev → <rev>^!（该提交自身）
        assert {c.path for c in gitdiff.list_changes("HEAD", root=str(repo))} == {"app.py"}

    def test_not_a_repo(self, tmp_path):
        with pytest.raises(GitRepoError):
            gitdiff.list_changes(root=str(tmp_path))

    def test_bad_revspec_raises(self, repo):
        with pytest.raises(GitRepoError):
            gitdiff.list_changes("no-such-ref..HEAD", root=str(repo))


class TestFilePatch:
    def test_patch_contains_hunk_headers(self, repo):
        (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        patch = gitdiff.file_patch("app.py", root=str(repo))
        assert "@@" in patch and "+    return 2" in patch

    def test_untracked_patch_is_new_file_format(self, repo):
        (repo / "draft.py").write_text("y = 1\n", encoding="utf-8")
        patch = gitdiff.file_patch("draft.py", root=str(repo))
        assert "+y = 1" in patch  # 合成的新文件 patch，与已跟踪格式一致

    def test_truncation_marks(self, repo):
        (repo / "big.py").write_text("x = 1\n" * 100, encoding="utf-8")
        patch = gitdiff.file_patch("big.py", root=str(repo), max_chars=100)
        assert "截断" in patch and len(patch) < 200


class TestCollect:
    def test_empty_diff(self, repo):
        assert gitdiff.collect(root=str(repo)) == "（无变更）"

    def test_overview_plus_detail(self, repo):
        (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        out = gitdiff.collect(root=str(repo))
        assert "变更概览" in out and "app.py" in out
        assert "```diff" in out and "+    return 2" in out

    def test_budget_defers_large_files(self, repo):
        """超预算文件降级为概览条目 + file_patch 指引（索引常驻，正文惰性）。"""
        (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        (repo / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
        out = gitdiff.collect(root=str(repo), budget=200, per_file=100)
        assert "```diff" in out  # 小文件仍有详情
        assert "超预算仅列概览" in out and "big.py" in out
