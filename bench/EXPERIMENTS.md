# EXPERIMENTS — 评测总档案（唯一权威的评测记录）

> 本文件是仓库内**唯一权威的评测记录**：任何真实 API 评测（`bench/run_bench.py`
> 真跑）都必须留下一个条目。结论、数据、副产品（抓到的内核缺陷）都放这里——
> 不要只活在对话里。原始数据在 `bench/results/*.json`（gitignore），本文件是索引与结论。

## 评测记录规范（MANDATE）

**触发**：每次用 `bench/run_bench.py` 真实调用 API 跑评测——无论结果如何（包括
负结果、失败的任务）——必须追加一个条目并随代码提交。零成本机制/单测不算（那是
测试不是评测）。

**条目模板**（新评测照此记录）：

```
### 【E{n}】{YYYY-MM-DD} {主题}
- 动机：要回答什么问题（假设/疑问），预期什么
- 控制变量：固定什么 / 操纵什么 / 指标是什么
- 设施：任务 + 开关（--group/--edit-mode/--model）+ 样本数 + 模型
- 数据：按组对齐的表格（通过率 / 轮数 / token / 行为路径）
- 结论：支持/否定了什么 + 数值证据 + 诚实边界（样本量、泄漏、负结果）
- 副产品：评测中抓到/修复的内核缺陷（已发生三次，是本体系最高价值产出，别省）
- 可复现：一两行命令即可重跑
```

---

## 历史档案索引

| ID | 主题 | 章节 | 状态 |
|---|---|---|---|
| E1 | 代码库感知 A/B（repo map + search_symbols） | §1-7 | 已成文（k3 两轮 + 方法论教训） |
| E2 | 编辑容错 A/B（fix_crlf_edit，CRLF 工作区） | §8 | 已成文（k2.7-code，抓到 @tool 装饰器挂错） |
| E3 | 乱命名库 A/B（fix_dedupe_obf，符号混淆） | §9 | 已成文（k2.7-code，验证"命名烂则地图区分度消失"） |
| E4 | 修复验证：search_symbols 加 path/docs 维度 | §10 | 已成文（token -53%，成功率持平，依赖模型主动用新维度） |
| E5 | 验证自动 docs 兜底（遭遇模型行为漂移，不可信） | §11 | 已成文（1/3，两个 5K 放弃样本污染；顺带修 search_tools 参数容错） |
| E6 | 复验自动 docs 兜底（容错已修，仍受弃疗干扰） | §12 | 已成文（2/3 无净提升，建议此方向收束） |
| E7 | 验证纪律 A/B（强制自验 vs 无要求） | §13 | 已成文（同批对照：双 3/3；自验 +91% 成本；跨日漂移提醒） |

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
## 8. 编辑容错 A/B（fix_crlf_edit，模型 kimi-k2.7-code）

**动机**：edit_file 容错链（L1 精确→L2 行级宽容→L3 指引 + 改后语法自检，AGENT_DESIGN
13/48/49）到底值不值？最有区分度的场景是 **CRLF 工作区**：模型从 read_file 的归一化
视图复制 old 必然带 LF，精确匹配必败，只有容错链能救。

**对照组机制**：`--edit-mode=lenient|strict`——strict 把 L2 容错替换失效（回到旧精确
时代）。payload 级控制，AGENTS.md 契约等不变。

**任务**（`fix_crlf_edit`）：CRLF 定价文件含折扣 bug；prompt 强制「只能用 edit_file
修改、禁止 bash 改写文件」；verify 同时断言修复正确 + 文件仍是 CRLF。

| 组 | 通过率 | token 均 | 行为 |
|---|---|---|---|
| lenient（容错链） | 3/3 | **21,321** | read → edit 一次成功 |
| strict（旧精确） | 3/3 | 49,188（**+131%**） | edit 失败 → cat -A/hexdump 诊断 CRLF → 手动构造带 `\r` 的 old 重试 |

**结论**：两组成功率相同——因为模型足够聪明（探测行尾后手动嵌 `\r\n` 让精确匹配
成功，严格绕开"bash 禁用"约束）。但容错链的价值以**信任成本**显形：同一任务，
strict 组花 2.3 倍 token 在「失败→诊断→重试」回合。**容错链不救成功率，救的是
"别让模型怀疑工具坏了"的往返开销**——工具在任何输入下都能工作，模型就不把时间
花在诊断工具自身。

**副产品（评测第三次抓到真实内核 bug）**：初跑 lenient 组全部 TypeError
（`_lenient_replace() missing content/preview`）——追查发现 `@tool("edit_file")`
装饰器在重构中挂到了 `_lenient_replace` 上：schema 是 `{path,old,new}`、执行体却是
5 参函数，**kwargs 传 3 参必炸。单测直调 `tools.edit_file` 全绿（不经注册表），
真实 agent 路径必失败——直调与注册表两条路径的差异只有真实运行才暴露，
再次证明「评测是内核缺陷探测器」。

---

## 9. 乱命名库 A/B（fix_dedupe_obf，【E3】）

**动机**：真实世界的代码库命名未必良好。搜索/排序/地图全部以符号名为轴线，若符号名
无意义（a1/b2/q1…），repo map 的区分度还剩多少？用户质疑「代码库命名胡乱维护，这个
作用是不是不大」——用实验回答。

**控制变量**：把 `fix_notify_dedupe` 全部符号名替换为无意义标识符（\b 词替换，含注释内
字样；`dedupe_key`→`q1`、`Notifier`→`SvcGG`…），**路径/模块名保留**（`src/worker/helpers.py`
不改）；bug、prompt、verify 行为不变（verify 改引用 q1）。map vs nomap，模型 k2.7-code，×3。

**数据**（token 均按 3 次全量，含 FAIL）：

| 组 | 通过率 | token 均 | 行为 |
|---|---|---|---|
| map（有地图+search_symbols） | 2/3 | 351,392 | read 4 文件起步；search_symbols("q1"/"q2"/"q4") 瞎猜（语义名已失效） |
| nomap（盲探） | 2/3 | 294,640 | find + read 漫游，甚至派 spawn_researchers 并行调研 |

**结论**：**用户判断成立——乱命名抹掉了地图的语义优势**：map/nomap 差异消失（都 2/3，
token map 反而略贵 19%）。对照命名良好的原任务（E1，k3）：map 4/4 PASS、token -38% vs
nomap。语义名称轴失效后，地图退化为「带行号的路径清单」，仅剩路径顺序 + 引用热度两
条弱线索——这正是「名称是 repo map 唯一钥匙」的代价侧写。

**副产品观察**：乱命名让 cost 暴涨（单次最高 483,952 tokens 仍是 PASS）；模型在
`search_symbols("q1")` 这种无意义查询间打转，退化为 grep 语义词探索
（trace 见 `grep -R "status\|FAILED\|SENT"`）。search_symbols 对烂命名不是"更难"，是
**概念上失灵**——因为查询词是模型的语义记忆，与库的任意名字无交集。

**局限（诚实）**：样本 3+3 小；E1 用 k3、E3 用 k2.7-code（跨模型横向仅参考，组内
对照有效）；注释仍保留业务语义（模型 read 后有"去重键漏 provider"字样线索）——
若连注释也混淆，差异可能进一步收窄；路径名也保留着语义，是地图的最后防线。

---

## 10. 修复验证：search_symbols 非名称维度（【E4】）

**动机**：E3 证明乱命名下 name 检索概念性失灵。给 search_symbols 加 scope=path（文件
路径片段）与 scope=docs（注释/正文行包含业务词），重跑同一任务量"区分度恢复多少"。

**控制变量**：工具新增维度（name 默认不变）；任务/prompt/模型全部同 E3
（fix_dedupe_obf，k2.7-code，map 组 ×3）；对照组 = E3 的 map 旧数据（2/3，均 351,392）。

**数据**：

| 版本 | 通过率 | token 均 | 行为 |
|---|---|---|---|
| E3 map（name 只有） | 2/3 | 351,392 | search_symbols("q1") 瞎猜 |
| E4 map（+path/docs） | 2/3 | **165,371（-53%）** | #2 用 scope=docs 快速命中（155K 最低）；#1/#3 仍走 name |

**结论**：**成本维度显著恢复（-53%），成功率维度持平（2/3，样本小无法区分）**。
E4 的赢面来自"模型主动用新维度"——trace 显示 #2 主动 scope=docs 命中（155K 三次最低），
#1/#3 仍默认 name（#3 放弃得早 97K 也算廉价）。工具描述已引导（烂命名时用 path/docs），
但模型并非总能联想到；故补充**主动兜底**：name miss 时返回信息附带
"若库命名混乱，可改用 scope=docs 搜业务词 / scope=path 按路径检索"——把教训编进行为。

**结论改写**：search_symbols 的命门不再只是名称——有了 path/docs 维度后，烂命名库的
搜索从"概念性失灵"降为"需要模型被引导到正确维度"。剩余提升空间（如 name miss 时
自动并跑 docs）留作后续。

---

## 11. 自动 docs 兜底验证（【E5】——负结果，遭遇模型行为漂移）

**动机**：E4 遗留空间——name miss 自动并跑 docs，把"模型可能想不到的下一步"机构化。
重跑 fix_dedupe_obf，期望成功率/成本至少不劣于 E4（2/3，165K）。

**控制变量**：工具已加自动 docs 兜底（name miss 直接返回正文命中）；任务/prompt/模型同
E3/E4（k2.7-code，map ×3）。

**数据**：

| 样本 | 结果 | tokens | 行为 |
|---|---|---|---|
| #1 | FAIL | 5,303 | 第一轮 `search_tools(query=…)`（schema 无此参数）→ TypeError → 模型立即放弃 |
| #2 | FAIL | 5,211 | 同上 |
| #3 | PASS | 397,689 | 正常路径完整跑完（自动兜底未破坏工具链） |

**结论（诚实）**：**E5 数据不可信**——1/3 的成功率被两个「工具误调+直接放弃」的 5K 样本
污染，与自动 docs 兜底无关（#3 正常路径 PASS 证明兜底未破坏任何东西）。可提取的真实
教训反而是**模型行为漂移**：k2.7-code 偶发把无参 search_tools 误传 query，且失败后
**不重试直接放弃**（不是大度试探，是一轮作废）。

**顺带修复**（提交同批）：`search_tools(** _ignored)` 容忍多余参数 + schema 描述写明
"Takes NO arguments"。一次工具误调不该让整轮作废——参数错误宁可忽略也不能抛异常。
这属于「工具自身健壮性」层，比评测更底层。

**遗留**：自动 docs 兜底的价值没有被 E5 有效验证（需要不受漂移污染的样本）。要复验
需排除漂移：要么增大样本、要么先修 search_tools 容错后再跑（本次已修）——下一次
同条件重跑即为 E6 候选。

---

## 12. 自动 docs 兜底复验（【E6】——无净提升，建议收束）

**动机**：E5 后已修 search_tools 参数容错，排除误调污染后复验自动 docs 兜底（E6）。

**控制变量**：同 E3/E4/E5（fix_dedupe_obf，k2.7-code，map ×3）；工具含自动 docs 兜底
+ search_tools 容错。

**数据**：

| 样本 | 结果 | tokens | 行为 |
|---|---|---|---|
| #1 | PASS | 256,486 | 正常路径（自动兜底可用） |
| #2 | PASS | 464,021 | 正常路径，探索较深 |
| #3 | FAIL | 5,119 | search_tools 已正常调用（容错生效），但拿到动态声明后**模型沉默**（第二轮空响应 → TurnEnd），3.2s 即弃（弃疗） |

**结论（诚实）**：**E6 无净提升**——成功率 2/3 与 E3(2/3)/E4(2/3) 持平；成本均 242K vs
E4 的 165K，但单样本波动大（155K~464K），无稳定信号。自动 docs 兜底在"模型正常探索"
的路径上没有改判成功率；异常样本是 k2.7-code 的**弃疗行为**（E5 误调弃疗，E6 正常
拿到声明后仍沉默）——两头 5K 样本是模型问题，不是工具问题。

**建议**：本方向收束。自动 docs 兜底保留（零成本、机制级测试已过），但不再用
单任务小样本追逐显著性——该库弃疗率 ~30%（5K 样本 2/6）让 n=3 的实验置信度太低。
若将来要严格验证：样本 ×5 或换模型，且把"弃疗"单独建为观察指标（非任务失败）。

**跨代总结（E3→E6）**：乱命名库上 name-only 351K → 加 path/docs 165K（-53%）→
自动兜底无再降。**教训链**：名称轴失效（E3 证）→ 非名称维度补位（E4 证，-53%）→
更激进的自动化作未达预期（E6 证，因受益被模型弃疗淹没）。逐代数据齐全，后人可据此
决策是否需要大样本复验。

---

## 13. 验证纪律 A/B（fix_dedupe_verify vs fix_dedupe_obf，【E7】）

**动机**：把"改完必须跑验证-绿灯才算完成"变成 agent 行为纪律（AGENT_DESIGN 49 的
落地形态 B：任务级自验，非 edit 自动触发）。验证纪律是否值得？同批 A/B 直接回答。

**控制变量**：唯一变量 = PROMPT 是否含【验证纪律】要求（改完必须 `python3 verify.py`
直到 "verify OK"）。任务内容/workspace/verify 完全一致（fix_dedupe_verify vs
fix_dedupe_obf）；模型 k2.7-code、map 组、同批次串行 ×3/每组。

**数据**：

| 组 | 通过率 | token 均 | 样本 |
|---|---|---|---|
| A（强制自验） | 3/3 | 136.9K | 118.9 / 113.9 / 177.8 |
| B（无要求，对照） | 3/3 | 71.5K | 56.2 / 88.2 / 70.1 |

**结论（诚实）**：
1. **成功率测不出差异**（双 3/3 天花板）——验证纪律没有负副作用，但也拿不出"提升
   成功率"的证据。要测出价值需更难任务（有红灯循环空间）。
2. **自验明确有成本**：A 组 +91% token（多出的是"跑 verify + 处理结果"的固定开销，
   单次验证约 10-20K tokens）。验证纪律是一份"交付保险"，保费不便宜。
3. **方法论重要提醒（跨日漂移 > 功能差异）**：B 组今天 3/3（56-88K），而同一模型
   同一任务的 E6 前几天是 2/3（256/464/5K）——模型/平台行为逐日波动远大于我们测的
   功能差异。**纵向跨日对比不可靠，A/B 必须同批跑**（本次守住了这个纪律）。
4. 最终建议：验证纪律作为"低风险保险"可接受但要意识到成本；日常不强推（模型常会
   自发验证，E2 已见）；若未来想严格量化其价值，需更难任务 + 更大样本。

---

## 14. 事故留档：包结构重构验证时误触真跑（非实验）

**经过**：2026-09-10 包结构重构（扁平模块 → mini_agent/ 包）收尾验证时，误以
`--help` 探测 run_bench.py（不识别该参数），意外真跑 explain_idempotency 任务
一次：PASS 1.00、5,448 tokens、82.3s（k3，map 组），第二任务启动时被 SIGPIPE
中止。结果文件 explain_idempotency-20260910-143324-map.json 即此产物。

**诚实边界**：单样本、非计划、无对照，不得用于任何纵向/横向对比结论。
唯一正面价值：作为重构后的端到端冒烟——包结构改动未破坏 bench 全链路
（沙箱替换/工具执行/judge 判分/trace 落盘均正常）。

---

## 15. Review 模式基线首秀：注入 bug 检出率（【E8】）

**动机**：R2 第一片——review 模式（agent 不知道 bug 存在）的基线检出率/误报率。
回答「单通道双轴 review 基线到底什么水平」，后续一切增强（并行通道/多轮深挖）
都以此为对照组。

**设施**：3 个注入 bug 任务（review_off_by_one / review_retry_swallow /
review_missing_guard，base/ + bug.patch + META ground truth 行区间）；
判分 score_review：path 匹配 + 行号落区间 ±3，score=recall。模型 k3（kimi-code），
单样本/任务。

**数据**：

| 任务 | 难度 | recall | precision | FP | tokens |
|---|---|---|---|---|---|
| review_off_by_one | easy | 1.0 | 1.0 | 0 | 8,031 |
| review_retry_swallow | medium | 1.0 | 0.33 | 2 | 11,767 |
| review_missing_guard | medium | 1.0 | 1.0 | 0 | 7,265 |

**结论（诚实边界：单样本、任务小而简单、无对照）**：
1. 基线检出率 3/3——小 diff 单 bug 场景下 k3 的 review 检出没有悬念；
   有区分度的任务（多 bug、大 diff、误导性变更）是下一批任务的方向。
2. **副产品（本次评测抓到的设计问题）：误报的语义需要细化。**
   review_retry_swallow 的 2 条「FP」其实是注入 bug 的合理派生观察（docstring 未
   同步、FetchError 变死代码）——确定性判分把「ground truth 之外」一律记为误报，
   但 review 的价值产出不止精确命中。下一迭代：ground truth 支持可接受的派生
   finding 白名单，或 FP 判定加一道 judge。「真 FP」与「有效但不在清单内」必须区分，
   否则 precision 指标会惩罚好的 review。
3. token 成本约 7-12K/任务（diff 小、上下文少）——大 diff 任务的成本曲线待测。

**可复现**：`.venv/bin/python bench/run_bench.py review_off_by_one`（任务级幂等，
base/ + bug.patch 确定性重建沙箱）。

---

## 16. Review 评测深化：难任务 + 误报语义修正（【E9】）

**动机**：E8 基线在简单任务上 3/3 触顶，需要区分度；同时修正 E8 暴露的
「派生正确 finding 被误记 FP」的指标缺陷。

**设施**：新增 3 个 hard 任务（review_multi_bug 双 bug / review_misleading_refactor
falsy 陷阱伪装成简化 / review_cache_key 三文件特性提交藏缓存 key 错位）；
判分升级：bug 支持 aliases（可接受定位）、META 顶层 neutral 区间（派生观察
不算检出也不算误报）、同 bug 区域多条 finding 记冗余不算误报。

**数据**（k3，单样本/任务）：

| 任务 | recall | precision | 备注 |
|---|---|---|---|
| review_retry_swallow（+neutral） | 1.0 | 1.0 | neutral=1（死代码观察正确归类） |
| review_multi_bug | 1.0 | 1.0 | 双 bug 双检出 |
| review_misleading_refactor | 1.0 | 1.0 | falsy 陷阱被识破 |
| review_cache_key 首跑 | 1.0→（升级后 0.5） | 0.5→1.0 | 见副产品 2 |
| review_cache_key 复跑 | 0.5 | 0.5→（同区冗余修正后 1.0） | 见结论 3 |

**结论（诚实边界：单样本、合成任务、作者知答案）**：
1. **合成任务区分度已触顶**：k3 对单/双 bug、伪装重构、跨文件藏匿全部检出。
   再上难度只能靠真实仓库回测（历史 bug-fix commit），这是下一批任务的方向。
2. **副产品 1（ground truth 修正循环）**：review_cache_key 首跑的「误报」实为
   注入代码里我没意识到的真缺陷（add 按 name 写缓存，改名重 add 留孤儿条目）——
   模型发现了 ground truth 之外的真 bug。处置：升级为第二个 ground truth 条目。
   「评测修正指标」之后，「评测修正 ground truth」——这条循环的纯度超预期。
3. **副产品 2（方差）**：同一任务两跑产出不同 findings（首跑抓到孤儿条目、
   复跑漏它但发现数字名键冲突）。单样本 review 结论噪音大，任何旋钮调优的
   结论都必须多样本（与 E7 的跨日漂移教训同构）。
4. 已知可钻空子（留档）：同区刷屏不算 FP——模型若在同一 bug 区域堆 finding 可
   虚高 precision。真出现再收紧。

**可复现**：`.venv/bin/python bench/run_bench.py review_cache_key`（注意单样本方差）。

---

## 17. 真实仓库回测：revert 注入真实 bug（【E10】）

**动机**：E9 证明合成任务区分度触顶。真实仓库任务堵两个偏差质疑：作者知答案、
任务过于玩具。取材方式：** revert 注入**——克隆真实仓库 @ 修复提交 F，revert F
把真实 bug 注回去（真实代码 + 真实缺陷，人工只标注不发明）。

**任务**（3 个，跨 2 个知名项目）：
- review_oss_packaging_ranges：pypa/packaging #1392（无界边界排序错误，比较逻辑）
- review_oss_click_sentinel：pallets/click（Sentinel 深拷贝破坏单例语义）
- review_oss_click_synopsis：pallets/click（synopsis 双重方括号）

**数据**（k3，单样本/任务）：

| 任务 | 首轮 | 修复后 | tokens |
|---|---|---|---|
| review_oss_packaging_ranges | ❌ 0 findings | ✅ 1.0 | 133K |
| review_oss_click_sentinel | ❌ 0 findings | ✅ 1.0 | 62K |
| review_oss_click_synopsis | ✅ 1.0 | — | 16K |

**副产品（本轮最高价值产出）：评测抓到 review 管道的设计缺陷。**
两个 FAIL 不是模型没看到 bug——原始输出显示模型「继续看 _ranges.py 的其余部分…」
还在调研中，**轮次保险丝（8 轮硬熔断）把 review 拦腰截断**，终稿是半成品中间文本，
findings 为空。真实仓库的调研深度（读大文件、追调用方、验证语义）远超合成任务。
修复：两档保险丝——SOFT 档（8 轮）经 steering 通道注入「立即收敛输出 findings」，
HARD 档（12 轮）才 abort。这正是 TurnControl.steer 设计来干的事。修复后两个
FAIL 双双转 PASS。

**结论（诚实边界：3 任务、单样本、一次网络瞬断重试）**：
1. 真实任务区分度真实存在：首轮 2/3 FAIL（合成任务全 PASS）——成本也真实
   暴露（16K-133K tokens/任务，10 倍于合成任务）。
2. 「检出率」只是指标之一：**管道行为（熔断时机）本身就是检出率的一部分**——
   review agent 的质量 = 模型能力 × 管道给它的空间。
3. 网络瞬断（RemoteProtocolError）一次：重试即过，记为基础设施噪音而非信号。
   后续若要严格，bench 需要失败重试策略。

**可复现**：`.venv/bin/python bench/run_bench.py review_oss_packaging_ranges`
（remote 任务需联网克隆；META 里有固定 commit sha）。

---

## 18. 并行双通道 A/B（【E11】——recall 假设被否定，precision 有收益）

**动机**：R1 刻意留下的实验——并行 researcher 通道（每轴一个独立会话，Python
确定性合并）是否优于单通道双轴基线。假设：轴聚焦提升 recall（单通道两轴互相
稀释注意力）。

**控制变量**：同一 9 任务评测集（6 合成 + 3 OSS revert 注入）、同模型 k3、
同 prompt 内容（仅轴范围不同）、合并逻辑零 LLM 成本（path:line 去重 severity 取高）。
唯一变量 = single / parallel。样本：合成 n=3/组，OSS n=1/组（成本约束）。
污染剔除：E10 保险丝修复前的 FAIL 不计入（那是旧管道的行为）。

**数据**（E11 批次 42 次运行）：

| 模式 | recall | precision | tokens/任务 |
|---|---|---|---|
| single（基线） | 0.963 | 0.889 | 25.3K |
| parallel | 0.944 | **1.000** | 33.2K（+32%） |

**结论（诚实边界：OSS 单样本、任务集小、recall 多在天花板）**：
1. **recall 假设被否定**：并行不提升检出（0.944 vs 0.963，差异在噪音内）。
   「轴间注意力稀释」在当前任务规模上不成立。
2. **precision 有真实收益**：parallel 零误报（9/9 任务），single 在两个 OSS 任务上
   各出 1 条遐想 finding（precision 0.5）。机制解释：standards 轴 prompt 写死
   「只报违反明文约定的」，约束了自由发挥。对「门禁可用性」而言 precision 比
   recall 更关键（误报多 = 用户关掉它）。
3. **成本 +32%** 换 precision +0.11。当前结论：默认保持 single，parallel 留作
   --mode 实验开关；门禁场景（precision 敏感）可以考虑 parallel。
4. 已知天花板效应：多数任务 recall=1.0 两组无差异，cache_key（唯一有漏检的任务）
   反而 single 略优——漏检的是「孤儿缓存条目」这类二次推理 bug，轴划分对它无影响。

**可复现**：`bench/run_bench.py <task> --review-mode=single|parallel`；
聚合脚本见 E11 对话记录（results/*.json 按 task+mode 分组取最新 n）。

---

### 【E11 补遗】2026-09-11 评测代码合入事故：parallel 实现从未进 git（dogfood 抓到，非 API 评测）

**事件**：E11 提交 `7ba5226`（feat(review): parallel 双通道模式）只合入了
`bench --review-mode` 接线、CLI `--max-findings` 与 3 条单测，**`_run_one` /
`_merge_findings` / `run_review(mode=)` 实现本身没有进 git**。合入后 main 状态：
- `test_review.py::TestParallelMode` 2 条测试挂（AttributeError: 无 `_merge_findings`）
- `bench/run_bench.py <review任务>` 当场 TypeError（`mode=` 不是 run_review 的关键字参数）

**根因**：评测在本地未提交状态跑完并留档，提交时只 add 了部分文件/部分 diff
（bench 接线 + 测试先到，实现没跟上）。E11 数据的「可复现」承诺因此在合入后
是假的——这是「评测必留档」纪律的教科书式反例：**数据留档 ≠ 代码留档，
可复现性必须靠 commit 里能跑的代码保证，不靠留档文字**。

**修复**（本次提交）：按测试契约恢复实现——`_run_one(prompt, render)` 共用
只读工具面+保险丝；`_merge_findings`（path:line 去重、severity 取高、同 severity
先到先得、high 在前）；`run_review(mode=single|parallel)` 恒返回 sessions 列表；
CLI 适配解包。320 测试全绿。**注意**：恢复的实现是重新写的，与当年跑出 E11
数据的那份本地代码**不是逐字节同源**（契约一致：两轴独立会话 + 确定性合并），
后续有 parallel 评测结论时按恢复版重新累积数据。

**工序沉淀**（防再犯）：evaluation commit 的提交信息与代码必须逐条对账——
写「单测：……」，git 历史里就该有对应符号。提交前跑一次 `pytest` 是最后防线
（这次就是 pytest 先抓到，CI 未跑）。

---

### 【E12】2026-09-11 多语言任务库摸底：6 新任务全检出，precision 0.833，OSS 注入副作用被抓

- 动机：R3 方向一任务库 26→32（+3 合成二次推理族 +3 OSS revert 注入，语言覆盖
  Go/Rust/JS）。跑第一轮摸底：新任务的检出质量如何、多语言下 review 行为是否一致、
  有没有可改进的判分/任务设计问题。
- 控制变量：同一模型 k3、single 模式（默认基线）、每任务 n=1（摸底不过度烧钱）。
  任务集 = 3 合成（二次推理族）+ 3 OSS；无 prompt/内核改动（纯任务新增）。
- 设施：`bench/run_bench.py <task>`；合成任务 base+bug.patch，OSS 任务
  remote revert 注入（F 为该仓库真实修复）。样本 n=1/任务。
- 数据（E12 批次 6 次运行，总 ~109K tokens，~6 分钟）：

  | 任务 | 语言 | recall | precision | FP | tokens |
  |---|---|---|---|---|---|
  | review_ttl_refresh_stale | py | 1.0 | 0.5 | 1 | 8.7K |
  | review_pool_lease_leak | py | 1.0 | 1.0 | 0 | 8.8K |
  | review_mirror_index_stale | py | 1.0 | 1.0 | 0 | 7.7K |
  | review_oss_mapstructure_unmarshal_panic | go | 1.0 | 0.5 | 1 | 12.6K |
  | review_oss_chrono_offset_24h | rust | 1.0 | 1.0 | 0 | 52.3K |
  | review_oss_morgan_token_escape | js | 1.0 | 1.0 | 0 | 18.9K |

- 结论：
  1. **6/6 全检出，recall 天花板依旧**——与 E11 观察一致（任务对 k3 可检出面饱和）。
     这批「二次推理族」本意压 E11 的 cache_key 漏检面，但 k3 全过了；真正的区分度
     提升要靠更难任务（multi-bug 混合、更大仓库），合成单体 bug 已到天花板。
  2. **precision 0.833→1.0（修复后复测）**：两个 FP 各有成因（见副产品），均非
     模型泛化错误；修复见「副产品」段。多语言（go/rust/js）行为与 py 无差异——
     语言不是区分维度。
  3. **OSS 注入的成本随仓库规模放大**：chrono 52K tokens 是 packaging(21K) 的两倍
     多——大仓库的 repo map 注入+read_file 深挖成本显著；任务成本预算要考虑。
- 副产品（两个判分/任务设计问题，比跑分更有价值）：
  1. **OSS revert 注入的机制性 FP**（mapstructure，decode_hooks_test.go:553）：
     revert F 会连 F 的测试改动一起回退，diff 里出现「删回归测试」，模型报告
     「回归覆盖被移除」——真实 review 里这是**正当观察**（删测试确实该审），但
     它不是注入 bug。方向：OSS 任务把测试回退区标为 neutral，或调整注入方式
     （revert 后复原测试文件，只留 src 的 bug）。
  2. **合成任务的行为杂音 FP**（ttl，cache.py:20）：旧实现 get 会 pop 过期条目，
     拆分两表后 pop 没了，模型报「不再清理过期条目」——不是 bug 是行为变化。
     方向：设计 bug 时保持无关行为不变（pop 语义保留），杂音是任务设计可抹平的。
- 可复现：`bench/run_bench.py review_ttl_refresh_stale`（及其余 5 个）；
  结果在 bench/results/*20260911-14*.json。

---

### 【E12.5】2026-09-11 加深任务第一炮：multi-bug+诱饵（txn）与状态残留（quota），假漏检机制缺陷被抓

- 动机：E12 结论「合成单体 bug 已到 recall 天花板」。加深两档：multi-bug 混合
  （两个真 bug 需跨调用/跨契约追踪）+ 诱饵（red herring，带注释的设计行为，
  报了即 FP，测 precision 防线）；quota 是「一行差（while→if）要追踪窗口
  语义」的二次推理难度档。
- 控制变量：k3、single、n=1；同一底座 base→bug.patch 合成模式；无 prompt/内核改动。
- 设施：`bench/run_bench.py review_txn_dedupe_flush|review_window_quota_slide`。
- 数据（2 任务，~17K tokens）：

  | 任务 | recall | precision | FP | 首次判定 |
  |---|---|---|---|---|
  | review_window_quota_slide | 1.0 | 1.0 | 0 | 一次过 |
  | review_txn_dedupe_flush | 1.0（初判 0.5，假漏检） | 1.0 | 0 | 修完重跑 |

- 结论：
  1. **加深任务仍未砸穿 recall**：multi-bug 两条真 bug 全检出（去重键族 25 行、
     return 单位 35 行），诱饵（0 元心跳入账跳过）没被当成 FP——k3 在
     8-9K tokens 预算内对这两个难度档依旧饱和。区分度要再加深（大仓库、
     跨文件状态、或弱模型验证）。
  2. **precision 防线 1.0**：诱饵零误报，注释防御被正确信任——precision 维度的
     加深验证通过（E11 说门禁场景 precision 敏感，这条线目前很硬）。
- 副产品（高价值，评测机制缺陷）：
  **假漏检：bug zones 重叠导致 finder 错配**。txn 初判 recall=0.5（missed=
  return-unit-drift）是假象——模型两条都报了：bug A 的 alias（ledger 入账行 32）
  与 bug B 主区（return 行 34）间距 2 行 < 2×tolerance(3)，alias 区间 29-35 吞掉了
  本该属于 bug B 的 finding，真实去重 finding 落单。修复：**ground truth zones 间距
  必须 > 2×line_tolerance，alias 不得靠近其它 bug 的 zone**。重放判分 recall=1.0
  后重跑端到端确认。教训：判分几何错误能伪装成模型漏检——zones 排布要静态检查。
- 可复现：两条命令同上；结果在 bench/results/*20260911-15*.json。

---

### 【E12.6】2026-09-11 加深任务 × 交叉模型第一发：k2.7 家族同样打不穿，模型间解释差异被抓

- 动机：E12.5 结论「k3 对加深任务饱和」。交叉模型找区分度：同档案 kimi-code
  （api.kimi.com，同一 key）内切 kimi-for-coding（k2.7 家族）——不是验证模型
  优劣，是找「哪条模型线能打出 recall<1.0」决定主力评测/门禁模型。
- 控制变量：同一 2 任务（txn multi-bug+诱饵 / quota 一行差）、n=1、single；
  仅模型变量（k3 基线数据见 E12.5）。
- 设施：`bench/run_bench.py <task> --model=kimi-for-coding`；结果文件带 model 标记。
- 数据（2 任务 × kimi-for-coding，~36K tokens；173s 的 quota 说明非同一模型）：

  | 任务 | 模型 | recall | precision | FP |
  |---|---|---|---|---|
  | review_window_quota_slide | kimi-for-coding | 1.0 | 1.0 | 0 |
  | review_txn_dedupe_flush | kimi-for-coding | 1.0 | 1.0 | 0 |

- 结论：
  1. **k2.7 家族同样饱和**：两个加深任务 recall/precision 全 1.0——「天花板」跨
     模型成立（至少这两档难度下）。找区分度需要更难的注入面（跨文件状态/大仓库/
     或刻意泄题验证诚实度），堆模型边际收益已很低，模型扫荡到此为止。
  2. **模型间解释差异**（副产品）：k2.7 在 quota 上报 low「注释描述的缺陷并不
     存在（受 len(hits)≤limit 限制）」——机理复核：窗口内持续请求时 pop 清理
     速度跟不上旧戳积攒，虚高保持成立，其质疑不成立。但这条暴露了一个真实
     风险：**ground truth 注释把机理写在代码里，可能被模型当「注释与实现不符」
     反咬**；是否把机理注释移出代码（只留 diff 符号）列为后续任务设计选项。
- 副产品 2（成本观察）：kimi-for-coding 单任务 13-23K tokens、2-3 倍于 k3
  耗时——高成本模型不适合做主力评测扫荡，适合做「终审复核」档。
- 可复现：上面两条命令（--model=kimi-for-coding）。
