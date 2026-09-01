工作区里有一个 paginate.py，它实现分页功能，但第一页的返回结果不对：page=1 时没有返回前 page_size 个元素，而是跳过了它们。

请修复这个 off-by-one bug。要求：
- page 从 1 开始编号（page=1 是第一页）
- 不要改动函数签名（paginate(items, page, page_size)）和返回类型
- 越界页或空页应返回空列表 []
