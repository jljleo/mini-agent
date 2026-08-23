"""业务工具实现：用 @tool 装饰器注册到 registry。

文件工具（read_file/write_file/edit_file）为窄接口：项目内免确认，项目外人工确认
（围栏内自由、围栏外审批——与权限系统同一思路）；run_bash 为通用接口：
permissions.json 规则裁决 + 人工审批兜底，且命令中出现项目外路径时 allow 降级为 ask。
两者并存——文件操作走窄接口，真正的命令走 bash。
"""
import json
import os
import re
import subprocess

import ui
from config import MAX_OUTPUT_LEN, MAX_TIMEOUT, PROJECT_ROOT
from input_utils import confirm
from tool_registry import tool


# ---------- 文件窄接口工具：路径围栏 + 免确认 ----------


def _resolve_safe_path(path: str, op: str) -> str:
    """把模型给的路径解析为绝对路径；项目内直接放行，项目外转人工确认。

    realpath 防符号链接逃逸。早期设计是越界硬拒绝，但模型会绕过：改用 run_bash
    执行 cat 读外界文件。既然权限系统已有"确认"这一档，围栏外降级为询问用户，
    比硬拒绝更可用、比放任更安全。拒绝时抛 ValueError，由 agent 统一转为工具结果。
    """
    full_path = os.path.realpath(os.path.join(PROJECT_ROOT, path))
    if full_path == PROJECT_ROOT or full_path.startswith(PROJECT_ROOT + os.sep):
        return full_path
    # write/edit 会改外界文件，确认框按危险操作红色高亮；read 只读，普通高亮
    if not confirm(f"{op} {full_path}", dangerous=(op != "read_file")):
        raise ValueError(f"用户拒绝了访问项目外路径：{full_path}")
    return full_path


@tool(
    "read_file",
    "Read the content of a file. Paths inside the project directory are read "
    "directly; absolute paths outside the project require user confirmation. "
    "The range to read is specified by offset/limit, defaulting to reading the "
    "first 10K characters. The max limit is 10K. Truncates any excess.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root. Absolute paths outside the project are allowed but require user confirmation.",
        },
        "offset": {
            "type": "integer",
            "description": "The starting character offset to read from (default 0).",
        },
        "limit": {
            "type": "integer",
            "description": "The maximum number of characters to read (default 10000, max 10000).",
        },
    },
    ["path"],
)
def read_file(path: str, offset: int = 0, limit: int = 10000) -> str:
    """读取文件内容（项目外路径需用户确认），返回字符串。
    读取范围由 offset/limit 指定，默认从头读 10K 字符；未读完时附续读提示。
    """
    if offset < 0 or limit <= 0:
        raise ValueError("Offset must be non-negative and limit must be positive.")
    if limit > 10_000:  # 10K 字符上限，防大文件灌爆上下文
        raise ValueError("Limit must be less than or equal to 10000.")
    with open(_resolve_safe_path(path, "read_file"), "r", encoding="utf-8") as f:
        content = f.read()
    total = len(content)
    if offset >= total:
        return f"（offset {offset} 已超出文件末尾，全文共 {total} 字符）"
    page = content[offset:offset + limit]
    # 截断保护先于续读提示：hint 是导航信号，不能自己也被截掉
    # （limit ≤ 10K = MAX_OUTPUT_LEN 时此分支是防御性兜底）
    if len(page) > MAX_OUTPUT_LEN:
        page = page[:MAX_OUTPUT_LEN] + "\n... [内容过长，已截断]"
    # 分页闭环：告诉模型读到哪、还剩多少、下一页怎么翻——没有位置信号的
    # 分页是半残的（模型无法区分“刚好读满”与“读到末尾”）
    end = offset + len(page)
    if end < total:
        page += f"\n...[未完：全文 {total} 字符，已返回 {offset}~{end}，续读用 offset={end}]"
    return page


@tool(
    "write_file",
    "Write content to a file. Paths inside the project directory are written "
    "directly; absolute paths outside the project require user confirmation. "
    "Creates the file if it doesn't exist, overwrites if it does. Automatically "
    "creates parent directories.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root. Absolute paths outside the project are allowed but require user confirmation.",
        },
        "content": {
            "type": "string",
            "description": "The content to write to the file.",
        },
    },
    ["path", "content"],
)
def write_file(path: str, content: str) -> str:
    full = _resolve_safe_path(path, "write_file")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Content written to {path}"


@tool(
    "edit_file",
    "Edit a file by exact text replacement. Paths outside the project directory "
    "require user confirmation. The old text must appear exactly ONCE in the file; "
    "on failure, read_file first to check the current content.",
    {
        "path": {
            "type": "string",
            "description": "The path to the file, relative to the project root. Absolute paths outside the project are allowed but require user confirmation.",
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
    full = _resolve_safe_path(path, "edit_file")
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
        ui.warn(f"permissions.json 解析失败（{e}），本次按空规则表处理")
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


def _has_outside_path(command: str) -> bool:
    """浅层检测命令中是否出现项目外路径（绝对路径 / ~ / .. 开头的 token）。

    背景：allow 规则按命令名匹配（如 cat 免确认），但 `cat /etc/hosts` 会绕过
    文件工具的路径围栏静默读取项目外内容。shell 语义无法静态穷尽（管道、变量、
    子 shell、`cd` 后接相对路径），这里是尽力而为的第一道筛子：命中即把 allow
    降级为 ask，漏检的部分由 allow 规则本身只覆盖只读命令来兜底。
    """
    for token in command.split():
        token = token.strip("'\"")
        if token.startswith(("/", "~", "..")):
            full = os.path.realpath(os.path.join(PROJECT_ROOT, os.path.expanduser(token)))
            if full != PROJECT_ROOT and not full.startswith(PROJECT_ROOT + os.sep):
                return True
    return False



@tool(
    "run_bash",
    "Run a bash command in the project root directory and return stdout/stderr with "
    "exit code. Read-only commands run directly; all other commands require interactive "
    "user confirmation, as do commands accessing paths outside the project. "
    "Has timeout (max 120s) and output truncation.",
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
    # allow 授权的是命令本身，不是越界访问：`cat /etc/hosts` 会绕过文件工具的
    # 路径围栏。命令中出现项目外路径时降级为 ask，由用户把关（与文件工具同一套确认）
    if verdict == "allow" and _has_outside_path(command):
        verdict = "ask"
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


# ---------- 历史检索：统一兑现三层上下文管理的“可恢复”承诺 ----------

# 数据源由 ChatSession 构造时注入（工具函数无状态，历史由会话持有）
_history_provider = None


def set_history_provider(fn) -> None:
    """注入历史数据源（返回 messages 列表的可调用对象）。"""
    global _history_provider
    _history_provider = fn


@tool(
    "search_history",
    "Search the FULL session history (storage), including content no longer visible in "
    "your context: slimmed tool results, truncated early history, summarized sections, "
    "and the omitted middle of oversized messages. Use when a placeholder/marker says "
    "content was removed, or when a summary seems incomplete. Returns matching snippets "
    "with surrounding context; prefer specific keywords (file paths, error messages).",
    {
        "keyword": {
            "type": "string",
            "description": "The substring to search for (case-insensitive). Use specific strings like file paths or error messages, not generic words.",
        },
        "context_chars": {
            "type": "integer",
            "description": "Characters of context to show around each hit (default 200, max 500).",
        },
    },
    ["keyword"],
    resident=False,
)
def search_history(keyword: str, context_chars: int = 200) -> str:
    """全文检索会话存储，返回命中片段（含消息下标与角色）。

    设计要点：
    - 检索对象是存储（self.messages）而非投影——被瘦身/截断/摘要/截中的原文都在；
    - 返回片段而非全文：膨胀天然有界，且结果以 tool 消息身份进上下文，被 L3 回收；
    - 一个工具覆盖三层降级（L1/L2/L3/单条上限），不给每层各铺一条恢复管道。
    """
    if _history_provider is None:
        return "（历史检索不可用：数据源未注入）"
    context_chars = max(50, min(int(context_chars), 500))
    needle = keyword.lower()
    if not needle:
        return "（关键词不能为空）"

    hits = []
    for i, m in enumerate(_history_provider()):
        content = str(m.get("content") or "")
        lowered = content.lower()
        pos = 0
        while len(hits) < 20:  # 命中上限：防高频词刷屏
            pos = lowered.find(needle, pos)
            if pos < 0:
                break
            start = max(0, pos - context_chars)
            end = min(len(content), pos + len(keyword) + context_chars)
            snippet = content[start:end].replace("\n", " ")
            role = m.get("role")
            name = f"({m['name']})" if m.get("name") else ""
            hits.append(f"[#{i} {role}{name}] …{snippet}…")
            pos = end
        if len(hits) >= 20:
            break

    if not hits:
        return f"（未在历史中找到 {keyword!r}）"
    out = f"命中 {len(hits)} 处：\n" + "\n".join(hits)
    if len(out) > MAX_OUTPUT_LEN:
        out = out[:MAX_OUTPUT_LEN] + "\n... [结果过长，已截断；换更精确的关键词]"
    return out


# ---------- todo 工具 ----------

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
    resident=False,
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
    resident=False,
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
