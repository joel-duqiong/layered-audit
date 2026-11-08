# layered-audit —— 分层多 Agent 缺陷审计系统

> **这个文件是给「下一个接手的人 / AI」看的。** 先读它，再读 `HANDOFF.md`（细节）、
> `EMERGENCE.md`（外部工作引用与映射）、`layers.yaml`（层契约，单一事实来源）。
>
> 目标、愿景、进度、约束全在这里。**不要靠猜。**

---

## 1. 我们要解决什么问题（目标，不可模糊）

> **一句话**：让一个代码仓库「自己走进来」，白盒地、自动地判定它有没有问题 ——
> 业务功能实现对不对、异常场景撑不撑得住、性能有没有崩、安全有没有洞 ——
> 并且判定结果**可复现、可归属、可回归**。

三个不可省略的性质：

| 性质 | 含义 | 为什么是硬要求 |
|---|---|---|
| **可复现** | 同输入同输出，改完能知道有没有回归 | 不可复现的结论没法当门禁用 |
| **可归属** | 每条缺陷明确归一层，高层只引用不重复开单 | 多人/多 Agent 并行才不会打架 |
| **可回归** | 缺陷固化成基线 + 用例，修好会红 | 否则修完还会犯 |

## 2. 目标形态（用户原话的结构化表达）

```
一个代码仓库 = 一群 Agent
 ├─ 粒度：函数 / 类 / 文件 / 对象 / 服务 —— 每个都是「一个单元细胞」，各有使命
 ├─ 每个 Agent 两条命：
 │    静态命 —— 读代码知道自己该干什么（职责、契约、边界、依赖）
 │    动态命 —— 真被调起来跑（真实调用、真实副作用、真实异常）
 │    判定   —— oracle 绑定 + 静态/动态差分出单
 ├─ 关系网：函数级 / 类级 / 服务级 / 跨仓库级的调用与依赖
 ├─ 改动传播：改一处 → 对应 Agent 的性质变 → 沿关系网传播 → 只更新受影响的 Agent
 ├─ 涌现：个体都「对」，合起来错 —— 要能系统化发现这种错
 ├─ 跨仓库：两个仓库两群 Agent，互相调用（像服务间调用），各自对外提供接口
 └─ 交付：白盒自动判定 + 帮架构师在多仓库间判断「这个功能该改哪个仓库」
```

**「涌现」是这套系统的存在理由**：单点测试全过 ≠ 系统是对的。外部研究同此结论
（单 Agent 的评估不能外推为群体的评估，见 `EMERGENCE.md` §1.1）。

## 3. 当前进度（截至 2026-11-08）

### 已建成并跑通的

| 块 | 状态 | 位置 |
|---|---|---|
| 五层契约（function/file/class/service/integration）+ 归属规则 + 证据 schema | 完成 | `layers.yaml` |
| 五层「静态声明 → 动态实测 → 差分出单」 | 完成 | `pilot/`（30+ 脚本） |
| 跨层收口引擎（schema 校验 / 归一化 / 根因归并 / 覆盖度矩阵 / 特性覆盖 / 不变式原语 / 涌现候选链 / FDR 门禁 / 关系网 / 跨仓库面） | 完成，**零项目字面量** | `pilot/engine/`（19 文件，纯净度守卫实测通过） |
| 项目绑定 + 人工标注表 | 完成 | `pilot/target.json`、`pilot/finding_meta.py` |
| 常驻门禁（基线比对，回归即红） | 完成 | `pilot/integration_gate.py` |
| 回归资产（pytest：7 闭合不变量真绿 + 7 攻击链 xfail strict） | 完成 | `/Users/june/tests/auto-api` |
| 负载实测 / 并发 / 配置失败模式 / ORM 查询放大 / Java 特性扫描 / 业务不变式 | 完成 | `pilot/*_probe.py` |

### 本轮新增（2026-11-07 晚那一轮 + 2026-11-08 这一轮）

| 块 | 状态 | 位置 |
|---|---|---|
| **Agent 群骨架** | ✅ unit / role / instance 三级分离，533 单元 / 21 角色 / 786 条边，契约违规 0 | `pilot/engine/agent_schema.py`、`build_agent_graph.py` → `units.json` / `edges.json` |
| **执行预算闸门** | ✅ `max_agents: 12` 第一次被真正消费（分轮 admit + oracle 多样性保底 + 产物并集合并） | `pilot/engine/budget.py`、`scheduler.py` → `schedule_report.json` |
| **函数层载体通用化** | ✅ **通用反射 harness**：目标类、依赖、打桩、用例、类型别名全部来自用例文件，harness 里零项目字面量。jiuyan 92 条用例判定 **0 条不同**（64/28 与原版一致）；demo-shop 6 条全过 | `pilot/fn_harness/GenericHarness.java`、`pilot/fn_cases.json`、`demos/shop/fn_cases.json` |
| **文件层载体通用化** | ✅ **通用反射 harness + 通用容器探针**：目标类、依赖、打桩、样例、期望全部来自用例文件，harness 里零项目字面量；6 个通用检查器（覆写后置条件 / 条件装配 / 实体值相等 / 缓存值形状 / 封装泄漏 / 跨仓库 HTTP）。jiuyan 22 用例 / 58 判定、18 条 finding **结论级 0 差异**；demo-shop 9 用例 / 25 判定全过、埋的 6 类缺陷命中 5 类（另挖出 2 个真缺陷）| `pilot/file_harness/GenericFileHarness*.java`、`pilot/file_http_probe.py`、`pilot/file_cases.json`、`demos/shop/file_cases.json`、`pilot/FILE_GENERIC_RESULT.md` |
| **涌现裁决器** | ✅ 七种裁决落成可执行引擎。**demo-shop 真跑判出 `EMERGENT_CONJUNCTIVE`**：F(∅)=0、F({A})=0、F({B})=0、F({A,B})=1，偏离 90000 分；35 项自证断言全过 | `pilot/engine/emergence.py`、`demos/shop/EMERGENCE_RESULT.md` |
| **分片存储 + 增量重跑** | ✅ 「改一处只动受影响的那几个」的第一个可用增量：第 2 次跑 **0 重跑 / 92 复用 / 1.5 秒**；删 1 个分片只重跑 1 条；改工具版本全失效 | `pilot/engine/store.py`、`pilot/fn_dynamic.py --incremental` |
| **服务层载体通用化** | ✅ 场景配置搬到 `service_scenarios.json`；**顺带挖出原版一个真 bug**：解析 `{id}` 时用复数资源名查单数键 → 24 条请求打的是 `/api/xxx/None` → 所有带路径参数的接口「合法请求」场景**从来没真正测过**。通用版修好后，那 20 条假缺陷消失 | `pilot/contract_probe.py`、`pilot/service_scenarios.json` |
| **变更影响分析** | ✅ git diff → 类名匹配 → 沿入边传播（深度 3）→ 受影响 Agent 集合；区分「解析出的」与「保守上界」 | `pilot/engine/change_impact.py` → `change_impact.json` |
| **盲区地图** | ✅ 按中心性排序的「最该先验的单元」 | `pilot/engine/agent_coverage.py` → `agent_coverage.json` |
| **变异测试** | ✅ `ground_truth=mutant_survivor` 从没人实现过 → 现在有真实数据 | `pilot/mutation_test.py` → `mutation_findings.json` |
| **运行时保真度** | ✅ 每条结论标 container / hybrid / stubbed | `target.json` 的 `carriers.fidelity` |
| **可执行依赖图** | ✅ 151 节点 / 414 边；25 个 HTTP 入口**全部**绑定到 openapi（0 未绑定） | `pilot/emergence_graph.py` → `emergence_graph.json` |
| **切面生效性探针** | ✅ 静态声明的 24 个 `@Transactional` 与运行时实际生效的**差分为空** | `pilot/transaction_probe.py` → `transaction_findings.json` |
| **残留守卫** | ✅ 探针容器 / `ai_` 行 / 非种子上传必须全 0，否则流水线红 | `pilot/probe_residue_check.py` |
| **涌现裁决器** | ✅ 通用引擎（七裁决）+ demo-shop 真跑判出 `EMERGENT_CONJUNCTIVE`：单注都不触发、合起来违反（偏离 90000 分），两次跑指纹一致，35 项自证全过 | `pilot/engine/emergence.py`、`demos/shop/emergence_probe.py` → `demos/shop/artifacts/emergence_chains.json`、`demos/shop/EMERGENCE_RESULT.md` |
| **服务层通用化 + 业务不变式判定通用化** | ✅ ① `contract_diff.py` 的 4 处项目耦合清零（包前缀 / 证人端点 / base_url / 报告标题全部走 target.json 与 `service_scenarios.json`），jiuyan 的 10 条 finding **逐条一致**；② 业务不变式的「调用 → 读行 → 判定 → 开单」抽成通用引擎 `engine/business_verdict.py`，项目侧只留「造什么场景、读哪张表」；③ 顺带补上一个可复现性缺口：jiuyan 的流水线**原先根本没跑业务不变式探针**，它的产物是手工跑的 | `pilot/contract_diff.py`、`pilot/engine/business_verdict.py`、`pilot/business_probe.py`、`pilot/service_scenarios.json` |
| **demo-shop 拿到服务层 → 涌现门禁 RED 转 GREEN** | ✅ demo 补上业务不变式探针（3 个差分场景：普通客户+会员装配 / VIP 客户+标准装配 / **VIP 客户+会员装配**）—— 前两个是「单条件不违反」的对照组，第三个才违反，**证明这个缺陷需要两个条件同时成立**。归属：口径分裂归 **service**（own，差分证据）、VIP 拿不到会员价归 **class**（cite，不重复开单）。门禁 **FDR_inj 1.000 / Attr_acc 1.000 → GREEN**，demo 流水线 exit 0。**阳性对照**：抽掉服务层那两条 finding，门禁立刻变回 RED（FDR 0.500）—— 这个绿是挣来的 | `demos/shop/business_probe.py`、`demos/shop/invariants.json`、`demos/shop/finding_meta.py`、`demos/shop/run_pipeline.sh` |
| **函数层 / 文件层的差分判定层通用化** | ✅ 五层的差分判定层至此全部通用。函数层：8 条缺陷语义（严重度/根因函数/边界挡没挡住）从代码搬进项目配置 `fn_groups.json`；文件层：19 条 finding 的判定内容 + 四类证据取值机制搬进 `file_findings_config.json` + 通用引擎 `engine/{fn,file}_diff.py`（派生事实用 filter/select/slice/sum_len/group_by 等通用算子）。**等价性**：jiuyan 的 fn_findings 与 file_findings 的 JSON 与 Markdown **逐字节一致** | `pilot/engine/fn_diff.py`、`pilot/engine/file_diff.py`、`pilot/fn_groups.json`、`pilot/file_findings_config.json` |
| **demo-shop 的文件层接上 + 归属翻转到最低层** | ✅ demo 的文件层用例原先断言的是**现状**（把缺陷写成了期望），所以 24 条判定全 PASS、开不出单。改成断言**应有行为**后两条变 FAIL，demo 拿到了 2 条真实发现：额度口径不一致（file.contract_alignment）+ 跨仓库汇率兜底量纲错（file.error_handling_pattern）。**归属随之翻转**：`order.quota_checked_on_quoted_amount_ledger_on_settled` 由**文件层 own**（最低层能证明它）、服务层改为 **cite** —— 与 jiuyan 的先例一致。门禁原话：「在 file 层认领，层序最低、类别合法」 | `demos/shop/file_cases.json`、`demos/shop/file_findings_config.json`、`demos/shop/finding_meta.py`、`demos/shop/run_pipeline.sh` |
| **契约变更：文件层新增 `contract_alignment` 类别** | ✅ 「同一文件内两处对同一业务量的口径不一致」这个粒度只有文件层看得见（单看两个函数各自都对，类层又跨了两个类）—— 之前只能塞进 UNMAPPED 等人决策。现在给它正式的家。**顺带修掉一个潜伏 bug**：契约解析器是逐行正则，`categories` 块里出现注释行会让**后面的类别静默丢失** —— 已修在解析器里 | `layers.yaml`（file 层 + `agents.total_roles` 22→23）、`pilot/engine/findings_schema.py` |
| **覆盖率指标修正：项目最关键的那个数字之前是错的** | ✅ 两个原因都不是数据问题，是**指标没把已有结论算进去**：① 单元状态只看「有没有用例」，不看判定结果 —— 17 条全 PASS 的用例被记成「有观测但义务未定」，「满足」恒为 0；② **只有函数层的结论能关联到单元** —— 文件层/类层/服务层那 70 条结论一条都没被记（文件单元按路径后缀、类单元按文件名反查类名、服务单元按层归集）。修法：用例文件声明「每个分组验的是哪一类义务」，fn_dynamic 把类别盖到用例上，Agent 图构建器按判定结果定状态、按层归集结论。**已判定 6 → 34（1.13% → 6.39%）**，「有观测但义务未定」13 → 0，义务满足 0 → 22、违反 5 → 63。护栏：义务类别必须与单元所属层一致 | `pilot/fn_cases.json`、`pilot/fn_dynamic.py`、`pilot/engine/build_agent_graph.py`、`pilot/engine/agent_coverage.py` |
| **类层补齐：接入类形状通用探针 + 第二载体复核** | ✅ ① `class_shape` 通用探针（继承/覆写后置条件/值相等/封装泄漏/缓存可变，纯反射不碰 DB）接进 **jiuyan** —— 类层 16 → 23 条，**新开 7 条真实发现**（6 实体缺值相等 + 1 封装泄漏），类别与归属都正确；② **第二载体复核**：文件载体（字节码）的 6 个通用检查器原本检出的是**类层类别**、无人消费 —— 现在把它的观测挂到类层结论上（同一结论被两个独立载体复现），没人认领的观测写成**覆盖缺口**（`class_carrier_gap.json`），项目可声明「已知不适用」并留理由。jiuyan：10 观测 → 复现 7 + 声明不适用 3 + **真缺口 0**；demo：10 观测 → 复现 7 + **真缺口 3** | `pilot/class_shape_probe.py`、`pilot/file_carrier.py`、`pilot/class_rules.py`、`pilot/class_diff.py`、`pilot/class_carrier_gap.json` |
| **涌现维度接进流水线** | ✅ demo-shop 有了自己的流水线（类层 → 关系网 → 候选链 → 真跑裁决 → FDR 门禁 → 收口 → 残留守卫）；jiuyan 的流水线显式报告涌现维度（退出码 2 = 没查完，不是绿灯）。demo 流水线 exit 1 是**预期**：门禁把「根因只被引用、无人 own」的漏归属逼出来了 | `demos/shop/run_pipeline.sh`、`pilot/run_pipeline.sh` |
| **涌现候选链生成器 + FDR 门禁** | ✅ 候选链生成（关系网 + 不变式 → **锚定已声明不变式**的可执行候选链：八条剪枝硬约束 + 枢纽度四维合成 + Monte-Carlo 级联估计，固定种子 20261107）；FDR 门禁（分母/分子/分母哈希/集合哈希/分类别 FDR/漏检清单，退出码 0 绿 / 1 红 / 2 没查完）。**demo-shop 实测**：生成候选 **2 条**（T3 ×2），已知链召回 **1/1 条件覆盖 full**，候选真跑裁决仍 `EMERGENT_CONJUNCTIVE`（核心集 INJ-TIER-VIP+INJ-PROFILE-VIP），门禁第一轮 **FDR_inj=0.500 / Attr_acc=0.500**（分母 2 / 检出 2 / 归属正确 1），漏检 `INJ-PROFILE-VIP`（service 层根因只被引用、无人 own 开单）→ 判红退出码 1；删掉检不出来的注入 FDR 会变 1.0，但分母哈希漂移照样判红。**第二轮补上 demo 的服务层后：FDR_inj=1.000 / Attr_acc=1.000 → GREEN**（见上一行） | `pilot/engine/emergence_candidates.py`、`pilot/engine/emergence_gate.py`、`demos/shop/emergence_candidate_run.py`、`pilot/emergence_baseline.json`、`pilot/EMERGENCE_CANDIDATES_RESULT.md` |

### 规模数字（以 `pilot/root_causes.json` 为准）

- 发现 **100** 条 → 根因 **72** 条（归并 28 条）
- 10 个必填字段结构齐备 **100/100**；`owner` 标注 **100/100**
- `ground_truth`：**verified 97 / unverified 2 / mutant_survivor 1**（三档都有真实数据了）
- `repro_cmd` 不可直接执行的从 14 条降到 **3** 条
- 契约类别零覆盖 **6** 个（其中 4 个对当前项目无意义：函数太简单 / 继承只有一层 /
  跨层关联只在 demo 上有 / 文件层口径一致性在 jiuyan 上没查过）
- 硬违规 1 条（越界开单）、归并提示 4 条、契约缺口 9 条（待人工决策）

### ⚠️ 盲区地图给出的最硬的一个数字

**532 个单元里，被真正判定过的有 34 个（6.39%）**；有动态观测但义务未定的 **0 个**；
**完全没查的 498 个**。义务三值：满足 22 / 违反 63 / 未知 2067。

> 这个数字的含义：**这个项目真正的稀缺资源不是模型额度，是「被动态验证过的单元数」。**
> 任何架构如果不先把这个数字涨上去，得到的都只是空壳。
> 「哪里值得投测试」现在可排序了（按中心性）—— 见 `pilot/agent_coverage.json`。

> **这个数字在 2026-11-08 之前是错的**（1.13% / 13 个「有观测但义务未定」/ 满足 0）。
> 两个原因，都不是数据的问题，是**指标没把已有的结论算进去**：
> ① 单元状态只看「有没有用例」，**完全不看用例的判定结果** —— 17 条全 PASS 的用例
>    被记成「有观测但义务未定」，「满足」恒为 0；
> ② **只有函数层的结论能关联到单元** —— 文件层/类层/服务层的结论一条都没被记
>    （文件单元按路径后缀关联、类单元按文件名反查类名、服务单元按层归集），
>    于是那 70 条结论对应的单元在盲区地图里全是「完全没查」。
> 修的是指标，不是数据：**这 5.6 倍不是新查出来的，是本来就在那儿、之前没被算进去。**
> 现在：已判定 6 → **34**（1.13% → **6.39%**），义务满足 0 → 22、违反 5 → 63。
> 按层：函数 19 / 文件 11 / 类 3 / 服务 1。

### 明确还没做的（就是本项目的主战场）

| # | 缺口 | 为什么是关键 | 状态 |
|---|---|---|---|
| **A** | **Agent 群不是真的「群」** —— 骨架有了，但没有编排/调度/并发 | 「有关系才调用，没关系不调用」未落地 | 🟡 骨架 ✅，编排 ❌ |
| **B** | **增量更新** —— 影响分析能算了，但各层脚本还不能只跑受影响的靶区 | 「改一处只动一处」只完成一半 | 🟡 分析 ✅，调度 ❌（设计已出 `DESIGN.md` §5） |
| **B2** | **载体的「动态侧」还有一层没通用化** | 换项目只能跑静态层，动态层要重写 | 🟡 函数层 / 类层 / 文件层的**探针**已通用化，服务层的 `contract_probe.py` 与 `contract_diff.py` 已通用；**还差**：函数层与文件层的差分判定层仍是项目专属（`fn_diff.py` / `file_diff.py`）|
| **C** | **涌现目前只有 1 条真链** —— 图 ✅ 裁决器 ✅ **候选链生成 ✅ FDR 门禁 ✅（本轮补上）**：demo-shop 上自动生成的候选已能命中那条真涌现（召回 1/1，真跑仍 `EMERGENT_CONJUNCTIVE`）。但**场景物化还是人给的**（候选只提出条件槽，schema 驱动的造数器没做）、七个模式只命中 T3、jiuyan 尚无注入目录（门禁对 jiuyan 退出码 2） | 系统的存在理由目前兑现了 1/∞ | 🟡 设计 ✅ 裁决 ✅ 链生成 ✅ 门禁 ✅；造数器 ❌ 模式覆盖 ❌ |
| **B16** | **跨仓库的涌现链** | 🟡 demo 的跨仓库**探针**通了（面清单 + 跨仓库图 + 契约漂移 + 归属），跨仓库的**缺陷**也开出来了（文件层 own 的「汇率兜底量纲错」）；但**跨仓库的涌现链**（两个仓库的条件组合才出问题）还没做 —— 关系网目前只画单仓库 | 与 B10 同一条线：候选链生成器换一张跨仓库的图 |
| **D** | **语义地图** —— 结构有了，「这个函数是干什么的」要变成**可执行断言** | Agent 的「使命」只有一半 | 🟡 断言编译 + 执行已通（8 条职责 / 7 单元），但**模型轨一条都没跑**（8 条全是人工 declared），双轨只落地了确定性那一轨 |
| **E** | **跨仓库** —— 只会审一个仓库 | 「两群 Agent 互相调用」未实现 | 🟡 骨架 ✅（面清单 + 跨仓库图 + 契约漂移 + 归属），实测 2 仓库 / 1 条确认边 / 0 漂移；**跨仓库涌现还没做** |
| **F** | **运行时保真度** | Java 动态性让静态结论不可信 | ✅ 已标注 |
| **G** | **SAST/SCA 接入** | 「全面发现安全问题」需要外部成熟工具 | ❌ |
| **H** | **动态验证覆盖率 0.94%**（532 单元里已判定 5 个） | 上面所有能力都建立在「测过的单元」之上，这个数不涨，一切都是空转 | ❌ **最高优先级** |
| **I** | **Java 特性还有 8 类没补**（事务/装配/属性绑定/语言索引/N+1 全量/Jackson/实体相等/优雅停机） | 设计已出（`DESIGN.md` §6.4），前 5 项 8.5 人日 | ❌（`#1` 自调用漏检已修 ✅，`#2` 事务生效性已做 ✅） |

## 4. 硬约束（违反就是做错了）

1. **不改被测系统**：`/Users/june/jiuyan` **只读**。缺陷出报告，不直接改代码。
   （唯一例外：B7 修复闭环需要用户明确解禁。）
2. **零残留**：探针数据一律 `ai_probe_` 前缀，写完即删；DB 必须回到种子基线；
   非种子上传文件 0；临时容器用完即删。
   **机器守，不靠人记得** —— `pilot/probe_residue_check.py` 在流水线最后检查这三样，
   有残留就退出码 1。（本轮就靠它抓到过 transaction_probe 漏删的 3 行订单。）
3. **确定性优先**：门禁结论必须可复现、可钉基线。**模型参与的部分只能产出
   「待验证线索」，判定必须由确定性层做**（双轨，见 `EMERGENCE.md` §2.2）。
4. **不过度设计**：不建通用扫描器、不引新测试框架、不做插件框架、不做多项目并行。
   能用 30 行解决的不要写 300 行。
5. **不编造**：不确定的事实说「不确定」；静态推断被实测推翻是正常的，写下来。
6. **归属唯一**：owner = 能给出可复现证据的最低层；高层可引用低层但不得重复开单。
7. **commit 前先展示 diff 等确认；不自动 push。**
8. **代码/日志/commit 里不出现密钥、token、密码、JWT。**

## 5. 怎么跑

```bash
cd /Users/june/.cc-weixin/workspace/layered-audit/pilot
bash run_pipeline.sh        # jiuyan 全量流水线，exit 0 = 全绿
                              # （涌现维度会单独报「没查完」，不是绿灯）

cd /Users/june/.cc-weixin/workspace/layered-audit/demos/shop
bash run_pipeline.sh        # demo-shop 流水线：类层 + 涌现全链 + FDR 门禁
                              # exit 1 = 涌现门禁判红（演示目标的预期状态）
python3 engine/normalize_findings.py    # 只跑归一化（exit 1 结构性违规 / 2 契约缺口）
python3 engine/aggregate_findings.py    # 只跑根因聚合 + 覆盖度矩阵
python3 engine/feature_coverage.py      # 只跑特性覆盖矩阵
python3 engine/check_engine_purity.py   # 引擎纯净度守卫（退出码 1 = 阻断）
./test_portability.sh                   # 换绑定可跑冒烟测试
```

## 6. 文件导航

| 文件 | 内容 |
|---|---|
| `layers.yaml` | **层契约，单一事实来源**。改层/类别/归属规则必须先改它 |
| `EMERGENCE.md` | 外部工作（集群/涌现/代码库理解）的引用清单 + 逐条映射 + 直接采用/需改造/不采用的划分 |
| `HANDOFF.md` | 详细交接：现状、方案、脚本矩阵、缺陷台账、剩余工作、验证记录 |
| `pilot/target.json` | 项目绑定（源码根/证据源/复现模板/载体声明/框架特性清单） |
| `pilot/finding_meta.py` | 人工标注表：finding id → category/claim/owner/confidence/root_cause_key |
| `pilot/invariants.json` | 业务不变式声明（项目配置） |
| `pilot/engine/` | **通用引擎**，零项目字面量，有守卫 |
| `pilot/*.py` | 项目实现（层脚本 + 探针） |
| `pilot/audit_report.md` | 根因台账（修复顺序、违规、覆盖度） |
| `pilot/agents.json` / `edges.json` | **Agent 群全集** + 全局边表（unit/role/instance 三级） |
| `pilot/change_impact.json` | 变更影响（resolved / 保守上界双报） |
| `pilot/agent_coverage.json` | **盲区地图**：哪些单元没验过、按中心性排序 |
| `pilot/file_carrier.py` | **第二载体复核**：把文件载体的观测挂到类层结论上，并算出「有观测但没人开单」的覆盖缺口 |
| `pilot/class_carrier_gap.json` | 覆盖缺口清单（无人认领的观测 + 项目声明不适用的观测及理由） |
| `pilot/engine/emergence_candidates.py` / `emergence_gate.py` / `relation_net.py` / `surface.py` | 涌现候选链生成 / FDR 门禁 / 通用可执行依赖图 / 跨仓库面清单与归属 |
| `pilot/baselines/` | **等价性基线**：通用化改造前的结论快照，让「通用化没回归」永远可复核 |
| `pilot/mutation_findings.json` | 变异测试结果（killed / survived） |
| `pilot/target.json` 的 `carriers` | 每个载体「能观察到什么 / 观察不到什么」+ 保真度 |
| `pilot/target.json` 的 `obligation_templates` | 每个契约类别对应的义务表述（项目知识，非通用） |
| `pilot/invariants.json` | 业务不变式声明（项目配置） |

## 7. 推进路线（依赖顺序，别跳）

```
阶段 1（不引模型，立刻可做）
  A1 依赖拓扑 + 变更影响分析        ✅ engine/change_impact.py
  A2 Agent 群骨架（unit/role/instance）✅ engine/agent_schema.py + build_agent_graph.py
  A3 运行时保真度标注                ✅ container/hybrid/stubbed
  A4 变异测试（mutant_survivor）     ✅ engine + mutation_test.py
  A5 盲区地图（投测试的优先级）      ✅ engine/agent_coverage.py
  A6 可执行依赖图 G_exec             ✅ emergence_graph.py（25/25 入口绑定）
  A7 切面生效性差分                  ✅ transaction_probe.py
  A8 预算闸门（max_agents 真消费）   ✅ engine/budget.py + scheduler.py
  A8' 按影响集调度（没波及就复用）    🟡 流水线已有整层跳过（run_layer + incremental_plan）；各层脚本还只支持整层粒度
  A9 候选链生成 + 枢纽排序 + FDR 门禁 ✅ engine/emergence_candidates.py + engine/emergence_gate.py（demo 实测召回 1/1、FDR_inj=0.500）
阶段 2（引模型，双轨）
  B1 语义地图：职责 → 可验证断言    🟡 断言编译与执行已通（8 条职责，违反 2 条开单）；模型轨未接（source_mix 全是 declared）
  B2 递归委派 + token 预算          ⬜
阶段 3
  C1 跨仓库 Agent 群 + 跨仓库涌现    ⬜
  C2 Java 特性补齐（8 类）            ⬜ DESIGN.md §6.4
```

**A8 之前的每一步都不引模型，判定口径不变，门禁结论不变。**

**每个阶段的第一个增量都必须能自证**：造一个已知改动，验证影响集与预期一致、
验证被跳过的 Agent 结论确实没变。

## 8. 已定的设计决议（别推翻，除非有更好的理由）

来自 6 个并行设计 Agent 的交叉结论，已落到代码或已记录：

| # | 决议 | 理由 | 落地 |
|---|---|---|---|
| **D1** | **Agent = 确定性执行体 + 作用域参数化 + 关系网 + 基线**，不是「一个函数一个 LLM」 | 461 个函数各起一个模型实例，成本与 token 都不可行；执行体必须随规模不变 | `engine/agent_schema.py` 三级分离 |
| **D2** | **模型只做「选模板 + 填槽位」，不写结论、不写断言结构、不写期望值** | 模型编造「符号」编译期就被拒；编造「规则」必须落在一个封闭原语上，而原语语义是封闭的 | 见 `EMERGENCE.md` §2.2 |
| **D3** | **双轨**：确定性轨可判红；模型轨只 advisory。晋升 = 人工冻结断言 | 模型参与基线的**扩充**，不参与基线的**判定** | 设计已出（`pilot/semantic/` 方案） |
| **D4** | **极性翻转是最危险的幻觉**（"不得"写成"必须"） | 断言能编译能执行能 FAIL，但方向反了，人工审阅最容易放过 | 对策：极性是枚举字段 + 互斥极性对自检 + 变异算子专测极性 |
| **D5** | **保守失效**：`resolution=unknown` 的边一律过近似失效 | 宁可多标不可漏标，否则「只动受影响」变成「漏更新」，门禁假绿 | `engine/change_impact.py` 的 resolved / upper_bound 双报 |
| **D6** | **拓扑序 = 理解顺序**（「A depends_on B」意味着 B 先被理解） | 有出处的成熟做法（CodeWiki, ACL 2026 Findings） | 待落：组级委派 |
| **D7** | **涌现的可判定性比完备性更重要** | 门禁只能建立在可复现的结论上；宁可只报 5 条经人工确认的，不要 200 条不可复现的候选 | 待落：`detect_emergence.py` |
| **D8** | **exit code 要有第三态**：0 绿 / 1 红 / 2 **没查完** | 「没查完」必须与「查了没问题」区分开，否则覆盖度矩阵会骗人 | ✅ 已落（门禁：`row_gone` 判 INCOMPLETE、退出码 2、拒绝 `--accept`） |
| **D13** | **`max_agents: 12` = 一轮 admit 的判定作业数上限**（不是 Agent 实例总数） | 实例总数随代码规模线性增长（533×21=2071），限制总数等于限制系统规模；且契约里 `max_tokens_per_round` 明确是「每轮」，两者必须同族才自洽 | ✅ 已落：`engine/budget.py` + `scheduler.py`，分轮 admit + oracle 多样性保底 + 产物并集合并；`layers.yaml` 已写明语义 |
| **D14** | **token 预算只约束模型轨，确定性轨不受约束** | 否则预算一开就把确定性工作也卡掉 | ✅ 已落（`BudgetLedger` 里确定性轨 token 恒为 0） |
| **D11** | **判定层永远全量跑，探测层才做增量** | diff/归一化/聚合/门禁都是纯函数，秒级；只有有副作用、昂贵、产证据的探测层才值得增量 | ✅ 决议已定，落点在 `DESIGN.md` §5.3 |
| **D12** | **产物合并是并集，永不缩集** | 少一个分片 = 退出码 2（没查完），不是 0。这是门禁红/绿语义不被增量破坏的唯一保证 | ✅ 决议已定 + 门禁已实现第三态 |
| **D9** | **模型通道的可复现性靠内容哈希缓存，不靠 temperature/seed** | 本环境的模型通道是 subagent，拿不到 temperature/seed；同 `cache_key` 二次调用必须命中缓存、禁止重采样 | 记录：账本降级为 (brief_hash, model_id, response_hash) |
| **D10** | **classpath 覆盖可行** → 变异测试证明力完整 | `fn_dynamic` 的 classpath 是 `[harness, CLASSES, deps]`，变异目录插到最前即可 | ✅ 已实现 `mutation_test.py` |
| **D15** | **两个载体看到同一件事时，结论由「契约里类别所属的层」开单，另一个载体只做复核** | 文件层载体检出的类别（封装泄漏/不变式/继承设计/契约违反）全属类层 —— 让文件层开单是越界开单，让两层都开单违反「不得重复开单」。所以低层观测挂到高层结论的证据上（两个独立载体复现同一结论），**没人认领的观测必须变成覆盖缺口清单，不能让它悄悄消失** | ✅ `pilot/file_carrier.py` + `class_carrier_gap.json` |
| **D16** | **「没查完」要能被看见，且不能伪装成绿灯** | jiuyan 没有注入目录 ⇒ 涌现维度根本没检查过。决议 D8 要求「没查完」与「查了没问题」区分开：流水线显式打出来 + 覆盖度矩阵里该类别零覆盖 + 门禁第三态退出码 2，三处口径一致 | ✅ `pilot/run_pipeline.sh` 涌现步骤 |

## 9. 我砍掉了什么（独立判断记录）

> 用户提醒过：「不要被之前的方案束缚，要有自己独立判断」。
> 这份记录是**我自己的取舍**，不是 Agent 的设计 —— 下一个人可以推翻，但要给理由。

| 砍掉 / 降级 | 理由 |
|---|---|
| **物化 2071 个 Agent 实例**（`agents.json` 8.4 MB） | 实测 95% 以上是占位符：mission.statement 非占位 **0** 条、义务 satisfied **0** 条、state 非 fresh **0** 条。**没人消费它**。改成 `units.json`（533 条轻单元，398 KB）+ 覆盖度按需实时算。行为等价、体积 21×↓ |
| 涌现的**七种裁决式** | 已实现（`engine/emergence.py`）。七种裁决之外，**跨仓库涌现**还没做 |
| 跨仓库 Agent 群 | **没有第二个仓库**。没有真实目标就写跨仓库实现 = 空转 |
| token 预算 / 递归委派 | 都依赖模型，而模型**还没接**。先把确定性层做厚 |
| 剩下 3 个零覆盖类别 | 这个项目本身没有那些面（函数太简单 / 继承只有一层 / 无缓存无消息队列）。**查不出东西是事实，不是不作为** |

**保留的**（每个都产出了可用的数字或真判定）：
Agent 群骨架、变更影响、盲区地图、变异测试、切面生效性、可执行依赖图、残留守卫、预算闸门。

## 9.5 本轮（2026-11-08）的判断记录

> 这一轮是接手一个被上下文撑爆中断的会话后继续做的。判断记录如下，
> 下一个人可以推翻，但要给理由。

1. **不重写已有成果，只做「让它可复现、可交接」**。中断时三块工作（文件层通用化 /
   类层通用化 / 涌现候选链 + FDR 门禁）已经跑通但**一笔没提交**，且文档数字与产物
   对不上。优先做的是拆分提交 + 同步文档，而不是推倒重来。
2. **文件层的 6 个通用检查器接成「第二载体」而不是「第二份开单」**。
   它们检出的是**类层类别**（封装泄漏/不变式/继承设计/契约违反），而文件层的契约
   类别只有 state_consistency / error_handling_pattern / cohesion —— 让文件层开单会
   越界，让类层重复开单会违反「不得重复开单」。所以：文件载体观测挂到类层结论上
   （两个独立载体复现同一结论），没人认领的观测写成覆盖缺口清单。
   **代价**：类层结论的 evidence 里多了一个 `file_carrier` 段。
3. **jiuyan 补上 `class_shape` 探针，而不是新建一套类层检查**。
   类形状（继承/值相等/封装泄漏/缓存）是语言级判断，探针本来就是通用的 ——
   jiuyan 只是没启用它。换项目只改配置这件事，在第二个目标上兑现了。
4. **涌现门禁在 demo 上判红是预期状态，不去「修绿它」**。
   它红是因为演示目标里真有「根因只被引用、无人 own 开单」的缺陷
   （INJ-PROFILE-VIP）。把门禁调绿 = 把真缺陷藏起来。
   demo 流水线的退出码因此是 1 —— 这是设计，不是故障。
5. **jiuyan 的涌现维度如实报「没查完」，不假装绿灯**。
   jiuyan 没有注入目录，涌现这一维根本没检查过。决议 D8 要求「没查完」与
   「查了没问题」区分开，所以流水线里显式打出来，覆盖度矩阵里
   `integration.emergent_masking` 对 jiuyan 也是零覆盖 —— 两处口径一致。
   **没做的事**：给 jiuyan 编写注入目录（那需要真的找出 jiuyan 的涌现链，
   不能编）。
6. **等价性基线从 /tmp 挪进仓库**。原来它们在 /tmp，重启即失，
   「通用化没回归」就成了一句没法复核的话。放进仓库成本 44 KB。
7. **两个图构建器并存这件事只记录、不合并**。
   `pilot/emergence_graph.py`（项目专属，151 节点 / 414 边，产物无消费方）与
   `pilot/engine/relation_net.py`（通用，162 节点 / 151 边，产物被候选生成器消费）
   形状不同、边数差一倍以上 —— 贸然替换会丢信息。合并是独立的一轮工作。

## 10. 进度更新约定

每完成一块，**同时更新**：`CLAUDE.md` §3 的进度表与数字 → `HANDOFF.md` §11/§14 →
`pilot/jiuyan-layer-manifest.yaml` 的 `cross_layer` 段 → 提交（带 Co-Authored-By）。
数字必须来自 `pilot/root_causes.json` 实测，不许估。
