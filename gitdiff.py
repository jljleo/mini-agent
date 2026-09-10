"""git diff 提取层：/review 的输入（Python 侧确定性提取，不经模型工具调用）。

为什么不是让模型 run_bash("git diff ...")：
- 上下文预算：真实 diff 动辄几千行，裸输出要么截断丢信息要么爆窗口——
  需要「stat 概览常驻 + 按文件分块、超预算降级为概览」的惰性结构
- 结构化：per-file 状态（新增/修改/删除/改名/二进制/未跟踪）+ 单文件 patch 独立获取
- 可复现：bench 的 review 任务按 commit range 定义，提取必须确定性

只读操作（等价于权限表里已 allow 的 git diff 只读命令），不进权限审批链。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import config

# collect() 详情预算：概览之外能放下多少 patch 正文
DEFAULT_BUDGET_CHARS = 60_000
# 单文件 patch 字符上限（超出截断并标注，全文可用 file_patch 再取）
DEFAULT_PER_FILE_CHARS = 8_000
# untracked 文件判二进制：头部含 NUL 即视为二进制
_BINARY_SNIFF_BYTES = 8192


class GitRepoError(RuntimeError):
    """非 git 仓库 / git 命令失败（如 revspec 不存在）。"""


@dataclass
class FileChange:
    """一个文件的变更元数据。added/deleted 为 None 表示二进制文件。"""

    path: str
    status: str                 # added / modified / deleted / renamed / untracked
    added: int | None
    deleted: int | None
    old_path: str | None = None  # renamed 的原路径


def _run_git(args: list[str], root: str, *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30,
    )
    if check and proc.returncode != 0:
        raise GitRepoError(proc.stderr.strip() or f"git {' '.join(args)} 失败")
    return proc


def _ensure_repo(root: str) -> None:
    proc = _run_git(["rev-parse", "--is-inside-work-tree"], root, check=False)
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise GitRepoError(f"{root} 不是 git 仓库（review 需要在 git 项目里运行）")


def _diff_revspec(spec: str | None) -> list[str]:
    """用户 spec → git diff 的 revspec 参数。

    - None/"" → ["HEAD"]：工作区 + 暂存区相对 HEAD 的全部改动
    - 含 ".."（main...HEAD / a..b）→ 原样透传（三点含 merge-base，语义交给 git）
    - 单个 rev（HEAD~3 / abc123）→ "<rev>^!"：该提交自身的改动（根提交会报错，可接受）
    """
    if not spec:
        return ["HEAD"]
    if ".." in spec:
        return [spec]
    return [f"{spec}^!"]


def list_changes(spec: str | None = None, root: str | None = None) -> list[FileChange]:
    """变更清单（概览层）：per-file 状态 + 增删行数；工作区模式附带 untracked 文件。

    用 --numstat -z 一次拿全：普通记录为 "added\\tdeleted\\tpath"，
    改名记录为 "added\\tdeleted\\t\\0old\\0new"（-z 下路径逐字给出，空格/括号无忧）。
    """
    root = root or config.PROJECT_ROOT
    _ensure_repo(root)
    revspec = _diff_revspec(spec)
    out = _run_git(["diff", "--numstat", "--find-renames", "-z", *revspec], root).stdout

    changes: list[FileChange] = []
    # -z 下记录边界需手工对齐：普通记录 1 个 NUL，改名记录额外跟 old/new 两个 NUL 字段
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        header = fields[i]
        if not header:
            i += 1
            continue
        added_s, deleted_s, path = (header.split("\t") + ["", "", ""])[:3]
        added = int(added_s) if added_s.isdigit() else None
        deleted = int(deleted_s) if deleted_s.isdigit() else None
        if not path:  # 空路径字段 = 改名记录，后接 old/new 两个字段
            old_path, new_path = fields[i + 1], fields[i + 2]
            changes.append(FileChange(new_path, "renamed", added, deleted, old_path=old_path))
            i += 3
            continue
        changes.append(FileChange(path, "modified", added, deleted))
        i += 1

    # name-status 补齐 A/D/R 语义（numstat 只有行数没有状态字母）
    status_map = _name_status(spec, root)
    for change in changes:
        change.status = status_map.get((change.old_path, change.path), status_map.get((None, change.path), change.status))

    if spec is None:
        changes.extend(_untracked(root))
    return changes


def _name_status(spec: str | None, root: str) -> dict[tuple[str | None, str], str]:
    """(old_path, new_path) → 状态词的映射表。"""
    out = _run_git(
        ["diff", "--name-status", "--find-renames", "-z", *_diff_revspec(spec)], root,
    ).stdout
    letter_map = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
    result: dict[tuple[str | None, str], str] = {}
    fields = [f for f in out.split("\0") if f]
    i = 0
    while i < len(fields):
        letter = fields[i]
        if letter.startswith("R"):
            old_path, new_path = fields[i + 1], fields[i + 2]
            result[(old_path, new_path)] = "renamed"
            i += 3
        else:
            result[(None, fields[i + 1])] = letter_map.get(letter[0], "modified")
            i += 2
    return result


def _untracked(root: str) -> list[FileChange]:
    """工作区模式附加：git 未跟踪的新文件（git diff 天然不含，review 工作区时漏掉它们是大坑）。"""
    out = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], root).stdout
    changes = []
    for path in [p for p in out.split("\0") if p]:
        full = os.path.join(root, path)
        try:
            with open(full, "rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\0" in head:
                changes.append(FileChange(path, "untracked", None, None))
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                added = sum(1 for _ in fh)
        except OSError:
            continue  # 竞态删除：静默跳过（清单是增强，不是依赖）
        changes.append(FileChange(path, "untracked", added, 0))
    return changes


def file_patch(
    path: str,
    spec: str | None = None,
    root: str | None = None,
    max_chars: int = DEFAULT_PER_FILE_CHARS,
) -> str:
    """单文件的 unified diff 正文（含 @@ 行号，模型可直接定位）。

    untracked 文件走 git diff --no-index /dev/null——合成标准的新文件 patch，
    与已跟踪文件的格式完全一致（退出码 1 是"有差异"的正常语义，不是错误）。
    """
    root = root or config.PROJECT_ROOT
    _ensure_repo(root)
    if _is_untracked(path, spec, root):
        proc = _run_git(["diff", "--no-index", "--", "/dev/null", path], root, check=False)
        patch = proc.stdout
    else:
        patch = _run_git(["diff", "--find-renames", *_diff_revspec(spec), "--", path], root).stdout
    if len(patch) > max_chars:
        patch = patch[:max_chars] + f"\n…（截断：全文 {len(patch)} 字符，可用 file_patch(max_chars=...) 续取）"
    return patch


def _is_untracked(path: str, spec: str | None, root: str) -> bool:
    if spec is not None:
        return False
    out = _run_git(["ls-files", "--others", "--exclude-standard", "-z", "--", path], root).stdout
    return path in out.split("\0")


def collect(
    spec: str | None = None,
    root: str | None = None,
    budget: int = DEFAULT_BUDGET_CHARS,
    per_file: int = DEFAULT_PER_FILE_CHARS,
) -> str:
    """一次取全：概览（全部文件）+ 预算内的 patch 详情，超预算文件降级为概览条目。

    输出是喂给 review prompt 的定稿文本（deterministic：同 spec 同输出）。
    """
    changes = list_changes(spec, root)
    if not changes:
        return "（无变更）"

    total_added = sum(c.added or 0 for c in changes)
    total_deleted = sum(c.deleted or 0 for c in changes)
    lines = [f"## 变更概览（{len(changes)} 个文件，+{total_added} -{total_deleted}）"]
    for c in changes:
        loc = "binary" if c.added is None else f"+{c.added} -{c.deleted}"
        renamed = f"（自 {c.old_path}）" if c.old_path else ""
        lines.append(f"{c.status:<10} {c.path}  {loc}{renamed}")

    lines.append("\n## 文件 diff")
    remaining = budget
    deferred = []
    for c in changes:
        patch = file_patch(c.path, spec, root, max_chars=per_file)
        if not patch.strip():
            continue  # 纯 mode 变更等无正文场景
        if len(patch) > remaining:
            deferred.append(c)
            continue
        lines.append(f"\n### {c.path}（{c.status}）\n```diff\n{patch.rstrip()}\n```")
        remaining -= len(patch)
    if deferred:
        lines.append("\n## 超预算仅列概览的文件（可用 file_patch 单独获取）")
        for c in deferred:
            loc = "binary" if c.added is None else f"+{c.added} -{c.deleted}"
            lines.append(f"- {c.path}  {loc}")
    return "\n".join(lines)
