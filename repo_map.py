"""代码库感知：符号索引 + repo map 生成（aider 式代码库地图，多语言自研版）。

语言适配层 = 提取器注册表（_EXTRACTORS）：每种扩展名对应一个提取函数。
    - .py 走标准库 ast（零依赖路径，符号精确、容错好）
    - 其他语言走 tree-sitter（lazy import：解析器与语言包只在仓库出现该语言时才加载，
      「用到才付」——语言包缺失时该语言提取静默为空，其余语言不受影响）
索引/排序/缓存/预算/检索逻辑与语言无关，加语言 = 加一个 @_extractor 注册函数。

已支持：python / javascript / typescript(含 tsx) / go / rust / java。
引用计数：.py 用 ast.Name 精确统计（Load 语境）；非 Python 语言用词边界文本计数
（注释/字符串会高估，但作为重要性排序的入度近似足够，且语言无关）。

对外接口：
    discover_source_files(root)      -> [绝对路径] 项目内待索引源码
    extract_symbols(source, relpath) -> [Symbol] 单文件符号（语言适配层入口）
    index_repo(root)                 -> RepoIndex
    build_repo_map(root, max_chars)  -> str 注入文本
    search_symbols(root, query, kind)-> str 供 search_symbols 工具调用
    build_repo_map_cached()          -> str 全局缓存版（agent 注入用）

纯函数 + 全局缓存；坏文件/缺语言包不阻塞索引。
"""

import ast
import os
import re
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
    sig: str = ""        # 如 `def foo(a, b)`（仅 .py 有；多语言留空）
    references: int = 0  # 被其他符号引用的次数（重要性排序依据）

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


# ---------------------------------------------------------------------------
# Python：标准库 ast（零依赖路径，精确计数）


def _func_sig(node: ast.AST, is_async: bool = False) -> str:
    args = []
    for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
        args.append(a.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    head = "async def" if is_async else "def"
    return f"{head} {node.name}({', '.join(args)})"[:80]


def _class_sig(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in node.bases] if node.bases else []
    return f"class {node.name}({', '.join(bases)})"[:80] if bases else f"class {node.name}"


def _is_constant(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant)


@_extractor(".py")
def _extract_python(source: str, relpath: str) -> list[Symbol]:
    """Python 符号提取：class/function/method/import/常量（模块级）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(Symbol(node.name, "class", relpath, node.lineno, _class_sig(node)))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(Symbol(
                        item.name, "method", relpath, item.lineno,
                        _func_sig(item, isinstance(item, ast.AsyncFunctionDef)),
                    ))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(Symbol(
                node.name, "function", relpath, node.lineno,
                _func_sig(node, isinstance(node, ast.AsyncFunctionDef)),
            ))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                value = node.value if isinstance(node, ast.Assign) else node.annotation
                if isinstance(t, ast.Name) and _is_constant(value):
                    symbols.append(Symbol(t.id, "constant", relpath, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(Symbol(alias.name.split(".")[0], "import", relpath, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    symbols.append(Symbol(alias.name, "import", relpath, node.lineno))
    return symbols


# ---------------------------------------------------------------------------
# JavaScript / TypeScript（含 tsx）


def _ts_import_names(node) -> list[str]:
    """从 import_statement 收集导入的符号名（含默认导入/命名导入/命名空间）。"""
    names = []
    for c in node.named_children:
        if c.type == "import_clause":
            names.extend(_ts_import_clause_names(c))
    return list(dict.fromkeys(names))


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


def _first_ident(node):
    stack = [node]
    while stack:
        n = stack.pop(0)
        if n.type in _IDENT_LEAF_TYPES and n.child_count == 0:
            return n
        stack.extend(n.named_children)
    return None


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
        elif t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            # TS 专属：类型定义也归入 class（模型需要知道这些类型存在）
            n = _name_of(node)
            if n:
                symbols.append(Symbol(n, "class", relpath, node.start_point[0] + 1))
        elif t == "import_statement":
            for n in _ts_import_names(node):
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


# ---------------------------------------------------------------------------
# 索引与引用计数


_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _word_re(name: str) -> re.Pattern:
    pat = _WORD_RE_CACHE.get(name)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(name)}\b")
        _WORD_RE_CACHE[name] = pat
    return pat


def _count_references(entries: list[tuple[str, str]], all_names: set[str]) -> Counter:
    """引用计数：.py 用 ast.Name 精确（Load 语境）；其余语言用词边界文本计数。

    文本计数会把注释/字符串里的同名也计入（高估），但作为重要性排序的入度
    近似足够——核心符号（被多处引用）天然排前。语言无关，零语法依赖。
    """
    refs: Counter = Counter()
    ast_names = all_names
    for source, rel in entries:
        if rel.endswith(".py"):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id in ast_names:
                        refs[node.id] += 1
        else:
            for name in all_names:
                refs[name] += len(_word_re(name).findall(source))
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
# search_symbols 工具的数据层


def search_symbols(root: str, query: str, kind: str | None = None,
                   limit: int = 20) -> str:
    index = _get_index(root)
    q = query.lower()
    matches = [
        s for s in index.symbols
        if q in s.name.lower() and (kind is None or s.kind == kind)
    ]
    matches.sort(key=lambda s: (-s.references, s.line, s.name))
    if not matches:
        hint = f"（kind 过滤 {kind}）" if kind else ""
        return f"未找到名称含 '{query}' 的符号{hint}。可用 read_file 直接读文件，或换关键词重试。"
    shown = matches[:limit]
    lines = [f"{s.file}:{s.line}  {s.label}" for s in shown]
    more = f"\n…还有 {len(matches) - len(shown)} 个匹配" if len(matches) > len(shown) else ""
    return "\n".join(lines) + more