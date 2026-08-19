"""全局配置：集中管理原本散落各处的常量。

所有可调参数收在这里，改行为不用翻业务代码。
"""

import os

from dotenv import load_dotenv

load_dotenv()  # 把 .env 加载进环境变量，API key 不落代码

# --- 模型 ---
MODEL = "kimi-k3"
BASE_URL = "https://api.moonshot.cn/v1"
API_KEY_ENV = "MOONSHOT_API_KEY"  # 从环境变量读 key，不入库

# --- agent 循环 ---
# 工具调用轮数上限：防模型陷入反复调工具的死循环。
# 取值考量：简单问答 1~3 轮，多工具任务 3~5 轮，自重构/多文件改造可达 20+ 轮；
# 取 30 给复杂任务留足余量，死循环时烧 30 轮 token 也在可接受范围。
MAX_TOOL_ROUNDS = 30

# --- 输出/上下文保护 ---
MAX_OUTPUT_LEN = 10_000       # 工具结果 / 命令输出的截断阈值：防大输出灌爆上下文
TOOL_RESULT_PREVIEW_LEN = 100  # 终端里工具结果的预览长度
MAX_TIMEOUT = 120             # bash 超时上限（秒）：由代码钳制，不信任模型传入的值

# --- 斜杠命令 ---
# 退出词表（非斜杠命令，主循环直接识别；/quit 也走这里统一退出）
QUIT_COMMANDS = ("exit", "quit", ":q", "/quit")

# --- 项目路径 ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(PROJECT_ROOT, ".chat_history")  # prompt_toolkit 历史（跨会话）

# --- system 提示词 ---
SYSTEM_MESSAGES = [
    {
        "role": "system",
        "content": (
            "你是 Kimi，由 Moonshot AI 提供的人工智能助手，你更擅长中文和英文的对话。"
            "你会为用户提供安全，有帮助，准确的回答。同时，你会拒绝一切涉及恐怖主义，"
            "种族歧视，黄色暴力等问题的回答。Moonshot AI 为专有名词，不可翻译成其他语言。"
        ),
    },
    {
        "role": "system",
        "content": "如果有的问题你没法回答,你可以使用search_tools查看有没有可以帮助你的",
    },
    {
        "role": "system",
        # 平台 bug 规避：kimi-k3 上回传 $web_search 结果必现 400 tokenization failed（官方论坛已报未修），
        # 且模型可能自发调用它，故明确禁用；联网需求引导走 run_bash + curl（需用户确认）。
        # 平台修复后：删除此条禁用句，并在 agent.BASE_TOOLS 加回 WEB_SEARCH_SCHEMA。
        "content": "不要调用 $web_search（该内置功能当前不可用）。当你需要联网获取实时信息（如天气、新闻、汇率）时，改用 run_bash 工具执行 curl 命令获取（例如 curl 天气服务 wttr.in、各类公开 API）；注意这属于需要用户确认的命令，执行前向用户说明你要访问的地址。除此之外，当你无法直接回答时，先用 search_tools 查看可用工具，再调用合适的工具来回答用户（例如用 run_bash 执行 date 命令获取当前时间）。",
    },
]
