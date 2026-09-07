"""代码库感知：符号索引 + repo map 生成（aider 式代码库地图，自研零依赖版）。

用标准库 ast 做 Python 符号提取——本项目纯 Python，零新依赖、零网络、完全自研
（roadmap 提的 tree-sitter 是为多语言通用性，本项目暂不需要；架构上留了语言适配
层 extract_symbols，未来接多语言只需补一个 extractor，对外接口不变）。

按「被引用次数」（入度）排序体现符号重要性，是轻量 PageRank 的近似：被别处
import / 调用的符号排前面，模型一眼看到项目里哪些是核心，取代 read_file + grep
盲探。

对外接口：
    discover_source_files(root)      -> [绝对路径] 项目内待索引源码（排除 .venv/.git 等）
    extract_symbols(source, relpath) -> [Symbol] 单文件符号（语言适配层入口）
    index_repo(root)                 -> RepoIndex（文件 → 符号 + 引用计数）
    build_repo_map(root, max_chars)  -> str 注入文本（受预算截断，头重尾轻）
    search_symbols(root, query, kind)-> str 供 search_symbols 工具调用
    build_repo_map_cached()          -> str 全局缓存版（agent 注入用，按文件 mtime 失效）

纯函数 + 全局缓存，测试用 tmp_path 建迷你项目即可，无网络、无副作用。
"""

import ast
import os
from collections import Counter
from dataclasses import dataclass, field

# 索引时排除的目录/文件（gitignore 思想的子集；.env 含密钥绝不索引）
_IGNORED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".idea", "node_modules", "dist", "build", ".mypy_cache", ".tox",
}
_IGNORED_FILES = {".env", ".session.json", ".chat_history", "session_todos.json"}
_SOURCE_EXT = (".py",)

# 符号类型排序权重：class 最核心，import 最次要（仅帮助模型知道有哪些模块可 import）
_KIND_RANK = {"class": 0, "function": 1, "method": 2, "constant": 3, "import": 4}

_IGNORED_ROOT = None  # 测试/bench 可替换根目录（与 tools.PROJECT_ROOT 同思想）


@dataclass
class Symbol:
    """一个代码符号：名字 + 类型 + 位置 + 简要签名。"""

    name: str
    kind: str            # class / function / method / import / constant
    file: str            # 相对项目根的路径
    line: int
    sig: str = ""        # 如 `def foo(a, b)` / `class Bar(Base)`（截断后）
    references: int = 0  # 被其他符号引用的次数（重要性排序依据）

    @property
    def label(self) -> str:
        """单行显示：`name (kind) · 被引用 N 次`；有签名则附签名。"""
        base = f"{self.name} ({self.kind}) · 引用 {self.references}"
        return f"{base} {self.sig}".rstrip()


@dataclass
class RepoIndex:
    """索引结果：符号表 + 引用计数，供 build_repo_map / search_symbols 复用。"""

    symbols: list[Symbol] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # 相对路径，按重要性降序


# ---------------------------------------------------------------------------
# 文件发现


def discover_source_files(root: str) -> list[str]:
    """列出项目内待索引的源码文件（绝对路径）。

    排除版本库/虚拟环境/缓存/密钥文件——这些要么与模型推理无关，要么含敏感
    信息（.env）绝不该进上下文。
    """
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地过滤：prune 掉被忽略目录，避免无谓下钻
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fn in filenames:
            if fn in _IGNORED_FILES:
                continue
            if fn.endswith(_SOURCE_EXT):
                files.append(os.path.join(dirpath, fn))
    return files


# ---------------------------------------------------------------------------
# 符号提取（语言适配层：接多语言时替换本函数，其余接口不变）


def _func_sig(node: ast.AST, is_async: bool = False) -> str:
    """从 FunctionDef/AsyncFunctionDef 生成 `def name(a, b)` 签名（截断防长）。"""
    args = []
    for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
        args.append(a.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    head = "async def" if is_async else "def"
    sig = f"{head} {node.name}({', '.join(args)})"
    return sig[:80]


def _class_sig(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in node.bases] if node.bases else []
    return f"class {node.name}({', '.join(bases)})"[:80] if bases else f"class {node.name}"


def _is_constant(value: ast.AST | None) -> bool:
    """判断赋值右侧是否为字面量常量（str/int/float/bool/None/bytes/常量表达式）。"""
    return isinstance(value, ast.Constant)


def extract_symbols(source: str, relpath: str) -> list[Symbol]:
    """解析单个 Python 源文件，提取顶层类/函数/常量/import + 类内方法。

    relpath: 相对项目根的路径（用于输出定位）。
    ast 解析失败返回空列表（坏文件不阻塞整个索引）。
    """
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
# 索引与引用计数


def _count_references(sources: list[tuple[str, str]]) -> Counter:
    """统计每个符号名被 ast.Name 引用的次数（跨文件，作为重要性入度）。

    简化：把全项目符号名放入全局集合，任一文件中出现的标识符命中即 +1。
    会受同名碰撞轻微高估，但对重要性排序足够——核心符号（被各处引用）天然
    排前面。轻量 PageRank 近似，零依赖、可解释。
    """
    all_names = set()
    for source, rel in sources:
        all_names.update(s.name for s in extract_symbols(source, rel))
    refs: Counter = Counter()
    for source, _ in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # 只统计标识符使用（Name 的 Load 语境）；Def/参数名不算"被引用"
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in all_names:
                    refs[node.id] += 1
    return refs


def _build_index(root: str, abs_files: list[str]) -> RepoIndex:
    """从文件列表构建索引：符号表 + 引用计数 + 文件重要性排序。"""
    raw: list[tuple[str, str]] = []  # (源码, 相对路径)
    for f in abs_files:
        rel = os.path.relpath(f, root)
        try:
            with open(f, encoding="utf-8") as fh:
                raw.append((fh.read(), rel))
        except OSError:
            continue
    refs = _count_references(raw)

    symbols: list[Symbol] = []
    for source, rel in raw:
        for s in extract_symbols(source, rel):
            s.references = refs.get(s.name, 0)
            symbols.append(s)

    # 文件重要性 = 文件内最高权重符号（class>function>method>constant>import）
    file_rank: dict[str, int] = {}
    for s in symbols:
        rank = _KIND_RANK.get(s.kind, 5) * 100_000 - s.references
        if s.file not in file_rank or rank < file_rank[s.file]:
            file_rank[s.file] = rank
    files = sorted(file_rank, key=lambda f: (file_rank[f], f))

    # 文件内符号：kind 权重升序 + 引用降序 + 行号升序
    symbols.sort(key=lambda s: (_KIND_RANK.get(s.kind, 9), -s.references, s.line, s.name))
    return RepoIndex(symbols=symbols, files=files)


# ---------------------------------------------------------------------------
# 全局缓存（按文件 mtime+size 签名失效；编辑文件后地图自动刷新）


_index_cache: dict[tuple, RepoIndex] = {}


def _dir_signature(root: str) -> tuple:
    """目录签名：(相对路径, mtime_ns, size) 元组——任一文件变化即整体失效。"""
    sig = []
    for f in discover_source_files(root):
        try:
            st = os.stat(f)
        except OSError:
            continue
        sig.append((os.path.relpath(f, root), st.st_mtime_ns, st.st_size))
    return tuple(sorted(sig))


def _get_index(root: str) -> RepoIndex:
    """取（并缓存）指定根的索引；签名变化时重建。"""
    sig = _dir_signature(root)
    cached = _index_cache.get(sig)
    if cached is not None:
        return cached
    idx = _build_index(root, [os.path.join(root, r) for r, _, _ in sig])
    _index_cache[sig] = idx
    return idx


def index_repo(root: str) -> RepoIndex:
    """公开索引入口（search_symbols / 测试用），内部走缓存。"""
    return _get_index(root)


# ---------------------------------------------------------------------------
# repo map 生成（注入 system prompt 用）


def build_repo_map(root: str, max_chars: int = 3000) -> str:
    """生成代码库地图文本：受 max_chars 预算截断，头重尾轻。

    头部是最高重要性文件及其核心符号（class/function），预算用尽即停——
    模型优先看到"项目里最关键的东西"，明细靠 search_symbols / read_file 惰性取。
    """
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
            # 预算不足整文件时，至少塞下文件头，让模型知道"有这个文件"
            if len(header) <= budget:
                out.append(header)
            break
        out.extend(block)
        budget -= len(text)
    n_syms = len(index.symbols)
    return f"[代码库地图 {len(index.files)} 文件 / {n_syms} 符号，预算 {max_chars} 字符；定位用 search_symbols]\n" + "\n".join(out)


def build_repo_map_cached(root: str | None = None, max_chars: int = 3000) -> str:
    """agent 注入用的缓存入口：root 缺省取默认项目根（config.PROJECT_ROOT）。"""
    if root is None:
        root = _default_root()
    return build_repo_map(root, max_chars)


def _default_root() -> str:
    """默认项目根：可被测试/bench 替换的模块级变量，否则取 config 常量。"""
    if _IGNORED_ROOT:
        return _IGNORED_ROOT
    from config import PROJECT_ROOT
    return PROJECT_ROOT


# ---------------------------------------------------------------------------
# search_symbols 工具的数据层


def search_symbols(root: str, query: str, kind: str | None = None,
                   limit: int = 20) -> str:
    """按名称子串（大小写不敏感）检索符号，可选 kind 过滤；按引用降序返回。

    返回格式化文本，供 tools.search_symbols 直接回传模型。
    """
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
