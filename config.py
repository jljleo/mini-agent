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
# 工具调用轮数上限：简单问答 1~3 轮，多工具任务 3~5 轮，自重构/多文件改造可达 30+ 轮
# （/plan 自实现实战单轮 29 次，贴着旧上限 30）。历史管理上线后单轮成本有界，
# 上限让位于任务复杂度，提到 50。
MAX_TOOL_ROUNDS = 50
# 死循环保险丝（行为识别，替代单纯的轮次计数）：同一 (工具名, 参数) 连续出现
# 此次数即判定死循环，强制结束本轮。正常任务每次调用参数不同，不会误伤。
MAX_SAME_TOOL_CALLS = 3

# --- 输出/上下文保护 ---
MAX_OUTPUT_LEN = 10_000  # 工具结果 / 命令输出的截断阈值：防大输出灌爆上下文
TOOL_RESULT_PREVIEW_LEN = 100  # 终端里工具结果的预览长度
MAX_TIMEOUT = 120  # bash 超时上限（秒）：由代码钳制，不信任模型传入的值

# --- 斜杠命令 ---
# 退出词表（非斜杠命令，主循环直接识别；/quit 也走这里统一退出）
QUIT_COMMANDS = ("exit", "quit", ":q", "/quit")

# --- L3 工具结果瘦身（compact.py）---
TOOL_RESULT_KEEP_RECENT = 5  # 保护窗口：最近 N 条 tool 消息不瘦身（churn 防线，勿设 0）
TOOL_RESULT_MIN_SLIM_LEN = 500  # 原文短于此长度不瘦身：占位符本身 ~80 字符，太短是负收益
TOOL_ARG_ECHO_LEN = 60  # 占位符中参数回显的截断长度（防占位符自身膨胀）
# 触发阈值：历史总字符数低于此值完全不动作（保护 prompt cache）。
# 粗估 1 token ≈ 2 字符（中英混合语料），80K 字符 ≈ 40K tokens。
SLIM_TRIGGER_CHARS = 80_000
# 收益门槛：本轮瘦身能省下的字符总量低于此值就不动——省几百字符却顶掉几千 tokens 的
# 缓存前缀是净亏损（典型场景：reasoning 占大头、tool 结果很小的会话）
SLIM_MIN_SAVINGS_CHARS = 2_000

# --- L1 历史截断（compact.py，兜底防爆）---
# 触发用估算 token（chars//2）：达到高水位才截，一刀切到低水位。
# 双水位滞后：防“刚好切到阈值下、下轮又超”导致每轮都截、每轮缓存全失效。
# （按 token 而非消息条数：条数与上下文占用无量纲关系，一条大文件结果可顶几十条闲聊）
TRUNCATE_HIGH_TOKENS = 100_000  # 硬触发线（kimi-k3 128K 窗口预留输出与余量）
TRUNCATE_LOW_TOKENS = 60_000  # 截断目标：切完留下足够增长空间

# --- 单条体积上限（compact.py，投影级）---
# 分层防御的“单条体积”层：MAX_OUTPUT_LEN 只管工具产出，user 输入无天然上限——
# 首条 user 超大会让保留区自身爆窗（会话永久报废），末条 user 超大会让 L1 兜底失效。
# 60K 字符 ≈ 30K tokens：即使首条+末条同时超大，两者合计仍远低于 128K 窗口。
SINGLE_MSG_CAP_CHARS = 60_000

# --- L2 摘要（compact.py，L1 的保值版）---
# 触发与 L1 同高水位：到线后先尝试让模型压缩中段，失败再回退硬切。
SUMMARIZE_MAX_CHARS = 150_000  # 摘要输入上限：中段超长时只取靠后部分（更贴近当前任务）

# --- 项目路径 ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(PROJECT_ROOT, ".chat_history")  # prompt_toolkit 历史（跨会话）
SESSION_FILE = os.path.join(PROJECT_ROOT, ".session.json")  # 会话存档（/resume 恢复用）

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
        "content": "文件读写、run_bash 等基础工具已直接声明可用；todo 清单、search_history 历史检索等工具用 search_tools 检索后即可调用。",
    },
    {
        "role": "system",
        # 平台 bug 规避：kimi-k3 上回传 $web_search 结果必现 400 tokenization failed（官方论坛已报未修），
        # 且模型可能自发调用它，故明确禁用；联网需求引导走 run_bash + curl（需用户确认）。
        # 平台修复后：删除此条禁用句，并把名字加进 tool_registry.RESIDENT_TOOL_NAMES。
        "content": "不要调用 $web_search（该内置功能当前不可用）。当你需要联网获取实时信息（如天气、新闻、汇率）时，改用 run_bash 工具执行 curl 命令获取（例如 curl 天气服务 wttr.in、各类公开 API）；注意这属于需要用户确认的命令，执行前向用户说明你要访问的地址。当你无法直接回答时，直接调用已声明的工具（例如用 run_bash 执行 date 命令获取当前时间）；现有工具都不够用时才用 search_tools 查找额外工具。",
    },
    {
        "role": "system",
        "content": (
            "任务规划规则（todo_write/todo_read 需先经 search_tools 检索发现后调用）：\n"
            "1. 满足以下任一条件，先用 todo_write 建清单再动手：步骤 ≥3、涉及多个文件、需求模糊需要拆解。\n"
            "2. 清单全量覆盖：每次 todo_write 传入完整列表，逐项更新状态（pending → in_progress → completed），"
            "开始某步前标 in_progress，完成立即标 completed。\n"
            "3. 单轮问答、一步能完成的任务不要使用 todo，直接完成。\n"
            "4. 不确定当前进度时，先 todo_read 查看清单再继续。"
        ),
    },
]
