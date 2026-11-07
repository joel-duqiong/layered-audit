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

## 3. 当前进度（截至 2026-11-07）

### 已建成并跑通的

| 块 | 状态 | 位置 |
|---|---|---|
| 五层契约（function/file/class/service/integration）+ 归属规则 + 证据 schema | 完成 | `layers.yaml` |
| 五层「静态声明 → 动态实测 → 差分出单」 | 完成 | `pilot/`（30+ 脚本） |
| 跨层收口引擎（schema 校验 / 归一化 / 根因归并 / 覆盖度矩阵 / 特性覆盖 / 不变式原语） | 完成，**零项目字面量** | `pilot/engine/`（5 文件） |
| 项目绑定 + 人工标注表 | 完成 | `pilot/target.json`、`pilot/finding_meta.py` |
| 常驻门禁（基线比对，回归即红） | 完成 | `pilot/integration_gate.py` |
| 回归资产（pytest：7 闭合不变量真绿 + 7 攻击链 xfail strict） | 完成 | `/Users/june/tests/auto-api` |
| 负载实测 / 并发 / 配置失败模式 / ORM 查询放大 / Java 特性扫描 / 业务不变式 | 完成 | `pilot/*_probe.py` |

### 本轮（2026-11-07 晚）新增

| 块 | 状态 | 位置 |
|---|---|---|
| **Agent 群骨架** | ✅ unit / role / instance 三级分离，533 单元 / 21 角色 / 786 条边，契约违规 0 | `pilot/engine/agent_schema.py`、`build_agent_graph.py` → `units.json` / `edges.json` |
| **执行预算闸门** | ✅ `max_agents: 12` 第一次被真正消费（分轮 admit + oracle 多样性保底 + 产物并集合并） | `pilot/engine/budget.py`、`scheduler.py` → `schedule_report.json` |
| **函数层载体通用化** | ✅ **通用反射 harness**：目标类、依赖、打桩、用例、类型别名全部来自用例文件，harness 里零项目字面量。jiuyan 92 条用例判定 **0 条不同**（64/28 与原版一致）；demo-shop 6 条全过 | `pilot/fn_harness/GenericHarness.java`、`pilot/fn_cases.json`、`demos/shop/fn_cases.json` |
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

### 规模数字（以 `pilot/root_causes.json` 为准）

- 发现 **111** 条 → 根因 **71** 条
- 10 个必填字段结构齐备 **110/110**；`owner` 标注 **110/110**
- `ground_truth`：**verified 108 / unverified 2 / mutant_survivor 1**（三档都有真实数据了）
- `repro_cmd` 不可直接执行的从 14 条降到 **3** 条
- 契约类别零覆盖 **4** 个（其中 3 个对当前项目无意义：函数太简单 / 继承只有一层）
- 硬违规 1 条（越界开单）、归并提示 4 条、契约缺口 9 条（待人工决策）

### ⚠️ 盲区地图给出的最硬的一个数字

**532 个单元里，被真正判定过的只有 5 个（0.94%）**；有动态观测但义务未定的 12 个；
**完全没查的 515 个**。义务三值：满足 0 / 违反 5 / 未知 2066。

> 这个数字的含义：**这个项目真正的稀缺资源不是模型额度，是「被动态验证过的单元数」。**
> 任何架构如果不先把这个数字涨上去，得到的都只是空壳。
> 「哪里值得投测试」现在可排序了（按中心性）—— 见 `pilot/agent_coverage.json`。

### 明确还没做的（就是本项目的主战场）

| # | 缺口 | 为什么是关键 | 状态 |
|---|---|---|---|
| **A** | **Agent 群不是真的「群」** —— 骨架有了，但没有编排/调度/并发 | 「有关系才调用，没关系不调用」未落地 | 🟡 骨架 ✅，编排 ❌ |
| **B** | **增量更新** —— 影响分析能算了，但各层脚本还不能只跑受影响的靶区 | 「改一处只动一处」只完成一半 | 🟡 分析 ✅，调度 ❌（设计已出 `DESIGN.md` §5） |
| **B2** | **函数层之外的三层载体仍是项目专属**（file/class/service 的动态脚本、探针） | 换项目只能跑静态层，动态层要重写 | ❌ 函数层已通，其余三层待通用化 |
| **C** | **涌现只做了 5 条手写攻击链** —— 图与判定式已设计（图 ✅），**裁决器已实现**（`pilot/engine/emergence.py`，demo-shop 上判出 1 条真涌现 ✅），但候选链生成、按枢纽排序、FDR 门禁未做 | 系统的存在理由只兑现了 5/∞ | 🟡 设计 ✅，裁决 ✅，链生成 ❌ |
| **D** | **语义地图** —— 只有结构特征，没有「这个函数是干什么的」 | Agent 的「使命」只有一半 | ❌（设计已出，见 §8） |
| **E** | **跨仓库** —— 只会审一个仓库 | 「两群 Agent 互相调用」未实现 | ❌（设计已出，见 §8） |
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
bash run_pipeline.sh        # 全量流水线，exit 0 = 全绿
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
  A8' 按影响集调度（没波及就复用）    ⬜ 各层脚本加 --scope
  A9 候选链生成 + 枢纽排序 + FDR 门禁 ⬜ emergence_chain.py + emergence_gate.py
阶段 2（引模型，双轨）
  B1 语义地图：职责 → 可验证断言    ⬜ 模型只出断言草案，确定性层判定
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

## 9. 我砍掉了什么（独立判断记录）

> 用户提醒过：「不要被之前的方案束缚，要有自己独立判断」。
> 这份记录是**我自己的取舍**，不是 Agent 的设计 —— 下一个人可以推翻，但要给理由。

| 砍掉 / 降级 | 理由 |
|---|---|
| **物化 2071 个 Agent 实例**（`agents.json` 8.4 MB） | 实测 95% 以上是占位符：mission.statement 非占位 **0** 条、义务 satisfied **0** 条、state 非 fresh **0** 条。**没人消费它**。改成 `units.json`（533 条轻单元，398 KB）+ 覆盖度按需实时算。行为等价、体积 21×↓ |
| 涌现的**七种裁决式** | 设计完整，但当前只有 5 条手写链，判定式跑不出更多结论 —— 先不实现，等候选链生成器先出来 |
| 跨仓库 Agent 群 | **没有第二个仓库**。没有真实目标就写跨仓库实现 = 空转 |
| token 预算 / 递归委派 / 语义地图 | 都依赖模型，而模型**还没接**。先把确定性层做厚 |
| 剩下 3 个零覆盖类别 | 这个项目本身没有那些面（函数太简单 / 继承只有一层 / 无缓存无消息队列）。**查不出东西是事实，不是不作为** |

**保留的**（每个都产出了可用的数字或真判定）：
Agent 群骨架、变更影响、盲区地图、变异测试、切面生效性、可执行依赖图、残留守卫、预算闸门。

## 10. 进度更新约定

每完成一块，**同时更新**：`CLAUDE.md` §3 的进度表与数字 → `HANDOFF.md` §11/§14 →
`pilot/jiuyan-layer-manifest.yaml` 的 `cross_layer` 段 → 提交（带 Co-Authored-By）。
数字必须来自 `pilot/root_causes.json` 实测，不许估。
