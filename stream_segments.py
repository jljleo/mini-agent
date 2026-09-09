"""流式分段器：两个前端（ui.py / tui.py）共用的唯一切割事实来源。

把 TextDelta 流切成「完成段」与「生长中的尾段」：
- 完成段一旦吐出即冻结落卷，永不重排；
- 尾段是当前唯一参与增量渲染的部分——从机制上杜绝
  "每 chunk 全量重排长回答"的 O(n²) 抖动与 Live 超高重绘重复。

切割规则（ui/tui 此前各抄了一份，已收敛到这里）：
- 代码 fence 未闭合（``` 奇数个）时不切：半拉的 ``` 单独渲染会错乱；
- 只在空行（\\n\\n）分界处切：段落是 Markdown 的最小完整语义单元。

参考 pi-tui 的线性流水线思想：历史组件 append 后不再重绘，
只有当前生长中的组件被增量更新。
"""

from __future__ import annotations


def split_complete(text: str) -> tuple[str, str]:
    """纯函数：把已完成块从流式缓冲里切出来，返回 (完成部分, 尾部)。"""
    if text.count("```") % 2 == 1:
        return "", text
    idx = text.rfind("\n\n")
    if idx <= 0:
        return "", text
    return text[: idx + 2], text[idx + 2 :]


class StreamSegmenter:
    """有状态分段器：喂 delta → 持有尾段 → 按需取走完成段。

    典型用法（两个前端同构）：
        seg.feed(chunk)                 # 每个 TextDelta
        done = seg.take_completed()     # flush 时取完成段落卷（可能为空串）
        render_tail(seg.tail)           # 尾段增量重排
        final = seg.finish()            # 收尾取走残余尾段，渲染终稿
    """

    def __init__(self) -> None:
        self._tail = ""

    @property
    def tail(self) -> str:
        """生长中的尾段（已完成部分被取走后剩下的内容）。"""
        return self._tail

    def feed(self, text: str) -> None:
        """喂入一段 delta。只累积，不切割——切割时机由前端 flush 策略决定。"""
        self._tail += text

    def take_completed(self) -> str:
        """切出可永久落卷的完成前缀并取走（无完成段时返回空串）。"""
        done, self._tail = split_complete(self._tail)
        return done

    def finish(self) -> str:
        """收尾：取走残余尾段（此时不再讲究 fence 闭合，流已结束）。"""
        tail, self._tail = self._tail, ""
        return tail
