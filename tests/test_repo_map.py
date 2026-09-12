"""repo_map.py 回归测试：符号提取 / 引用计数 / repo map 生成 / search_symbols。

全部用 tmp_path 建迷你 Python 项目隔离真实仓库，零网络、无副作用。
文件工具测试惯例：把根目录指向 tmp，不触碰真实项目。
"""

import led_review.repo_map as repo_map


def make_project(tmp_path, files: dict[str, str]):
    """在 tmp_path 下建迷你项目（files: 相对路径 → 源码），返回根路径。"""
    root = tmp_path / "proj"
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


# ---------- 符号提取 ----------

class TestExtractSymbols:
    def test_class_function_method_constant(self):
        src = (
            "IMPORTANT = 42\n"
            "\n"
            "def top_level(a, b):\n"
            "    return a + b\n"
            "\n"
            "class Greeter:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "    async def greet(self):\n"
            "        return f'hi {self.name}'\n"
        )
        syms = repo_map.extract_symbols(src, "app.py")
        kinds = {s.name: s.kind for s in syms}
        assert kinds["IMPORTANT"] == "constant"
        assert kinds["top_level"] == "function"
        assert kinds["Greeter"] == "class"
        assert kinds["__init__"] == "method"
        assert kinds["greet"] == "method"
        # 统一契约下 sig 留空（细节靠 read_file）；kind/行号仍准确
        greet = next(s for s in syms if s.name == "greet")
        assert greet.kind == "method" and greet.sig == ""
        top = next(s for s in syms if s.name == "top_level")
        assert top.kind == "function" and top.line == 3

    def test_imports_extracted(self):
        src = "import os\nfrom pathlib import Path\nfrom typing import List\n"
        syms = repo_map.extract_symbols(src, "m.py")
        names = {s.name for s in syms}
        assert {"os", "Path", "List"} <= names
        assert all(s.kind == "import" for s in syms)

    def test_syntax_error_tolerant(self):
        """tree-sitter 容错：坏文件也能捞到部分符号（而非整文件丢弃）。"""
        syms = repo_map.extract_symbols("def broken(:\n", "bad.py")
        names = {s.name for s in syms}
        assert "broken" in names  # 容错解析仍识别出函数名

    def test_nested_def_not_indexed(self):
        src = "def outer():\n    def inner():\n        pass\n"
        syms = repo_map.extract_symbols(src, "m.py")
        names = {s.name for s in syms}
        assert names == {"outer"}  # inner 是局部函数，不索引


# ---------- 文件发现 ----------

class TestDiscover:
    def test_ignores_venv_git_cache_env(self, tmp_path):
        root = make_project(tmp_path, {
            "app.py": "x = 1\n",
            ".venv/lib/x.py": "y = 2\n",
            ".git/config.py": "z = 3\n",
            "pkg/__pycache__/x.py": "w = 4\n",
            ".env": "SECRET=1\n",
        })
        files = repo_map.discover_source_files(str(root))
        rels = {__import__("os").path.relpath(f, str(root)) for f in files}
        assert rels == {"app.py"}


# ---------- 引用计数（重要性排序）----------

class TestReferences:
    def test_core_symbol_ranked_by_references(self, tmp_path):
        root = make_project(tmp_path, {
            "core.py": "def api():\n    pass\n",
            "use1.py": "from core import api\napi()\n",
            "use2.py": "from core import api\napi()\napi()\n",
        })
        idx = repo_map.index_repo(str(root))
        api = next(s for s in idx.symbols if s.name == "api")
        assert api.references >= 3, f"api 应被多文件引用，实际 {api.references}"
        # 核心文件排第一（被引用最多）
        assert idx.files[0] == "core.py"

    def test_repo_map_places_core_first(self, tmp_path):
        root = make_project(tmp_path, {
            "core.py": "class Engine:\n    def run(self):\n        pass\n",
            "main.py": "from core import Engine\nEngine().run()\n",
        })
        text = repo_map.build_repo_map(str(root), max_chars=2000)
        assert "core.py" in text
        assert "Engine" in text
        # 预算内头部优先：core.py 应在 main.py 前
        assert text.index("core.py") < text.index("main.py")

    def test_repo_map_respects_budget(self, tmp_path):
        root = make_project(tmp_path, {
            "a.py": "def a1():\n    pass\ndef a2():\n    pass\n",
            "b.py": "def b1():\n    pass\n",
        })
        text = repo_map.build_repo_map(str(root), max_chars=30)
        assert len(text) <= 200  # 预算应被尊重（含文件头兜底，允许略超标头行）


# ---------- search_symbols ----------

class TestSearchSymbols:
    def test_find_by_name_and_kind(self, tmp_path):
        root = make_project(tmp_path, {
            "svc.py": "class Service:\n    def handle(self):\n        pass\n"
                      "def helper():\n    pass\n",
        })
        out = repo_map.search_symbols(str(root), "serv")
        assert "Service" in out and "svc.py" in out
        # kind 过滤
        out_m = repo_map.search_symbols(str(root), "handle", kind="method")
        assert "handle" in out_m
        out_c = repo_map.search_symbols(str(root), "handle", kind="class")
        assert "未找到" in out_c

    def test_no_match_gives_guidance(self, tmp_path):
        root = make_project(tmp_path, {"a.py": "def foo():\n    pass\n"})
        out = repo_map.search_symbols(str(root), "nonexistent_xyz")
        assert "未找到" in out

    def test_case_insensitive(self, tmp_path):
        root = make_project(tmp_path, {"a.py": "class MyWidget:\n    pass\n"})
        out = repo_map.search_symbols(str(root), "mywidget")
        assert "MyWidget" in out

    def test_path_scope_finds_file_by_fragment(self, tmp_path):
        """path 维度：烂命名/记不住名字时按路径片段找文件。"""
        root = make_project(tmp_path, {
            "pkg/worker/helpers.py": "def q1(x):\n    return x\n",
            "pkg/main.py": "print(1)\n",
        })
        out = repo_map.search_symbols(str(root), "worker/helpers", scope="path")
        assert "pkg/worker/helpers.py" in out
        out2 = repo_map.search_symbols(str(root), "HELPERS", scope="path")  # 大小写不敏感
        assert "pkg/worker/helpers.py" in out2
        assert "main.py" not in out2

    def test_docs_scope_finds_business_words(self, tmp_path):
        """docs 维度：符号名烂但注释/正文有业务词 → 仍能定位。"""
        root = make_project(tmp_path, {
            "x.py": "# 去重键漏掉了渠道，导致通知被吞\ndef q1(a, b):\n    return a\n",
            "y.py": "y = 1\n",
        })
        out = repo_map.search_symbols(str(root), "去重", scope="docs")
        assert "x.py:1" in out and "去重" in out
        assert "y.py" not in out

    def test_docs_scope_miss_guidance(self, tmp_path):
        root = make_project(tmp_path, {"a.py": "x = 1\n"})
        out = repo_map.search_symbols(str(root), "不存在的词zzz", scope="docs")
        assert "未找到正文" in out

    def test_name_miss_auto_runs_docs(self, tmp_path):
        """name miss 自动并跑 docs：符号名烂但注释有业务词 → 直接返回正文命中。"""
        root = make_project(tmp_path, {
            "q.py": "# 去重键漏掉渠道导致通知被吞\ndef q1(a, b):\n    return a\n",
            "other.py": "y = 1\n",
        })
        out = repo_map.search_symbols(str(root), "去重")  # 无符号名叫去重 → name miss
        assert "未找到" in out  # 说明名称未命中
        assert "q.py:1" in out and "去重" in out  # 但自动补了 docs 命中

    def test_name_miss_no_docs_gives_guidance(self, tmp_path):
        root = make_project(tmp_path, {"a.py": "x = 1\n"})
        out = repo_map.search_symbols(str(root), "完全不存在zzz")
        assert "scope=docs" in out and "scope=path" in out  # 兜底引导

    def test_name_miss_with_kind_does_not_auto_docs(self, tmp_path):
        """带 kind 过滤时不自动并跑 docs（kind 是符号语义，正文行无 kind）。"""
        root = make_project(tmp_path, {"a.py": "# 去重\ndef q1():\n    pass\n"})
        out = repo_map.search_symbols(str(root), "去重", kind="class")
        assert "scope=docs" in out  # 只有引导，无 docs 结果


# ---------- 缓存失效（编辑后刷新）----------

class TestCacheInvalidation:
    def test_edit_file_refreshes_index(self, tmp_path):
        root = make_project(tmp_path, {"a.py": "def old():\n    pass\n"})
        repo_map.search_symbols(str(root), "old")
        # 新增一个符号，签名变化 → 缓存应失效
        (root / "a.py").write_text("def new_sym():\n    pass\n", encoding="utf-8")
        out = repo_map.search_symbols(str(root), "new_sym")
        assert "new_sym" in out


# ---------- 工具接入（tools.search_symbols）----------

class TestToolIntegration:
    def test_registered_and_resident(self):
        import led_review.tools.builtin as tools  # noqa: F401 触发 @tool 注册副作用
        import led_review.tools.registry as tool_registry
        assert "search_symbols" in tool_registry.TOOLS
        assert "search_symbols" in tool_registry.RESIDENT_TOOL_NAMES

    def test_tool_respects_project_root_isolation(self, tmp_path, monkeypatch):
        import led_review.tools.builtin as tools
        root = make_project(tmp_path, {"mod.py": "class Widget:\n    pass\n"})
        monkeypatch.setattr(tools, "PROJECT_ROOT", str(root))
        out = tools.search_symbols("widget")
        assert "Widget" in out and "mod.py" in out

    def test_agent_injects_repo_map(self, tmp_path, monkeypatch):
        """ChatSession 构造后应带一条 repo map system 消息（头部保留区）。"""
        import led_review.kernel.agent as agent
        import led_review.repo_map as repo_map
        root = make_project(tmp_path, {"core.py": "def api():\n    pass\n"})
        # 让 build_repo_map_cached 指向迷你项目（而非真实仓库）
        monkeypatch.setattr(repo_map, "_IGNORED_ROOT", str(root))
        session = agent.ChatSession(set_provider=False)
        system_texts = [m.get("content", "") for m in session.messages
                        if m.get("role") == "system"]
        assert any("代码库地图" in t and "api" in t for t in system_texts), \
            "会话应注入含 api 符号的代码库地图"
