# 分层多 Agent 审计系统 · 架构设计

> 配套文档：`CLAUDE.md`（目标与进度，**先读它**）、`HANDOFF.md`（细节）、
> `EMERGENCE.md`（外部工作引用与映射）、`layers.yaml`（层契约）。
>
> 本文是**设计决议的正文**。每条决议都注明来源 Agent 与落地状态。
> 6 个设计 Agent 并行产出，本文件是它们的交叉结论。

---

## 0. 一句话架构

> **一个代码仓库 = 一群 Agent。**
> 每个单元（函数/类/文件/服务）是一个单元细胞，各有使命；
> 每个 Agent = 确定性执行体 + 作用域参数化 + 关系网 + 基线；
> 静态命（读代码知道自己该干什么）与动态命（真被调起来跑）差分出单；
> 单点全过不等于系统对 —— 涌现层负责个体对、合起来错的那部分。

---

## 1. Agent 群的数据模型

### 1.1 三个概念，不许混

| 概念 | 定义 | jiuyan 数量 | 10 万行预估 |
|---|---|---|---|
| **unit（单元）** | 可寻址的代码实体：method / class / file / service / flow | 533 | ~5500 |
| **agent_role（角色）** | 判定手段的绑定 = 层契约的 category + oracle | 21 | 恒定 ≤ 12 |
| **agent_instance（实例）** | (role, unit) 二元组 —— 「某个判定角色施加于某个单元」 | **2071** | ~5500 |

**规模规则**：执行体（角色数）不随代码规模增长；只有实例数线性增长。
模型成本按 **batch** 增长，不按实例增长。

### 1.2 Agent 实例结构

```
agent_id / role_id / layer
unit          {unit_kind, unit_key, file, symbol, line}
mission       {statement, source, confidence, gate_eligible, obligations[]}
                 └ obligation {obligation_id, statement, status, source,
                                evidence_refs[], gate_eligible}
static_life   {carrier_id, facts, contract_fingerprint, blind_to[]}
dynamic_life  {fidelity, exercised, cases[], behavior_fingerprint}
relations     {in_edges[], out_edges[]}
conclusions   {agent_status, fulfillment{}, coverage{}, finding_ids[],
                root_cause_keys[], owner_layer}
state         {status, stale_reason, baseline_key}
```

**三个设计要点**：

1. **双轴指标**：`fulfillment`（义务达成度）与 `coverage`（覆盖率）分开。
   单轴会把「没测」混进「对」。
2. **三值逻辑**：义务状态只能是 `satisfied` / `violated` / `unknown` / `not_exercised`。
   `unknown` **不进 satisfied**。有 `unknown` 时单元不能判 PASS。
3. **承重字段分级**：`statement`（自然语言）不参与任何判定；
   `template_id` / `polarity` / `slots` / `when` / `assertions` 才承重。
   → 模型产出的自由文本进不了门禁，物理上隔离。

### 1.3 义务（obligation）的确定性来源

| 来源 | 产出 | 是否需要模型 |
|---|---|---|
| 契约派生 | 层契约的每个 category → 一条义务模板 | 否（模板是项目配置） |
| 不变式派生 | `invariants.json` 每条不变式 → 按写入单元展开 | 否 |
| 接口派生 | OpenAPI 每个 endpoint → 服务级契约义务 | 否 |
| **模型候选** | 模型提出候选 → `source=model_hint, gate_eligible=false` | **是，但只是候选** |

---

## 2. 关系网与失效传播

### 2.1 五种边、三级解析状态

| 边 | 抽取来源 | 稳定性 | 失效语义 |
|---|---|---|---|
| `call`（函数级） | `javap` 的 `internal_calls` / `external_calls` | behavior | 调用方重验 |
| `type_use`（类级） | `cross_file_edges`（229 条） | semantic | 使用方复核 |
| `file_use` | 同文件/import | semantic | 同文件复核 |
| `http`（服务级） | OpenAPI + 前端 mapper + Controller 映射（三方一致才发） | signature | 消费方重验 |
| `runtime`（DI/代理） | 见下 | unknown | **保守失效** |
| `cross_repo` | 消费方客户端代码 + 提供方能力声明 | signature | 消费方重验 |

解析状态：`static`（静态可定）/ `inferred`（推断）/ `observed`（运行时观测到）/ `unknown`（解不出）。

### 2.2 Java 动态性：解不出来的边怎么办

被测系统**无 actuator**、**不许改源码** → Spring 的 DI 装配结果无法在运行时直接反射取出。
按三级证据处理，**不假装能解**：

| 级别 | 判据 | resolution | 处理 |
|---|---|---|---|
| E1 结构级 | 注入点类型唯一候选（排除 @Profile/@Conditional 不匹配） | `inferred` | 可用，失效时弱强度传播 |
| E2 行为级 | 打桩代理**实际观测到**该实现被调用 | `observed` | 强传播 |
| E3 不可解 | 多候选 + @Primary/@Qualifier 组合 | `unknown` | **保守失效 + 进盲区账** |

**保守失效规则**（关键）：`resolution=unknown` 的边一律触发过近似失效。
宁可多标不可漏标 —— 否则「只更新受影响的 Agent」会变成「漏更新」，门禁假绿。
报告同时给 `N_resolved` 与 `N_upper_bound`。

### 2.3 失效传播的五级

```
L0 单元指纹变（源码/契约指纹）           → dirty
L1 观测用例失效（case.target ∈ 受影响集） → 只重跑 stale 的 case
L2 Agent 结论失效                        → stale
L3 沿入边上行（强度由 stability 定，深度上限 3）
L4 门禁：stale 的重跑 → 与基线 diff → 回归即红
```

---

## 3. 语义地图与幻觉治理（模型怎么用、怎么不被它带偏）

### 3.1 模型只做三件事

> **模型只做「选模板 + 填槽位」，不写结论、不写断言结构、不写期望值。**
> 断言的形状来自**封闭的原语目录**；期望值要么来自既有声明，要么由人工在晋升时冻结。
> **判定永远由确定性层做，且一旦晋升就与模型脱钩。**

### 3.2 职责 → 可验证断言的编译链

```
模型产出（只允许这些字段）：
  unit_id, template_id, polarity, slots, when, statement, why, basis, confidence
        │
        ▼  compiler.py（T1–T5，纯函数、零模型、零 IO 副作用）
  T1 定位：slots 的每个符号必须能在符号表解析到 → 解析不到 = dropped(unknown_symbol)
  T2 定型：kind 与 expect.type 由模板固定，模型不可改
  T3 量化：given 由编译器从 slots 构造（bound*10+1 / bound-1 / 0 / -1 / null）
           → **模型永远不写数字**
  T4 绑定：executor / carrier / repro_cmd 按目录查表
  T5 校验：schema + 符号存在 + executor 可达 + 载体层一致 → 任一不过 = uncompilable
        │
        ▼  executor.py（路由到**既有**确定性 oracle）
  assertion_results.json
        ├── PASS → shadow（advisory，不进门禁）
        ├── FAIL → clue（待人工标注，**不开单不判红**）
        └── NA / dropped（带分类原因）
```

**关键**：编译出的 `given.args` 直接复用 `fn_dynamic.py` 已有的 `$t` 类型化参数 DSL，
所以编译产物**原样就是** `build_cases()` 的 case 格式，零新增执行器语义。

### 3.3 幻觉的具体形态与防线

| 形态 | 防线 |
|---|---|
| 把方法名当职责 | `slots` 必须在符号表存在；`relabel` 变异算子专测「结论是不是从名字猜的」 |
| 把注释当契约 | 断言的 `expect` 必须落在一个封闭原语上，注释无法产生原语 |
| 编造不存在的业务规则 | 规则卡（模板的 `why_template`）显式写死业务前提；人工审批时对照检查 |
| **极性翻转**（"不得"写成"必须"） | 最危险：能编译、能执行、能 FAIL，但方向反了，人工最容易放过。<br>对策：极性是枚举字段 + **互斥极性对自检**（正反两个极性不可能同时 PASS）+ 变异算子专测极性 |
| 把局部实现当接口契约 | 断言的 `scope` 字段 + 载体层一致性校验（T5） |
| 把异常分支当正常路径 | `when` 条件是承重字段，编译期校验 |

### 3.4 双轨：一条流水线两个准入等级

| 产出 | 轨道 | 可进门禁基线 | 判定者 | 失败后果 |
|---|---|---|---|---|
| 五层 findings（现有 110 条） | D | 是 | 确定性脚本 | **RED** |
| 对账 / 攻击链 | D | 是 | 确定性脚本 | **RED** |
| 模型给的断言草案 | M | 否 | 编译器 | 丢弃（带原因） |
| 模型断言的执行结果 | M | 否，只 advisory | 确定性 executor | **黄**，不改退出码 |
| **promoted**（人工冻结的断言） | D | 是 | 确定性 executor | **RED** |

**支点**：模型参与基线的**扩充**，不参与基线的**判定**。

### 3.5 可复现性：三段差分 triage

| 指纹 | 覆盖 | 变了意味着 |
|---|---|---|
| `code_hash` | 受影响代码单元的内容 | 代码变了 |
| `assert_hash` | promoted 断言集合的签名 | 资产变了（需重批） |
| `model_hash` | 本次全部模型输出的聚合签名 | 模型输出变了 |

判定表：`code_regression` / `environment_drift` / `asset_change` / `model_drift` / `clean`。
`model_hash` 连续变化而 `code_hash` 不变 → **模型漂移**独立通道，报黄。

**本环境的约束**：模型通道是 subagent，拿不到 temperature/seed。
→ 可复现性靠**内容哈希缓存**：同 `cache_key` 的第二次调用**必须命中缓存、禁止重采样**。
账本降级为 `(brief_hash, model_id, response_hash)` 三元组。

### 3.6 递归委派与 token 预算

- **分组硬约束**：① 类边界是硬边界（否则「内部调用」语义在切分处丢失）；
  ② 拓扑序 = 处理顺序；③ 预算耗尽必须**显式标记**（`budget_exhausted`），
  「没查」和「查了没问题」在报表上不能一样。
- **预算**：`layers.yaml` 已写 `max_tokens_per_round: 2000000`（此前完全没用上）。
  层间按 √entity_count 分配，层内按 cost 比例，硬闸：超预算即停，剩余标 `NOT_COVERED`。
- **向上聚合 = 结构化 union**，不是自然语言拼接。三条守恒自检写成 pytest：
  响应数守恒、丢弃数守恒、跨组边守恒。

### 3.7 上下文变异测试

| 算子 | 变异 | 测什么 |
|---|---|---|
| `relabel` | 类名/方法名换成匿名标签 | 结论是从代码读的还是从命名猜的 |
| `reorder` | 打乱函数与 upstream 顺序 | 位置偏置 |
| `prune` | 删掉一块上下文 | 依赖哪块上下文 |
| `perturb` | 改一个结构特征数值 | 是否被结构特征锚定 |

脆弱四型：`robust` / `fragile_existence` / `fragile_binding` / `fragile_polarity`。
**`fragile_polarity` 直接销毁不留影子**（极性翻转会直接改变缺陷判定方向）。
`prune`/`perturb` 只降级为「需人工看」，不自动销毁（信息削减型变异，结论变不必然是脆弱）。

---

## 4. 涌现：从关系网自动生成，而非手挑

### 4.1 操作性定义（判定式的地基）

一个链 `c` 的缺陷集 `D`，观察谓词 `F(s)` = 以注入集合 s 执行链的步骤后，目标不变量是否被违反。

```
EMERGENT(c) ⟺ F(D)=1  且  ∀d∈D: F({d})=0
```

「A 和 B 各有缺陷但互不相干」被明确排除为 `NOT_EMERGENT_INDEPENDENT`
（每个单注都能自己触发，且违反的是不相交的不变量集合）。

### 4.2 判定式：七种裁决

| verdict | 条件 | owner | category |
|---|---|---|---|
| `EMERGENT_CONJUNCTIVE` | 合起来错、单独都不错 | integration (own) | `end_to_end_business` |
| `EMERGENT_CONJUNCTIVE_CORE` | leave-one-out 求出最小核心 | integration (own) | `end_to_end_business` |
| `EMERGENT_AMPLIFIED` | 合起来错，且错得更狠（量级 ≥1.5×） | integration (own) | `cross_service_performance` |
| `EMERGENT_MASKING` | B 把 A 的症状掩盖了 | **掩盖者的层** (own) + integration (cited) | 需新增 `integration.emergent_masking` |
| `NOT_EMERGENT_INDEPENDENT` | 各自都错，违反的不变量不相交 | 各低层 (own)，集成层**不开单** | — |
| `NOT_EMERGENT_ADDITIVE` | 相交但无倍数效应 | 已有根因 (cited) | — |
| `FALSIFIED_CHAIN` | 假设不成立 | 不开单 | — |

**工程前提**：注入必须是**请求/数据层面的条件注入**，不是代码变异（被测系统只读）。
注入目录条目形态：字段极值 / 请求序列（重复提交、先改后读、并发）/ 身份切换（匿名 vs 主人两个 session）。

**产品口径待定**：现有 5 条手写链里，多数是「单缺陷 + 后果放大」，按严格定义不算涌现
（NO_AUTHZ 单注即可改写快照）。如果产品上要求「攻击链一律算涌现」，
`NOT_EMERGENT_*` 的口径要重新定义 —— 这是**产品口径问题，不是算法问题**。

### 4.3 从图生成候选链

不做通用路径枚举（类图只有 68 条内部边，全枚举是玩具）。用 **7 个模式模板**做有界匹配，深度 ≤4，
每条链必须**锚定一条已声明不变量**（没锚点的链不开单 —— 那属普通单点缺陷）：

```
T1 GUARDLESS-WRITE      入口 → svc → repo(save)          约束: 某 source 字段无 guard 覆盖
T2 DERIVED-SNAPSHOT     入口 → svc → repo(read A) → svc → repo(save B)   断言: field_changed
T3 CROSS-ENTITY-TRUST   入口 → svc → repo(read A) → svc → repo(save B)   断言: 跨表一致性
T4 NO-AUTHZ-CHAIN       无鉴权入口 → ... → table          断言: 跨主体读写可达成
T5 STATE-MACHINE        状态写汇 → 枚举 valueOf           断言: 非法跃迁可达成
T6 CASCADE-FK           table_A →(FK) table_B → 入口       断言: B 行被连带改写
T7 AMPLIFY-LOOP         高分支 svc → repo(read)           断言: 查询放大
```

剪枝 8 条硬约束（链首必须是 HTTP_ENTRY、深度 ≤4、必须含跨边界边、必须锚定不变式、
每个写汇最多展开 3 条路径、三元组去重、只保留含写汇的路径、全局候选上限 500）。

**「图上路径 → 可执行步骤」的桥接靠三张已有表**（不需要新数据源）：
入口绑定表（Controller↔OpenAPI，已 25/25 对齐）、字段-表映射表（DDL+jpa 已有）、
不变量锚表（invariants.json 已有）+ 一个 **schema 驱动的造数器**
（必填字段全覆盖，取值规则固定：`ai_probe_` 前缀 / 极值池 / 固定时间模板）。

### 4.4 枢纽度四维合成 + Monte-Carlo 级联估计

```
b_v 加权 betweenness   r_v PageRank   w_v 写放大(可达表数)   t_v 信任边界穿越数
hub(v) = 0.35·b + 0.25·r + 0.25·w + 0.15·t
score(c) = 2.0·blast + 1.5·hub + 2.0·inv + 1.0·prec + 1.0·gap − 0.8·cost − 1.5·red
```

**MC 级联估计**：在图上按枢纽加权采样故障节点，BFS 传播（CALLS 0.5 / READS 0.3 / WRITES 1.0），
2000 次采样估计该链的可检出性与 blast radius。**纯静态模拟，不碰被测系统** —— 排序用，不判定用。

**防塌缩四机制**（缺一条就只会跑最中心的几个）：
集中度上限 40% / 三组配额（每入口 ≤4、每表 ≤8、每不变量 ≤6、每模式 ≥1）/
70% 利用 + 30% 探索双预算 / **「没跑的链」全部记录**（门禁比对集合哈希，不是「跑了几条」）。

### 4.5 FDR：分母三档，必须同时报

```
K_inj  = 注入目录中 active=true 的条目    → FDR_inj      = 检出且归属正确 / 总数  （主指标）
K_root = 已有根因中 ground_truth=verified → Recall_root  = 被任一层命中 / 总数
K_reg  = 7 条 xfail 回归链对应的根因      → Recall_emerg = 被涌现子系统命中 / 总数
```

**归属错了不算检出** —— 本项目的核心承诺是「可归属」，归错层等于没检出来。
门禁门槛：`FDR_inj ≥ 0.85`、`Attr_acc ≥ 0.95`、某类别注入 ≥3 且检出 0 → 红、
`denominator_hash` 变 → 红（防「把检不出来的注入删掉来提高 FDR」）。

### 4.6 跨仓库涌现

跨仓库图**不是把两张单仓库图拼起来** —— 必须新增节点类型 `AGENT_ENDPOINT`，并引入
**面清单（surface manifest）**：每个仓库声明自己的 HTTP 端点、拥有的表、发布/消费的事件、Agent 工具。

跨仓库边分四类（HTTP_CALL / DB_SHARED / EVENT / AGENT_CALL），可信性分三级：
`CONFIRMED`（运行时观测到）/ `DECLARED`（双边声明一致但未观测）/ `DRIFT`（单边声明 → 产根因不产链）。
**只有 CONFIRMED 边参与链生成。**

跨仓库 owner 新规则：**owner = 最早违反契约的那一跳**（因果序最靠前），受害方只被 cited。

⚠️ 现实约束：跨语言跨仓库需要该语言的声明抽取器（Java 的 `javap` 管线不能直接用于 Python 仓）。
**没有真实第二仓之前，先用「双仓夹具」验证算法**，不要一上来就接真仓。

---

## 5. 增量与调度

### 5.1 两个必须先修的既有缺陷（本轮已修）

| 缺陷 | 位置 | 后果 | 状态 |
|---|---|---|---|
| `unit.file` 全空 | `build_agent_graph.py` 给全部实例写死 `"file": ""` | `change_impact` 的 class→file 链路是死的，只能靠类名猜 | ✅ 已修（按类名从文件单元反查） |
| 门禁把「本次没这一项」判成 GREEN | `integration_gate.py` 的 `row_gone` | 增量跑只产出部分对账行时，比对集缩小 → **真回归被吞** | ✅ 已修（改判 INCOMPLETE，退出码 2，且拒绝 `--accept`） |

### 5.2 影响集：四档分开 + 强度衰减

```
direct       方法本身被改
same_file    同文件其他方法 —— 只影响文件层，不影响函数层其他用例
propagated   沿反向边传播到（附 hop + strength）
watch        强度不足，仅登记不重跑（可审计，不是静默截断）
```

强度公式（全是确定性量，可回归）：

```
w(edge)    = kind_w[kind] × stability_w[stability]
kind_w      = {call: 1.0, type_use: 0.5, http: 0.8, runtime: 0.8, file_use: 0.6}
stability_w = {signature: 1.0, behavior: 0.8, semantic: 0.5}
strength(u) = 1.0 × Π w(edge_i) × 0.5^(h-1)      τ_rerun = 0.2
```

代进去：**硬调用链有效半径 3 跳，软类型边 1 跳**。现有实现的平铺 depth=3
会把 `type_use` 三跳外的单元也标上 —— 这是当前 79/532 的主要来源。

**播种下沉到方法级**：`javap -l` 的 `LineNumberTable` 已验证能把 diff hunk 行区间
反查到具体方法（hunk `[150,156] → resolveFinalPrice`），流水线自己的编译产物就带行号表，
**不需要新工具**。无行号表 → 退化为类级种子 → 该类及上游全量重跑（不静默降级）。

**非 Java 改动**（`application.yml` / `docker-compose.yml`）不进方法级影响集，
单独触发 `config_domain_changed` → 服务层 + 配置探针全量重跑。
否则配置结论会静默过期。

### 5.3 增量单元是「分片」不是「Agent 实例」

一个分片 = 一个可独立执行、独立判定、独立缓存的靶区（一个用例 / 一个场景 / 一个 operation / 一个实体 / 一条链）。

**判定层永远全量跑，探测层才做增量** —— 这是第一原则：
`fn_diff` / `contract_diff` / `normalize_findings` / `aggregate_findings` / `integration_gate`
都是**纯函数**（输入 = 合并后的产物），秒级，全量跑反而最安全。
只有**有副作用、昂贵、会产生证据**的探测层做增量。

每个层脚本加的参数是**该层自己的靶区 id**（`--case` / `--scenario` / `--operation` / `--probe` / `--entity`），
**不是通用 `--scope`** —— 通用参数会让 5 个层的语义打架。靶区映射表放**项目侧**
`pilot/agent_targets.yaml`（引擎不许写死脚本名）。

### 5.4 产物管理：分片 + 索引 + **并集合并**

产物从「单一扁平 JSON 全量覆盖」改为「分片 + 索引 + 并集合并」。每个分片自带
`input_fingerprint`（能不能复用）与 `produced_by`（哪次跑、哪个 commit 产出）。

**合并规则只有一条：并集，永不缩集。** 上次全量跑产出的每个 shard_id，这次必须仍在。
缺一个 → **退出码 2（没查完）**，绝不 0。这条守住门禁的红/绿语义。

指纹分两层：
- `code_fingerprint` = 分片定义 ⊕ 相关方法的 body_hash（已在 file_declaration.json）⊕ 工具源码 hash ⊕ 工具版本号
- `env_fingerprint` = DB schema hash ⊕ 容器 image digest ⊕ 种子数据 hash ⊕ 依赖 jar 列表 hash
  **取不到 ⇒ 该分片不可复用**（宁可重跑）—— 这是防「代码没变但环境变了导致结论过期」的唯一办法

永不重算：静态声明类（~25s）+ 所有 diff/收口/门禁类。
永不缓存：时间敏感探针（`perf_load` / `concurrency_props` / `scale_experiment`）。

### 5.5 调度器：两段式 + 四条件合取 + 四条兜底

**跳过判据是四条件的合取**，任一不满足就重跑：
① 不在强制重跑集内；② 指纹与当前重算值一致；③ TTL 未过期；④ 非「永远重跑」项。

**兜底四条**（防漏）：定期全量（每 N=10 次）；影响半径超阈值（受影响单元 > 40%）回退全量；
`unresolved_seeds` 非空 → 该类及上游全量；`config_domain_changed` → 服务层 + 配置探针全量。

**双跑一致性校验**：增量跑完后，对**不在影响集内**的分片与上一次全量产物**逐字节比对**；
不一致 → 退出码 2 并报 divergence。这是「跳过的 Agent 结论确实没变」的**可执行断言**。

### 5.6 token / 成本预算（契约里写了但从未被消费）

`layers.yaml` 的 `max_agents: 12` / `max_tokens_per_round: 2000000` /
`diversity_source: bound_oracle` 至今**零消费**。

- **`max_tokens_per_round` 只约束将来的模型轨**；确定性轨不受 token 预算约束，只受 `max_agents` 约束。
  这条边界必须写死在契约里，否则预算一开就会把确定性工作也卡掉。
- **`max_agents: 12` 的语义需要拍板**：按「每轮最多 12 个**需重新判定**（未被缓存复用）的
  agent 实例进入执行队列」设计，与 `agents.total: 9` 相容；若是「整个审计最多 12 个实例」，
  则分片模型要换成「12 个粗粒度角色 × 533 单元」的矩阵形态 —— **工作量差一个量级**。
- **预算耗尽 ≠ 跳过**：被挤掉的进 `deferred` 队列，报告显式列出，覆盖度矩阵显示 `deferred` 数量。
- admit 顺序必须**确定性**：先按 oracle 多样性各取一个最高优先任务，再按 priority 填满。
  **不得引入任何随机性或模型打分**（D2/D9）。

---

## 6. Java / Spring 特性支持矩阵

### 6.1 关键判断：原先认为「结构性不可判定」的三类，运行时都能判

设计 Agent 用**只改 `JAVA_TOOL_OPTIONS` 的容器探针**实测验证：

| 手段 | 拿到什么 |
|---|---|
| `-Ddebug=true` | **CONDITIONS EVALUATION REPORT**：逐条自动配置「matched / Did not match + 原因」+ active profiles |
| `-Dlogging.level...DefaultListableBeanFactory=DEBUG` | **327 个 bean 的创建过程** + `Autowiring by type from bean name 'X' to bean named 'Y'` → **装配的是哪个实现** |
| `-Dlogging.level...transaction.interceptor=TRACE` | **逐方法** `Getting transaction for [X.y]` → 事务是否真生效 |
| `-Dlogging.level...JpaTransactionManager=DEBUG` | 事务边界 / 提交 / 回滚 |
| `APP_FILE_UPLOAD_DIR` 环境变量 | 宽松绑定实测：`APP_` 形式**旁路**了 `FILE_UPLOAD_DIR` 的占位符链 |

所以：**@Primary/@Qualifier 选了谁、@Profile/@Conditional 生效没有、切面到底织没织入**
—— 全部可以在运行时判定，不该留空。

### 6.2 本项目实际用到的 Java 特性（扫真实代码得出）

| 类别 | 用了 | 没用的（标 not_applicable，不编造风险） |
|---|---|---|
| 语言 | record(22)、lambda/方法引用(密集)、泛型(擦除后调用图漏边)、文本块(1)、try-with-resources(3)、异常链(被截断)、ThreadLocal(有清理)、实体无 equals/hashCode(8/8) | sealed、JPMS、BigDecimal(金额全用 Long 分，无浮点误差)、原生 Serializable、正则(ReDoS 面为零)、synchronized/Lock/Atomic/CompletableFuture/Executor/虚拟线程(全零) |
| Spring | IoC(全构造器注入)、@Value(4)、@Transactional(24)、JPA Repository 代理、懒加载(OSIV 关闭)、Specification(3)、@JsonInclude(1) | @Async、@Cacheable、@Scheduled、@EventListener、方法安全(且无 security 依赖)、@Modifying、@Version(8 个实体全无 → 无乐观锁)、@Retryable/CircuitBreaker、RestTemplate/WebClient、@ConfigurationProperties、Validation 分组/自定义 validator、Actuator(缺) |

**注意「零命中」**：@Async / 缓存 / 定时 / 消息队列这些面在本项目**根本不存在** ——
不是「没查」，是「没有」。这直接决定补齐顺序（见 §6.4）。

### 6.3 扫出来的真缺陷（此前没有）

| 缺陷 | 位置 | 性质 |
|---|---|---|
| **自调用绕过 Spring 代理** | `ProductService` 5 处 + `PackageService` 5 处调用 public `@Transactional(readOnly=true)` 的 `findProduct`/`findPackage` | 代理绕过 → readOnly 语义失效。当前后果有限（外层已是写事务），但形态典型 |
| **异常链被截断** | `BusinessException` 构造器只收 message，`OrderService:262` / `UploadService:68` catch 后重抛**不带 cause** | 根因（IOException 的 errno、IAE 来源）在生产日志里不可见 |
| **实体无值相等** | 8 个实体全不重写 equals/hashCode | DTO 是 record（自动 equals）、实体是身份语义 → 混用会出静默 bug |
| **时区基准不一致** | `BaseEntity:20,27` 用 `OffsetDateTime.now()`（无 ZoneId），而业务时间全用 Asia/Shanghai | 容器 TZ 不是 Asia/Shanghai 时 `created_at` 与 `reservation_time` 落在不同基准 |
| **LIKE 通配符未转义** | `OrderService:296` / `ProductService:135` / `PackageService:171` 的 `cb.like(...)` 无 escape | `keyword="%"` → 全表匹配 |
| **优雅停机未开** | `application.yml` 无 `server.shutdown: graceful` | 部署时在途事务被 SIGTERM 强杀 |
| **死分支** | 无 controller 用 `@Validated` → `GlobalExceptionHandler:48` 的 `ConstraintViolationException` 不可达 | page/pageSize 无下界校验 |
| **死代码** | `PackageItemRepository:21,23` 两个方法全项目零调用，且派生 `deleteBy` 无 `@Transactional` | 一旦有人调用就炸 |
| **N+1 只修了一半** | `ProductService:131` 用 `root.fetch("category")` 修了商品列表，**套餐列表没修** | `mapper/PackageMapper:18,40` 每包 1+N+M 条 SQL |
| **宽绑定优先级反直觉** | `APP_FILE_UPLOAD_DIR` 旁路 `FILE_UPLOAD_DIR` | 实测确认。这类优先级事故静态永远看不出来 |

### 6.4 补齐顺序（成本单位：人日）

| # | 项 | 收益 | 成本 | 需模型 | 需改 jiuyan |
|---|---|---|---|---|---|
| 1 | 修 `java_feature_scan.py` 自调用检测（不要求 `this.` 前缀 + 方法名二次过滤） | 补上 **11 处漏检** | 0.5 | 否 | 否 |
| 2 | `transaction_probe.py`（TRACE 日志 → 逐方法事务生效性，与静态 24 处**差分**） | 整套审计最大的语义盲区；`aop_aspect` 特性从零覆盖变有证据 | 1.5 | 否 | 否 |
| 3 | `spring_ctx_probe.py`（bean 图 + 装配解析） | `dependency_injection` 从零覆盖变 327 bean 全可见 | 1.5 | 否 | 否 |
| 4 | `property_binding_probe.py`（属性溯源 + 宽松绑定矩阵 + CONDITIONS REPORT） | 用户点名的「Spring Boot 动态注入」落点 | 2 | 否 | 否 |
| 5 | `java_lang_scan.py`（语言特性总索引） | 一次性覆盖 16 类语言特性的静态面 | 3 | 否 | 否 |
| 9 | **特性规则库 + 自动差分引擎**（`feature_rules.yaml` + `engine/feature_inventory.py` + `engine/feature_diff.py`） | 把「特性覆盖」从人工维护变成自动差分；新特性**零引擎改动** | 3 | 否 | 否 |

前 5 项 8.5 人日，覆盖 6 条特性中的 3 条（`aop_aspect` / `dependency_injection` / `dynamic_config`）从零覆盖变有运行时证据。

### 6.5 特性规则库：让「新增特性不用改引擎」

把「特性覆盖」变成和现有五层一样的「静态声明 → 动态实测 → 差分出单」问题，
天然复用已有的 `findings_schema` / `normalize_findings` / `aggregate_findings`：

```yaml
# pilot/feature_rules.yaml（项目侧，引擎不碰）
features:
  - id: aop_aspect
    label: AOP 切面（代理织入）
    detect:
      - {kind: annotation, names: [Transactional, Async, Cacheable, ...]}
    probes:
      - id: tx_effective
        script: transaction_probe.py
        mode: container
        argv: ["--probe", "tx_effective"]
        extracts:
          - {type: log_regex, pattern: "Getting transaction for \[(?P<target>\S+)\.(?P<method>\w+)\]"}
    coverage:
      - {when: not_detected, status: not_applicable}
      - {when: detected_and_probe_pass, status: covered}
      - {when: detected_and_probe_fail, status: gap_runtime_missed,
         finding: JAVA-FEATURE-DECLARED-NOT-EFFECTIVE}
    carriers:
      blind_in: [function.jvm_reflection, file.jvm_whole_class]
      seen_in: [service.http_contract]
```

**新特性怎么进来**：改规则库一个文件 → **引擎零改动**。
**差分怎么出单**：`declared - effective`（声明了没生效，HIGH）、
`effective - declared`（生效了没识别 → 规则库漏了）。
**探针失效怎么区分**：0 抽取 → 该特性状态置 `unknown`（**不是 covered**），
并进「人工确认义务」；门禁三态退出码 0 绿 / 1 红 / **2 没查完**。

### 6.6 探针工程纪律（Agent 5 实测踩出来的）

| 纪律 | 原因 |
|---|---|
| 探针**不得依赖单一日志字符串** | 实测 `TransactionInterceptor` 的 "Getting transaction for" 是 **TRACE 不是 DEBUG**（先用 DEBUG 试，0 命中）；`JpaTransactionManager` 的 "Opening JPA EntityManager" 在 6.1.16 **根本不存在**（只有 Closing） |
| 探针**自检**：0 抽取 → 报「探针失效（版本漂移）」，**绝不**报「特性未覆盖」 | 两者混淆 = 门禁假绿 |
| **单容器多探针**：一次克隆开全部日志级别，跑完一次解析 | 单次克隆起停 ~30s，20 条特性各起一容器 = 10 分钟起 |
| 否定结论（"没覆盖"）必须**双证据** | 运行时日志 + 行为观测交叉验证 |
| 探针**只走不落库的端点** | 实测 `PUT /api/orders/999999` 会先反序列化再查库 → 未知字段探测用它，404 = 没写入 |
| 残留守卫接进流水线收尾 | 探针起过容器、写过请求，必须查 `docker ps` 无残留 + DB 无 `ai_probe_%` + 上传目录无非种子文件 |

---

## 7. 六个设计 Agent 的交叉结论与路线

| 来源 | 主题 | 核心结论 | 落地 |
|---|---|---|---|
| Agent 1 | Agent 群架构 | unit/role/instance 三级；Agent = 确定性执行体 + 作用域参数化 | 🟡 **部分采纳**：三级分离保留；**物化 2071 个实例被砍**（95% 占位符，8.4 MB → `units.json` 398 KB）→ `engine/agent_schema.py`、`build_agent_graph.py` |
| Agent 2 | 涌现检测 | （见 §4） | ⏳ |
| Agent 3 | 增量与调度 | （见 §5）；它指出的两个既有缺陷是真的 | 🟡 影响分析 ✅ / 调度 ⏳；**两个 bug 已修**（`unit.file` 全空、`row_gone` 判绿吞回归） |
| Agent 4 | 语义地图与双轨 | 职责 = 可执行断言；双轨两个准入等级 | ⏳（设计已定） |
| Agent 5 | Java 特性矩阵 | （见 §6）；它指出我的自调用检测有真盲区 | 🟡 **已采纳并修**：自调用漏检 11 处 → 8 处命中；事务生效性探针已做 |
| Agent 6 | 幻觉治理 | 模型只选模板填槽位；极性翻转最危险；triage 三段差分 | 🏗️ 变异测试已落地（极性/绑定脆弱性已有可测机制） |
| Agent 6 | 幻觉治理 | 模型只选模板填槽位；极性翻转是最危险幻觉；triage 三段差分 | 🏗️ 已落地：变异测试 `mutation_test.py` |

### 已落地的验证结果

| 增量 | 结果 |
|---|---|
| Agent 群骨架 | 533 单元 / 21 角色 / **2071 实例** / 786 边，契约违规 0 |
| 变更影响 | 改 4 个 Java 文件 → 79/533 单元受影响（14.85%）→ 精确命中 3 条相关根因 |
| 盲区地图 | 532 单元里**已判定 5（0.94%）**，完全没查 515；Top 优先级 = 中心性 × 逻辑密度 |
| 变异测试 | 3 个变异杀死 2 个；存活 RFP-06 暴露「断言只查结果不查该抛还是该返回」 |

---

## 8. 待补章节的占位与下一步

- §4 涌现检测：等 Agent 2 → 落 `engine/detect_emergence.py`（写者集合 → 共现剪枝 →
  局部自洽 → 组合验证 → 链生成 → 枢纽排序 → FDR）
- §5 增量调度：等 Agent 3 → 各层脚本加 `--scope`，`run_pipeline.sh` 按影响集调度
- §6 Java 特性矩阵：等 Agent 5 → 特性规则库 + 支持矩阵
