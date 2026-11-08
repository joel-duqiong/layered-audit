# 涌现候选链生成器 + FDR 门禁 —— 实测结果

> 数字全部来自实测产物，没有估值。跑法见文末「怎么复现」。
> 判定口径的取舍记在最后一节「判断记录」，不同意可以直接推翻（但要给出理由）。

## 0. 一句话结论

候选生成器在 demo 上**独立提出了那个真的涌现缺陷所在的链**（入口、脊柱、写汇、
条件种类全对），把它真跑一遍，裁决仍然是 `EMERGENT_CONJUNCTIVE`；
FDR 门禁给出 **FDR_inj = 0.500**（分母 2 / 检出 2 / 归属正确 1），
漏掉的是 `INJ-PROFILE-VIP` —— 它的根因**只被引用、没有任何一层开单**，
按「可归属」口径不算检出。门禁因此判**红**（退出码 1），这是它该做的事。

> ### 后续：这条红是怎么变绿的（2026-11-08 第二轮）
>
> 这份报告写完的时候，demo-shop 的**服务层还没审过** —— 所以
> `INJ-PROFILE-VIP` 的根因
> `order.quota_checked_on_quoted_amount_ledger_on_settled`
> 没有任何一层能开单（它只被集成层引用）。门禁把这件事**指名道姓地报了出来**，
> 这正是它存在的意义。
>
> 第二轮把 demo 的服务层补上之后：
>
> | | 第一轮（本文档） | 第二轮（补上服务层） |
> |---|---|---|
> | demo 服务层 | 没审 | 有（业务不变式判定，3 个差分场景） |
> | `INJ-PROFILE-VIP` 的 owner | 无（只被引用） | **service** 层 own：`BI-QUOTA-LEDGER-PARITY` |
> | FDR_inj | 0.500 | **1.000** |
> | Attr_acc | 0.500 | **1.000** |
> | 门禁结论 | RED（退出码 1） | **GREEN（退出码 0）** |
> | 流水线退出码 | 1 | **0** |
>
> **阳性对照**：把服务层的两条 finding 从 `all_findings.json` 里抽掉再跑门禁，
> 立刻变回 RED（FDR 0.500 / 漏检 `INJ-PROFILE-VIP`）——
> 也就是说这个绿是**挣来的**，不是把阈值调松换来的。
>
> 完整闭环现在是：**检出（候选生成器）→ 裁决（真跑）→ 判归属（门禁）
> → 补归属（服务层的差分证据）→ 转绿**。

## 1. 交付物

| 文件 | 是什么 | 状态 |
|---|---|---|
| `pilot/engine/emergence_candidates.py` | 候选链生成器（通用，零项目字面量） | 新增，712 行 |
| `pilot/engine/emergence_gate.py` | FDR 门禁（通用，零项目字面量） | 新增，708 行 |
| `pilot/emergence_baseline.json` | 阈值 + 分母哈希 + 集合哈希（按目标分节） | 新增（`--init` 生成） |
| `demos/shop/emergence_candidate_run.py` | demo 侧执行器：候选 → 场景 → 裁决 | 新增 |
| `demos/shop/artifacts/emergence_candidates.json` | 候选链产物 | 新增 |
| `demos/shop/artifacts/emergence_candidate_run.json` | 候选执行结果（裁决指纹） | 新增 |
| `demos/shop/artifacts/emergence_gate.json` / `.md` | 门禁报告 | 新增 |

引擎纯净度：`pilot/engine/check_engine_purity.py` 通过（19 个引擎文件零项目耦合）。
残留守卫：`pilot/probe_residue_check.py --selftest` 通过；demo 实跑后残留 0 行。

## 2. 算法

### 2.1 候选生成器（`pilot/engine/emergence_candidates.py`）

输入：可执行依赖图 `relation_net.json` + 不变式声明 + （可选）注入目录做召回比对。

**剪枝硬约束**（少一条就会爆出几千条不可复现的候选）：

1. 链首必须是 `HTTP_ENTRY`（外部可触发）
2. 深度 ≤ 4（入口之后的代码节点数）
3. 禁止环（路径内节点不重复）
4. 每个写汇最多展开 3 条路径
5. 三元组（入口, 写汇, 不变量）去重
6. 只保留含写汇的路径（只读路径不是缺陷候选）
7. **必须锚定一条已声明的不变量**（没锚点的链不开单 —— 那属普通单点缺陷）
8. 全局候选上限 500

**模式模板**（DESIGN §4.3 的七类，代码里是 `PATTERNS = ("T1".."T7")`）：
T3 跨实体信任（读 A 表 → 写 B 表）、T2 派生快照（读 A → 写 A）、T1 无守卫写、
T5 状态机（写汇落在状态字段）、T7 查询放大（分支 ≥ 8）在本轮各自按节点特征判定；
T4 无鉴权链、T6 级联外键留了模板位但本轮没有落到图上（见 §5 缺口）。

**排序**：
- 枢纽度四维合成 `hub = 0.35·b + 0.25·r + 0.25·w + 0.15·t`
  （加权 betweenness / PageRank / 写放大 / 信任边界穿越）
- `score = 2.0·blast + 1.5·hub + 2.0·inv + 1.0·prec + 1.0·gap − 0.8·cost − 1.5·red`
  （blast 来自 Monte-Carlo 级联估计：2000 次采样，按枢纽加权采样故障、BFS 传播，
  纯静态模拟，不碰被测系统）
- 随机数走**固定种子 20261107** → 同输入两次跑结论完全一致

**防塌缩**：集中度上限 40%；配额 每入口 ≤4 / 每表 ≤8 / 每不变式 ≤6 / 每模式 ≥1；
70% 利用 + 30% 探索；**没跑的链全部记录**（`unrun`），比对的是集合指纹不是「跑了几条」。

### 2.2 FDR 门禁（`pilot/engine/emergence_gate.py`）

度量对象是**审计系统本身**，不是被测系统：
注入目录里每条 `active=true` 的注入代表一个已知的低层根因，审计跑完之后，
这条根因要么被某一层正确开单（检出且归属正确），要么没有。

```
FDR_inj  = 检出且归属正确 / 分母            （主指标）
Attr_acc = 检出且归属正确 / 检出            （抓到的里面，归对层的比例）
```

- **分母** = 目录里 `active=true` 的条目，减去活性检查不过的
  （被剔的必须告警并记录，**不许静默剔除**）
- **分子** = 该注入代表的根因有且只有一个 `own` 单认领，且认领层是所有提及该根因的层里
  **最低的一层**（`function < file < class < service < integration`），
  类别在该层契约允许范围内、`ground_truth=verified`
- **只被引用（cited）没有 own 单 = 没有归属 = 不算检出**
  —— 「可归属」是本项目的核心承诺，归错层等于没检出来

其它产物：分母哈希、集合哈希、分类别 FDR、漏检清单、逐注入判定明细。

**退出码三态**（沿用 D8 决议）：

| 码 | 含义 | 触发 |
|---|---|---|
| 0 | 绿 | FDR/Attr_acc 达标、无漂移、输入齐 |
| 1 | 红 | FDR 或 Attr_acc 低于阈值；分母哈希漂移；集合哈希漂移；某类别注入 ≥3 且检出 0 |
| 2 | 不完整（**没查完**） | 输入缺、注入没跑、分母为空、基线缺失、活性剔除集合漂移 |

防作弊实测：把 `INJ-PROFILE-VIP` 从目录里删掉（就是「把检不出来的注入删掉来提高 FDR」），
FDR 会跳到 1.0 —— 但分母哈希与集合哈希双双漂移，门禁直接判**红**，
`--init` 拒绝覆盖不一致的基线，必须显式 `--accept`。

## 3. demo 上的真实数字

输入规模：demo 关系网 **37 节点 / 29 边**
（`HTTP_ENTRY` 5、`SVC_OP` 13、`REPO_OP` 4、`TABLE` 4、`ENTITY_FIELD` 11；
`CALLS` 14 / `READS` 5 / `EXPOSES` 5 / `DERIVES` 4 / `WRITES` 1），
不变式锚 2 条（`BI-QUOTA-LEDGER-PARITY`、`BI-VIP-MEMBER-PRICE`，锚表都是 `shop_order`）。

### 3.1 候选生成

| 指标 | 值 |
|---|---|
| 候选链总数 | **2** |
| 选中 / 没跑 | 2 / 0（预算 12，全部选中） |
| 模式分布 | `T3: 2`，`T1/T2/T4/T5/T6/T7: 0` |
| 入口分布 | `POST /api/orders`（`OrderController#create`）：2 |
| 候选集指纹 | `4ad5d478ba218655`（两轮一致） |

两条候选同一条脊柱、不同锚：

- `C0000` → 不变式 `BI-QUOTA-LEDGER-PARITY`，score **1.666667**
  脊柱 `OrderController#create → OrderService#create → ShopOrderRepository#save`，
  写汇 `table:shop_order`，读汇 `table:shop_customer`，
  条件槽 `instance:PricingStrategy.settle` / `data:table:shop_customer` /
  `request:CreateOrderRequest.{currency,customerName,quotedAmountCent}`
- `C0001` → 不变式 `BI-VIP-MEMBER-PRICE`，score **0.666667**（同脊柱、同写汇）

### 3.2 召回（生成器 vs 注入目录里的已知链）

| 已知链 | 命中 | 候选 | 条件覆盖 | 召回率 |
|---|---|---|---|---|
| `EMERG-QUOTA-LEDGER-DIVERGENCE` | ✓ | `C0000` | **full** | 1.0（1/1） |

### 3.3 候选真跑（`demos/shop/emergence_candidate_run.py`）

把候选提出的条件槽落到已知链的真实场景上，用同一个裁决器跑：

```
[EMERG-QUOTA-LEDGER-DIVERGENCE] 候选 C0000 条件覆盖 ✓｜裁决 = EMERGENT_CONJUNCTIVE
    条件槽 data     ← 注入 INJ-TIER-VIP：候选已提出
    条件槽 instance ← 注入 INJ-PROFILE-VIP：候选已提出
    条件槽 request  ← 注入 INJ-FX-NONBASE（inactive）：候选已提出
残留校验：探针数据行 0 条（必须为 0）
```

- 裁决 `EMERGENT_CONJUNCTIVE`，owner = `integration`（own），
  category = `integration.end_to_end_business`
- 核心集 `INJ-TIER-VIP + INJ-PROFILE-VIP`（leave-one-out 求出的最小核心集）
- 裁决指纹 `f10753a38f7ef780`（与人工链那一轮**逐位相同**）
- 候选执行指纹 `b146ff57eb2fd281`，两轮一致

### 3.4 FDR 门禁

| 指标 | 值 |
|---|---|
| 分母（active ∧ 活性通过） | **2**（`INJ-TIER-VIP`、`INJ-PROFILE-VIP`） |
| 目录里 inactive（不进分母） | 1（`INJ-FX-NONBASE`，只用于敏感性分析） |
| 检出（根因被提及） | 2 |
| 检出且归属正确 | **1** |
| **FDR_inj** | **0.500** |
| **Attr_acc** | **0.500** |
| 分类别 FDR | `layer:service`：2 条 / 检出 2 / 归属正确 1 → 0.500 |
| 分母哈希 | `6cd68a40b6a5d3b9` |
| 集合哈希 | `8661e3c30899ed59` |
| 门禁指纹 | `b89aa52606d938f9`（两轮一致） |
| 结论 | **RED，退出码 1**（FDR 0.5 < 0.85；Attr_acc 0.5 < 0.95） |

**漏检清单（1 条）**

| 注入 | 根因 | 检出 | 状态 | 原因 |
|---|---|---|---|---|
| `INJ-PROFILE-VIP` | `order.quota_checked_on_quoted_amount_ledger_on_settled` | 是（被引用） | `cited_only` | 只被引用、没有 own 单：这个根因没有归属，按「可归属」口径不算检出 |

## 4. 为什么门禁是红的（这才是它该报的）

demo 只跑了 **class 层 + integration 层**，service 层没有产物。
`INJ-PROFILE-VIP` 代表的是 service 层的根因
（`order.quota_checked_on_quoted_amount_ledger_on_settled`），
全库里没有任何一条 finding 以 own 关系认领它 —— 集成层只是把它写进了
`related_root_causes` 当佐证。

按契约「高层可引用低层缺陷作佐证，但不得重复开单」，
集成层引用它是对的，但**没人开单就意味着这个缺陷没有归属**。
所以它既不是「检出了」，也不是「归错层」，是**没检出**。

另一条 `INJ-TIER-VIP`（`pricing.member_tier_bound_to_deployment_profile`）
则被 class 层的 `CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE` 以 own 关系正确认领，
层序最低、类别 `class.invariant` 合法、`ground_truth=verified` → 计入分子。

**因此 0.5 是一个真实且有意义的数**：
它同时说明「涌现链抓到了」「低层根因有一半没人认领」。

## 5. 剩余缺口（明确写出来）

1. **场景物化还是人给的。** 候选只提出「条件槽」（`data:` / `instance:` / `request:`），
   具体造数（客户等级 = VIP、装配 = vip profile）来自已知链的 `flow`。
   生成器 → 可执行场景 之间缺一个 **schema 驱动的造数器**。
   所以「候选抓到了真链」目前验证到的是「条件种类对得上 + 真跑结论一致」，
   **不是**「生成器自己造出了那个场景」。这是最大的一条缺口。
2. **七个模式只命中 T3。** demo 只有 37 个节点、1 条 `WRITES` 边，
   T1（无守卫写）/ T2（读同表）/ T5（状态字段）/ T7（分支 ≥ 8）在图上天然不成立；
   T4（无鉴权链）、T6（级联外键）本轮没落到图上。
   需要到 jiuyan（151 节点 / 414 边）上再验一遍模式覆盖 —— 现在还不能说七个模板都能用。
3. **分母 2 的统计意义很弱。** 0.5 是实测值，但 2 条注入撑不起任何统计结论；
   它的价值是「门禁跑得动、判定口径是硬的、漏检能指名道姓」。
4. **jiuyan 绑定没有注入目录。** `pilot/emergence_injections.json` 不存在，
   门禁对 jiuyan 直接退出码 **2（没查完）**，不是 0。
   要在 jiuyan 上量 FDR，得先有一份 jiuyan 的注入目录。
5. **没有接进 `run_pipeline.sh`（故意的）。** jiuyan 没有注入目录，
   接进去会让每一次 jiuyan 流水线都以退出码 2 收尾 —— 那是假红。
   等 jiuyan 有注入目录、门禁在 jiuyan 上先绿一次再接。
6. **`instance` 条件的来源是补丁。** 装配上下文条件是从 `CALLS` 闭包里取的
   （被调用到的装配点），只认「调用得到」的那种注入点，
   没有做「同一份代码在不同 profile 下行为分叉」的通用识别。

## 6. 自证（四条，全部实测）

| 自证项 | 结果 |
|---|---|
| ① 候选集包含真涌现，且裁决仍是 `EMERGENT_CONJUNCTIVE` | ✓ `C0000` 命中 `EMERG-QUOTA-LEDGER-DIVERGENCE`，条件覆盖 full，真跑裁决 `EMERGENT_CONJUNCTIVE`（核心集 `INJ-TIER-VIP+INJ-PROFILE-VIP`） |
| ② FDR 门禁算出的是**真数**（不是 0、不是 1） | ✓ `FDR_inj = 0.500`（分子 1 / 分母 2），`Attr_acc = 0.500` |
| ③ 两次跑结论完全一致 | ✓ 候选集 `4ad5d478ba218655` ×2、裁决 `f10753a38f7ef780` ×2、候选执行 `b146ff57eb2fd281` ×2、门禁 `b89aa52606d938f9` ×2 |
| ④ 引擎纯净 + 零残留 | ✓ `check_engine_purity.py` 通过（19 个文件零耦合）；`probe_residue_check.py --selftest` 通过；demo 实跑后残留 0 行 |

额外验证（防作弊路径）：删掉 `INJ-PROFILE-VIP` 后 FDR 变 1.0，
但分母哈希与集合哈希双双漂移 → 门禁判**红**；`--init` 拒绝静默重钉，要 `--accept`。

## 7. 怎么复现

```bash
cd /Users/june/.cc-weixin/workspace/layered-audit

# 1. 候选生成（固定种子 → 两次跑指纹一致）
python3 pilot/engine/emergence_candidates.py \
  --net demos/shop/artifacts/relation_net.json \
  --invariants demos/shop/invariants.json \
  --catalog demos/shop/emergence_injections.json \
  --out demos/shop/artifacts/emergence_candidates.json

# 2. 候选真跑（真容器 + 真库；跑完自删，末尾机器校验零残留）
python3 demos/shop/emergence_candidate_run.py

# 3. FDR 门禁（demo 绑定）
LAYERED_AUDIT_TARGET=demos/shop/target.json python3 pilot/engine/emergence_gate.py

# 4. 首次钉基线 / 接受漂移后重钉
LAYERED_AUDIT_TARGET=demos/shop/target.json python3 pilot/engine/emergence_gate.py --init
LAYERED_AUDIT_TARGET=demos/shop/target.json python3 pilot/engine/emergence_gate.py --init --accept

# 5. 守卫
python3 pilot/engine/check_engine_purity.py
python3 pilot/probe_residue_check.py --selftest
LAYERED_AUDIT_TARGET=demos/shop/target.json python3 pilot/probe_residue_check.py
```

门禁自带的基线路径是 `<工具目录>/emergence_baseline.json`（即 `pilot/`），
按目标分节存（`targets.<target>`），因为**阈值是工具级契约、分母是项目数据**；
换项目不会互相污染。demo-shop 已钉；
jiuyan 暂无注入目录 → 没有分母可钉，门禁对 jiuyan 退出码 2。

## 8. 判断记录

| # | 判断 | 理由 | 反对的人要给出什么 |
|---|---|---|---|
| J1 | 「检出」= 根因有 own 单认领；只被 cited 引用**不算**检出 | 契约原文「高层可引用低层缺陷作佐证，但不得重复开单」；引用不是开单，缺陷没有归属就等于没抓到 | 说明「引用」为什么算开单 |
| J2 | `Attr_acc` 的分母是**检出数**（不是分母） | 若分母是全量，`Attr_acc` 恒等于 `FDR_inj`，两个指标就重复了；现在的定义下「抓到但归错层」会被单独暴露 | 给出第三种定义 |
| J3 | 活性检查不过的注入：剔除 + 告警 + 记进基线的 `alive_dropped`；**剔除集合漂移判 INCOMPLETE 而不是 RED** | 活性失败通常是环境漂移（容器挂了、注入的缺陷被修好了），不是有人作弊；用第三态「没查完」表达，既不放过也不误报 | 说明为什么不该红 |
| J4 | 分母哈希钉「active=true 集合」，集合哈希钉全目录 | 删一条注入两个都变 → 双红；`--init` 拒绝覆盖不一致的基线，必须 `--accept`，把「重钉」变成一次显式动作 | — |
| J5 | 基线放 `pilot/emergence_baseline.json` 并按目标分节 | 阈值属工具、分母属项目；放一起但分节，换项目不互相污染 | — |
| J6 | owner 必须是「提及该根因的层里**最低**的一层」，比它低的层只引用却不开单 = 归错层 | 契约原文「owner = 能给出可复现证据的最低层(最接近根因)」 | 说明层序判定该怎么换 |
| J7 | `ground_truth != verified` 的 own 单不算分子 | 未复现的单是「可能存在」，不是「检出」 | 说明未验证的检出该不该算数 |
| J8 | 门禁**没有**接进 `run_pipeline.sh` | jiuyan 无注入目录 → 每次流水线都退出码 2 = 假红；等 jiuyan 有目录并先绿一次 | 或者给出一个「无目录即跳过」的语义 |
| J9 | 候选的 `instance` 条件只认 `CALLS` 闭包里的装配点 | 通用图上没有「同一份代码在不同 profile 下分叉」的表达，先用可算的近似；等造数器落地再收紧 | — |
| J10 | 分类别 FDR 的类别缺失时退化为 `layer:<层>` | 目录里的注入没有 `category` 字段；用注入所在层做兜底桶，至少不把不同类混在一起算 | 指定该用什么替代 |
