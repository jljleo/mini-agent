"""edit_file 容错闭环回归测试（AGENT_DESIGN 13 条 + 48/49 的落地）：

编辑容错策略链：精确替换（现状）→ 行级宽容定位（行尾空白/换行差异、stale 文件）
→ 失败给「read_file 指引 + 文件预览」。
改后自检：语法错误零成本回喂（opencode 式 diagnostics，模型当场自愈）。
文件工具统一用 sandbox fixture（PROJECT_ROOT 指向 tmp）隔离真实仓库。
"""

import pytest

import mini_agent.tools.builtin as tools
from mini_agent.tools.builtin import edit_file, write_file


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(tools, "PROJECT_ROOT", str(root))
    return root


# ============ 容错定位（策略链 L2：行级宽容匹配） ============

class TestLenientEdit:
    def test_line_trailing_whitespace_tolerated(self, sandbox):
        """old 块带行尾空格（read_file 复制常见误差）→ 仍能定位替换整块。"""
        write_file("f.py", "def f():\n    return 1\n")
        edit_file("f.py", "def f(): \n    return 1", "def g():\n    return 2")
        assert (sandbox / "f.py").read_text() == "def g():\n    return 2\n"

    def test_crlf_vs_lf_tolerated(self, sandbox):
        """文件 CRLF、编辑用 LF → 仍能定位，且保留原文件 CRLF 风格（不破坏换行）。"""
        write_file("f.py", "def f():\r\n    return 1\r\n")
        edit_file("f.py", "def f():\n    return 1", "def f():\n    return 2")
        # newline="" 保留原文物理换行（Path.read_text 会归一成 \n，不能验证 CRLF）
        with open(sandbox / "f.py", encoding="utf-8", newline="") as fh:
            assert fh.read() == "def f():\r\n    return 2\r\n"

    def test_last_line_without_newline_tolerated(self, sandbox):
        """文件末尾无换行 + old 带尾空格 → 容错定位且不破坏行尾。"""
        write_file("f.py", "def f():\n    return 1")
        edit_file("f.py", "def f(): \n    return 1", "def g():\n    return 2")
        assert (sandbox / "f.py").read_text() == "def g():\n    return 2"

    def test_lenient_match_must_stay_unique(self, sandbox):
        """容错匹配仍强制唯一：精确 count=0 但行级多处命中必须拒绝，防误改。"""
        write_file("f.py", "FILTER = x\nFILTER = x\n")
        with pytest.raises(ValueError, match="provide more context"):
            edit_file("f.py", "FILTER = x ", "FILTER = 0")  # 尾空格：精确 0 次，行级 2 处

    def test_missing_guides_to_read_with_preview(self, sandbox):
        """确实找不到 → 报错带文件预览 + read_file 指引（可执行指导，不是死句）。"""
        write_file("f.py", "line one\nline two\nline three\n")
        with pytest.raises(ValueError, match="read_file"):
            edit_file("f.py", "完全不存在的内容", "x")


# ============ 改后自检（语法冒烟回喂） ============

class TestEditSyntaxCheck:
    def test_broken_syntax_reported_with_line(self, sandbox):
        """edit 改出语法错误 → 工具结果立即带行号报错（零成本，当场自愈）。"""
        write_file("f.py", "x = 1\ny = 2\n")
        result = edit_file("f.py", "x = 1", "x = (")
        assert "语法" in result and "f.py:1" in result, result

    def test_clean_syntax_no_noise(self, sandbox):
        write_file("f.py", "x = 1\ny = 2\n")
        result = edit_file("f.py", "x = 1", "x = 10")
        assert "语法" not in result, result

    def test_js_syntax_check(self, sandbox):
        write_file("f.js", "const a = 1;\n")
        result = edit_file("f.js", "const a = 1;", "const a = ;")
        assert "语法" in result and "f.js:1" in result, result

    def test_error_list_capped(self, sandbox):
        """坏文件报错限制条数（防海量 ERROR 刷屏上下文）。"""
        write_file("f.py", "x = 1\n")
        result = edit_file("f.py", "x = 1", "a = (\nb = [\nc = {\n")  # 三行不闭合
        assert result.count("语法错误") <= 5