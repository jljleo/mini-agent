def parse_keyval(text):
    """解析 "key=value" 多行文本为字典。

    空行和 # 开头的注释行忽略。
    例：parse_keyval("a=1\\n# c\\nb=2\\n") -> {"a": "1", "b": "2"}
    """
    # TODO: 待实现
    raise NotImplementedError


if __name__ == "__main__":
    print(parse_keyval("a=1\nb=2\n"))
