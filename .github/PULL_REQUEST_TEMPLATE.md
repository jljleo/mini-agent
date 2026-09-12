## 这个改动解决了什么

（一句话说清楚背景与动机——不是「改了 X」，而是「为什么需要改」）

## 改动内容

- 列出关键变更点（评审者扫一眼就能判断范围）

## 测试

- [ ] 已跑 `.venv/bin/python -m pytest -q`（或说明为何跳过）
- [ ] 已跑 `.venv/bin/ruff check .`
- 涉及行为变化时补充说明验证方式

## 自审（led 视角）

> 本仓库的 review.yml 会自动 review 这个 PR 并评论 findings。合并前请先看
> bot 评论，high 级别问题应修复后再合并。