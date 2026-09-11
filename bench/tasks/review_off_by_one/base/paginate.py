"""分页工具。"""


def paginate(items: list, page: int, size: int) -> tuple[list, int]:
    """返回 (第 page 页内容, 总页数)。page 从 1 开始。"""
    total = max(1, -(-len(items) // size))
    start = (page - 1) * size
    end = min(start + size, len(items))
    return items[start:end], total
