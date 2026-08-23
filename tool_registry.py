"""工具注册表：函数与 schema 的唯一事实来源（Single Source of Truth）。

旧设计里 tools_dict（执行表）和 ALL_TOOL_SCHEMAS（声明表）手工维护两份，
新增工具要改多处、极易不同步。现在用 @tool 装饰器一次声明，自动聚合。

工具分两档（2026-08-23 起）：
    resident（默认）：基本工具，直接常驻请求 tools= 参数（8 个工具才 ~2.4K
        tokens，缓存全命中近乎免费；发现制反而多一次往返 + 缓存断裂）
    可发现（resident=False）：未来扩展工具（如 MCP），由 search_tools 检索后
        动态注入。两档必须互斥——同一工具既常驻又被注入会 duplicate tool name 400

对外暴露：
    TOOLS                     : {name: fn}    执行表（含 search_tools）
    SEARCH_TOOLS_SCHEMA       : schema        常驻顶层请求的 search_tools 声明
    get_all_tool_schemas()    : [schema, ...] 常驻工具的声明（惰性聚合）
    get_extended_tool_schemas(): [schema, ...] 可发现工具的声明（search_tools 返回）

为什么聚合是函数而非模块级常量：
    业务工具在 tools.py 导入期才注册，模块级常量会在那一刻定稿、之后新增工具
    就得手动刷新。惰性聚合让"声明"永远跟"注册表"一致，没有同步窗口。
"""

import json

# 执行表：{工具名: 可调用对象}
TOOLS: dict = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None,
         resident: bool = True):
    """注册一个业务工具：把函数和它的 schema 绑在一起。

    用法::

        @tool("get_weather", "查询天气", {"location": {...}}, ["location"])
        def get_weather(location: str): ...

    resident=True（默认）：常驻 tools= 参数；resident=False：由 search_tools
    检索后动态注入（面向未来扩展工具，如 MCP）。
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
        fn.tool_resident = resident
        TOOLS[name] = fn
        return fn
    return decorator

# search_tools 自身的 schema：常驻工具的唯一事实来源，agent 直接引用
SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": "Search for additional (non-resident) tools beyond the ones already "
                       "declared. Only needed when existing tools cannot do the job.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _search_tools() -> str:
    """search_tools 的执行体：返回可发现（非常驻）工具的声明。

    当前没有可发现工具时明确告知——空列表会让模型困惑（“是不是检索失败了”）。
    """
    extended = get_extended_tool_schemas()
    if not extended:
        return "（当前没有额外的可发现工具，请直接使用已声明的工具）"
    return json.dumps(extended, ensure_ascii=False)


# search_tools 可被模型调用，进执行表；但它的声明由 agent 常驻顶层请求，
# 不参与业务工具的惰性聚合（避免自我引用）。
TOOLS["search_tools"] = _search_tools


def get_all_tool_schemas() -> list[dict]:
    """惰性聚合常驻工具声明（resident=True）。

    search_tools 自身的声明由 SEARCH_TOOLS_SCHEMA 常驻，不参与聚合（防自我引用）。
    """
    return [fn.tool_schema for fn in TOOLS.values()
            if hasattr(fn, "tool_schema") and getattr(fn, "tool_resident", True)]


def get_extended_tool_schemas() -> list[dict]:
    """惰性聚合可发现工具声明（resident=False）：search_tools 检索后注入的那批。"""
    return [fn.tool_schema for fn in TOOLS.values()
            if hasattr(fn, "tool_schema") and not getattr(fn, "tool_resident", True)]
