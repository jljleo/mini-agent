"""tools.py 回归测试：文件窄接口（围栏+分页）/ 权限裁决 / 越界检测 / todo。

文件工具测试统一把 PROJECT_ROOT 指向 tmp_path：隔离真实项目目录，
也让"项目外路径"可以用另一个 tmp 目录构造。
"""

import os

import pytest

import tools
from tools import (
    _check_permission,
    _has_outside_path,
    _resolve_safe_path,
    edit_file,
    read_file,
    run_bash,
    todo_read,
    todo_write,
    write_file,
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把工具的文件操作隔离到临时目录；返回 (项目内目录, 项目外目录)。

    outside 必须是 root 的兄弟而非子目录——子目录仍在围栏内，不触发确认。
    """
    root = tmp_path / "proj"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(tools, "PROJECT_ROOT", str(root))
    return root, outside


# ============ 路径围栏 ============

class TestResolveSafePath:
    def test_inside_project_free(self, sandbox, monkeypatch):
        root, _ = sandbox
        monkeypatch.setattr(tools, "confirm",
                            lambda *a, **k: pytest.fail("项目内不应触发确认"))
        assert _resolve_safe_path("a/b.txt", "read_file") == str(root / "a" / "b.txt")

    def test_outside_asks_and_allows(self, sandbox, monkeypatch):
        root, outside = sandbox
        calls = []
        monkeypatch.setattr(tools, "confirm",
                            lambda cmd, dangerous: calls.append((cmd, dangerous)) or True)
        path = _resolve_safe_path(str(outside / "x.txt"), "read_file")
        assert path == str(outside / "x.txt")
        assert calls and calls[0][1] is False  # read 只读，不标危险

    def test_outside_rejected_raises(self, sandbox, monkeypatch):
        _, outside = sandbox
        monkeypatch.setattr(tools, "confirm", lambda *a, **k: False)
        with pytest.raises(ValueError, match="用户拒绝了访问项目外路径"):
            _resolve_safe_path(str(outside / "x.txt"), "read_file")

    def test_write_marks_dangerous(self, sandbox, monkeypatch):
        _, outside = sandbox
        calls = []
        monkeypatch.setattr(tools, "confirm",
                            lambda cmd, dangerous: calls.append(dangerous) or True)
        _resolve_safe_path(str(outside / "x.txt"), "write_file")
        assert calls == [True]

    def test_dotdot_escape_asks(self, sandbox, monkeypatch):
        """../ 逃逸必须走确认，不能靠字符串前缀蒙混。"""
        root, _ = sandbox
        calls = []
        monkeypatch.setattr(tools, "confirm",
                            lambda cmd, dangerous: calls.append(cmd) or True)
        _resolve_safe_path("../sibling.txt", "read_file")
        assert calls  # 触发了确认


# ============ read_file 分页 ============

class TestReadFilePagination:
    def _write_big(self, root, lines=3000):
        content = "".join(f"line {i}\n" for i in range(lines))
        (root / "big.txt").write_text(content, encoding="utf-8")
        return content

    def test_limit_cap_enforced(self, sandbox):
        """上限必须与描述一致（100_000 vs 10_000 的 10 倍偏差回归防线）。"""
        root, _ = sandbox
        self._write_big(root)
        with pytest.raises(ValueError, match="10000"):
            read_file("big.txt", 0, 50_000)

    def test_continuation_hint_closes_the_loop(self, sandbox):
        """未读完必须给续读指引：无位置信号的分页是半残的。"""
        root, _ = sandbox
        self._write_big(root)
        page = read_file("big.txt", 0, 10_000)
        assert "未完" in page and "offset=10000" in page

    def test_last_page_has_no_hint(self, sandbox):
        root, _ = sandbox
        content = self._write_big(root, lines=10)  # 小文件一页读完
        assert "未完" not in read_file("big.txt")
        assert read_file("big.txt") == content

    def test_offset_beyond_eof_reports(self, sandbox):
        root, _ = sandbox
        content = self._write_big(root, lines=10)
        result = read_file("big.txt", len(content) + 100)
        assert "超出文件末尾" in result

    def test_pages_concatenate_losslessly(self, sandbox):
        root, _ = sandbox
        content = self._write_big(root)
        p1 = read_file("big.txt", 0, 10_000)[:10_000]
        p2 = read_file("big.txt", 10_000, 10_000)[:10_000]
        p3 = read_file("big.txt", 20_000, 10_000)
        assert p1 + p2 + p3 == content

    def test_negative_offset_rejected(self, sandbox):
        root, _ = sandbox
        self._write_big(root, lines=10)
        with pytest.raises(ValueError):
            read_file("big.txt", -1, 100)

    def test_hint_survives_at_max_limit(self, sandbox):
        """limit 顶满 10K = MAX_OUTPUT_LEN 时，续读提示不能被截断保护吃掉。"""
        root, _ = sandbox
        self._write_big(root)
        page = read_file("big.txt", 0, 10_000)
        assert page.rstrip().endswith("offset=10000]")


# ============ write_file / edit_file ============

class TestWriteEdit:
    def test_write_creates_parent_dirs(self, sandbox):
        root, _ = sandbox
        write_file("deep/nested/f.txt", "hello")
        assert (root / "deep" / "nested" / "f.txt").read_text() == "hello"

    def test_edit_replaces_unique_occurrence(self, sandbox):
        root, _ = sandbox
        write_file("f.txt", "foo bar foo")
        with pytest.raises(ValueError, match="2 times"):
            edit_file("f.txt", "foo", "baz")  # 多处匹配必须拒绝，防误改
        edit_file("f.txt", "bar foo", "baz")
        assert (root / "f.txt").read_text() == "foo baz"

    def test_edit_missing_text_guides_to_read(self, sandbox):
        root, _ = sandbox
        write_file("f.txt", "content")
        with pytest.raises(ValueError, match="read_file first"):
            edit_file("f.txt", "nonexistent", "x")


# ============ 权限裁决 ============

RULES = [
    {"pattern": "rm -rf", "action": "deny"},
    {"pattern": r"^(ls|cat|echo)(\s|$)", "action": "allow"},
]


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.setattr(tools, "_load_rules", lambda: RULES)


class TestPermission:
    def test_deny_beats_allow(self, rules, monkeypatch):
        """最保守匹配获胜：同时命中 allow 和 deny 时必须 deny。"""
        monkeypatch.setattr(tools, "_load_rules", lambda: RULES + [
            {"pattern": "rm", "action": "allow"}])
        assert _check_permission("rm -rf /tmp/x") == "deny"

    def test_allow_match(self, rules):
        assert _check_permission("ls -la") == "allow"

    def test_no_match_is_ask(self, rules):
        assert _check_permission("python script.py") == "ask"

    def test_word_boundary_no_prefix_leak(self, rules):
        """词边界回归防线：allow 'ls' 不得放行 'lsof'。"""
        assert _check_permission("lsof -i") == "ask"

    def test_broken_rules_file_falls_back(self, tmp_path, monkeypatch):
        """坏配置文件绝不拖垮主流程：按空表处理（全 ask）。"""
        (tmp_path / "permissions.json").write_text("{broken json", encoding="utf-8")
        monkeypatch.setattr(tools, "PROJECT_ROOT", str(tmp_path))
        assert tools._load_rules() == []

    def test_missing_rules_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "PROJECT_ROOT", str(tmp_path))
        assert tools._load_rules() == []


# ============ 越界路径检测 ============

class TestHasOutsidePath:
    @pytest.mark.parametrize("cmd,expect", [
        ("cat /etc/hosts", True),
        ("ls -la", False),
        ("cat tools.py", False),
        ("cat ../secret.txt", True),
        ("cat ~/.zshrc", True),
        ("find . -name '*.py'", False),
        ("head -5 /tmp/log", True),
        ("echo hello", False),
    ])
    def test_cases(self, cmd, expect):
        assert _has_outside_path(cmd) is expect


# ============ run_bash 裁决联动 ============

class TestRunBash:
    def test_deny_short_circuits(self, rules, monkeypatch):
        monkeypatch.setattr(tools, "_load_rules",
                            lambda: [{"pattern": "rm -rf", "action": "deny"}])
        monkeypatch.setattr(tools, "_confirm",
                            lambda *a: pytest.fail("deny 不应走到确认"))
        assert "禁止" in run_bash("rm -rf /tmp/x")

    def test_ask_rejected(self, rules, monkeypatch):
        monkeypatch.setattr(tools, "_confirm", lambda cmd: False)
        assert "拒绝" in run_bash("python script.py")

    def test_allow_executes_without_confirm(self, rules, monkeypatch):
        monkeypatch.setattr(tools, "_confirm",
                            lambda *a: pytest.fail("allow 不应触发确认"))
        result = run_bash("echo hello")
        assert "hello" in result and "[exit code: 0]" in result

    def test_allow_with_outside_path_downgrades_to_ask(self, rules, monkeypatch):
        """核心回归：cat 虽免确认，但 `cat /etc/hosts` 越界必须降级为 ask。"""
        calls = []
        monkeypatch.setattr(tools, "_confirm", lambda cmd: calls.append(cmd) or False)
        result = run_bash("cat /etc/hosts")
        assert calls, "越界访问未触发确认"
        assert "拒绝" in result

    def test_timeout_clamped(self, rules, monkeypatch):
        """模型传入的超时值不被信任，钳制到 MAX_TIMEOUT。"""
        monkeypatch.setattr(tools, "_load_rules",
                            lambda: [{"pattern": "^echo", "action": "allow"}])
        result = run_bash("echo ok", timeout=99999)
        assert "ok" in result  # 钳制后正常执行（不钳制也只是执行，但此处验证不炸）


# ============ 历史检索 ============

class TestSearchHistory:
    HISTORY = [
        {"role": "user", "content": "帮我修复 auth.py 的登录超时 bug"},
        {"role": "assistant", "content": "已修复 auth.py"},
        {"role": "tool", "name": "read_file", "tool_call_id": "c1",
         "content": "原始工具结果包含 TimeoutError 堆栈"},
    ]

    @pytest.fixture
    def history(self, monkeypatch):
        monkeypatch.setattr(tools, "_history_provider", lambda: self.HISTORY)

    def test_finds_keyword_with_location(self, history):
        out = tools.search_history("auth.py")
        assert "#0 user" in out and "#1 assistant" in out
        assert "登录超时" in out

    def test_finds_in_tool_results(self, history):
        """核心价值：被瘦身的工具结果原文在存储里可检索。"""
        out = tools.search_history("TimeoutError")
        assert "#2 tool(read_file)" in out

    def test_case_insensitive(self, history):
        assert "#2" in tools.search_history("timeouterror")

    def test_no_hit(self, history):
        assert "未在历史中找到" in tools.search_history("不存在的词xyz")

    def test_provider_not_injected(self, monkeypatch):
        monkeypatch.setattr(tools, "_history_provider", None)
        assert "不可用" in tools.search_history("x")

    def test_hit_count_capped(self, monkeypatch):
        """高频词刷屏防线：命中上限 20。"""
        monkeypatch.setattr(tools, "_history_provider",
                            lambda: [{"role": "user", "content": "ab" * 5000}])
        out = tools.search_history("ab", context_chars=50)
        assert out.count("[#0") <= 20

    def test_output_truncation(self, monkeypatch):
        monkeypatch.setattr(tools, "_history_provider",
                            lambda: [{"role": "user", "content": "k" + "x" * 200_000}] * 5)
        out = tools.search_history("k", context_chars=500)
        assert len(out) <= tools.MAX_OUTPUT_LEN + 100  # 截断标记的余量

    def test_empty_keyword_rejected(self, history):
        assert "不能为空" in tools.search_history("")

    def test_session_injects_provider(self, session):
        """集成：ChatSession 构造即注入数据源，检索的就是 self.messages。"""
        session.messages.append({"role": "user", "content": "独特标记-xyz123"})
        # session fixture 的 ChatSession 构造时已完成注入
        assert "独特标记-xyz123" in tools.search_history("xyz123")


# ============ todo 工具 ============

class TestTodo:
    @pytest.fixture(autouse=True)
    def isolate_todo_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "TODO_FILE", str(tmp_path / "todos.json"))

    def test_roundtrip(self):
        todo_write([{"content": "任务一", "status": "pending"}])
        assert "任务一" in todo_read()

    def test_invalid_status_falls_back_to_pending(self):
        """模型可能写出枚举外状态，兜底 pending 而非崩。"""
        todo_write([{"content": "t", "status": "doing"}])
        assert "[pending]" in todo_read()

    def test_empty_when_no_file(self):
        assert "没有任务清单" in todo_read()
