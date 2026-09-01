import sys

sys.path.insert(0, ".")

from parse_keyval import parse_keyval

assert parse_keyval("a=1\nb=2\n") == {"a": "1", "b": "2"}, f"基本解析错误: {parse_keyval('a=1\nb=2\n')}"
assert parse_keyval("# c\na=1\n\nb=2\n") == {"a": "1", "b": "2"}, f"注释与空行应忽略: {parse_keyval('# c\na=1\n\nb=2\n')}"
assert parse_keyval("") == {}, f"空文本应返回空字典: {parse_keyval('')}"
assert parse_keyval("x=1\ny=2\nz=3") == {"x": "1", "y": "2", "z": "3"}, f"无尾换行应正常: {parse_keyval('x=1\ny=2\nz=3')}"

print("verify OK")
