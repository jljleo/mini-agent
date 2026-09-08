# EXPERIMENTS — 代码库感知（repo map + search_symbols）A/B 评测记录

> 这是「repo map 到底有没有用」的对照实验档案：设计、数据、结论、教训。
> 设施（任务/开关/分析脚本）都在本仓库，结论随时可复现、可扩展。
> 数据存档在 `bench/results/*.json`（gitignore，运行时产物；含完整 messages 与
> group/model 标记，是 `compare_ab.py` 的输入）。

---

## 1. 动机

repo map（注入 system prompt 的代码库地图）+ `search_symbols`（按名称/类型检索符号）
是代码库感知能力。机制层正确性由单测锁定（`tests/test_repo_map*.py`），但
**「机制不坏」≠「用起来更好」**——本实验回答后者：同样的任务，开着它和关掉它，
成功率/成本/定位效率差多少。

## 2. 实验设计

```
固定变量：同一模型、同一任务集、同一 prompt
操纵变量：代码库感知能力
  map（实验组）    = 注入 repo map + search_symbols 常驻
  nomap（对照组）  = 不注入地图 + 工具面完全移除 search_symbols（模型不知道它存在，
                     只能 read_file / run_bash 盲探）
  AGENTS.md 行为契约两组都注入——它不是被测变量，保持公平
指标：通过率 / 轮数 / token 消耗 / 定位路径
```

- `run_bench.py --group=map|nomap`：对照组在 `apply_group()` 里摘除注入（patch
  `agent.build_repo_map_cached` 返回空）+ 从 `tool_registry.TOOLS`/常驻名单移除
  `search_symbols`，再重算 `agent.BASE_TOOLS`
- `run_bench.py --model=`：切换评测模型（已验证平台可用：`kimi-k3` / `kimi-k2.6` /
  `kimi-k2.7-code`）。结果 JSON 与文件名带 `group` + `model` 标记，跨模型样本不混
- `compare_ab.py --model=`：按任务对齐两组多次运行，输出对比表
- **纪律**：同一次 A/B 两组必须同一模型；换模型即开新实验
- repo map 索引进 bench 沙箱（`repo_map._IGNORED_ROOT = sandbox`），不指向真实仓库

## 3. 任务集

| 任务 | 语言/形态 | 目标 bug | 评测角色 |
|---|---|---|---|
| `fix_checkout` | Python 商城 3 文件 | `Checkout.compute_total` 重复征税 | 多文件定位入门 |
| `fix_discount` | JS/ESM 商城 4 文件 | `fees.calcDiscount` 折扣叠加 | 跨语言 |
| `fix_retry` | Python 服务包 5 文件 | `retry.run_with_retry` 重试条件写反 | 服务包结构 |
| `fix_notify_dedupe` | Python 通知服务 **40+ 文件** | `worker/helpers.dedupe_key` 漏掉渠道维度 | 地图预算外藏 bug（主要区分度来源） |

每个任务的 `META.json`/`PROMPT.md`/`verify.py`/`workspace/` 完整入库，`verify.py`
均已双态验证（buggy 版必 FAIL、修复版必 OK）。任务刻意不点名目标文件。

## 4. 结果

### 4.1 第一轮（kimi-k3，fix_checkout/fix_discount/fix_retry）

| 指标 | map（10 样本） | nomap（8 样本） |
|---|---|---|
| 通过率 | 10/10 (100%) | 8/8 (100%) |
| 轮均 | 4.6 | 5.4（-15%） |
| token 均 | 22,602 | 25,285（-11%） |
| 首读命中 | 10/10 | 8/8 |

**结论**：有地图时稳定更省（token/轮数），但成功率天花板（任务过易）+ 目标文件命名
直白（`checkout.py` 之类），测不出定位价值。另：`fix_discount` 在 nomap 组有 2 次
会话卡死无结果（agent 循环 49 分钟不退出，疑似无引导下原地打转；根因未定论）。

### 4.2 第二轮（kimi-k3，fix_notify_dedupe，40+ 文件）

地图预算（3000 字符）装不下整个仓库，**目标文件 `worker/helpers.py` 两组都看不见**；
但地图暴露核心链路 `services/notifier.py`（含 `dedupe_key (import)` 行）。

| 指标 | map（4 样本） | nomap（4 样本） |
|---|---|---|
| **通过率** | **4/4 (100%)** | 3/4 (75%) |
| 轮均 | 10.8 | 15.0 |
| token 均 | **116,679** | 186,881（**-38%**） |
| 定位路径 | 4/4 次 `notifier.py → helpers.py`（2 步） | 读 12-14 个无关文件后才碰到目标（1 次靠运气首击） |

**结论**：有地图时定位效率显著更好，直接换算成成本（-38% token）与成功率（+25pp）。
这是区分度正确的任务形态：**大仓库 + 目标在地图预算外 + 文件名不直白**。

### 4.3 教训（方法论）

1. **任务类型陷阱**：单文件小任务里 repo map 可能近乎零收益甚至负收益（固定
   ~1.5K token 注入开销）。区分度来自多文件 + 目标文件不进预算 + 需要推导链路。
2. **「首读命中」指标会误导**：map 组的聪明做法是「先读地图暴露的核心链路，再读目标」
   （第 2 次 read_file 命中），首读命中率反而可能低于 nomap 靠运气首击。看「几步内
   定位 + 读过多少无关文件」，不看首读。
3. **模型随机性**：每任务需多次运行取中位数/分布，单次结论不可靠。
4. **对照组公平性**：被移除的搜索工具不能残留任何可见入口（schema/执行体/常驻名单
   三处同步摘除，否则模型仍可能经 search_tools 发现它）。

## 5. 评测的意外产出：抓到并修复真实内核 bug

第二轮 nomap 组一次运行撞上 `400: run_bash:1 did not have response messages`。
根因（`agent.py` 修复前）：

> assistant 一次发两个并行 tool_call（`search_tools` + `run_bash`）时，`search_tools`
> 结果一落地就插入可发现工具声明（`{"role":"system","tools":[...]}`），把第二条
> 工具响应夹在中间 → Moonshot 校验「tool_calls 必须紧接全部响应」判其孤儿 → 400。

修复（提交 `d9737fc`）：动态注入统一挪到**本轮全部工具响应之后**执行，并加回归测试
锁定历史顺序 `tool → tool → system → assistant`。bench 从「打分工具」变成了
「内核缺陷探测器」——这正是评测体系超出评分之外的价值。

## 6. 重跑

```bash
# 单轮（每任务一次）：
python bench/run_bench.py --group=map --model=kimi-k2.6 fix_notify_dedupe
python bench/run_bench.py --group=nomap --model=kimi-k2.6 fix_notify_dedupe
# 对比：
python bench/compare_ab.py --model=kimi-k2.6

# 多任务多次（脚本循环，注意串行避免 API 限速；偶发退出 hang 时 kill 后重跑即可）：
for t in fix_checkout fix_discount fix_retry fix_notify_dedupe; do
  for i in 1 2 3; do
    python bench/run_bench.py --group=map "$t"
    python bench/run_bench.py --group=nomap "$t"
  done
done
```

## 7. 已知边界（诚实清单）

- 样本量小（每任务 3-4 次），统计意义有限；趋势可信，数值会随模型版本漂移
- bench 存在偶发「run 已完成但进程不退出」的现象（fix_discount nomap 复现过），
  不影响已写出结果，但会阻塞脚本循环——遇 kill 后重跑
- 大仓库任务 token 消耗大（单次最高 29 万），跑多轮前先估算预算
- 低成本方案：`--model=kimi-k2.6`（已验证可用）。换模型时同一 A/B 两组必须一致