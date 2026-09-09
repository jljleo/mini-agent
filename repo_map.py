"""代码库感知：符号索引 + repo map 生成（aider 式代码库地图，全语言统一 tree-sitter）。

语言适配层 = 提取器注册表（_EXTRACTORS）：每种扩展名对应一个提取函数。
全部语言统一走 tree-sitter（lazy import：解析器与语言包只在仓库出现该语言时才加载，
「用到才付」——语言包缺失时该语言提取静默为空，其余语言不受影响）。

已支持：python / javascript / typescript(含 tsx) / go / rust / java。
统一契约（无语言特例）：
    - 符号：class（含 interface/type/struct/trait）+ function + method + import + constant
    - 引用计数：所有语言数 identifier 叶子节点（注释/字符串是独立节点，天然不计入），
      定义处也计入（近似「出现次数」，作为重要性入度的排序依据足够且完全一致）
    - 容错：tree-sitter 增量解析，坏文件出部分树，能捞多少是多少，绝不阻塞索引

对外接口：
    discover_source_files(root)      -> [绝对路径] 项目内待索引源码
    extract_symbols(source, relpath) -> [Symbol] 单文件符号（语言适配层入口）
    index_repo(root)                 -> RepoIndex
    build_repo_map(root, max_chars)  -> str 注入文本
    search_symbols(root, query, kind)-> str 供 search_symbols 工具调用
    build_repo_map_cached()          -> str 全局缓存版（agent 注入用）

纯函数 + 全局缓存（mtime 签名失效）；加语言 = 注册 @_extractor + 对应语言包。
"""

import os
from collections import Counter
from dataclasses import dataclass, field

# 索引时排除的目录/文件（.env 含密钥绝不索引）
_IGNORED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".idea", "node_modules", "dist", "build", ".mypy_cache", ".tox", ".next",
}
_IGNORED_FILES = {".env", ".session.json", ".chat_history", "session_todos.json"}

# 符号类型排序权重：class 最核心，import 最次要
_KIND_RANK = {"class": 0, "function": 1, "method": 2, "constant": 3, "import": 4}

_IGNORED_ROOT = None  # 测试/bench 可替换根目录


@dataclass
class Symbol:
    """一个代码符号：名字 + 类型 + 位置 + 简要签名。"""

    name: str
    kind: str            # class / function / method / import / constant
    file: str            # 相对项目根的路径
    line: int
    sig: str = ""        # 统一契约下留空（细节靠 read_file）
    references: int = 0  # 被引用的近似次数（identifier 出现次数，重要性排序依据）

    @property
    def label(self) -> str:
        base = f"{self.name} ({self.kind}) · 引用 {self.references}"
        return f"{base} {self.sig}".rstrip()


@dataclass
class RepoIndex:
    """索引结果：符号表 + 引用计数，供 build_repo_map / search_symbols 复用。"""

    symbols: list[Symbol] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # 相对路径，按重要性降序


# ---------------------------------------------------------------------------
# 提取器注册表（语言适配层：加语言 = 加一个 @_extractor 注册函数）


_EXTRACTORS: dict[str, callable] = {}


def _extractor(*exts: str):
    """注册提取器：扩展名（小写、带点）→ (source, relpath) -> [Symbol]。"""

    def deco(fn):
        for ext in exts:
            _EXTRACTORS[ext] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# tree-sitter 通用基座（lazy import：解析器/语言包用到才加载）


_LANG_GETTERS: dict[str, callable] = {}  # 扩展名 → 语言 getter（引用计数/解析用）


def _register_lang(exts: tuple[str, ...], getter):
    for ext in exts:
        _LANG_GETTERS[ext] = getter


def _ts_parse(source: str, language_getter) -> object | None:
    """解析源码返回根节点；tree-sitter 未装/语言包缺失/解析失败都返回 None。"""
    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        lang = Language(language_getter())
        root = Parser(lang).parse(source.encode("utf-8")).root_node
    except Exception:
        return None
    return root


_IDENT_LEAF_TYPES = {
    "identifier", "type_identifier", "field_identifier", "property_identifier",
    "shorthand_property_identifier", "shorthand_property_identifier_pattern",
}


def _name_of(node) -> str | None:
    """取节点标识符名字：优先 name 字段，回落第一个标识符叶子。"""
    c = node.child_by_field_name("name")
    if c is not None and c.type in _IDENT_LEAF_TYPES:
        try:
            return c.text.decode("utf-8")
        except Exception:
            return None
    stack = list(node.named_children)
    while stack:
        n = stack.pop(0)
        if n.type in _IDENT_LEAF_TYPES and n.child_count == 0:
            try:
                return n.text.decode("utf-8")
            except Exception:
                return None
        stack.extend(n.named_children)
    return None


def _first_ident(node):
    """深度优先找第一个标识符叶子（import 取符号名用）。"""
    stack = [node]
    while stack:
        n = stack.pop(0)
        if n.type in _IDENT_LEAF_TYPES and n.child_count == 0:
            return n
        stack.extend(n.named_children)
    return None


# ---------------------------------------------------------------------------
# Python


_PY_LITERAL_TYPES = {
    "integer", "float", "string", "concatenated_string", "bytes",
    "true", "false", "none", "complex_number",
}


def _py_module_name(node) -> str | None:
    """dotted_name 'a.b' → 'a'；aliased_import → 内部名；identifier → 自身。"""
    if node.type == "dotted_name":
        return node.text.decode("utf-8").split(".")[0]
    if node.type == "aliased_import":
        for c in node.named_children:
            if c.type == "dotted_name":
                return c.text.decode("utf-8").split(".")[0]
            if c.type == "identifier":
                return c.text.decode("utf-8")
        return None
    if node.type == "identifier":
        return node.text.decode("utf-8")
    return None


def _py_import_names(node) -> list[str]:
    """import_statement / import_from_statement → 导入的符号名。"""
    names = []
    if node.type == "import_statement":
        for c in node.named_children:
            n = _py_module_name(c)
            if n:
                names.append(n)
    else:  # import_from_statement：首个 named child 是 from 模块，其余是符号
        for c in node.named_children[1:]:
            n = _py_module_name(c)
            if n:
                names.append(n)
    return names


@_extractor(".py")
def _extract_python(source: str, relpath: str) -> list[Symbol]:
    """Python 符号提取：class/function/method/import/常量（模块级 + 类级）。"""
    root = _ts_parse(source, _py_lang)
    if root is None:
        return []
    symbols: list[Symbol] = []

    def walk(node, *, in_class: bool = False) -> None:
        t = node.type
        if t == "class_definition":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
            for c in node.named_children:
                walk(c, in_class=True)
            return  # 类内已深入（block）
        if t == "function_definition":
            n = _name_of(node)
            if n:
                kind = "method" if in_class else "function"
                symbols.append(Symbol(n, kind, relpath, node.start_point[0] + 1))
            return  # 不深入函数体：嵌套 def / 局部变量不进地图
        if t == "decorated_definition":
            # 装饰器包着的 class/function：直接处理被装饰者，跳过 decorator 细节
            for c in node.named_children:
                if c.type in ("class_definition", "function_definition"):
                    walk(c, in_class=in_class)
            return
        if t in ("import_statement", "import_from_statement"):
            for n in _py_import_names(node):
                symbols.append(Symbol(n, "import", relpath, node.start_point[0] + 1))
            return
        if t == "expression_statement":
            for c in node.named_children:
                if c.type != "assignment":
                    continue
                left = c.child_by_field_name("left")
                right = c.child_by_field_name("right")
                if (left and left.type == "identifier" and right
                        and right.type in _PY_LITERAL_TYPES):
                    symbols.append(Symbol(left.text.decode("utf-8"), "constant",
                                          relpath, c.start_point[0] + 1))
            return
        for c in node.named_children:
            walk(c, in_class=in_class)

    walk(root)
    return symbols


def _py_lang():
    import tree_sitter_python
    return tree_sitter_python.language()


_register_lang((".py",), _py_lang)


# ---------------------------------------------------------------------------
# JavaScript / TypeScript（含 tsx）


def _ts_import_clause_names(clause) -> list[str]:
    names = []
    for c in clause.named_children:
        if c.type == "identifier":
            names.append(c.text.decode("utf-8"))          # 默认导入 fs
        elif c.type == "namespace_import":                 # * as ns
            n = _name_of(c)
            if n:
                names.append(n)
        elif c.type == "named_imports":                    # { a, b as c }
            for imp in c.named_children:
                if imp.type == "import_specifier":
                    n = _name_of(imp)
                    if n:
                        names.append(n)
    return names


def _extract_js_ts(source: str, relpath: str, language_getter) -> list[Symbol]:
    root = _ts_parse(source, language_getter)
    if root is None:
        return []
    symbols: list[Symbol] = []

    def walk(node, *, in_class: bool = False) -> None:
        t = node.type
        if t == "class_declaration":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
        elif t == "method_definition":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "method", relpath, node.start_point[0] + 1))
        elif t == "function_declaration":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "function", relpath, node.start_point[0] + 1))
        elif t == "lexical_declaration":
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                n = _name_of(decl)
                if not n:
                    continue
                value = decl.child_by_field_name("value")
                kind = "function" if value and value.type in ("arrow_function", "function") else "constant"
                symbols.append(Symbol(n, kind, relpath, decl.start_point[0] + 1))
        elif t == "import_statement":
            for c in node.named_children:
                if c.type == "import_clause":
                    for n in _ts_import_clause_names(c):
                        symbols.append(Symbol(n, "import", relpath, node.start_point[0] + 1))
        for c in node.named_children:
            walk(c, in_class=in_class)

    walk(root)
    return symbols


def _js_lang():
    import tree_sitter_javascript
    return tree_sitter_javascript.language()


def _ts_lang():
    import tree_sitter_typescript
    return tree_sitter_typescript.language_typescript()


def _tsx_lang():
    import tree_sitter_typescript
    return tree_sitter_typescript.language_tsx()


@_extractor(".js", ".jsx", ".mjs", ".cjs")
def _extract_javascript(source: str, relpath: str) -> list[Symbol]:
    return _extract_js_ts(source, relpath, _js_lang)


@_extractor(".ts", ".mts", ".cts")
def _extract_typescript(source: str, relpath: str) -> list[Symbol]:
    return _extract_js_ts(source, relpath, _ts_lang)


@_extractor(".tsx")
def _extract_tsx(source: str, relpath: str) -> list[Symbol]:
    return _extract_js_ts(source, relpath, _tsx_lang)


_register_lang((".js", ".jsx", ".mjs", ".cjs"), _js_lang)
_register_lang((".ts", ".mts", ".cts"), _ts_lang)
_register_lang((".tsx",), _tsx_lang)


# ---------------------------------------------------------------------------
# Go


@_extractor(".go")
def _extract_go(source: str, relpath: str) -> list[Symbol]:
    root = _ts_parse(source, _go_lang)
    if root is None:
        return []
    symbols: list[Symbol] = []

    def walk(node) -> None:
        t = node.type
        if t == "type_declaration":
            for spec in node.named_children:
                if spec.type == "type_spec":
                    n = _name_of(spec)
                    if n:
                        symbols.append(Symbol(n, "class", relpath, spec.start_point[0] + 1))
        elif t == "method_declaration":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "method", relpath, node.start_point[0] + 1))
        elif t == "function_declaration":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "function", relpath, node.start_point[0] + 1))
        elif t == "const_declaration":
            for spec in node.named_children:
                if spec.type == "const_spec":
                    n = _name_of(spec)
                    if n:
                        symbols.append(Symbol(n, "constant", relpath, spec.start_point[0] + 1))
        elif t == "import_declaration":
            for spec in node.named_children:
                if spec.type == "import_spec":
                    for leaf in spec.named_children:
                        if leaf.type == "interpreted_string_literal":
                            path = leaf.text.decode("utf-8").strip('"')
                            symbols.append(Symbol(path.rsplit("/", 1)[-1], "import",
                                                  relpath, leaf.start_point[0] + 1))
                            break
        for c in node.named_children:
            walk(c)

    walk(root)
    return symbols


def _go_lang():
    import tree_sitter_go
    return tree_sitter_go.language()


_register_lang((".go",), _go_lang)


# ---------------------------------------------------------------------------
# Rust


@_extractor(".rs")
def _extract_rust(source: str, relpath: str) -> list[Symbol]:
    root = _ts_parse(source, _rs_lang)
    if root is None:
        return []
    symbols: list[Symbol] = []

    def walk(node, *, in_impl: bool = False) -> None:
        t = node.type
        if t == "function_item":
            n = _name_of(node)
            if n:
                kind = "method" if in_impl else "function"
                symbols.append(Symbol(n, kind, relpath, node.start_point[0] + 1))
        elif t == "impl_item":
            for c in node.named_children:
                walk(c, in_impl=True)
            return
        elif t in ("struct_item", "enum_item", "union_item", "trait_item", "type_item"):
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
        elif t == "const_item":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "constant", relpath, node.start_point[0] + 1))
        elif t == "use_declaration":
            leaf = _first_ident(node)
            if leaf is not None:
                symbols.append(Symbol(leaf.text.decode("utf-8"), "import",
                                      relpath, node.start_point[0] + 1))
        for c in node.named_children:
            walk(c, in_impl=in_impl)

    walk(root)
    return symbols


def _rs_lang():
    import tree_sitter_rust
    return tree_sitter_rust.language()


_register_lang((".rs",), _rs_lang)


# ---------------------------------------------------------------------------
# Java


@_extractor(".java")
def _extract_java(source: str, relpath: str) -> list[Symbol]:
    root = _ts_parse(source, _java_lang)
    if root is None:
        return []
    symbols: list[Symbol] = []

    def walk(node) -> None:
        t = node.type
        if t in ("class_declaration", "interface_declaration",
                 "enum_declaration", "record_declaration"):
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
        elif t == "method_declaration":
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "method", relpath, node.start_point[0] + 1))
        # 构造函数与类同名：跳过（与 class 冲突会吞掉类的检索结果）
        elif t == "import_declaration":
            leaf = _first_ident(node)
            if leaf is not None:
                symbols.append(Symbol(leaf.text.decode("utf-8"), "import",
                                      relpath, node.start_point[0] + 1))
        elif t == "field_declaration":
            # 只收 static final 常量（普通字段太多会污染地图）
            mods = [m.text.decode("utf-8") for m in node.named_children if m.type == "modifiers"]
            if mods and "static" in mods[0] and "final" in mods[0]:
                for decl in node.named_children:
                    if decl.type == "variable_declarator":
                        n = _name_of(decl)
                        if n:
                            symbols.append(Symbol(n, "constant", relpath,
                                                  node.start_point[0] + 1))
        for c in node.named_children:
            walk(c)

    walk(root)
    return symbols


def _java_lang():
    import tree_sitter_java
    return tree_sitter_java.language()


_register_lang((".java",), _java_lang)


# ---------------------------------------------------------------------------
# 文件发现与符号提取入口


_SOURCE_EXTS = frozenset(_EXTRACTORS)  # 与注册表单一事实来源


def discover_source_files(root: str) -> list[str]:
    """列出项目内待索引源码（已知扩展名），排除版本库/虚拟环境/缓存/密钥。"""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fn in filenames:
            if fn in _IGNORED_FILES:
                continue
            ext = os.path.splitext(fn)[1]
            if ext in _SOURCE_EXTS:
                files.append(os.path.join(dirpath, fn))
    return files


def extract_symbols(source: str, relpath: str) -> list[Symbol]:
    """按扩展名分发到语言提取器；未知语言/缺失语言包返回空（尽力而为）。"""
    ext = os.path.splitext(relpath)[1]
    fn = _EXTRACTORS.get(ext)
    if fn is None:
        return []
    try:
        return fn(source, relpath)
    except Exception:
        return []


def _parse_root(source: str, relpath: str) -> object | None:
    """按扩展名取语言解析器，返回根节点（引用计数/提取共用同一解析路径）。"""
    ext = os.path.splitext(relpath)[1]
    getter = _LANG_GETTERS.get(ext)
    if getter is None:
        return None
    return _ts_parse(source, getter)


# ---------------------------------------------------------------------------
# 索引与引用计数


def _collect_identifiers(root) -> Counter:
    """数一棵树里所有标识符叶子（注释/字符串是独立节点，天然不计入）。"""
    counts: Counter = Counter()
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in _IDENT_LEAF_TYPES and n.child_count == 0:
            try:
                counts[n.text.decode("utf-8")] += 1
            except Exception:
                pass
        stack.extend(n.named_children)
    return counts


def _count_references(entries: list[tuple[str, str]], all_names: set[str]) -> Counter:
    """统一引用计数：所有语言数 identifier 叶子。

    定义处/属性访问也计入（近似「出现次数」）——注释里、字符串里的同名不会
    混入（它们是独立节点类型）。排序语义与语言无关，核心符号天然排前。
    """
    refs: Counter = Counter()
    for source, rel in entries:
        root = _parse_root(source, rel)
        if root is None:
            continue
        counts = _collect_identifiers(root)
        for name in all_names:
            if name in counts:
                refs[name] += counts[name]
    return refs


def _build_index(root: str, abs_files: list[str]) -> RepoIndex:
    raw: list[tuple[str, str]] = []
    for f in abs_files:
        rel = os.path.relpath(f, root)
        try:
            with open(f, encoding="utf-8") as fh:
                raw.append((fh.read(), rel))
        except OSError:
            continue

    all_names: set[str] = set()
    for source, rel in raw:
        all_names.update(s.name for s in extract_symbols(source, rel))
    refs = _count_references(raw, all_names)

    symbols: list[Symbol] = []
    for source, rel in raw:
        for s in extract_symbols(source, rel):
            s.references = refs.get(s.name, 0)
            symbols.append(s)

    file_rank: dict[str, int] = {}
    for s in symbols:
        rank = _KIND_RANK.get(s.kind, 5) * 100_000 - s.references
        if s.file not in file_rank or rank < file_rank[s.file]:
            file_rank[s.file] = rank
    files = sorted(file_rank, key=lambda f: (file_rank[f], f))

    symbols.sort(key=lambda s: (_KIND_RANK.get(s.kind, 9), -s.references, s.line, s.name))
    return RepoIndex(symbols=symbols, files=files)


# ---------------------------------------------------------------------------
# 全局缓存（按文件 mtime+size 签名失效）


_index_cache: dict[tuple, RepoIndex] = {}


def _dir_signature(root: str) -> tuple:
    sig = []
    for f in discover_source_files(root):
        try:
            st = os.stat(f)
        except OSError:
            continue
        sig.append((os.path.relpath(f, root), st.st_mtime_ns, st.st_size))
    return tuple(sorted(sig))


def _get_index(root: str) -> RepoIndex:
    sig = _dir_signature(root)
    cached = _index_cache.get(sig)
    if cached is not None:
        return cached
    idx = _build_index(root, [os.path.join(root, r) for r, _, _ in sig])
    _index_cache[sig] = idx
    return idx


def index_repo(root: str) -> RepoIndex:
    return _get_index(root)


# ---------------------------------------------------------------------------
# repo map 生成（注入 system prompt 用）


def build_repo_map(root: str, max_chars: int = 3000) -> str:
    index = _get_index(root)
    if not index.files:
        return ""
    out: list[str] = []
    budget = max_chars
    for rel in index.files:
        header = rel
        block = [header]
        for s in index.symbols:
            if s.file != rel:
                continue
            block.append(f"  {s.label}")
        text = "\n".join(block) + "\n"
        if len(text) > budget:
            if len(header) <= budget:
                out.append(header)
            break
        out.extend(block)
        budget -= len(text)
    n_syms = len(index.symbols)
    return f"[代码库地图 {len(index.files)} 文件 / {n_syms} 符号，预算 {max_chars} 字符；定位用 search_symbols]\n" + "\n".join(out)


def build_repo_map_cached(root: str | None = None, max_chars: int = 3000) -> str:
    if root is None:
        root = _default_root()
    return build_repo_map(root, max_chars)


def _default_root() -> str:
    if _IGNORED_ROOT:
        return _IGNORED_ROOT
    from config import PROJECT_ROOT
    return PROJECT_ROOT


# ---------------------------------------------------------------------------
# 语法自检（edit_file 改后闭环用的零成本冒烟：opencode 式 diagnostics 的轻量版）


def syntax_diagnostics(source: str, relpath: str, limit: int = 5) -> list[str]:
    """解析源码，返回语法错误列表（空 = 通过）。

    供 edit_file 等在改动文件后做零成本自检：坏代码必然产出 ERROR/MISSING 节点，
    带行号回喂给模型（49 条「验证信号：编译器/typecheck 优先」的最小形态，
    模型下一轮就看到自己改坏了，当场自愈）。语言不支持/解析器缺失返回空（不误报）。
    """
    root = _parse_root(source, relpath)
    if root is None:
        return []
    errors: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or getattr(n, "is_missing", False):
            line = n.start_point[0] + 1
            snippet = (n.text.decode("utf-8", "replace") or "")[:60]
            errors.append(f"{relpath}:{line} 语法错误：{snippet!r}")
            if len(errors) >= limit:
                break
            continue  # 不深入 ERROR/MISSING 内部（避免重复与海量报错）
        stack.extend(n.named_children)
    return errors


# ---------------------------------------------------------------------------
# search_symbols 工具的数据层


def search_symbols(root: str, query: str, kind: str | None = None,
                   limit: int = 20, scope: str = "name") -> str:
    """按关键词检索代码，返回 file:line。维度由 scope 控制：

    name（默认）= 符号名称子串（E3 实测：乱命名库下概念性失灵——
        查询词是模型的语义记忆，与库的任意名字无交集；miss 时自动并跑 docs 兜底）
    path        = 文件路径/文件名片段（烂命名下路径语义仍存，是最后防线）
    docs        = 注释与正文行包含（搜业务语义词，如中文注释"去重"）
    """
    q = query.lower()
    more = ""
    if scope == "path":
        index = _get_index(root)
        files = sorted({s.file for s in index.symbols if q in s.file.lower()})
        if not files:
            return f"未找到路径含 '{query}' 的文件。可用 read_file 直接读文件，或换关键词重试。"
        shown = files[:limit]
        lines = [f"{f}  {f}" for f in shown]
        more = f"\n…还有 {len(files) - len(shown)} 个" if len(files) > len(shown) else ""
        return "\n".join(lines) + more

    if scope == "docs":
        hits = _docs_search_lines(root, q, limit)
        if not hits:
            return f"未找到正文含 '{query}' 的行。可用 read_file 直接读文件，或换关键词重试。"
        hits.sort(key=lambda h: (h[0], h[1]))
        shown = hits[:limit]
        lines = [f"{f}:{i}  {t}" for f, i, t in shown]
        more = f"\n…还有 {len(hits) - len(shown)} 个" if len(hits) > len(shown) else ""
        return "\n".join(lines) + more

    index = _get_index(root)
    matches = [
        s for s in index.symbols
        if q in s.name.lower() and (kind is None or s.kind == kind)
    ]
    matches.sort(key=lambda s: (-s.references, s.line, s.name))
    if not matches:
        hint = f"（kind 过滤 {kind}）" if kind else ""
        # E4 实测：乱命名库（q1/helper2）name 检索概念性失灵。主动兜底二连：
        #   ① 自动并跑 docs 检索（把"模型可能想不到的下一步"变成工具默认做完）
        #   ② 仍无命中才给引导文本
        if kind is None:
            docs_hits = _docs_search_lines(root, q, limit)
            if docs_hits:
                dlines = [f"{f}:{i}  {t}" for f, i, t in docs_hits]
                return (f"名称含 '{query}' 的符号未找到（{hint or '无 kind 过滤'}），但正文命中：\n"
                        + "\n".join(dlines))
        return (f"未找到名称含 '{query}' 的符号{hint}。若库命名混乱（无意义标识符），"
                f"可改用 scope=docs 搜业务词（如中文注释/变量名），或 scope=path 按文件路径检索；"
                f"也可 read_file 直接读文件。")
    shown = matches[:limit]
    lines = [f"{s.file}:{s.line}  {s.label}" for s in shown]
    more = f"\n…还有 {len(matches) - len(shown)} 个匹配" if len(matches) > len(shown) else ""
    return "\n".join(lines) + more


def _docs_search_lines(root: str, q: str, limit: int) -> list[tuple[str, int, str]]:
    """docs 检索的共用实现：返回 (文件, 行号, 行文本) 列表。"""
    hits: list[tuple[str, int, str]] = []
    for f in discover_source_files(root):
        rel = os.path.relpath(f, root)
        try:
            with open(f, encoding="utf-8") as fh:
                for i, line in enumerate(fh.read().splitlines(), 1):
                    if q in line.lower():
                        hits.append((rel, i, line.strip()[:60]))
        except OSError:
            continue
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits[:limit]