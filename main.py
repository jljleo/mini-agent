import json
import os
import sys
import unicodedata

from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console

from tool import tools_dict, SEARCH_TOOLS_SCHEMA, ALL_TOOL_SCHEMAS

load_dotenv()

client = OpenAI(
    api_key=os.environ.get("MOONSHOT_API_KEY"),
    base_url="https://api.moonshot.cn/v1",
)

console = Console()

# prompt_toolkit 会话（惰性创建）：历史记录存项目目录，↑ 键可翻出历史提问（跨会话保留）
# 惰性原因：模块级创建在非 tty 环境（管道输入）会打印警告
_prompt_session = None


def _get_prompt_session() -> PromptSession:
    global _prompt_session
    if _prompt_session is None:
        _prompt_session = PromptSession(
            history=FileHistory(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chat_history"))
        )
    return _prompt_session

MODEL = "kimi-k3"

SYSTEM_MESSAGES = [
    {"role": "system",
     "content": "你是 Kimi，由 Moonshot AI 提供的人工智能助手，你更擅长中文和英文的对话。你会为用户提供安全，有帮助，准确的回答。同时，你会拒绝一切涉及恐怖主义，种族歧视，黄色暴力等问题的回答。Moonshot AI 为专有名词，不可翻译成其他语言。"},
    {"role": "system",
     "content": "如果有的问题你没法回答,你可以使用search_tools查看有没有可以帮助你的"},
]

messages = list(SYSTEM_MESSAGES)  # 拷贝一份，避免污染模板

# 顶层请求常驻的工具（其余工具由 search_tools 按需动态挂载）
BASE_TOOLS = [SEARCH_TOOLS_SCHEMA]

MAX_TOOL_ROUNDS = 10  # agent 循环上限：防模型陷入反复调工具的死循环


def _tools_already_injected() -> bool:
    """检查是否已注入过动态工具声明（保证幂等，避免重复注入撑胖上下文）"""
    return any(isinstance(m, dict) and m.get("tools") for m in messages)


def stream_and_assemble(completion) -> list[dict]:
    """遍历流式 chunk：content 边收边打印，tool_calls 碎片逐步拼装。

    返回组装完成的 assistant 消息列表（普通 dict，已做定稿处理）。
    """
    stream_messages_dict = {}

    for chunk in completion:
        for choice in chunk.choices:
            index = choice.index
            message = stream_messages_dict.setdefault(index, {})
            delta = choice.delta

            if delta.role:
                message["role"] = delta.role

            content = delta.content
            if content:  # 大部分 chunk 的 content 是 None，必须守卫
                print(content, end="", flush=True)
                message["content"] = message.get("content", "") + content

            if delta.tool_calls:
                tool_calls = message.setdefault("tool_calls", [])
                for tool_call in delta.tool_calls:
                    tool_call_index = tool_call.index
                    # 惰性扩容：缺几个补几个，保证下标可访问
                    if len(tool_calls) < tool_call_index + 1:
                        tool_calls.extend([{}] * (tool_call_index + 1 - len(tool_calls)))
                    tool_call_object = tool_calls[tool_call_index]
                    tool_call_object["index"] = tool_call_index

                    # 一次性字段：直接赋值
                    if tool_call.id:
                        tool_call_object["id"] = tool_call.id
                    if tool_call.type:
                        tool_call_object["type"] = tool_call.type

                    if tool_call.function:
                        function = tool_call_object.setdefault("function", {})
                        if tool_call.function.name:
                            function["name"] = tool_call.function.name
                        # 分片字段：拼接累加
                        if tool_call.function.arguments:
                            function["arguments"] = function.get("arguments", "") + tool_call.function.arguments

    # 定稿：补 role、摘掉组装辅助字段 index
    for message in stream_messages_dict.values():
        message.setdefault("role", "assistant")
        for tc in message.get("tool_calls", []):
            tc.pop("index", None)

    return list(stream_messages_dict.values())


def execute_tool_call(tool_call: dict) -> tuple[str, str]:
    """执行单个工具调用，返回 (工具名, 结果文本)。任何失败都转为文本结果，不向上抛。"""
    name = tool_call["function"].get("name")
    raw_arguments = tool_call["function"].get("arguments") or "{}"
    # 灰色打印调用过程，让用户看见 agent 在干什么
    print(f"\n\033[90m[调用工具] {name}({raw_arguments})\033[0m", flush=True)
    try:
        if name not in tools_dict:
            raise KeyError(f"Unknown tool: {name}")
        arguments = json.loads(raw_arguments)
        result = str(tools_dict[name](**arguments))  # 兜底：工具可能返回非字符串
    except Exception as e:
        result = f"调用失败: {type(e).__name__}: {e}"
    preview = result if len(result) <= 100 else result[:100] + "..."
    print(f"\033[90m[工具结果] {preview}\033[0m", flush=True)
    return name, result


def chat(user_input: str):
    messages.append({"role": "user", "content": user_input})
    for _ in range(MAX_TOOL_ROUNDS):
        # 等待首字到达的间隙显示 spinner，填掉“静默尴尬期”
        with console.status("[dim]思考中...[/dim]", spinner="dots"):
            completion = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=BASE_TOOLS,
                stream=True,
            )

        for assistant_message in stream_and_assemble(completion):
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls")
            if not tool_calls:
                # content 已在流式过程中边收边打印，这里只补换行和分隔线
                print()
                print("********************************")
                return

            for tool_call in tool_calls:
                name, tool_result = execute_tool_call(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": tool_result,
                    }
                )

                # search_tools 被调用后，注入完整工具声明（Moonshot 动态加载机制），幂等
                if name == "search_tools" and not _tools_already_injected():
                    messages.append({
                        "role": "system",
                        "tools": ALL_TOOL_SCHEMAS,
                    })
    else:
        print(f"[Warning] 工具调用超过 {MAX_TOOL_ROUNDS} 轮，已强制结束本轮对话")


def sanitize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return text.strip()


def read_input(prompt: str = "") -> str:
    """读取用户输入：终端下用 prompt_toolkit（历史/行编辑），管道模式退回原始读法。"""
    if sys.stdin.isatty():
        # prompt_toolkit 遇到 Ctrl+C/Ctrl+D 会抛 KeyboardInterrupt/EOFError，
        # 与主循环的捕获逻辑兼容
        return sanitize(_get_prompt_session().prompt(prompt))

    # 非交互环境（管道/重定向）：prompt_toolkit 不适用，退回字节读取
    print(prompt, end="", flush=True)
    raw = sys.stdin.buffer.readline()

    if raw == b"":
        raise EOFError

    text = raw.decode("utf-8", errors="replace")
    return sanitize(text)


def main():
    while True:
        try:
            question = read_input("Input your question (exit/quit to quit): ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D (EOFError) 或 Ctrl+C (KeyboardInterrupt)：安全退出
            print("\nBye!")
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", ":q"):
            print("Bye!")
            break

        history_mark = len(messages)  # 记录历史位置，失败时整体回滚本轮产生的所有消息
        try:
            chat(question)
        except Exception as e:
            del messages[history_mark:]
            print(f"[Error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
