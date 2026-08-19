"""工具注册表：函数与 schema 的唯一事实来源（Single Source of Truth）。

旧设计里 tools_dict（执行表）和 ALL_TOOL_SCHEMAS（声明表）手工维护两份，
新增工具要改多处、极易不同步。现在用 @tool 装饰器一次声明，自动聚合。

对外暴露：
    TOOLS                  : {name: fn}    执行表（含 search_tools）
    SEARCH_TOOLS_SCHEMA    : schema        常驻顶层请求的 search_tools 声明
    get_all_tool_schemas() : [schema, ...] 惰性聚合业务工具声明（不含 search_tools）

为什么 get_all_tool_schemas 是函数而非模块级常量：
    业务工具在 tools.py 导入期才注册，模块级常量会在那一刻定稿、之后新增工具
    就得手动刷新。惰性聚合让"声明"永远跟"注册表"一致，没有同步窗口。
"""

import json

# 执行表：{工具名: 可调用对象}
TOOLS: dict = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    """注册一个业务工具：把函数和它的 schema 绑在一起。

    用法::

        @tool("get_weather", "查询天气", {"location": {...}}, ["location"])
        def get_weather(location: str): ...
    """
    def decorator(fn):
        fn.tool_schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                },
            },
        }
        TOOLS[name] = fn
        return fn
    return decorator

# search_tools 自身的 schema：常驻工具的唯一事实来源，agent 直接引用
SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": "Search for available tools and return their schemas.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _search_tools() -> str:
    """search_tools 的执行体：返回完整工具声明（JSON 字符串）。"""
    return json.dumps(get_all_tool_schemas())


# search_tools 可被模型调用，进执行表；但它的声明由 agent 常驻顶层请求，
# 不参与业务工具的惰性聚合（避免自我引用）。
TOOLS["search_tools"] = _search_tools


def get_all_tool_schemas() -> list[dict]:
    """惰性聚合全部业务工具声明（不含 search_tools）。

    search_tools 常驻顶层请求（BASE_TOOLS），若注入声明里再带它，
    API 会报 duplicate tool name 400，故此处排除。
    """
    return [fn.tool_schema for fn in TOOLS.values() if hasattr(fn, "tool_schema")]
