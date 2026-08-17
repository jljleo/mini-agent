import json
import os
import sys
import unicodedata

from dotenv import load_dotenv
from openai import OpenAI

from tool import tools_dict, SEARCH_TOOLS_SCHEMA, ALL_TOOL_SCHEMAS

load_dotenv()

client = OpenAI(
    api_key=os.environ.get("MOONSHOT_API_KEY"),
    base_url="https://api.moonshot.cn/v1",
)

messages = [
    {"role": "system",
     "content": "你是 Kimi，由 Moonshot AI 提供的人工智能助手，你更擅长中文和英文的对话。你会为用户提供安全，有帮助，准确的回答。同时，你会拒绝一切涉及恐怖主义，种族歧视，黄色暴力等问题的回答。Moonshot AI 为专有名词，不可翻译成其他语言。"},
    {"role": "system",
     "content": "如果有的问题你没法回答,你可以使用search_tools查看有没有可以帮助你的"},
]

tools = [SEARCH_TOOLS_SCHEMA]

MAX_TOOL_ROUNDS = 10  # agent 循环上限：防模型陷入反复调工具的死循环


def _tools_already_injected() -> bool:
    """检查是否已注入过动态工具声明（保证幂等，避免重复注入撑胖上下文）"""
    return any(isinstance(m, dict) and m.get("tools") for m in messages)



def chat(user_input: str):
    messages.append({"role": "user", "content": user_input})
    for _ in range(MAX_TOOL_ROUNDS):
        completion = client.chat.completions.create(
            model="kimi-k3",
            messages=messages,
            tools=tools,
            stream=True,
        )
        stream_messages_dict = {}

        for chunk in completion:
            for choice in chunk.choices:
                index = choice.index
                message = stream_messages_dict.setdefault(index, {})
                delta = choice.delta
                role = delta.role
                if role:
                    message["role"] = role
                content = delta.content
                if content:  # 大部分 chunk 的 content 是 None，必须守卫
                    print(content, end="", flush=True)
                    message["content"] = message.get("content", "") + content

                tool_calls = delta.tool_calls
                if tool_calls:
                    if "tool_calls" not in message:
                        message["tool_calls"] = []
                    for tool_call in tool_calls:
                        tool_call_index = tool_call.index
                        if len(message["tool_calls"]) < (tool_call_index + 1):
                            message["tool_calls"].extend([{}] * (tool_call_index + 1 - len(message["tool_calls"])))
                        tool_call_object = message["tool_calls"][tool_call_index]
                        tool_call_object["index"] = tool_call_index

                        tool_call_id = tool_call.id
                        if tool_call_id:
                            tool_call_object["id"] = tool_call_id
                        tool_call_type = tool_call.type
                        if tool_call_type:
                            tool_call_object["type"] = tool_call_type
                        tool_call_function = tool_call.function
                        if tool_call_function:
                            if "function" not in tool_call_object:
                                tool_call_object["function"] = {}
                            tool_call_function_name = tool_call_function.name
                            if tool_call_function_name:
                                tool_call_object["function"]["name"] = tool_call_function_name
                            tool_call_function_arguments = tool_call_function.arguments
                            if tool_call_function_arguments:
                                if "arguments" not in tool_call_object["function"]:
                                    tool_call_object["function"]["arguments"] = tool_call_function_arguments
                                else:
                                    tool_call_object["function"]["arguments"] += tool_call_function_arguments

                        message["tool_calls"][tool_call_index] = tool_call_object


        # 组装结果是普通 dict，下游统一用字典方式访问
        for assistant_message in stream_messages_dict.values():
            assistant_message.setdefault("role", "assistant")
            # 删掉组装用的辅助字段，保持消息格式干净
            for tc in assistant_message.get("tool_calls", []):
                tc.pop("index", None)
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls")
            if not tool_calls:
                # content 已在流式过程中边收边打印，这里只补换行和分隔线
                print()
                print("********************************")
                return

            for tool_call in tool_calls:
                name = tool_call["function"].get("name")
                # try/except 在循环体内：保证每个 tool_call 必有一条结果回传，
                # 否则残留的 tool_call 会导致下一轮请求被 API 拒绝（400）
                try:
                    if name not in tools_dict:
                        raise KeyError(f"Unknown tool: {name}")
                    arguments = json.loads(tool_call["function"].get("arguments") or "{}")
                    print(f"\n调用工具: {name}，参数: {arguments}", end="", flush=True)
                    tool_result = tools_dict[name](**arguments)
                    print(f"\n工具调用结果: {tool_result}\n", end="", flush=True)

                except Exception as e:
                    tool_result = f"调用失败: {type(e).__name__}: {e}"
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
    print(prompt, end="", flush=True)
    raw = sys.stdin.buffer.readline()

    if raw == b"":
        raise EOFError

    text = raw.decode("utf-8", errors="replace")
    return sanitize(text)


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
