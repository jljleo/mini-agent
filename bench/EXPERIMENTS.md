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
