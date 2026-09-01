def count_words(text):
    """统计文本中的单词数（按空格分隔）。空字符串返回 0。

    例：count_words("hello world") -> 2
    """
    if not text:
        return 0
    return len(text.split(" "))  # FIXME: 连续空格会多算


if __name__ == "__main__":
    print(count_words("a  b"))
