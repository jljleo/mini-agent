"""mini-agent：评测驱动的 coding agent 内核。

库用法：
    from led_review import ChatSession
    for event in ChatSession().chat("..."):
        ...
"""
from .kernel.agent import ChatSession

__all__ = ["ChatSession"]
