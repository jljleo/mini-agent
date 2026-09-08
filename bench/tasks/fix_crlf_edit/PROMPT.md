工作区有 `pricing.py`：计价工具，其中 `apply_discount(price, percent)` 的语义是——
`percent=10` 表示打九折（减 10%），`percent=25` 表示七五折。但当前实现算错了：
它把 percent 当成了折扣额直接乘。

请修复 `apply_discount`，要求：
- `apply_discount(100, 10) == 90`，`apply_discount(200, 25) == 150`，`apply_discount(50, 0) == 50`
- **修改文件只能用 edit_file 工具**：不得使用 run_bash（sed/perl/python 写入等）或任何
  其它方式直接改写文件内容——必须通过 edit_file 完成修复
- 不要重写整个文件：用精确替换改 get 到的那一处
