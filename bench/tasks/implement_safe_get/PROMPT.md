工作区里有一个 `safe_get.py`，请实现 `safe_get(data, path, default=None)` 函数：按点分路径从嵌套的 dict/list 中取值。

例如：
- `safe_get({"a": {"b": 1}}, "a.b")` → `1`
- `safe_get({"a": [{"b": 2}]}, "a.0.b")` → `2`
- `safe_get({}, "a.b", "x")` → `"x"`
- `safe_get({"a": [1, 2]}, "a.5", -1)` → `-1`

路径不存在、索引越界或 `data` 本身异常时都应返回 `default`。不要修改函数签名。
