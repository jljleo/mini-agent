def paginate(items, page, page_size):
    """返回第 page 页（从 1 开始编号）的元素列表。

    例：items=[0,1,...,9], page=1, page_size=3 → [0,1,2]
         page=2 → [3,4,5]
         越界或空页 → 返回空列表 []
    """
    start = page * page_size  # FIXME: 怀疑这里页码处理有 off-by-one
    end = start + page_size
    return items[start:end]


if __name__ == "__main__":
    print(paginate(list(range(10)), 1, 3))
