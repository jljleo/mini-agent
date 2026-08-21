"""业务工具实现：用 @tool 装饰器注册到 registry。

文件工具（read_file/write_file/edit_file）为窄接口：路径围栏代码级强制、
免人工确认；run_bash 为通用接口：permissions.json 规则裁决 + 人工审批兑底。
两者并存——文件操作走窄接口，真正的命令走 bash。
"""
import json
import os
import re
import subprocess

from config import MAX_OUTPUT_LEN, MAX_TIMEOUT, PROJECT_ROOT
from input_utils import confirm
from tool_registry import tool


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
        raise ValueError(
            f"Old text '{preview}' not found in {path}; please read_file first to check the current content")
    if count > 1:
        raise ValueError(f"Old text '{preview}' found {count} times in {path}; provide more context to make it unique")

    content = content.replace(old, new, 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Edited {path}: replaced 1 occurrence"


# 权限规则：permissions.json 是唯一事实来源（原 SAFE_PREFIXES 已迁入）。
# 评估语义：deny 优先于 allow 优先于默认 ask——最保守的匹配获胜。
# 每条命令实时读文件：改规则无需重启（热加载），文件小，开销可忽略。

# 危险命令关键词：即使在确认环节也用红色高亮提醒（黑名单仅作提示，不能替代人工审查）
DANGEROUS_PATTERNS = [
    "rm ", "sudo", "mv ", "> /", "curl", "wget",
    "chmod", "kill", "shutdown", "reboot", "mkfs", "dd ",
]


def _load_rules() -> list[dict]:
    """加载 permissions.json 的规则表；文件缺失或损坏按空表处理（绝不拖垮 bash）。"""
    rules_path = os.path.join(PROJECT_ROOT, "permissions.json")
    if not os.path.exists(rules_path):
        return []
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            return json.load(f).get("rules", [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"\033[93m[权限] permissions.json 解析失败（{e}），本次按空规则表处理\033[0m")
        return []


def _check_permission(command: str) -> str:
    """评估命令的权限裁决：deny（规则禁止）/ allow（免确认）/ ask（走人工确认）。"""
    matched = [
        rule.get("action")
        for rule in _load_rules()
        if rule.get("pattern") and re.search(rule["pattern"], command)
    ]
    if "deny" in matched:
        return "deny"
    if "allow" in matched:
        return "allow"
    return "ask"


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

    安全模型：permissions.json 规则裁决（deny 禁止 / allow 免确认 / ask 人工确认），
    危险关键词在确认环节高亮提醒。注意 cwd=PROJECT_ROOT 只缩小误伤半径，
    并非沙箱——绝对路径访问不受限，人工确认是最后一道防线。
    """
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))  # 钳制在 [1, MAX_TIMEOUT]

    verdict = _check_permission(command)
    if verdict == "deny":
        return "该命令被权限规则禁止执行（permissions.json 中为 deny）"
    if verdict == "ask" and not _confirm(command):
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


TODO_FILE = os.path.join(PROJECT_ROOT, "session_todos.json")
TODO_STATUSES = ("pending", "in_progress", "completed")
_STATUS_ICONS = {"pending": "○", "in_progress": "▶", "completed": "✓"}


def _render_todos(todos: list[dict]) -> str:
    """把任务清单渲染为文本行（模型读着比 Python repr 友好）。"""
    if not todos:
        return "（当前没有任务清单）"
    return "\n".join(
        f"{_STATUS_ICONS.get(t['status'], '?')} [{t['status']}] {t['content']}" for t in todos
    )


@tool(
    "todo_write",
    "Create and manage a task list for the current coding session. Use it for tasks "
    "with 3+ steps, multi-file changes, or ambiguous requests; skip it for single-step "
    "questions. Each call REPLACES the entire list; update statuses as work progresses.",
    {
        "todos": {
            "type": "array",
            "description": "List of tasks for the current coding session.",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content of the task.",
                        "maxLength": 100,
                    },
                    "status": {
                        "type": "string",
                        "description": "The status of the task.",
                        "enum": ["pending", "in_progress", "completed"],
                    }
                },
                "required": ["content", "status"]
            }
        }
    },
    ["todos"],
)
def todo_write(todos: list[dict]) -> str:
    """创建和管理当前编码会话的任务列表。

    任务列表最多包含 20 个任务，每个任务包括内容和状态。
    """
    # 防御：模型可能写出枚举外的状态（平台校验不严时），兜底为 pending
    for t in todos:
        if t.get("status") not in TODO_STATUSES:
            t["status"] = "pending"

    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=4)

    # 回显清单：模型写完能自我校对；不泄漏绝对路径
    return f"已保存 {len(todos)} 个任务：\n" + _render_todos(todos)


@tool(
    "todo_read",
    "Read the current coding session's task list.",
    {},
)
def todo_read() -> str:
    """读取当前编码会话的任务列表。文件不存在时返回空清单提示。"""
    if not os.path.exists(TODO_FILE):
        return "（当前没有任务清单）"
    with open(TODO_FILE, "r", encoding="utf-8") as f:
        todos = json.load(f)
    return _render_todos(todos)


def clear_todo_file() -> None:
    """清空当前编码会话的任务列表文件（/clear 时由 commands 调用）。"""
    if os.path.exists(TODO_FILE):
        os.remove(TODO_FILE)
