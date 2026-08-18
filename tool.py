import json
import os
import subprocess
import sys
from datetime import datetime


def get_weather(location: str):
    return f"The weather in {location} is sunny."


def get_current_datetime():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_safe_path(path: str):
    full_path = os.path.realpath(os.path.join(PROJECT_ROOT, path))
    if full_path != PROJECT_ROOT and not full_path.startswith(PROJECT_ROOT + os.sep):
        raise ValueError("Path must be within the project root.")
    return full_path


def read_file(path: str):
    with open(_resolve_safe_path(path), "r", encoding="utf-8") as f:
        content = f.read()
    # 截断保护：大文件灌爆上下文是 agent 常见死法
    if len(content) > 10_000:
        content = content[:10_000] + "\n... [内容过长，已截断]"
    return content


def write_file(path: str, content: str):
    full = _resolve_safe_path(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Content written to {path}"

def edit_file(path: str, old: str, new: str):
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
    "ls", "pwd", "cat ", "echo ", "grep", "find", "head", "tail", "wc",
    "git status", "git log", "git diff", "git show", "git branch",
)

# 危险命令关键词：即使在确认环节也用红色高亮提醒（黑名单仅作提示，不能替代人工审查）
DANGEROUS_PATTERNS = ["rm ", "sudo", "mv ", "> /", "curl", "wget", "chmod", "kill", "shutdown", "reboot", "mkfs", "dd "]

MAX_TIMEOUT = 120  # 超时上限由代码钳制，不信任模型传入的值


def run_bash(command: str, timeout: int = 30):
    """在项目根目录执行 bash 命令，返回 exit code + stdout/stderr。

    安全模型：只读白名单免确认，其余命令需用户确认，危险关键词高亮提醒。
    注意 cwd=PROJECT_ROOT 只缩小误伤半径，并非沙箱——绝对路径访问不受限，人工确认是最后一道防线。
    """
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))

    if not command.strip().startswith(SAFE_PREFIXES):
        if not sys.stdin.isatty():
            # 管道/重定向模式下无法交互，默认拒绝（安全默认值）
            return "非交互环境无法确认，已默认拒绝执行该命令"
        is_dangerous = any(p in command for p in DANGEROUS_PATTERNS)
        color = "\033[91m" if is_dangerous else "\033[93m"  # 危险=红，普通=黄
        label = "危险命令" if is_dangerous else "需要确认"
        confirm = input(f"\n{color}[{label}] 即将在项目目录执行: {command}\n确认执行? (y/N): \033[0m")
        if confirm.strip().lower() != "y":
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

    # 与 read_file 一致的截断保护：防大输出灌爆上下文
    if len(output) > 10_000:
        output = output[:10_000] + "\n... [输出过长，已截断]"

    return f"[exit code: {result.returncode}]\n{output}"



ALL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": "Get the current datetime.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather in a location. (Demo tool: returns fake data, not real weather.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The location to get the weather for.",
                    },
                },
                "required": ["location"],
            },
        }
    },

    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the content of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file.",
                    },
                },
                "required": ["path"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit the content of a file by replacing old text with new text. Returns the string 'Edited {path}: replaced 1 occurrence' on success; raises ValueError if the old text is not found or appears more than once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path to the file.",
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
                "required": ["path", "old", "new"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a bash command in the project root directory and return stdout/stderr with exit code. Read-only commands run directly; all other commands require interactive user confirmation. Has timeout (max 120s) and output truncation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30).",
                    },
                },
                "required": ["command"],
            },
        }
    },
]

# search_tools 自身的 schema：常驻工具的唯一事实来源，main.py 直接引用
SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": "Search for available tools and return their schemas.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


def search_tools():
    return json.dumps(ALL_TOOL_SCHEMAS)


tools_dict = {
    "get_current_datetime": get_current_datetime,
    "get_weather": get_weather,
    "read_file": read_file,
    "write_file": write_file,
    "search_tools": search_tools,
    "edit_file": edit_file,
    "run_bash": run_bash,
}