"""/undo 编辑回滚回归测试：写前快照 → 回滚 → 连续回退 → 新建/空日志边界。

快照日志路径运行时解析（tools.PROJECT_ROOT 可变副本），sandbox fixture 把
PROJECT_ROOT 指到 tmp 即自动隔离，无需单独 monkeypatch 日志路径。
"""

import pytest

import tools
from commands import cmd_undo


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(tools, "PROJECT_ROOT", str(root))
    return root


def log_entries(root):
    p = root / ".undo.log"
    if not p.exists():
        return []
    return [line for line in p.read_text(encoding="utf-8").splitlines() if line]


class TestSnapshot:
    def test_edit_snapshots_original(self, sandbox):
        (sandbox / "f.py").write_text("x = 1\n", encoding="utf-8")
        tools.edit_file("f.py", "x = 1", "x = 2")
        entries = log_entries(sandbox)
        assert len(entries) == 1
        assert '"x = 1\\n"' in entries[0] and '"existed": true' in entries[0]

    def test_write_new_file_records_not_existed(self, sandbox):
        tools.write_file("new.py", "y = 1\n")
        entries = log_entries(sandbox)
        assert len(entries) == 1
        assert '"existed": false' in entries[0] and '"new.py"' in entries[0]

    def test_snapshot_failure_does_not_block_write(self, sandbox):
        """快照异常静默：即使 .undo.log 写不进，写文件也必须成功。"""
        (sandbox / "f.py").write_text("a\n", encoding="utf-8")
        # 让快照失败：日志路径变成一个不可写的目录
        (sandbox / ".undo.log").mkdir()  # 目录占位 → open(w) 抛 IsADirectoryError
        r = tools.edit_file("f.py", "a", "b")
        assert "Edited" in r  # 写未受影响


class TestUndo:
    def test_undo_restores_last_edit(self, sandbox):
        (sandbox / "f.py").write_text("x = 1\n", encoding="utf-8")
        tools.edit_file("f.py", "x = 1", "x = 2")
        msg = tools.undo_last()
        assert "回滚" in msg
        assert (sandbox / "f.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_continuous_undo_steps_back(self, sandbox):
        (sandbox / "f.py").write_text("v0\n", encoding="utf-8")
        tools.edit_file("f.py", "v0", "v1")
        tools.edit_file("f.py", "v1", "v2")
        assert (sandbox / "f.py").read_text() == "v2\n"
        tools.undo_last()
        assert (sandbox / "f.py").read_text() == "v1\n"
        tools.undo_last()
        assert (sandbox / "f.py").read_text() == "v0\n"
        # 到底了：再 undo 应明确提示
        assert "没有可撤销" in tools.undo_last()

    def test_undo_new_file_removes_it(self, sandbox):
        tools.write_file("tmp.py", "boom\n")
        assert (sandbox / "tmp.py").exists()
        msg = tools.undo_last()
        assert "删除" in msg and "tmp.py" in msg
        assert not (sandbox / "tmp.py").exists()

    def test_undo_empty_log_guidance(self, sandbox):
        assert "没有可撤销" in tools.undo_last()

    def test_undo_syntax_warning_on_bad_restore(self, sandbox):
        """回滚恢复的是坏代码时给语法提示（改回坏状态也要立刻知道）。"""
        (sandbox / "f.py").write_text("x = 1\n", encoding="utf-8")
        tools.edit_file("f.py", "x = 1", "x = (")   # 改坏（tree-sitter 可报错）
        tools.edit_file("f.py", "x = (", "x = 1")  # 改回（快照记录坏状态）
        msg = tools.undo_last()
        assert "语法" in msg  # 回滚到坏状态 → 冒烟提示


class TestCommandUndo:
    def test_cmd_undo_wires_to_tools(self, sandbox, session):
        (sandbox / "f.py").write_text("hello\n", encoding="utf-8")
        tools.edit_file("f.py", "hello", "world")
        cmd_undo(session)
        assert (sandbox / "f.py").read_text() == "hello\n"
        assert cmd_undo(session) is None  # handler 无返回值，输出走 ui


class TestClearWipesUndo:
    def test_clear_removes_undo_log(self, sandbox, session, monkeypatch):
        from commands import cmd_clear
        (sandbox / "f.py").write_text("a\n", encoding="utf-8")
        tools.edit_file("f.py", "a", "b")
        assert (sandbox / ".undo.log").exists()
        cmd_clear(session)
        assert not (sandbox / ".undo.log").exists()