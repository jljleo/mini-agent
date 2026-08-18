"""业务工具实现：用 @tool 装饰器注册到 registry。

文件工具（read_file/write_file/edit_file）为窄接口：路径围栏代码级强制、
免人工确认；run_bash 为通用接口：白名单免确认 + 其余人工审批。
两者并存——文件操作走窄接口，真正的命令走 bash。
"""

import os
import subprocess

from config import MAX_OUTPUT_LEN, MAX_TIMEOUT, PROJECT_ROOT
from input_utils import confirm
from registry import tool


# ---------- 文件窄接口工具：路径围栏 + 免确认 ----------


def _resolve_safe_path(path: str) -> str:
    """把模型给的路径解析为绝对路径，并强制限制在项目目录内（含符号链接防护）。"""
    full_path = os.path.realpath(os.path.join(PROJECT_ROOT, path))
    if full_path != PROJECT_ROOT and not full_path.startswith(PROJECT_ROOT + os.sep):
        raise ValueError("Path must be within the project root.")
    return full_path


@tool(
    "read_file",
    "Read the content of a file within the project directory.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root.",
        },
    },
    ["path"],
)
def read_file(path: str) -> str:
    with open(_resolve_safe_path(path), "r", encoding="utf-8") as f:
        content = f.read()
    # 截断保护：大文件灌爆上下文是 agent 常见死法
    if len(content) > MAX_OUTPUT_LEN:
        content = content[:MAX_OUTPUT_LEN] + "\n... [内容过长，已截断]"
    return content


@tool(
    "write_file",
    "Write content to a file within the project directory. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root.",
        },
        "content": {
            "type": "string",
            "description": "The content to write to the file.",
        },
    },
    ["path", "content"],
)
def write_file(path: str, content: str) -> str:
    full = _resolve_safe_path(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Content written to {path}"


@tool(
    "edit_file",
    "Edit a file by exact text replacement. The old text must appear exactly ONCE in the file; on failure, read_file first to check the current content.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root.",
        },
        "old": {
            "type": "string",
            "description": "The exact text to replace. Must appear exactly ONCE in the file; include enough surrounding context (e.g. a few lines) to make it unique.",
        },
        "new": {
            "type": "string",
            "description": "The new text to insert.",
        },
    },
    ["path", "old", "new"],
)
def edit_file(path: str, old: str, new: str) -> str:
    full = _resolve_safe_path(path)
    with open(full, "r", encoding="utf-8") as f:
        content = f.read()

    preview = old if len(old) <= 50 else old[:50] + "..."
    count = content.count(old)
    if count == 0:
        raise ValueError(f"Old text '{preview}' not found in {path}; please read_file first to check the current content")
    if count > 1:
        raise ValueError(f"Old text '{preview}' found {count} times in {path}; provide more context to make it unique")

    content = content.replace(old, new, 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Edited {path}: replaced 1 occurrence"


# 只读安全命令白名单：命中则免确认直接执行；
# 其余命令一律需要人工确认（默认怀疑，而非默认信任——白名单外的世界交给用户审查）
SAFE_PREFIXES = (
    "ls", "pwd", "cat ", "echo ", "grep", "find", "head", "tail", "wc", "date",
    "git status", "git log", "git diff", "git show", "git branch",
)

# 危险命令关键词：即使在确认环节也用红色高亮提醒（黑名单仅作提示，不能替代人工审查）
DANGEROUS_PATTERNS = [
    "rm ", "sudo", "mv ", "> /", "curl", "wget",
    "chmod", "kill", "shutdown", "reboot", "mkfs", "dd ",
]


def _is_safe(command: str) -> bool:
    """命令是否命中只读白名单（免确认）。"""
    return command.strip().startswith(SAFE_PREFIXES)


def _confirm(command: str) -> bool:
    """非白名单命令请求用户审批。统一委托给 input_utils.confirm（prompt_toolkit 单键确认）。

    全程序只有一套输入体系，避免与主输入的 prompt_toolkit 抢 stdin 导致缓冲冲突。
    """
    is_dangerous = any(p in command for p in DANGEROUS_PATTERNS)
    return confirm(command, dangerous=is_dangerous)


@tool(
    "run_bash",
    "Run a bash command in the project root directory and return stdout/stderr with "
    "exit code. Read-only commands run directly; all other commands require interactive "
    "user confirmation. Has timeout (max 120s) and output truncation.",
    {
        "command": {
            "type": "string",
            "description": "The bash command to execute.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default 30).",
        },
    },
    ["command"],
)
def run_bash(command: str, timeout: int = 30) -> str:
    """在项目根目录执行 bash 命令，返回 exit code + stdout/stderr。

    安全模型：只读白名单免确认，其余命令需用户确认，危险关键词高亮提醒。
    注意 cwd=PROJECT_ROOT 只缩小误伤半径，并非沙箱——绝对路径访问不受限，
    人工确认是最后一道防线。
    """
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))  # 钳制在 [1, MAX_TIMEOUT]

    if not _is_safe(command) and not _confirm(command):
        return "用户拒绝了该命令的执行"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            errors="replace",  # 命令输出含非 UTF-8 字节时替换而非崩溃
            timeout=timeout,
            cwd=PROJECT_ROOT,  # 固定工作目录为项目根目录，缩小爆炸半径
        )
    except subprocess.TimeoutExpired:
        return f"执行超时（>{timeout}s），已强制终止"

    output = result.stdout
    if result.stderr:
        output += f"\n[stderr]\n{result.stderr}"
    output = output.strip() or "(无输出)"

    if len(output) > MAX_OUTPUT_LEN:
        output = output[:MAX_OUTPUT_LEN] + "\n... [输出过长，已截断]"

    return f"[exit code: {result.returncode}]\n{output}"
