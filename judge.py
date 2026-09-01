"""LLM-as-judge：对无确定性判据的任务用 LLM + rubric 打分。

独立于 agent 会话：发起一次独立 judge 请求，不污染主会话上下文。
judge() 接收可注入的 client（测试用 mock），_parse_score 是纯函数。
"""

import json
import os
import re

from openai import OpenAI

from config import API_KEY_ENV, BASE_URL, MODEL

_JUDGE_SYSTEM = (
    "你是一名客观的评测判分员。根据给定的评分标准（rubric），评估任务产出是否达标，"
    "并给出 0 到 1 的分数（1=完全达标，0=完全未达标）。只输出一个 JSON 对象："
    '{"score": <0到1的小数>, "reason": "<一句话中文理由>"}，不要输出其他内容。'
)


def make_client() -> OpenAI:
    """构造独立 judge 用的 OpenAI client（复用 config 的模型与 key 配置）。"""
    return OpenAI(api_key=os.environ.get(API_KEY_ENV), base_url=BASE_URL)


def judge(client: OpenAI, task_output: str, rubric: str) -> dict:
    """用 LLM 判分，返回 {"score": float, "reason": str}。

    task_output：任务的最终产出文本；rubric：META.json 里的评分标准。
    """
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": f"评分标准：\n{rubric}\n\n任务产出：\n{task_output}"},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    return _parse_score(raw)


def _parse_score(raw: str) -> dict:
    """从 LLM 原始输出解析 score + reason，容错（模型可能包 ```json 或多余文字）。"""
    try:
        return _coerce(json.loads(raw))
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    s = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
    if s:
        try:
            return {"score": max(0.0, min(1.0, float(s.group(1)))), "reason": raw[:200]}
        except ValueError:
            pass
    return {"score": 0.0, "reason": f"judge 解析失败: {raw[:200]}"}


def _coerce(data: dict) -> dict:
    """把解析出的 JSON 收敛成 {"score": float, "reason": str}，缺字段给默认值。"""
    score = data.get("score", 0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return {"score": score, "reason": str(data.get("reason", ""))}
