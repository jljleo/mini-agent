"""repo_map.py 多语言支持回归测试：javascript/typescript/go/rust/java。

每语言用迷你源码验证符号提取映射（class/function/method/import/constant），
以及混合语言项目的发现/索引/检索。tree-sitter 为本机库，零网络。
"""

import os

import mini_agent.repo_map as repo_map


def kinds_of(src, relpath):
    return {s.name: (s.kind, s.line) for s in repo_map.extract_symbols(src, relpath)}


# ---------- JavaScript ----------

class TestJavaScript:
    def test_symbols_mapped(self):
        src = (
            "import fs from 'fs';\n"
            "import { readFile } from 'fs/promises';\n"
            "const RATE = 0.1;\n"
            "const calc = (x) => x * RATE;\n"
            "class Cart {\n"
            "  total(items) { return items.length; }\n"
            "}\n"
            "function helper() {}\n"
        )
        k = kinds_of(src, "cart.js")
        assert k["fs"][0] == "import"
        assert k["readFile"][0] == "import"
        assert k["RATE"][0] == "constant"
        assert k["calc"][0] == "function"      # 箭头函数赋值 → function
        assert k["Cart"][0] == "class"
        assert k["total"][0] == "method"
        assert k["helper"][0] == "function"
        assert k["Cart"][1] == 5 and k["total"][1] == 6  # 行号（1-based）

    def test_syntax_error_returns_empty(self):
        assert repo_map.extract_symbols("class {", "bad.js") == []


# ---------- TypeScript（含 tsx）----------

class TestTypeScript:
    def test_ts_interface_and_enum_as_class(self):
        src = (
            "import { ref } from 'vue';\n"
            "interface Cart { items: string[] }\n"
            "type ID = number;\n"
            "enum Role { Admin }\n"
            "class CartService {\n"
            "  checkout(cart: Cart) {}\n"
            "}\n"
        )
        k = kinds_of(src, "cart.ts")
        assert k["ref"][0] == "import"
        assert k["Cart"][0] == "class"
        assert k["ID"][0] == "class"
        assert k["Role"][0] == "class"
        assert k["CartService"][0] == "class"
        assert k["checkout"][0] == "method"

    def test_tsx_parses(self):
        k = kinds_of(
            "export default function App() { return <div/>; }\nclass Btn { render() {} }\n",
            "app.tsx",
        )
        assert k["App"][0] == "function"
        assert k["Btn"][0] == "class"
        assert k["render"][0] == "method"


# ---------- Go ----------

class TestGo:
    def test_symbols_mapped(self):
        src = (
            'package main\n'
            'import "fmt"\n'
            'import "github.com/user/pkg"\n'
            'const RATE = 0.1\n'
            'type Cart struct { Items []string }\n'
            'func (c *Cart) Total() float64 { return 0 }\n'
            'func helper() {}\n'
        )
        k = kinds_of(src, "cart.go")
        assert k["fmt"][0] == "import"
        assert k["pkg"][0] == "import"        # 路径最后一段
        assert k["RATE"][0] == "constant"
        assert k["Cart"][0] == "class"        # struct → class
        assert k["Total"][0] == "method"
        assert k["helper"][0] == "function"


# ---------- Rust ----------

class TestRust:
    def test_symbols_mapped(self):
        src = (
            "use std::collections::HashMap;\n"
            "const RATE: f64 = 0.1;\n"
            "struct Cart { items: Vec<f64> }\n"
            "impl Cart {\n"
            "    fn total(&self) -> f64 { 0.0 }\n"
            "}\n"
            "fn helper() {}\n"
        )
        k = kinds_of(src, "cart.rs")
        assert k["HashMap"][0] == "import"
        assert k["RATE"][0] == "constant"
        assert k["Cart"][0] == "class"
        assert k["total"][0] == "method"      # impl 内 fn → method
        assert k["helper"][0] == "function"   # 顶层 fn → function


# ---------- Java ----------

class TestJava:
    def test_symbols_mapped(self):
        src = (
            "package app;\n"
            "import java.util.List;\n"
            "import java.util.Map;\n"
            "public class Cart {\n"
            "  private static final double RATE = 0.1;\n"
            "  public double total(List<Double> items) { return 0; }\n"
            "  public Cart() {}\n"
            "}\n"
        )
        k = kinds_of(src, "Cart.java")
        assert k["List"][0] == "import"
        assert k["Map"][0] == "import"
        assert k["Cart"][0] == "class"
        assert k["total"][0] == "method"
        assert k["RATE"][0] == "constant"     # static final → constant

    def test_plain_field_not_indexed(self):
        k = kinds_of("class Box { private int width; public int w() { return 0; } }",
                     "Box.java")
        assert "width" not in k               # 普通字段不进地图
        assert k["w"][0] == "method"


# ---------- 混合项目：发现 / 索引 / 检索 ----------

class TestMixedProject:
    def make_project(self, tmp_path, files):
        root = tmp_path / "proj"
        for rel, src in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        return root

    def test_discovers_supported_extensions_only(self, tmp_path):
        root = self.make_project(tmp_path, {
            "a.py": "x = 1\n", "b.js": "let x = 1;\n",
            "c.go": "package main\n", "d.rb": "def x; end\n",  # ruby 不支持
            "e.txt": "text\n", ".env": "SECRET=1\n",
        })
        files = repo_map.discover_source_files(str(root))
        rels = {os.path.relpath(f, str(root)) for f in files}
        assert rels == {"a.py", "b.js", "c.go"}

    def test_search_across_languages(self, tmp_path):
        root = self.make_project(tmp_path, {
            "cart.js": "class Cart {\n  total() { return 0; }\n}\n",
            "cart.go": "type Cart struct{}\nfunc (c *Cart) Total() float64 { return 0 }\n",
            "cart.py": "class Cart:\n    def total(self):\n        return 0\n",
        })
        out = repo_map.search_symbols(str(root), "Cart", kind="class")
        assert "cart.js" in out and "cart.go" in out and "cart.py" in out

    def test_repo_map_has_all_language_headers(self, tmp_path):
        root = self.make_project(tmp_path, {
            "srv/main.go": "package main\ntype Server struct{}\n",
            "web/app.js": "class App { render() {} }\n",
        })
        text = repo_map.build_repo_map(str(root), max_chars=2000)
        assert "srv/main.go" in text and "web/app.js" in text
        assert "Server" in text and "App" in text

    def test_js_references_ranked(self, tmp_path):
        root = self.make_project(tmp_path, {
            "core.js": "class Engine { run() {} }\n",
            "use.js": "const e = new Engine();\ne.run();\n",
        })
        idx = repo_map.index_repo(str(root))
        engine = next(s for s in idx.symbols if s.name == "Engine")
        assert engine.references >= 1
        assert idx.files[0] == "core.js"