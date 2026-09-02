"""业务工具实现：用 @tool 装饰器注册到 registry（含 search_tools 发现入口）。

文件工具（read_file/write_file/edit_file）为窄接口：项目内免确认，项目外人工确认
（围栏内自由、围栏外审批——与权限系统同一思路）；run_bash 为通用接口：
permissions.json 规则裁决 + 人工审批兜底，且命令中出现项目外路径时 allow 降级为 ask。
两者并存——文件操作走窄接口，真正的命令走 bash。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import ui
from config import (
    MAX_OUTPUT_LEN,
    MAX_SUBAGENT_DEPTH,
    MAX_TIMEOUT,
    PROJECT_ROOT as _CONFIG_PROJECT_ROOT,
    SUBAGENT_DENIAL_LIMIT,
    SUBAGENT_HIDDEN_TOOLS,
    SUBAGENT_TYPES,
)
from input_utils import confirm
from tool_registry import TOOLS, get_extended_tool_schemas, get_tool_schemas, tool

# 项目根：文件围栏 / bash cwd 的边界锚点，是模块级可变变量（不是 import 绑定）。
# 沙箱（spawn_subagent）与 bench 通过临时改它实现隔离：`saved = PROJECT_ROOT` →
# 指向副本 → 用完还原。config.PROJECT_ROOT 是常量，这里是它的可变副本。
PROJECT_ROOT = _CONFIG_PROJECT_ROOT


# ---------- 工具发现入口：search_tools ----------


@tool(
    "search_tools",
    "Search for additional (non-resident) tools beyond the ones already "
    "declared. Only needed when existing tools cannot do the job.",
    {},
)
def search_tools() -> str:
    """返回可发现（非常驻名单外）工具的声明，供模型检索后调用。

    当前没有可发现工具时明确告知——空列表会让模型困惑（“是不是检索失败了”）。
    本工具在常驻名单内（RESIDENT_TOOL_NAMES），因此自动被可发现档排除，
    无自我引用问题（旧设计需手写 schema 特殊处理，名单制后与普通工具无异）。
    子 agent 上下文里额外过滤元工具（spawn_subagent 防套娃、todo 防覆盖主清单）。
    """
    extended = get_extended_tool_schemas()
    if _in_subagent():
        extended = [s for s in extended if s["function"]["name"] not in SUBAGENT_HIDDEN_TOOLS]
    if not extended:
        return "（当前没有额外的可发现工具，请直接使用已声明的工具）"
    return json.dumps(extended, ensure_ascii=False)


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
    # 子 agent 上下文不冒泡找人：越界访问直接硬拒绝（环境安全层），计入拒绝熔断
    if _in_subagent():
        raise ValueError(_record_subagent_denial(f"子 agent 禁止访问项目外路径：{full_path}"))
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

# 子 agent 上下文标记：spawn_subagent 执行期间入栈，run_bash 据此走路由审批（read_only 硬拒 / human 冒泡）
_subagent_context: list[dict] = []  # 栈结构，支持嵌套（虽然限深 1）


def _in_subagent() -> bool:
    """当前是否处于子 agent 上下文。"""
    return len(_subagent_context) > 0


def _subagent_task() -> str:
    """当前子 agent 的任务描述（审批冒泡时作为上下文）。"""
    return _subagent_context[-1]["task"] if _subagent_context else ""


def _subagent_command_policy() -> str:
    """当前子 agent 的命令策略（config.SUBAGENT_TYPES 里声明）：read_only（白名单直通，其余硬拒）/ human（ask 冒泡给人工审批）。"""
    if not _subagent_context:
        return "human"  # 防御：无上下文时按默认人工审批处理
    agent_type = _subagent_context[-1].get("type", "coder")
    spec = SUBAGENT_TYPES.get(agent_type, {})
    return spec.get("command_policy", "human")


def _record_subagent_denial(reason: str) -> str:
    """子 agent 命令/访问被拒计数；连续达到 SUBAGENT_DENIAL_LIMIT 即熔断本轮。

    反复试探授权 = 边界划错了（类型选错 / 环境选错），掐死止血并把次数带回主 agent
    自我修正（codex GuardianRejectionCircuitBreaker 思路）。返回给模型的拒绝文案。
    """
    if not _subagent_context:
        return reason
    ctx = _subagent_context[-1]
    ctx["denied"] = ctx.get("denied", 0) + 1
    if ctx["denied"] >= SUBAGENT_DENIAL_LIMIT:
        control = ctx.get("control")
        if control is not None:
            control.abort()
        return (reason + f"（已连续 {ctx['denied']} 次被拒，本轮已熔断——"
                "任务边界可能划错了，请改用更合适的类型或环境）")
    return reason


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
    if verdict == "deny":
        # 子 agent 上下文：规则 deny 也计入熔断（反复试探被禁命令 = 边界划错）
        if _in_subagent():
            return _record_subagent_denial("该命令被权限规则禁止执行（permissions.json 中为 deny）")
        return "该命令被权限规则禁止执行（permissions.json 中为 deny）"
    # 子 agent 环境安全：含项目外路径的命令硬拒绝（与文件工具同一语义）。
    # 边界违规是明确事实，不冒泡不评审，直接硬拒绝，并计入拒绝熔断。
    if _in_subagent() and _has_outside_path(command):
        return _record_subagent_denial("子 agent 禁止执行含项目外路径的命令（环境安全边界，不可评审豁免）")
    # allow 授权的是命令本身，不是越界访问：`cat /etc/hosts` 会绕过文件工具的
    # 路径围栏。命令中出现项目外路径时降级为 ask，由用户把关（与文件工具同一套确认）
    if verdict == "allow" and _has_outside_path(command):
        verdict = "ask"
    if verdict == "ask":
        if _in_subagent():
            policy = _subagent_command_policy()
            if policy == "read_only":
                # 确定性白名单：非 allow 即拒（researcher 等只读类型零评审成本）
                return _record_subagent_denial(
                    "该命令不在白名单内，researcher 等只读型子 agent 一律拒绝。"
                    "如需执行，请由主 agent 改用 coder 类型。")
            # human 策略（opencode 式）：ask 冒泡给人工审批，带子 agent 前缀标明来源
            if not _confirm(f"[子 agent] {command}"):
                return _record_subagent_denial("用户拒绝了该命令的执行")
        elif not _confirm(command):
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


def get_history_provider():
    """读取当前历史数据源（spawn_subagent 保存/恢复用——子 ChatSession 构造会覆盖它）。"""
    return _history_provider


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


# ---------- 子 agent：工具化派生 + 上下文隔离 + 沙箱 + 类型化命令策略 ----------
#
# 设计（opencode 简版，见 AGENT_DESIGN.md #20-23）：
# - 子 agent = 进程内新 ChatSession，只传 task prompt 不传历史（上下文彻底隔离）
# - 工具集收窄（SUBAGENT_TOOL_NAMES），search_tools 里元工具不可见（防套娃）
# - 无人值守 → 硬性轮次上限（MAX_TURNS），到顶 abort 优雅收尾（防孤儿 tool call）
# - researcher=read_only（白名单直通，其余硬拒）；coder=human（ambiguous 冒泡给人工审批）
# - sandbox=True（默认）在临时项目副本里干活，改动不落真实项目；
#   sandbox=False 改真实项目，靠 git 兜底
# - 只回最终结论（最后一条 assistant text），完整轨迹不进主上下文

_SANDBOX_IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".idea", ".pytest_cache", ".env",
    "bench/results", "*.pyc", ".session.json", ".chat_history", "session_todos.json",
)

_SUBAGENT_DISCIPLINE = (
    "你是被主 agent 派生的子 agent，正在独立执行一个受限子任务。纪律：\n"
    "1. 只做任务范围内的事，不要扩展动作。\n"
    "2. 完成后立即用 200 字以内返回结论，包含关键结果（改了哪些文件、测试是否通过）。\n"
    "3. 权限规则未覆盖的 bash 命令：researcher 一律拒绝，coder 冒泡给人工审批；被拒绝后换思路，不要反复尝试同类命令。"
)


@tool(
    "spawn_subagent",
    "Spawn a subagent with isolated context for a well-scoped, self-contained subtask. "
    "It runs autonomously and returns only its final conclusion. "
    "Delegation discipline: (1) Only use when there is a genuinely independent subtask "
    "or the user explicitly asks for delegation/parallel work; do NOT use for trivial "
    "tasks you can do directly. (2) Before spawning, decide the boundaries first: task "
    "scope (what exactly to do and what NOT to do), environment (sandbox copy vs real "
    "project), and action needs (is read-only enough, or does it need write/bash) — then "
    "pick the agent_type whose boundary fits. (3) The task must be self-contained: the "
    "subagent sees none of your history, so include all needed context (file paths, "
    "background, constraints) in `task`. (4) If the subagent reports denied commands, "
    "your boundary was likely wrong — fix it before re-spawning.",
    {
        "task": {
            "type": "string",
            "description": "The subtask to delegate. Must be specific, self-contained, and include all needed context (file paths, background, constraints).",
        },
        "agent_type": {
            "type": "string",
            "description": (
                "Which specialized agent to use. This is REQUIRED — decide the boundary "
                "before delegating. "
                "'researcher': read-only analysis in a throwaway sandbox (cannot edit files; read-only bash commands like grep/find run directly). "
                "'coder': can edit files and run commands in the real project (git-recoverable)."
            ),
        },
        "max_turns": {
            "type": "integer",
            "description": "Hard cap on agent rounds (default 10, max 50).",
        },
        "sandbox": {
            "type": "boolean",
            "description": "Override the type's default: run in a throwaway project copy. "
                           "Researcher defaults to true, coder defaults to false.",
        },
    },
    ["task", "agent_type"],
)
def spawn_subagent(task: str, agent_type: str, max_turns: int = 10,
                   sandbox: bool | None = None) -> str:
    """派生子 agent 执行独立子任务：上下文隔离 + 类型化工具边界 + 轮次上限 + 类型化命令策略。

    只回最终结论（最后一条 assistant 正文），完整轨迹不进主上下文。
    """
    # 嵌套限深：保险丝。子 agent 的工具集本就不含 spawn_subagent（search_tools 也过滤），
    # 这是双保险——防未来误配置导致无限套娃。
    if len(_subagent_context) >= MAX_SUBAGENT_DEPTH:
        return (
            f"错误：子 agent 嵌套已达上限（MAX_SUBAGENT_DEPTH={MAX_SUBAGENT_DEPTH}）。"
            "请直接自行完成该任务，不要再派生。"
        )

    spec = SUBAGENT_TYPES.get(agent_type)
    if spec is None:
        available = "、".join(f"{k}（{v['description']}）" for k, v in SUBAGENT_TYPES.items())
        return f"错误：未知子 agent 类型 '{agent_type}'。可用类型：{available}"
    if sandbox is None:
        sandbox = spec["sandbox"]  # 沙箱默认值随类型：researcher 隔离、coder 落真实项目

    from agent import ChatSession  # 延迟导入：避免 tools ↔ agent 循环依赖
    from events import StreamStart, TurnControl, TurnEnd

    global PROJECT_ROOT
    max_turns = max(1, min(int(max_turns), 50))
    depth = len(_subagent_context)

    # 环境准备：沙箱模式 = 项目副本（.venv/.git 等大件不复制，跑不了项目自身测试——
    # 需要跑测试的子任务应用 coder 类型，sandbox 默认 False）
    sandbox_dir = None
    saved_root = PROJECT_ROOT
    saved_history_provider = get_history_provider()  # 子 ChatSession 构造会覆盖它
    if sandbox:
        sandbox_dir = tempfile.mkdtemp(prefix="subagent_")
        shutil.copytree(PROJECT_ROOT, sandbox_dir, dirs_exist_ok=True, ignore=_SANDBOX_IGNORE)
        PROJECT_ROOT = sandbox_dir

    # control 存进上下文：拒绝熔断要在 run_bash 里 abort 本轮，需要拿到子 agent 的控制通道
    control = TurnControl()
    _subagent_context.append({"task": task, "depth": depth, "type": agent_type,
                              "denied": 0, "control": control})
    session = None
    hit_limit = False
    denied_count = 0
    try:
        session = ChatSession(tools=get_tool_schemas(spec["tools"]), depth=depth + 1)
        session.messages.append({"role": "system", "content": (
            _SUBAGENT_DISCIPLINE +
            f"\n4. 你的类型是 {agent_type}：{spec['description']}。不要尝试超出能力边界的操作。"
        )})

        rounds = 0
        for ev in session.chat(task, control=control):
            if isinstance(ev, StreamStart):
                rounds += 1
                if rounds > max_turns:
                    # 硬性轮次上限：abort 让内核优雅收尾（补孤儿 tool 结果、产 TurnEnd），
                    # 而不是直接弃流（会留半截 tool 配对，下次请求 400）
                    control.abort()
                    hit_limit = True
            elif isinstance(ev, TurnEnd):
                break
    except Exception as e:
        return f"子 agent 执行异常：{type(e).__name__}: {e}"
    finally:
        entry = _subagent_context.pop()
        denied_count = entry.get("denied", 0)
        PROJECT_ROOT = saved_root
        set_history_provider(saved_history_provider)
        if sandbox_dir:
            try:
                shutil.rmtree(sandbox_dir)
            except OSError as e:
                ui.warn(f"子 agent 沙箱清理失败（{sandbox_dir}）：{e}，可手动删除")

    # 只回结论：取子 agent 最后一条有正文的 assistant 消息（取"最后文本"与 opencode
    # findLast(text) 同款，防上下文污染）。注意：中断/熔断时尾部是 tool 补结果消息，
    # 反遍历跳过它们；但被 abort 时找到的是"半截话"而非终稿，须用 interrupted 标注。
    final = ""
    for m in reversed(session.messages):
        if m.get("role") == "assistant" and m.get("content"):
            final = m["content"]
            break

    # opencode 在取 text 前先判失败（assistant error / tool error）→ Effect.fail。
    # 我们事件流无 part 状态机，用 control.interrupt 标志等价识别"未正常走完"。
    interrupted = control.interrupt.is_set()

    notes = []
    if hit_limit:
        notes.append(f"已达轮次上限 {max_turns}，以下是其中途结论")
    elif interrupted:
        notes.append("子 agent 本轮被中断，未产出完整结论，以下是其中途内容")
    if denied_count:
        notes.append(f"运行中 {denied_count} 条命令/访问被拒——若子 agent 因此受阻，"
                     "说明任务边界或类型选错了（如该用 coder 而非 researcher，或该 sandbox=False）")
    note = f"（注意：{'；'.join(notes)}）" if notes else ""
    body = final or "（子 agent 未产出结论）"
    return f"[子 agent 结论]{note}\n{body}"
