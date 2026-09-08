工作区有 `pricing.py`：计价工具，其中 `apply_discount(price, percent)` 的语义是——
`percent=10` 表示打九折（减 10%），`percent=25` 表示七五折。但当前实现算错了：
它把 percent 当成了折扣额直接乘。

请修复 `apply_discount`，要求：
- `apply_discount(100, 10) == 90`，`apply_discount(200, 25) == 150`，`apply_discount(50, 0) == 50`
- 用 edit_file 工具修改（不要整个文件重写）
- 不要改变文件的行尾风格（保持与 read_file 看到的一致）
