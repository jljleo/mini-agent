import sys

sys.path.insert(0, ".")

from count_words import count_words

assert count_words("hello world") == 2, f"两个单词应为 2，实际 {count_words('hello world')}"
assert count_words("") == 0, f"空字符串应为 0，实际 {count_words('')}"
assert count_words("a  b") == 2, f"连续空格应只算一个分隔符，实际 {count_words('a  b')}"
assert count_words("one two three") == 3, f"三个单词应为 3，实际 {count_words('one two three')}"

print("verify OK")
