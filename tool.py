import json
import os
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
}