"""工具注册表：函数与 schema 的唯一事实来源（Single Source of Truth）。

旧设计里 tools_dict（执行表）和 ALL_TOOL_SCHEMAS（声明表）手工维护两份，
新增工具要改多处、极易不同步。现在用 @tool 装饰器一次声明，自动聚合。
全部工具（含 search_tools 发现入口）都在 tools.py 里用同一机制维护。

工具分两档（2026-08-23 起），分档的唯一事实来源是 RESIDENT_TOOL_NAMES 名单：
    常驻（名单内）：核心高频工具 + search_tools 自身，直接进请求 tools= 参数
        （~1.5K tokens，缓存全命中近乎免费）
    可发现（名单外）：其余工具由 search_tools 检索后动态注入（todo、
        search_history、未来 MCP）。新注册工具默认可发现——进常驻需在名单显式登记
    两档必须互斥：同一工具既常驻又被注入会 duplicate tool name 400
    search_tools 在名单内 → 自动被可发现档排除，无自我引用问题

对外暴露：
    TOOLS                     : {name: fn}    执行表
    RESIDENT_TOOL_NAMES       : (str, ...)    常驻名单（分档的唯一事实来源）
    get_resident_tool_schemas(): [schema, ...] 名单内工具的声明（按名引用，写错名当场 KeyError）
    get_extended_tool_schemas(): [schema, ...] 名单外工具的声明（search_tools 返回）

为什么聚合是函数而非模块级常量：
    业务工具在 tools.py 导入期才注册，模块级常量会在那一刻定稿、之后新增工具
    就得手动刷新。惰性聚合让"声明"永远跟"注册表"一致，没有同步窗口。
"""

# 执行表：{工具名: 可调用对象}
TOOLS: dict = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    """注册一个业务工具：把函数和它的 schema 绑在一起。

    用法::

        @tool("get_weather", "查询天气", {"location": {...}}, ["location"])
        def get_weather(location: str): ...

    新工具默认可发现（search_tools 检索后注入）；高频核心工具把名字加进
    RESIDENT_TOOL_NAMES 即常驻 tools= 参数。
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

# 常驻名单：分档的唯一事实来源。只放名字（不 import tools），registry 不依赖业务模块。
# search_tools 在名单内：发现入口必须常驻，且借此自动被可发现档排除（防自我引用）
RESIDENT_TOOL_NAMES = ("search_tools", "read_file", "write_file", "edit_file", "run_bash")


def get_resident_tool_schemas() -> list[dict]:
    """按常驻名单显式引用声明（在 tools.py 完成导入后调用）。

    写错名字当场 KeyError（fail fast），好过静默漏挂一个工具。
    """
    return [TOOLS[name].tool_schema for name in RESIDENT_TOOL_NAMES]


def get_tool_schemas(names: tuple[str, ...] | list[str]) -> list[dict]:
    """按给定名字列表生成工具声明（子 agent 受限工具集等场景用）。

    写错名字当场 KeyError（fail fast）。
    """
    return [TOOLS[name].tool_schema for name in names]


def get_extended_tool_schemas() -> list[dict]:
    """可发现工具声明：已注册但不在常驻名单内的（search_tools 检索后注入）。"""
    return [fn.tool_schema for name, fn in TOOLS.items()
            if hasattr(fn, "tool_schema") and name not in RESIDENT_TOOL_NAMES]
