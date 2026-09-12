"""写后验证闭环测试（R0 未勾项 2026-09-11 落地）：

config.POST_WRITE_CHECK_COMMANDS 配置的校验命令在 edit_file/write_file 改完后
自动执行；{path} 替换为项目相对路径；失败把 stderr 回喂工具结果（验证信号），
不算工具失败。默认空列表 = 行为零回归。

与容器说明：sandbox fixture 把 PROJECT_ROOT 指向 tmp；校验命令经 shell 在
PROJECT_ROOT cwd 下执行（本测试用真实 shell 命令，零网络）。
"""

import pytest

import led_review.tools.builtin as tools
from led_review.tools.builtin import edit_file, write_file


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(tools, "PROJECT_ROOT", str(root))
    return root


@pytest.fixture
def no_checks(monkeypatch):
    """测试默认配置：空校验列表（确保默认行为零回归的显式断言）。"""
    monkeypatch.setattr(tools, "POST_WRITE_CHECK_COMMANDS", [])
    return []


class TestPostWriteChecks:
    def test_default_no_checks_behavior_unchanged(self, sandbox, no_checks):
        """默认空配置：write/edit 结果不含校验标记（零回归保底）。"""
        r = write_file("f.py", "x = 1\n")
        assert "校验" not in r and "⚠" not in r
        r2 = edit_file("f.py", "x = 1", "y = 2")
        assert "校验" not in r2 and not r2.startswith("⚠")

    def test_path_placeholder_replaced_with_relpath(self, sandbox, monkeypatch):
        """{path} 被替换为项目相对路径且命令确实在 PROJECT_ROOT 下执行。"""
        monkeypatch.setattr(
            tools, "POST_WRITE_CHECK_COMMANDS",
            ["printf %s {path} > .check_marker"],
        )
        write_file("src/f.py", "x = 1\n")
        assert (sandbox / ".check_marker").read_text() == "src/f.py"

    def test_successful_check_no_warning(self, sandbox, monkeypatch):
        """校验通过：结果无 ⚠（校验命令输出不注入结果，保持工具结果干净）。"""
        monkeypatch.setattr(tools, "POST_WRITE_CHECK_COMMANDS",
                            ["test -f {path}"])
        write_file("f.py", "x = 1\n")
        r = write_file("g.py", "x = 1\n")
        assert "校验" not in r

    def test_failure_feeds_stderr_back(self, sandbox, monkeypatch):
        """失败：exit code + stderr 前几行回喂工具结果，不抛异常。"""
        monkeypatch.setattr(
            tools, "POST_WRITE_CHECK_COMMANDS",
            ["sh -c 'echo BOOM-LINE >&2; exit 7'"],
        )
        r = write_file("f.py", "x = 1\n")
        assert "⚠ 校验失败" in r and "exit 7" in r and "BOOM-LINE" in r

    def test_fail_fast_on_first_error(self, sandbox, monkeypatch):
        """首个命令失败即停：后续命令不再执行（快速失败）。"""
        monkeypatch.setattr(
            tools, "POST_WRITE_CHECK_COMMANDS",
            ["sh -c 'exit 1'", "printf ran >> .should_not_exist"],
        )
        write_file("f.py", "x = 1\n")
        r = edit_file("f.py", "x = 1", "y = 2")
        assert "⚠ 校验失败" in r
        assert not (sandbox / ".should_not_exist").exists()

    def test_both_tools_share_checks(self, sandbox, monkeypatch):
        """edit_file 与 write_file 都挂校验（同一闭环两条口）。"""
        monkeypatch.setattr(tools, "POST_WRITE_CHECK_COMMANDS",
                            ["sh -c 'echo MARK >&2; exit 3'"])
        write_file("f.py", "x = 1\n")
        assert "⚠ 校验失败" in write_file("a.py", "x\n")
        assert "⚠ 校验失败" in edit_file("f.py", "x = 1", "y = 2")