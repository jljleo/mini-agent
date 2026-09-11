"""bench review 任务的沙箱构建测试：local（base+patch）与 remote（clone+revert 回注）。

remote 用 file:// 本地仓库模拟（离线确定性），验证「F 状态 → revert F → bug 回注」链路。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.run_bench import _build_review_sandbox  # noqa: E402


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout


def test_remote_revert_injects_bug(tmp_path):
    """remote 任务：克隆 F 所在仓库 → revert F → HEAD 的 diff = bug 回注。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "a.py").write_text("def f():\n    return buggy\n", encoding="utf-8")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    (origin / "a.py").write_text("def f():\n    return fixed  # 修复\n", encoding="utf-8")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "fix")
    fix_sha = git(origin, "rev-parse", "HEAD").strip()

    meta = {"remote": {"repo": str(origin), "commit": fix_sha}, "spec": "HEAD"}
    sandbox = _build_review_sandbox(tmp_path / "review_oss_fake", meta)

    # revert 后 buggy 状态回来
    assert "return buggy" in (sandbox / "a.py").read_text(encoding="utf-8")
    # HEAD diff 就是 revert（删除修复）
    diff = git(sandbox, "diff", "HEAD~1", "HEAD")
    assert "-    return fixed  # 修复" in diff and "+    return buggy" in diff


def test_local_base_patch_still_works(tmp_path):
    """local 任务：base/ + bug.patch 两提交重建（原有路径回归）。"""
    task = tmp_path / "review_fake"
    (task / "base").mkdir(parents=True)
    (task / "base" / "a.py").write_text("x = 1\n", encoding="utf-8")
    # 手工造一个合法 patch：x = 1 → x = 2
    (task / "bug.patch").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        encoding="utf-8")
    sandbox = _build_review_sandbox(task, {})
    assert (sandbox / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert git(sandbox, "log", "--format=%s").split() == ["feature", "base"]
