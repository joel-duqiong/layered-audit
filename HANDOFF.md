# 分层缺陷审计 —— 交接文档

> 更新时间：2026-11-07 ｜ 面向「接手的人 / 另一个 AI」
> 读法：先读本文 → 再读 `layers.yaml`（层契约，唯一事实来源）→ 不要靠猜代码。
> 本文的 §5（方案）与 §11（剩余工作）是接手重点，别只翻台账。

---

## 1. 这个项目是做什么的

**一句话**：造一套**与被测项目无关**的多层缺陷审计系统 —— 输入代码库 + 层契约，
输出带证据、带归属、带复现命令的缺陷台账 + 可回归的门禁。
`/Users/june/jiuyan`（农村宴席 Pad 谈单系统）是**第一个落地验证的目标**，不是系统的适用范围。

通用的是**这套审计流水线**，不是「通用漏洞扫描器」—— 这两件事不一样，别混。

### 为什么不是通用扫描器

| 通用工具给什么 | 这套给什么 |
|---|---|
| 「这里可能有问题」 | 「错在哪一层、谁负责、怎么复现、修了会不会再犯」 |
| 一次性输出 | 有基线、有门禁、改完代码立刻知道有没有回归 |
| 扁平的缺陷列表 | 按层归属，高层引用低层，不重复开单 |

### 成功标准（可验证，不是口号）

1. **每条缺陷有可复跑证据** —— 证据来自真实容器 / 真实 DB / 真实前端代码，不是推测。
2. **每条缺陷归属唯一层** —— owner = 能给出可复现证据的最低层。
3. **修完能自动回归** —— 缺陷固化进门禁基线 + pytest 用例。
4. **漏检率有对抗性度量** —— 攻击链从「单点洞」升级为「串起来能干什么」。
5. **换项目不改编引擎** —— 引擎零项目字面量，且有自动检查守住这条（见 §5.12）。

### 边界（明确不做）

- **不改被测系统任何文件**（含前端源码，只读）。发现的问题出成报告，不直接改代码。
- 不做通用 SAST/DAST 集成，不做安全扫描器；不做性能压测平台。
- 不重做下层已开单的缺陷；高层只能引用并量化后果。
- 不做插件框架、不做多项目并行 —— 通用性靠「一份绑定」实现，不靠机制。

---

## 2. 一句话现状

五层全部落地并跑通；共 **100 条 finding，按根因归并后 72 条根因**。
集权层已升级为「真三方对账 + 攻击链 + 常驻门禁」，全部缺陷已固化成 pytest 回归资产。
**跨层收口层也已落地**：五层输出统一到 layers.yaml 的 10 字段证据 schema（100/100 结构齐备），
`owner` 补齐到 100/100，根因归并 100→72，产出 category×层 覆盖度矩阵（5 个类别零覆盖）。
**负载实测层已落地**：端到端延迟曲线 + 分页 total 开销量化，把 `service.service_latency`
与 `integration.cross_service_performance` 两个零覆盖类别填上了。
**涌现这条链也落地了**：候选链生成（固定种子）+ FDR 门禁 + 回归流水线，
demo-shop 上自动生成的候选能命中那条真涌现（召回 1/1），门禁判红并指出漏归属。
**最硬的数字没变**：532 个单元里被真正动态判定过的只有 **6 个（1.13%）**。

---

## 3. 环境前提（缺一不可）

| 依赖 | 值 | 检查方式 |
|---|---|---|
| 容器 | `jiuyan-backend` / `jiuyan-postgres` / `jiuyan-frontend` 必须 up | `docker ps` |
| 后端 | `http://127.0.0.1:8081`（`JIUYAN_BASE_URL` 可覆盖） | `curl /api/categories` |
| 前端源码 | `/Users/june/jiuyan/src`（`JIUYAN_FE_DIR` 可覆盖） | 文件存在即可，**只读** |
| Node | v22+（跑前端真实 mapper 用） | `node -v` |
| Python | 3.14（试点用），依赖 `requests` | `python3 -V` |

---

## 4. 目录

```
/Users/june/.cc-weixin/workspace/layered-audit/
├── HANDOFF.md          ← 本文，唯一入口
├── layers.yaml         层契约 v0.1（层定义 / 证据 schema / 归属规则 / agent 编排规划）
└── pilot/              全部实现与产物（22 个脚本）
/Users/june/tests/auto-api/   另一个仓库：pytest 回归资产（见 §10）
```

---

## 5. 方案：这套审计到底怎么跑的

### 5.1 核心方法论：把每个 agent 当成一个函数来设计

```
签名 = scope（管哪一段） + oracle（用什么手段判定）
行为 = 先静态（便宜，得出「声明」） → 再动态（实测，得出「实况」） → 差分（缺口 = 缺陷）
```

三层含义：

- **静态 = 声明**：这个系统自称有什么契约、什么结构、什么规则。例：字节码里声明的方法签名、DDL 里的约束、OpenAPI 里的类型、前端 mapper 里读的字段。
- **动态 = 实况**：真跑一遍，看它实际做了什么。例：JVM 直调、整类实例化、HTTP 实测、连真实 PostgreSQL、Node 直跑前端 mapper。
- **差分 = 出单**：声明与实况对不上的地方，就是缺陷。**只看声明不看实况会漏，只看实况不看声明会变成随机测试。**

这套「声明 vs 实况」的差分，是全项目所有缺陷的来源。

### 5.2 五层契约与边界

| 层 | 粒度 | 内禀问题（categories） | 明确不管 | 越层升级到 |
|---|---|---|---|---|
| function | 单个函数体 | 边界 / 复杂度 / 异常路径 / 数值 | 状态一致性、调用方契约、服务延迟 | file, service |
| file | 单文件内多函数 | 文件内共享状态 / 异常处理风格分裂 / 内聚性 | 类契约、服务延迟 | class, service |
| class | 单个类及成员 | 封装泄漏 / 类不变式 / 方法契约 / 继承设计 | 服务延迟、端到端业务 | service |
| service | 一个可部署服务边界 | 接口契约 / 韧性 / 鉴权 / 数据一致性 / 资源泄漏 / 服务延迟 | 端到端业务、跨服务攻击链 | integration |
| integration | 跨服务端到端流程 | 端到端业务语义 / 端到端性能 / 跨服务攻击链 / 跨层关联 | 单点分析 | — |

**「一层一层往上抽」**：每层只管自己那层的内禀问题；只有在跨层执行时才显现的问题
（端到端业务语义、性能塌陷、攻击链）归集成层。

### 5.3 Oracle 绑定（层契约里规定的判定手段）

| 层 | 每类问题绑定的 oracle（`layers.yaml` 权威） |
|---|---|
| function | mutation_test / property_based_test / static_analysis / benchmark |
| file | static_analysis / order_permutation_experiment / mutation_test |
| class | property_based_test / mutation_test / contract_test / static_analysis |
| service | contract_test / schema_diff / chaos_experiment / authz_test / concurrency_test / load_test / trace_analysis |
| integration | e2e_scenario / trace_reconstruction / e2e_load_test / attack_graph / threat_modeling / graph_correlation |

绑不同 oracle 是为了**让不同 agent 用不同手段查同一片代码**，避免重复劳动与同源盲区。
实际落地时每层选了一个可执行子集：

| 层 | 实际用的判定手段 | 实现在 |
|---|---|---|
| 函数层 | 字节码声明 + JVM 直调实测 | `fn_static.py` / `fn_dynamic.py` |
| 文件层 | 静态声明 + 整类实例化实测 | `file_static.py` / `file_dynamic.py` |
| 服务层 | OpenAPI 声明 + 真实 HTTP 实测 | `contract_static.py` / `contract_dynamic.py` |
| 类层 | DDL+实体声明 + DTO 反射契约 + DB 实况 + 数据库实测 | `class_contract.py` / `class_static.py` / `class_dynamic.py` |
| 集成层 | 真实 HTTP + 真实 DB + **前端真实 mapper** + 攻击链 | `integration_reconcile.py` / `integration_chain.py` |

### 5.4 出单与证据

`layers.yaml` 定义了统一证据 schema（10 个必填字段）。**本轮已落地**（见 §6 的三个新脚本）：

| 字段 | 函数层 | 文件层 | 服务层 | 类层 | 集成层 | 归一化后 |
|---|---|---|---|---|---|---|
| `id` / `severity` / `title` / `evidence` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `layer` | ✓ | ✓ | ✗ | ✓ | ✗ | ✓ 86/86 |
| `cwe` | ✓ | ✓ | ✗ | ✗ | ✗ | ✓ 86/86（有值 57，空值合法） |
| `category` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86（9 条 UNMAPPED） |
| `location`（file+line+symbol） | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86（文件 66 / 行号 43） |
| `claim`（一句话怀疑什么） | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86 |
| `evidence_refs` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86 |
| `repro_cmd` | 变体 `repro`/`endpoint` | ✗ | 变体 `repro` | ✗ | 变体 `repro` | ✓ 88/88（case 8 / scenario 9 / request 30 / layer 36 / command 2 / reference 3） |
| `confidence` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86 |
| `ground_truth` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ 86/86 |

落地的三个脚本（在 `pilot/`）：

| 脚本 | 干什么 | 产物 |
|---|---|---|
| `findings_schema.py` | 从 `layers.yaml` **现场解析**每层允许的 category / escalation_to / out_of_scope，并校验归一化结果 | — |
| `finding_meta.py` | 人工标注表：59 个 finding id → category / claim / owner / confidence / ground_truth / cwe / root_cause_key | — |
| `normalize_findings.py` | 五层输出 → 一张符合 schema 的总表；退出码 1 = 结构性违规，2 = 契约缺口 | `all_findings.json` |
| `aggregate_findings.py` | 根因归并 + 归属校验 + 覆盖度矩阵 | `root_causes.json` / `audit_report.md` |

关键设计：**类别清单不硬编码**，从 `layers.yaml` 解析，契约改了矩阵自动跟着改。
`finding_meta.py` 里的 `category` 是人工判定，不是脚本推断 —— 判不出来的写 `UNMAPPED`
并给 `suggested_category`，把「契约装不下」显式暴露成缺口，而不是硬塞进一个勉强的类别。

`fn_dynamic.py` 新增 `--case <id>`（可重复），使函数层的 `repro_cmd` 变成真能跑的命令：
`python3 fn_dynamic.py --case RFP-05`。

集成层同理：`integration_dynamic.py --scenario SC01_...`（6 个业务场景，已拆成可单独执行的函数）、
`integration_chain.py --chain CHAIN-A`（5 条攻击链）。**不可直接执行的 repro 从 14 条降到 3 条** ——
剩下 3 条确实是「操作步骤描述」或「数据文件指针」，硬编成命令反而是假的。

粒度口径（`normalize_findings.py` 的 `_granularity`）：

| 粒度 | 含义 | 例子 |
|---|---|---|
| `case` | 单个单元用例 | `python3 fn_dynamic.py --case RFP-05` |
| `scenario` | 单个缺陷场景 / 攻击链 | `python3 integration_chain.py --chain CHAIN-A` |
| `request` | 一个 HTTP 请求 | `curl -X DELETE /api/categories/None` |
| `command` | 其它 shell 命令 | `grep -n 'packageSnapshot' .../OrderResponse.java` |
| `layer` | 整层脚本（无更细粒度） | `python3 file_dynamic.py` |
| `reference` | **不是命令**，只是一句指引 | 「见 observation.json 中 key=…」 |

`reference` 单独成一档，是为了不把「读一下这个文件」伪装成「跑这条命令」。

### 5.5 归属规则

```
owner = 能给出可复现证据的最低层（最接近根因）
高层可以引用低层缺陷作佐证，但不得重复开单
跨服务业务 / 性能 / 攻击链 → owner 归 integration
```

规则全文：`layers.yaml` 的 `attribution`；具体 pattern 表：`pilot/jiuyan-layer-manifest.yaml` 的 `attribution` 段（现有 25 条 pattern）。

### 5.6 集权层 v2：为什么「声明 vs 声明」的对账不够

v1 比的是两份声明（前端调的接口后端有没有、前端读的字段响应里有没有）—— 那不是对账。
v2 要求**三个来源同时取值再逐字段比**：

```
1. 通过 API 写入哨兵值（每个可写字段填成可区分的确定值）
2. API 读回响应体
3. 直接查 DB 的真实行
4. 前端 mapper 的消费口径（读哪些字段、怎么换算、拿不到时兜底成什么）
```

**第三个来源必须是前端线上同款 mapper，不能复刻。**
`pilot/frontend_mapper.mjs` 由 Node 直接 import `/Users/june/jiuyan/src/api/banquetApi.js`；
`fe_loader.mjs` 是 loader 钩子，把 `import.meta.env` 换成 `globalThis.__VITE_ENV__`，
因此**不需要改前端源码**就能让 Vite 模块在 Node 里跑起来。

对账结论 6 类：`ALIGNED` / `VALUE_MISMATCH` / `API_OMITS` / `DERIVED` / `FRONTEND_IGNORES` / `FRONTEND_MISSING`，
外加语义级 `SEMANTIC_MISMATCH`（金额单位、时区、快照键、明细合计、兜底图、DB 直写绕过服务层）。

### 5.7 攻击链：把「单点洞」升级成「串起来能干什么」

低层各自证明「这一层有洞」，集成层要证明「这些洞串起来能干什么」。
每条链是真实跑出来的步骤序列，每步留一条可复现证据；链级后果归集成层，低层根因只引用。
现有 5 条（A 匿名读写他人订单并重写快照 / B 任意字节上传后匿名原样下载 / C 客户端控价三方一致接受 /
D 三个价格并存且界面不显示明细合计 / E 无定金订单一步跳到 COMPLETED），全部 CONFIRMED。

### 5.8 回归闭环（本轮新增）

| 环节 | 机制 | 位置 |
|---|---|---|
| 声明层回归 | 对账结论钉成基线，回归即红（退出码 1） | `pilot/integration_gate.py` |
| 行为层回归 | 闭合不变量写成真通过用例 | `tests/test_integration_alignment.py` |
| 缺陷层回归 | 已知缺陷写成 `xfail(strict=True)`，修好即 XPASS 变红强制摘标记 | `tests/test_attack_chains.py` |

### 5.10 负载实测：怎么把「性能假设」变成可判定的结论

类层只证明了「索引对查询模式不可用」（591 倍），那是**机制**；用户能感知的是**后果**。
两者的关系不是显然的 —— 本轮实测给出了答案：机制成立，后果在当前规模下不成立。

做法（`pilot/perf_load.py`）：

1. 按阶梯把 `products` 放大（探针行带 `ai_probe_perf_` 前缀，写完即删）；
2. 每级跑一次 `ANALYZE products`（否则优化器没有统计信息，plan 不真实）；
3. 量端到端 HTTP 延迟（真实走 Spring + JPA + 网络，取 p50 而不是均值）；
4. 用 `EXPLAIN (ANALYZE, BUFFERS)` 跑语义等价 SQL 做归因，分清是 seq scan 还是 count(*)。

**判定口径**（关键，别只看倍数）：

| 判定 | 条件 |
|---|---|
| CONFIRMED | 延迟增长快于数据量增长（超线性），或实测规模内已跨过 1 秒预算 |
| PARTIALLY_CONFIRMED | 延迟随数据量线性增长（机制成立），但远未到不可用阈值 |
| FALSIFIED | 延迟基本不随数据量增长 |

本轮结论：`PARTIALLY_CONFIRMED`。数据量 30 → 50030 行（1668×）时，带 keyword 的列表接口
端到端 p50 从 ~6ms 涨到 ~36ms（6×）—— 增长是真的，但按每行 0.0007ms 折算，要到
**约 140 万行**才触及 1 秒预算。一个农村宴席系统的商品目录不会到这个量级。

所以类层那条 `INDEX_UNUSABLE_FOR_QUERY_PATTERN`（HIGH）**风险等级需要下调**：
它是真实的结构性缺陷，但当前规模下不产生用户可感知的伤害。
真正该修的不是「加索引」，而是**别让后端算一个没人要的数**（见下）。

### 5.11 顺手挖出来的：分页 total 是纯开销

`banquetApi.js` 的 `listProducts` / `listPackages` / `listOrders` 三个函数
一律只取 `data.items.map(...)` —— **`total` 从未被读取**，且 `page` 固定 1、`pageSize` 固定 100。
但后端对每次列表请求都会额外跑一条 `count(*)`（全表 seq scan，因为
`lower(name) LIKE '%…%'` 用不上 `idx_products_name`）。

实测（5 万行时）：count(*) 单条 11.8ms，占带 keyword 列表接口端到端延迟的 **31%**，
占不带 keyword 的 **40%**。**这笔开销 100% 浪费**，且随数据量线性增长。

归属：根因归 **service**（后端白算），集成层只引用「三个列表接口都白付这份开销」
这个链级后果。这条把集成层已有的 `LIST_TRUNCATED_AT_100`（前端只取前 100 条）
从「可见性缺陷」升级成了「可见性缺陷 + 纯成本」两罪并罚。

### 5.13 补零覆盖类别：property_based_test 与 concurrency_test 怎么落地

覆盖度矩阵暴露「某个 category 一条都没查到」之后，补法不能是「写个 finding 把格子填满」——
那只是自欺。补法是**把那个类别对应的 oracle 真的跑起来**，让性质成立与否由实测决定。

#### function.numeric —— `numeric_props.py`

载体是前端**线上同款**模块的钱相关函数（Node 直接 import `src/api/banquetApi.js`），
外加 `App.jsx` 里那个金额输入规范化函数（**从 shipped 源码抽出来直接执行**，不是复刻；
抽不出来就报错，不静默跳过）。随机数用固定种子的 LCG → 每次同一批输入 → 可复现。

性质与结果：

| 性质 | 结果 |
|---|---|
| FP1 分→元→分往返闭合（2000 例） | ✅ 全过 |
| FP2 显示精度 = 精确十进制展开（2000 例） | ✅ 全过 |
| FP3 元→分 = round(y×100)（2000 例） | ✅ 全过 |
| NP1 金额输入不得静默改变方向 | ❌ **挂** —— `-19.99` 被规范成 `19.99` |
| NP2 金额输入不得静默截断 | ❌ **挂** —— `19.995` 存成 1999 分（少 1 分） |
| FP4 超 2^53 分的前后端一致性 | ❌ **挂** —— 后端无上界，前端 Number 装不下 |

两条真缺陷：
- `FN-NUMERIC-MONEY-INPUT-SILENT-MUTATION`（LOW，新根因）：`normalizeMoneyInput` 是
  **6 个金额输入框的唯一入口**（成交价 / 定金 / 套餐价 / 商品价），它把非数字字符全删、
  把小数截到 2 位，两处都静默。注意性质：**不是「显示与存储不一致」，而是「存储值与用户
  输入不一致且无任何反馈」**。
- `FN-NUMERIC-BIGINT-PRECISION-LOSS`（LOW，**引用**已有根因）：金额无上界 →
  前端显示的金额与库里存的对不上。根因已由函数层开单，这里只量化后果，不重复开单。

外加一条正面结论 `FN-NUMERIC-FE-MONEY-ROUNDTRIP-EXACT`（INFO）：金额换算本身没有精度 bug，
问题不在换算，在输入规范化与上界缺失。

#### service.data_consistency / service.resilience —— `concurrency_props.py`

真实容器 + 真实 DB，三个性质：

| 性质 | 结果 |
|---|---|
| DC1 两个并发 PUT 改不同字段，最终行必须同时含两个新值 | ❌ **挂** |
| DC2 被拒的写请求不能留下部分写入 | ✅ 全过（正面结论） |
| RS1 同一个 POST 重发两次只能产生一个订单 | ❌ **挂** |

两条真缺陷：
- `SVC-CONCURRENT-WRITE-LOST-UPDATE`（MEDIUM，新根因）：订单编辑走 PUT 全量覆盖，
  **没有乐观锁（version / ETag / If-Match），没有冲突检测**。实测两个 PUT **都返回 200**，
  最终行只含后写者的完整快照 —— 先写者的修改凭空消失，两边都以为自己成功了。
  谈单场景：销售 A 改成交价、销售 B 同时改电话，A 的改价消失，金额回到旧值。
- `SVC-POST-NOT-IDEMPOTENT`（MEDIUM，新根因）：`POST /api/orders` 没有幂等键、
  也没有业务键去重。实测同一个 body 发两次 → 两个订单 id、库里 2 行。
  客户端超时后重试（所有 HTTP 客户端的标准行为）就会多出一张订单；零鉴权下更是放大器。

### 5.14 Java / Spring 生态特性：我们看得见什么，看不见什么

**先说结论：之前完全没考虑到。** 这是这套审计在真实 Java 项目上的**系统性盲区**，
不是某个探针的疏漏。根因是：我们把「层契约」当成了语言无关的东西（对的），
但把**载体（carrier）**当成了实现细节（错的）—— 而 DI / 切面 / 代理 / RPC 这些
恰恰全都活在载体里。

现在的载体是 Java 专属的：

| 层 | 载体 | 语言 |
|---|---|---|
| function | JVM 内反射直调（仓库打桩） | java |
| file | javap 字节码 + 整类实例化（打桩） | java |
| class | DTO 反射 + PostgreSQL 实况 | java |
| service | 真实容器 HTTP 契约测试 | java |
| integration | HTTP + DB + Node 跑前端 mapper | java+js |

**换语言 = 重写全部 5 个载体**，而不是「换个实现细节」。这就是不通用的地方。

#### 关键盲区：function / file 层根本不在 Spring 容器里

反射直调 + 动态代理打桩 → **Spring 代理不参与**。所以这两层的载体**结构性地看不见**：

| 盲区 | 后果 |
|---|---|
| AOP 切面语义（@Transactional/@Async/@Cacheable/@PreAuthorize） | function 层任何跟事务有关的结论都不成立 |
| Spring DI 装配结果（@Primary/@Qualifier/@Profile/@Conditional 选了谁） | 看到的是打桩后的假依赖图 |
| Spring 代理行为（自调用失效、private/final/static 不被织入） | 切面失效点查不出来 |
| 配置注入（@Value）与配置刷新 | 配置不参与，行为不可复现 |
| 任何依赖持久化 / 容器生命周期的语义 | 同上 |

#### 补法：加一根「特性覆盖」轴（跟 category 覆盖度矩阵同模式）

- `target.json` 里加 `carriers`（每个载体「能观察到什么 / 观察不到什么」）与
  `features`（项目用到的框架特性清单）
- `engine/feature_coverage.py`（**通用引擎**）：跑各特性的项目侧检测器 → 产出
  「特性 × 载体」矩阵 + 盲区清单 + **受影响的结论条数**
- `pilot/java_feature_scan.py`（**项目侧 Java 检查器**）：切面失效点、事务内异步逃逸、
  对外暴露 JPA 实体、DI 形态、RPC 入口、动态配置形态

**换语言 = 换特性清单 + 换检测器，引擎不动** —— 这就是「扩展到其他语言」的正确接缝，
和 `target.json` / `finding_meta.py` / agent 脚本是同一套切分逻辑。

#### 本项目（jiuyan）的实际特性盘点

| 特性 | 用了吗 | 状态 |
|---|---|---|
| AOP 切面 | 只用 @Transactional（24 处） | 无切面失效点、无事务内异步逃逸、无自调用 → **静态层面干净** |
| DI | 全构造器注入 + 4 处 @Value，**零字段注入** | 装配结果（@Primary/@Profile）**无检查器 → 盲区** |
| RPC（Dubbo/gRPC/Feign/MQ） | **零入口**（扫描器已确认） | 本项目无盲区；下一个项目有的话 service 层载体要加 `rpc_contract` |
| 动态配置 | 4 处 @Value 都是启动时读一次，无 @RefreshScope / 配置中心 | 一旦引入配置中心 → **门禁基线会在无人改代码的情况下漂移**，需要基线失效策略 |
| JPA 持久化 | 8 个实体，懒加载关联 | 声明一致性已查；**查询放大（N+1）无检查器 → 盲区** |

#### 本轮因此新增的两条发现

- `JAVA-SERVICE-EXPOSES-JPA-ENTITY`（MEDIUM，class 层，`class.encapsulation_leak`）：
  `ProductService.findProduct` 与 `PackageService.findPackage` 是 **public 且返回 JPA 实体**，
  可见性过宽 —— 任何调用方都能绕过 DTO 契约与全部业务校验直接改实体。
  当前**无跨类调用方**，是潜在泄漏而非正在发生的缺陷（所以是 MEDIUM 不是 HIGH）。
- `JAVA-SPRING-PROXY-SEMANTICS-CLEAN`（INFO，正面结论）：24 个切面方法全部 public、
  无 private/final/static 上的切面、无同类内部自调用、事务方法内无异步逃逸。
  **注意这是静态结论**，证不了「运行时切面真的生效了」—— 那要在容器内观测。

### 5.15 业务不变式：把「业务功能问题」变成可机械判定的东西

「业务功能问题」里，只有**自洽性**那一类能被自动发现 —— 数据自己跟自己必须一致。
「定金该不该收」「取消订单退不退定金」这类需要业务知识，系统判不了，只能由人声明。

做法（**声明是项目配置，判定是通用引擎**）：

| 件 | 位置 | 换项目时 |
|---|---|---|
| 不变式声明（字段关系 + 违反时算什么） | `pilot/invariants.json` | 换清单 |
| 判定原语（`field_lte_field` / `jsonb_sum_eq_field` / `jsonb_subset_of` / `when` 条件…） | `pilot/engine/invariants.py` | **不改** |
| 造场景 + 取值 + 出 finding | `pilot/business_probe.py` | 换场景 |

引擎认得「字段之间是什么关系」，不认得「定金」是什么 —— 这是它能通用的原因。

**跑第一遍就发现我自己声明错了三条**（这本身就是不变式机制的价值）：

| 我写的 | 为什么不能用 |
|---|---|
| 订单金额 = 明细合计 | **前提不成立**：套餐是打包价，套餐价 ≠ 明细合计（8888 vs 15000） |
| 用成交价时金额 = 报上来的成交价 | **不可判定**：DB 里就是同一列，恒等式 |
| 未用成交价时金额 = 套餐总价 | 少了 `when` 条件 → 在报了成交价的订单上误判 |

结论：**不变式必须由人来定，而且必须能从持久化的数据判定，否则是废话。**
清单跑一遍就把「哪些是废话」暴露出来了。

本项目最终成立的 6 条自洽性不变式：

| 不变式 | 结果 |
|---|---|
| 定金 ≤ 订单金额 | ❌ 违反（定金可超订单金额） |
| 订单金额 ≤ 套餐标价 | ❌ 违反（客户端可报天价） |
| 订单宴会类型 = 套餐宴会类型 | ❌ **违反 —— 本轮新发现** |
| 快照明细 ⊆ 套餐明细 | ❌ 违反（已知根因） |
| 未用成交价时金额 = 套餐总价 | ✅ 成立 |
| 加购明细的商品存在且未软删 | ✅ 成立（入口就 404 拒了，结构性保证） |

### 5.16 ORM 查询放大：N+1 的精确计数

「索引不可用」是机制，「每多一条数据多几次查询」才是后果。精确计数需要
**statement 级**观测 —— 事务级计数（`xact_commit`）做不到，一个 Spring 事务里
多少条 SELECT 都只 +1。

做法：**克隆正在跑的容器**（镜像/网络/环境变量全部从 `docker inspect` 现场读，
密码不落仓库），加 `-Dspring.jpa.properties.hibernate.generate_statistics=true`，
每个请求结束会打一段 `Session Metrics { ... spent executing N JDBC statements ... }`。
`pilot/probe_instance.py` 是这个克隆机制的可复用封装。

测出来的斜率（用**分类互不相同**的商品，否则同分类商品命中持久化上下文缓存，
分类查询被省掉，斜率被系统性低估）：

| 场景 | 语句数 | 斜率 |
|---|---|---|
| 下单（1/2/4 个明细） | 6 / 8 / 12 | **恰好 2.0 条/明细** = 1 次商品查询 + 1 次 LAZY 分类查询 |
| 套餐列表（pageSize 1/2/4） | 15 / 26 / 39 | **恰好 8.0 条/套餐** = 1 + 1 + 3 明细 × 2 |

### 5.17 配置失败模式：配错之后是 fail-open 还是 fail-closed

`@Value` 决定行为的配置项，配错时往哪个方向坏，决定它是配置问题还是安全问题。
同样用克隆实例逐项改配置、用真实请求观察。

**本轮最反直觉的一条**：把 CORS 白名单**清空**，跨域不是被收紧，而是**被放开** ——
实测 `Access-Control-Allow-Origin: *`。

机制：`WebConfig` 把配置串拆开、滤掉空项 → 空数组 → 传给 `allowedOriginPatterns(...)`；
而 Spring 的 `CorsRegistration` 默认先 `applyPermitDefaultValues()`（allowedOrigins = 单星号），
空数组**没有把它清掉** → 最终任何来源都放行。

> 运维的直觉是「清空白名单 = 关掉跨域」，实际拿到的是「对全世界开放跨域」。
> 叠加零鉴权：任何网页都能读全部业务数据。
>
> **我读代码时的推断是「空数组 → 不加 CORS 映射 → 全拒」，实测推翻了它。**
> 这正是「差分出单」的价值 —— 静态推理会错，实测不会。

### 5.18 涌现裁决器：把「个体对、合起来错」变成可判定的结论

> 这一节是 §4.2 判定式的落地。设计见 `DESIGN.md` §4，实跑结果见
> `demos/shop/EMERGENCE_RESULT.md`。

**判定式**：`F(s)` = 以注入集合 s 执行链的步骤后，目标不变式是否被违反；
`EMERGENT(c) ⟺ F(D)=1 且 ∀d∈D: F({d})=0`。

**七种裁决**（`pilot/engine/emergence.py`，优先级从高到低）：

| verdict | 条件 | owner | category |
|---|---|---|---|
| `FALSIFIED_CHAIN` | 基线就违反 / 合起来也不违反 / 没有活着的注入 | 不开单 | — |
| `EMERGENT_MASKING` | 单注触发、成对组合后症状反而消失 | **掩盖者的层**(own) + integration(cited) | `integration.emergent_masking`（契约新增） |
| `EMERGENT_CONJUNCTIVE` | 合起来错、单独都不错 | integration(own) | `integration.end_to_end_business` |
| `EMERGENT_CONJUNCTIVE_CORE` | 同上，但 leave-one-out 求出**更小**的核心集 | integration(own) | `integration.end_to_end_business` |
| `EMERGENT_AMPLIFIED` | 合起来错且偏离量 ≥1.5× 单注 | integration(own) | `integration.cross_service_performance` |
| `NOT_EMERGENT_ADDITIVE` | 单注也能触发，且违反的不变量相交 | 已有根因 | 集成层不开新单 |
| `NOT_EMERGENT_INDEPENDENT` | 单注也能触发，且违反的是互不相交的不变量 | 各低层 | 集成层不开单 |

**工程前提：注入是条件注入，不是代码变异。** 被测系统只读，裁决器只换输入与上下文：
目录里 `mutation.kind` 只有 `data`（数据行）/ `request`（请求字段）/ `instance`（装配上下文）。

**活性门控**：每条注入都要过 `alive_check` —— 单独施加它，能在它自己的层上看到症状
= 活；看不到 = 死。**死的注入被剔除出分母并告警**（不许静默），三态降级为 `mutant_survivor`。

**三态**：`verified`（实测确认）/ `unverified`（判不了：基线已违反、执行失败、无活着注入）/
`mutant_survivor`（判出来了，但有注入在这个环境里是死的）。

**引擎 / 项目分离**：`pilot/engine/emergence.py` 零项目字面量，只判定；
项目侧提供场景执行器（怎么注入、怎么跑、怎么读不变式）。这样「同一份观测 → 同一份裁决」
可以脱离被测系统复算。

**实跑（demo-shop）**：`EMERG-QUOTA-LEDGER-DIVERGENCE` 判 `EMERGENT_CONJUNCTIVE` ——
额度按报价查（900000）、账按实付记（810000）两个单点各自都对，
组合起来才出现「额度被占住、账上没有对应账目」。详见 `demos/shop/EMERGENCE_RESULT.md`。

### 5.12 通用 / 配置 / 实现 三层切分（这套系统怎么才算「通用」）

**切错边界是最大的浪费**：把 agent 也做成通用 → 变成通用扫描器（本项目明确不要）；
什么都不切 → 就是「一个项目的脚本集」，换不了项目。本项目的切法：

| 层 | 内容 | 换项目时 | 位置 |
|---|---|---|---|
| **通用** | 层契约 `layers.yaml` | 不改 | `layered-audit/layers.yaml` |
| **通用** | 收口引擎：schema 校验 / 归一化 / 根因聚合 / 覆盖度矩阵 | **不改** | `pilot/engine/` |
| **配置** | 项目绑定：源码根、包路径、证据源清单、标注表 | 改这个文件 | `pilot/target.json` |
| **配置** | 人工标注表：category / claim / owner / confidence / root_cause_key | 换一份 | `pilot/finding_meta.py` |
| **实现** | 各层 agent 探针脚本（`fn_*` / `file_*` / `contract_*` / `class_*` / `integration_*` / `perf_load.py`） | 重写 | `pilot/` |
| **实现** | **载体**：层与运行时之间的那层（反射直调 / 字节码 / HTTP 契约 / 三方对账） | **按语言重写** | 各层脚本 |

**agent 层绑定具体靶区不是缺陷** —— agent 本来就该打具体靶区。硬把它通用化，
就成了「换个项目就自动生成审计」的通用扫描器，那是另一个产品。

**但「载体」是另一回事**：载体是语言专属的（Java 的反射直调、字节码、HTTP 契约，
换成 Python/Go 就得重写）。所以「通用」的真正含义是：
**契约与收口引擎通用，载体按语言重写**。载体要覆盖哪些运行时特性、
看不见哪些，写在 `target.json` 的 `carriers` 段里，由特性覆盖矩阵守着（见 §5.14）。

**通用性怎么被守住（不是靠自觉）**：

| 检查 | 做什么 | 跑法 |
|---|---|---|
| `engine/check_engine_purity.py` | 引擎目录不许出现：项目名、绝对路径、`docker exec`、`psql`、HTTP 客户端、内网地址 | 流水线里自动跑，退出码 1 = 阻断 |
| `test_portability.sh` | 换一份 target.json（不同项目名 / 不同源码根 / 不同证据源 / 不同标注表副本）跑一遍引擎，验证产物落在新目标目录且不碰原产物 | 流水线里自动跑 |

`test_portability.sh` 造的是一个**合成目标**（`acme-banquet`），不是真第二个项目。
它验证的是「引擎不依赖 jiuyan 的任何东西」，**不**验证「换个真实项目就能跑」——
后者还需要为新项目重写 agent 脚本，是另一件事，别混淆。

**换项目的实际步骤**（引擎一步都不用改）：

```bash
cp -r pilot/ targets/acme-banquet/          # 复制项目目录
cd targets/acme-banquet
# 1. 写 target.json（源码根 / 包路径 / 证据源清单）
# 2. 写 finding_meta.py（本项目的 finding 标注）
# 3. 重写各层 agent 脚本
# 4. 跑：python3 engine/normalize_findings.py && python3 engine/aggregate_findings.py
```

### 5.9 规划 vs 实现（差距，写出来免得下一个人类 AI 误判）

| `layers.yaml` 规划 | 当前实现 | 差距 |
|---|---|---|
| 9 个 agent 编排（function 1 / file 1 / class 2 / service 4 / integration 1），max 12，有 token 预算 | 22 个 Python 脚本串行跑，每层 = 静态 + 动态 + 差分 | 语义等价，但**没有 agent 化、没有并发、没有 token 预算控制**；类层的 2 个异质 agent、service 的 4 个分组 agent 都被压成单脚本 |
| 统一证据 schema（10 字段） | 每层自定义字段，0/10 落地 | 见 §5.4 表格 |
| `ground_truth`（verified / unverified / mutant_survivor） | 无 | 没有变异测试，缺陷无法判定「是否被变异体存活」 |
| `confidence` 0~1 | 无 | 无法按置信度排序/过滤 |
| `category` 按层分类 | 无 | 无法做「某类问题零覆盖」的自检 |

---

## 6. 脚本矩阵（层 → 脚本 → 产物）

全部脚本在 `pilot/`，一律 `python3 <脚本>` 单跑，无参数。

| 层 | 静态 | 动态实测 | 差分出单 | 产物 |
|---|---|---|---|---|
| 函数层 | `fn_static.py` | `fn_dynamic.py` | `fn_diff.py` | `fn_declaration.json` / `fn_observation.json` / `fn_findings.json` |
| 文件层 | `file_static.py` | `file_dynamic.py`（**通用 harness**：`file_harness/GenericFileHarness*.java` + `file_http_probe.py`，用例在 `file_cases.json`） | `file_diff.py`（**仍项目专属**，未改） | `file_declaration.json` / `file_observation.json` / `file_findings.json` / `file_detectors.json` |
| 服务层 | `contract_static.py` | `contract_dynamic.py` | `contract_diff.py` | `declaration.json` / `observation.json` / **`findings.json`**（注意不叫 contract_findings.json） |
| 类层 | `class_static.py` | `class_contract.py` | `class_dynamic.py` → `class_diff.py` | `class_declaration.json` / `class_contract.json` / `class_observation.json` / `class_findings.json` |
| 集权层 | `integration_static.py` | `integration_dynamic.py` | `integration_reconcile.py` → `integration_chain.py` → `integration_diff.py` → `integration_gate.py` | `integration_declaration.json` / `integration_observation.json` / `integration_reconciliation.json` / `integration_chains.json` / `integration_findings.json` / `integration_gate_report.md` |

其它：`openapi.json`（OpenAPI 快照，服务层输入）、`jiuyan-layer-manifest.yaml`（每层状态 / 归属 pattern / 回归资产台账）、`cleanup.sh` + `cleanup_uploads.py`（收尾）。

### 6.2 项目侧检测器（不是引擎，按语言/框架各写各的）

| 脚本 | 干什么 | 产出 |
|---|---|---|
| `perf_load.py` | 阶梯放大商品表，量端到端延迟曲线 + EXPLAIN 归因 | `perf_findings.json` |
| `numeric_props.py` | 前端金额换算与金额输入规范化的确定性随机性质 | `numeric_findings.json` |
| `concurrency_props.py` | 并发写丢更新 / 事务原子性 / POST 幂等 | `concurrency_findings.json` |
| `java_feature_scan.py` | Java/Spring 特性：切面失效点 / 事务内异步逃逸 / 对外暴露 JPA 实体 / DI 形态 / RPC 入口 / 动态配置 | `feature_findings.json` |

### 6.3 负载实测脚本

| 脚本 | 干什么 | 产物 |
|---|---|---|
| `perf_load.py` | 阶梯放大 `products`（探针行 `ai_probe_perf_` 前缀，写完即删），量端到端 HTTP 延迟曲线 + `EXPLAIN (ANALYZE, BUFFERS)` 归因，判定「延迟塌陷」假设 | `perf_load.json` / `perf_load.md` / `perf_findings.json` |

`perf_findings.json` 是**第 6、7 号证据源**（服务层 1 条 + 集成层 1 条），
`normalize_findings.py` 会读它，所以负载实测的结论直接进总表和覆盖度矩阵。
跑法：`python3 perf_load.py --scales 0,2000,10000,50000 --rounds 10`

### 6.1 通用收口引擎（`pilot/engine/`，换项目零改动）

| 脚本 | 输入 | 输出 | 作用 |
|---|---|---|---|
| `findings_schema.py` | `layers.yaml`（路径来自 target.json）+ `target.json` | — | 解析契约 + 读项目绑定 + schema 校验（不硬编码类别） |
| `normalize_findings.py` | `target.json` + 五层 `*_findings.json` | `all_findings.json` | 统一 10 必填字段，退出码 1=结构性违规 2=契约缺口 |
| `aggregate_findings.py` | `all_findings.json` | `root_causes.json`、`audit_report.md` | 根因归并（88→53）、归属/越界校验、覆盖度矩阵 |
| `feature_coverage.py` | `target.json` 的 carriers/features + 各项目侧检测器 | `feature_coverage.json`、`feature_coverage.md` | 特性 × 载体覆盖矩阵 + 盲区清单 + 受影响结论数 |
| `check_engine_purity.py` | `target.json` + 引擎源码 | — | 引擎纯净度守卫：出现项目字面量就退出码 1 |
| `emergence.py` | 注入目录（项目配置）+ 场景执行器（项目侧） | 裁决结论 + finding | **涌现裁决器**：F(注入集合) → 七种裁决之一（合起来错/单独对 = 涌现）；活性门控 + leave-one-out 求最小核心集 |
| `emergence_candidates.py` | 关系网 `relation_net.json` + 不变式声明 +（可选）注入目录 | `emergence_candidates.json` | **涌现候选链生成器**：八条剪枝硬约束（链首 HTTP_ENTRY / 深度 ≤4 / 无环 / 每写汇 ≤3 路径 / 三元组去重 / 必含写汇 / **必锚定已声明不变式** / 全局 ≤500）+ 枢纽度四维合成 + Monte-Carlo 级联估计（2000 采样，固定种子）+ 配额与集中度防塌缩 + 未跑链全记录 + 对已知链算召回 |
| `emergence_gate.py` | 注入目录 + 裁决产物 + `all_findings.json` + 基线 | `emergence_gate.json` / `.md` | **涌现 FDR 门禁**：分母 = 目录 active=true 减活性不过的（告警并记录，不静默剔除）；分子 = own 单正确归属最低层（只被引用不算检出）；输出 FDR_inj / Attr_acc / 分母哈希 / 集合哈希 / 分类别 FDR / 漏检清单；退出码 0 绿 / 1 红 / 2 没查完；基线 `pilot/emergence_baseline.json` 按目标分节 |

配套：`pilot/target.json`（项目绑定，含 `carriers` / `features` / `repro_templates`）、
`pilot/finding_meta.py`（人工标注表，含 `CLASS_FILE_HINTS` / `COLUMN_FILE_HINTS` 定位提示）、
`pilot/test_portability.sh`（换绑定可跑冒烟测试）、
`pilot/java_feature_scan.py`（Java/Spring 特性检查器）。

**注意引擎不许写死项目脚本名**：复现命令模板也在 `target.json` 的 `repro_templates` 里。
这条是被 `check_engine_purity.py` 抓出来的 —— 守卫在替我们守自己的边界。

`aggregate_findings.py` 的判定口径（都写死在代码里，别改口径不改文档）：

- **重复开单**：同层同一 `root_cause_key` 开多条 `own` → 归并提示（修一次即可，不是跨层违规）。
- **跨层重复开单**：同一根因多层都声明 `own` → 硬违规。
- **越界开单**：开单层的 category 落在自己的 `out_of_scope` 里 → 硬违规。
- **归属冲突**：声明的 owner ≠ 能复现的最低层，且契约 `escalation_to` 不允许该上提 → 硬违规。
- **合法上提**：owner 在上提链上 → 记为说明，不算违规。
- **跨层佐证**：高层 `relation=cited` 引用低层根因（链级后果归高层）→ 合法。

---

## 7. 怎么跑

```bash
cd /Users/june/.cc-weixin/workspace/layered-audit/pilot

bash run_pipeline.sh                    # 五层 → 清理 → 门禁；exit 0 = 全绿
python3 integration_gate.py             # 只跑门禁（比对基线）
python3 integration_gate.py --refresh   # 先重跑对账与攻击链，再比对
python3 integration_gate.py --accept    # 门禁通过后接受新基线（改了前端/DTO 且确认是预期改动时用）
python3 integration_gate.py --init      # 首次建立基线

python3 normalize_findings.py           # 只跑归一化（exit 1 结构性违规 / 2 契约缺口）
python3 aggregate_findings.py           # 只跑根因聚合 + 覆盖度矩阵
python3 aggregate_findings.py --strict  # 有硬违规时退出码 1（CI 用）
python3 fn_dynamic.py --case RFP-05     # 单用例重放（repro_cmd 用它）
python3 perf_load.py                    # 负载实测（延迟曲线 + 分页 total 开销）
```

流水线**有并发锁**（`/tmp/layered_audit.lock`）：各层脚本共用 `/tmp/jiuyan_fn`、
`/tmp/jiuyan_file` 这些固定工作目录，两个流水线同时跑会互相覆盖 class 文件，
表现为 `javap` 随机失败。第二个进程会直接退出码 2 并提示。

门禁**判红**（退出码 1）三类：
① 基线 ALIGNED 的对账项这次不是 ALIGNED；
② 新增 `VALUE_MISMATCH` / `SEMANTIC_MISMATCH` / `FRONTEND_MISSING`；
③ 攻击链由未确认转为 `CONFIRMED`。

---

## 8. 关键设计决策（接手别推翻）

1. **第三个来源是前端线上同款 mapper，不是复刻。** 复刻前端逻辑的结论一律不作数。
2. **门禁的键必须结构化。** 对账表 111 行里有 24 组「实体.字段」撞车（列级行 vs 语义级行、
   `DERIVED` vs `FRONTEND_IGNORES`）。键形如 `实体.字段` / `实体.字段@semantic:用途` /
   `实体.字段@derived|ignored|missing`。用朴素的「实体.字段」会让基线把 111 行折叠成 87 行，
   回归直接被吃掉（已踩过）。
3. **攻击链回归用例断言「应该发生什么」，不是「现在发生什么」。** 所以今天它们全部失败并标
   `xfail(strict=True)`；缺陷修好后 XPASS 变红，逼人回来摘标记。
4. **归属规则**：owner = 能给出可复现证据的最低层；高层只引用不重复开单。

---

## 9. 铁律（务必遵守）

- **不许改 `/Users/june/jiuyan` 下任何文件**（含前端源码）。它只被读取。
- 探针数据前缀 `ai_probe_`，写完即删；DB 直写探针必须包在 `BEGIN; ...; ROLLBACK;` 里。
- 收尾清理：`bash pilot/cleanup.sh`（删 `ai_probe_` 前缀行 + 删未被引用的非种子上传文件）。
- 不在代码 / 日志 / 报告里输出 API key、token、密码、JWT。
- 提交前先展示 diff 等人确认，不自动 push。
- 代码里不用 emoji。

---

## 10. 回归资产（另一个仓库 `/Users/june/tests/auto-api`，**已提交** `3b0fae6`）

| 文件 | 行数 | 作用 |
|---|---|---|
| `tests/probe.py` | 198 | 三方取值（DB/API/前端 mapper）+ 前端契约静态抽取 + `ai_probe_` 清理 |
| `tests/fe/fe_loader.mjs` `fe_register.mjs` `frontend_mapper.mjs` | 42 | 前端 mapper 运行时（与 pilot 同款） |
| `tests/test_integration_alignment.py` | 215 | 7 条闭合不变量，**今天全绿**（前端必读字段齐全 / 金额单位 / 时区 / 快照键 / itemCount / 状态枚举 / 抽取器自检） |
| `tests/test_attack_chains.py` | 207 | 7 条攻击链，`xfail(strict=True)` 钉住已知缺陷 |

跑法：`cd /Users/june/tests/auto-api && python3 -m pytest -q` → 上次结果 **70 passed / 8 xfailed**。

`tests/probe.py` 的前端契约是**静态抽取**的（从 `banquetApi.js` 现场解析 mapper 读了哪些字段），
前端一改期望值自动跟着变，不会变成「拿旧期望值糊弄过去的假门禁」。

**通用性说明**：这套回归资产的**写法**（三方对账 + 攻击链 + `xfail(strict=True)` 钉缺陷）
是通用的模板；**实现**是 jiuyan 专属的（探针表名、端点、前端 mapper 路径）。
换项目照着这个模板重写一套，不要指望改配置就能用 —— 那正是 §5.12 里「实现」层的定义。

---

## 11. 剩余工作

### A. 工程待办（已发现，可直接做）

> **详细的架构设计、6 个并行设计 Agent 的交叉结论、以及每项的落地增量见 `DESIGN.md`。**
> **目标与进度见 `CLAUDE.md`（下一个接手的人先读它）。**

| # | 事项 | 现状 | 建议 |
|---|---|---|---|
| A0 | `layered-audit/` 版本控制 | ✅ **本轮完成**：`git init` + `.gitignore`（运行产物不入库），待确认后提交 | 首次提交后，`layers.yaml` 的契约变更才有地方提 |
| A1 | 提交 harness 仓库的新增文件 | ✅ **本轮完成**：commit `3b0fae6`（4 新文件 + README，692 行；`test:` 前缀；已扫敏感信息 0 命中；未 push） | — |
| A2 | 门禁挂 CI / pre-commit | 未做 | 做：让「改前端 mapper 或后端 DTO」必须过门禁再进主干 |
| A3 | `owner` 字段补标 | ✅ **本轮完成**：86/86 条都有 `owner{layer,relation,basis}`，见 `all_findings.json` | — |
| A4 | 服务层重复开单 | ✅ **本轮完成**：按 `root_cause_key` 归并，86 条发现 → **52 条根因**（25 条 `INTERNAL_LEAK` 收敛成 1 条） | — |
| A5 | 性能类假设 | ✅ **本轮完成**：`perf_load.py` 阶梯压测到 5 万行，判定 `PARTIALLY_CONFIRMED`（机制成立、后果未兑现），见 §5.10；同时挖出「分页 total 是纯开销」新根因 | — |
| A7 | 回归套件自身漏残留 | ✅ **本轮完成**：harness 造数用运行级 tag `ai_<时间戳>_<uuid6>`，但 `ResourceRegistry.cleanup` 走 API DELETE（软删，行还留在表里）、`make_order` 造的订单没人管 → 跑一次 pytest 留 33 行。已加 session 级按 RUN_TAG 硬删 + `pilot/cleanup.sh` 前缀从 `ai_probe_` 放宽到 `ai_` | — |
| A6 | 契约缺口的处置 | 9 条发现 `category=UNMAPPED`（file 层 6 / class 层 2 / integration 层 1），`normalize_findings.py` 退出码 2 | **要人决策**：要么修订 `layers.yaml`（file 层加「安全策略 / 可信输入」类别），要么把发现挪到真正该开的层 |

### B. 项目层面待办（架构缺口，工作量大）

> 「通用」的切法见 §5.12：通用的是契约 + 收口引擎，配置的是项目绑定与标注表，
> 实现的是各层 agent 脚本。**别把 agent 也做成通用**，那是另一个产品。

| # | 事项 | 说明 | 建议顺序 |
|---|---|---|---|
| B1 | 证据 schema 落地 | ✅ **本轮完成**：`findings_schema.py` + `finding_meta.py` + `normalize_findings.py`，10 必填字段 86/86 结构齐备 | — |
| B2 | 字段统一与聚合器 | ✅ **本轮完成**：`aggregate_findings.py` → `root_causes.json`（52 条根因）+ 硬违规判定 | — |
| B3 | 覆盖度自检 | ✅ **本轮完成**：layer × category 命中矩阵，零覆盖即警报（当前 10 个类别零覆盖，见 §12.2） | — |
| B4 | **9 agent 编排化** | 规划 9 个 agent、并发、token 预算；当前是 22 脚本串行 | 先做类层（2 agent）和服务层（4 agent）的拆分，验证编排收益再推广 |
| B5 | **变异测试 / ground_truth** | `ground_truth`（verified / unverified / mutant_survivor）无实现 | 函数层最适合做（mutation_test 已在 oracle 里），是打通 schema 的试验田 |
| B6 | **推广到第二个项目** | 🟡 **接缝已补（本轮）**：引擎零项目字面量 + `target.json` 绑定 + 纯净度守卫 + 换绑定冒烟测试。**但还没在真实第二个项目上跑过** —— 那需要为新项目重写 agent 脚本 | 等有第二个项目时做；agent 脚本本来就该重写，不是「通用性缺口」 |
| B7 | **被测系统自身修复闭环** | 发现 6 条 CRITICAL，但没走「修复 → 门禁转绿 → 摘 xfail」流程 | 挑 1 条（推荐 `NO_AUTHZ`，收益最大）走完整闭环，验证回归资产真的能守住 |
| B8 | **契约缺口的结构性修法** | 9 条发现装不进现有 category，靠 `UNMAPPED` 标记 | 不是加类别就完事：要能区分「类别表太窄」与「发现开错层」，后者靠 `out_of_scope` 校验自动判（已实现 1 例） |
| B10 | **涌现裁决器 + 候选链生成 + FDR 门禁** | ✅ 裁决器；✅ 候选链生成 + FDR 门禁（demo 实测召回 1/1、FDR_inj=0.500、漏检 INJ-PROFILE-VIP）；✅ **已接进流水线**（demo 全链；jiuyan 如实报「没查完」）。**还缺**：schema 驱动的造数器（候选只提出条件槽，场景还是人给的）、七个模式只命中 T3、**jiuyan 尚无注入目录** | 先给 jiuyan 写注入目录（需要真的找出 jiuyan 的涌现链，不能编），再造数器 |
| B11 | **服务层差分判定层通用化** | ✅ **已完成**：`contract_diff.py` 的 4 处项目耦合清零（jiuyan 的 10 条 finding 逐条一致）；业务不变式的判定层抽成通用引擎 `engine/business_verdict.py`；**demo-shop 的服务层已跑起来**（3 个差分场景）→ 涌现门禁的漏检归因补上了，FDR 0.500 → **1.000，门禁 RED → GREEN** | — |
| B12 | **两个可执行依赖图构建器并存** | ✅ **已合并**（原来的判断是错的）：当初以为「通用 151 边 vs 项目专属 414 边」是能力差距，其实**项目专属那版的 DERIVES 边是错的** —— 它把一次 setter 调用连到实体的**所有**字段（321 条边的来源），通用版只连到被设的那一个。污点 / READS / WRITES / DERIVES 通用版一样不少，且产物真的被候选生成器消费。⇒ `pilot/emergence_graph.py` 已退役，调度任务改用 `engine/relation_net.py` | — |
| B15 | **函数层 / 文件层的差分判定层通用化** | ✅ **已完成**：`engine/fn_diff.py` + `pilot/fn_groups.json`、`engine/file_diff.py` + `pilot/file_findings_config.json`；**等价性**：jiuyan 的 fn/file findings 的 JSON 与 Markdown **逐字节一致**。**连带后果也解决了**：demo 的函数层用例分 4 组（PRICING/QUOTA/ORDER/LSP），与 jiuyan 的 8 组**完全不重叠** —— 原来即使把 demo 的函数层跑起来也开不出任何单 | — |
| B16 | **跨仓库的涌现链** | 🟡 跨仓库**探针**通了（面清单 + 跨仓库图 + 契约漂移 + 归属），跨仓库**缺陷**也开出来了（文件层 own 的「汇率兜底量纲错」）；但**跨仓库的涌现**（两个仓库的条件组合才出问题）没做 —— 关系网目前只画单仓库 | 候选链生成器换一张跨仓库的图：把另一个仓库的入口并进关系网，链的模板要能跨 HTTP 边界 |
| B18 | **模型轨的任务形态** | 🟡 已跑通：问「**这个方法应该拒绝什么**」（错误码可枚举）产出的断言可用 —— 2 条经人工复核冻结为回归守卫；问「应该返回什么」只产出一堆 `not_null`。**下一步**：把这个形态推广到更多单元（现在只挑了 7 个写方法），再试「输入应该满足什么不变式」这一类任务形态 | `pilot/SEMANTIC_MODEL_TRACK.md` 有完整的 6 轮实验记录 |
| B17 | **demo 的用例断言的是现状而不是应有行为** | ✅ **已修**（demo 的文件层两条用例）：断言现状的用例永远 PASS，也就永远开不出单。改成断言应有行为后，两条用例变 FAIL，demo 拿到 2 条真实发现 | 其它目标照此自查：每个用例的 `expect` 是不是「应该是什么样」 |
| B13 | **跨仓库涌现** | 🟡 跨仓库探针骨架已通（面清单 / 跨仓库图 / 契约漂移 / 归属，实测 2 仓库 1 条确认边 0 漂移）；**跨仓库涌现（两个仓库的条件组合才出问题）没做** | 与 B10 同一条线：候选链生成器目前只在单仓库的关系网上找链，跨仓库链是同一个机制换一张图 |
| B14 | **demo 的 3 条覆盖缺口** | 🟡 文件载体看到、没人认领：`profile_assembly` ×2（@Profile 互斥装配的静态形态）、`outbound_http` ×1（FxClient 里写死了 `http://127.0.0.1:8098`）。列在 `demos/shop/artifacts/class_carrier_gap.json` | `profile_assembly` 可以补成类层的静态判定（同一接口多实现用 @Profile 互斥装配 —— 业务维度被绑到部署维度）；`outbound_http` 属于跨仓库配置面 |
| B9 | **`repro_cmd` 从 reference 升级成可跑** | ✅ **本轮完成**：14 → **3 条**（函数层 `--case`、集成层 `--scenario` / `--chain`）。剩下 3 条是操作步骤描述或数据文件指针，不硬编 | 剩余 34 条 `layer` 级复现（file / class 层）还可继续降维到用例级 |

### C. 明确暂缓（别做）

- 不要把 22 个脚本改写成通用框架 —— 现在只服务一个系统，过早抽象会拖慢。
- 不要引入新测试框架 —— 仓库已用 pytest，回归资产留在 `auto-api` 仓库。
- 不要动 `/Users/june/jiuyan` —— 被测系统只读。

---

## 12. 当前缺陷台账（速查；完整明细看各层 `*_findings.json`）

| 层 | 条目 | 产物 |
|---|---|---|
| 函数层 | 15 | `fn_findings.json` |
| 文件层 | 18 | `file_findings.json` |
| 服务层 | 27 | `findings.json` + `perf_findings.json` |
| 类层 | 25 | `class_findings.json` |
| 集权层 | 15 | `integration_findings.json` + `perf_findings.json` |
| **合计** | **100 → 根因 72**（归并 28） | `all_findings.json` + `root_causes.json` |

> 分布与严重度逐条见 `audit_report.md`；`owner` 100/100 齐备，
> `ground_truth` verified 97 / unverified 2 / mutant_survivor 1。
> 这一轮类层从 16 条涨到 25 条（接入 `class_shape` 通用探针，新开 7 条真实发现）。

### 6 条 CRITICAL

| 归属 | ID | 一句话 |
|---|---|---|
| 函数层 | `FN-PRICE-CLIENT-CONTROLLED` | 成交价只做下界检查，不与套餐价比核对 |
| 函数层 | `FN-UPLOAD-NO-CONTENT-SNIFF` | 上传只信自报 contentType，任意字节以 `.png` 落盘 |
| 服务层 | `NO_AUTHZ` | 全系统零鉴权，无凭证可读写全部业务数据 |
| 集成层 | `INT-ANON-WRITE-REWRITES-SNAPSHOT` | 匿名写入连带重写订单快照 |
| 集成层 | `INT-UPLOAD-ANONYMOUS-READBACK` | 落盘字节匿名可原样下载 |
| 集成层 | `PRICE_CLIENT_CONTROLLED_XL` | 同一订单并存 0.01 元与 468.00 元两个价 |

### 12.1 根因视角（`root_causes.json` / `audit_report.md`）

86 条发现按 `root_cause_key` 归并成 **52 条根因**，修复工作量按 52 计，不是 86。

| 根因 | 严重度 | 归属层 | 归并条数 |
|---|---|---|---|
| `authz.no_authorization_layer` | CRITICAL | service | 3（service 1 + integration 2 条佐证） |
| `price.final_price.upper_bound_unvalidated` | CRITICAL | function | 3（function 1 + integration 2 条佐证） |
| `upload.content_type_not_sniffed` | CRITICAL | function | 2 |
| `errhandler.internal_detail_leak` | MEDIUM | service | **25** |
| `errhandler.client_error_mapped_to_500` | HIGH | service | 2 |
| `jackson.lenient_type_coercion` | MEDIUM | service | 2 |
| `order.item_quantity_unvalidated` | HIGH | function | 2 |
| `order.snapshot.item_ownership_unchecked` | HIGH | function | 2 |
| `column.default_declared_in_three_places` | LOW | class | 2 |

### 12.2 覆盖度：5 个类别零命中

| 层 | 零覆盖的 category | 契约给的 oracle | 状态 |
|---|---|---|---|
| function | `complexity` | `static_analysis` / `benchmark` | 未做 |
| class | `inheritance_design` | `static_analysis` / `property_based_test` | 未做 |
| service | `resource_leak` | `load_test` | 未做 |
| integration | `cross_layer_correlation` | `graph_correlation` | 未做（机制已在聚合器里，见下） |
| integration | `emergent_masking` | `e2e_scenario` / `differential_injection` | 🟡 涌现裁决器 + FDR 门禁已做，**但只对 demo-shop 生效**；jiuyan 没有注入目录，这一维对它仍是「没查完」（流水线里如实报 INCOMPLETE） |
| ~~function~~ | ~~`numeric`~~ | `property_based_test` | ✅ 补上（`numeric_props.py`，3 条） |
| ~~service~~ | ~~`data_consistency`~~ | `concurrency_test` | ✅ 补上（`concurrency_props.py`，2 条） |
| ~~service~~ | ~~`resilience`~~ | `chaos_experiment` | ✅ 补上（幂等性，1 条） |
| ~~service~~ | ~~`service_latency`~~ | `load_test` | ✅ 补上（`perf_load.py`，1 条） |
| ~~integration~~ | ~~`cross_service_performance`~~ | `e2e_load_test` | ✅ 补上（1 条） |
| ~~class~~ | ~~`encapsulation_leak`~~ | `mutation_test` | ✅ 补上（1 条，`java_feature_scan.py`） |

关于 `integration.cross_layer_correlation`：机制**已经实现**（聚合器会归并同源根因、
报告 5 处跨层佐证、`audit_report.md` 有「跨根因关联」段）。它没命中不是「没人查」，
而是那 5 处跨层同源**同时都有端到端后果**，按契约该归 `end_to_end_business` /
`cross_service_attack_chain`。**不为了把矩阵填绿而单开一条 finding** —— 那是自欺。

零覆盖 ≠ 没问题，只代表**这个类别这一轮没人查**。契约里给这些类别配了 oracle
（`chaos_experiment` / `concurrency_test` / `attack_graph` …），但对应的
agent 还没写 —— 这是 §11-B4 编排化的优先级依据。
**上表同时说明：覆盖度矩阵不是摆设，本轮就靠它挑出了要补的类别。**

### 12.3 负载实测结论（本轮新增）

| 阶梯（表内商品数） | 带 keyword 端到端 p50 | 不带 keyword p50 | count(*) 单条 | count 扫描 |
|---|---|---|---|---|
| 30 | ~6 ms | ~7 ms | 0.02 ms | Seq Scan |
| 2 030 | 9.3 ms | 7.1 ms | 0.53 ms | Seq Scan |
| 10 030 | 19.1 ms | 8.4 ms | 2.04 ms | Seq Scan |
| 50 030 | 35.5 ms | 11.6 ms | 11.03 ms | Seq Scan |

- **判定**：`PARTIALLY_CONFIRMED`。数据量 1668× 时延迟只涨 6.2×，每行成本 0.0007ms，
  折算约 **140 万行**才触及 1 秒预算 —— 机制（索引不可用、全表扫描）成立，
  后果（用户可感知的塌陷）在真实业务规模下不成立。
- **新根因** `pagination.count_total_computed_but_discarded`（MEDIUM，owner=service）：
  前端三个 list 函数都丢弃 `total`，后端每次列表请求却要付一次 count(*) 全表扫描，
  5 万行时占端到端延迟 31%（带 keyword）/ 40%（不带 keyword）。**100% 浪费。**
- **类层那条 `INDEX_UNUSABLE_FOR_QUERY_PATTERN`（HIGH）风险等级建议下调** ——
  机制是真的，但当前规模不产生用户可感知的伤害。

### 12.4 硬违规 1 条 + 归并提示 4 条 + 契约缺口 9 条

- **硬违规**：`index.idx_products_name_unusable` —— `service_latency` 明确在 class 层的
  `out_of_scope` 里，这条应由 service 层开单（class→service 的归属上提本身合法，但开单位置越界）。
- **归并提示**（不是违规，是「修一次即可」）：`errhandler.internal_detail_leak` ×25、
  `errhandler.client_error_mapped_to_500` ×2、`jackson.lenient_type_coercion` ×2、
  `column.default_declared_in_three_places` ×2。
- **契约缺口** 9 条（`category=UNMAPPED`）：file 层 6（安全策略类：过宽 CORS / 信任 XFF /
  客户端可控 traceId / 3 条相关正向结论）、class 层 2（默认值声明三处）、integration 层 1
  （覆盖度记录缺口）。处置方案见 §11-A6，**需要人决策是否修订 `layers.yaml`**。

### 集权层 v2 的三项结论

- 对账 **111 项**：ALIGNED 52，其余为 API_OMITS / DERIVED / FRONTEND_IGNORES / FRONTEND_MISSING / VALUE_MISMATCH / SEMANTIC_MISMATCH。
- **5 条攻击链全部 CONFIRMED**（见 §5.7）。
- 已闭合的不变量：金额单位分→元、时区 +08:00→UTC→Asia/Shanghai、快照键 DB↔API↔前端一致、`itemCount` 与 DB 行数一致、状态枚举 3/3。

---

## 13. 不确定 / 未验证的（别当结论用）

- 「端到端延迟随数据量塌陷」只到 `PARTIALLY_CONFIRMED`：索引在类层实测不可用，集成层未复现延迟塌陷。
- `ProductController` **没有** `GET /api/products/{id}`（商品只能列表读），集成层对账因此走 `GET /api/products?keyword=`。
- `packages.banquet_type` 无 DB 外键，但服务层会校验 `package_types`；`orders.banquet_type` 是自由文本。
- `orders` 表无软删列；`products`/`packages`/`categories`/`package_types` 有。
- 前端 `mapProduct.image = imageUrl || photo`，兜底图取的是**套餐**种子图（`/uploads/seed/packages/package-01.jpg`），属已知 MEDIUM 缺陷。

---

## 14. 最近一次验证记录（2026-11-08）

**第六轮：属性绑定优先级探针 —— 抓到一个真实的部署陷阱**

- 新探针 `pilot/property_binding_probe.py`：对每个属性，把各种绑定形式
  （环境变量 / 宽松绑定变体 / `SPRING_APPLICATION_JSON` / 系统属性）
  **同时**设成可区分的值，再**行为观测**实际生效的是哪个。
- **实测抓到一条真缺陷**（jiuyan）：`application.yml` 里写的是
  `${CORS_ALLOWED_ORIGINS:...}`（占位符引用的那个名字），但 Spring 的宽松绑定
  把 `APP_CORS_ALLOWED_ORIGINS` 也映射到同一个属性，而**环境变量优先级高于 YAML**
  —— 两个同时设上，**`APP_` 那个赢**。部署的人按 yml 里写的名字想事情，
  那个名字可能根本不生效，**且没有任何报错**。
  归类 `service.interface_contract`，根因 `config.binding_priority_surprise`。
- 特性覆盖 `dynamic_config`：**blind → partial**（特性盲区 3 → 2）。
- jiuyan 流水线 exit 0；发现 100 → **101**、根因 72 → **73**；
  硬违规 1（未变）、契约缺口 9（未变）；集权层门禁 GREEN；残留守卫通过。
- **顺带解开了两个耦合**（都在通用件里写死了项目东西）：
  ① `probe_instance.py` 里写死 `FILE_UPLOAD_DIR` 注入 —— 既是项目耦合，
     又让「属性绑定优先级」这类探针根本观察不到默认值；现在默认注入、
     要剔除的变量、源容器名全部由 target.json 声明。
  ② `feature_coverage.py` 的检测器路径只按项目目录解析 —— 换目标后
     找不到住在 pilot/ 的探针脚本，两个检测器报「跑不起来」；
     现在先项目目录、再工具目录。

---

## 14.1 第五轮验证记录（2026-11-08 深夜）

## 14.1 第五轮验证记录（2026-11-08 深夜）

**第五轮：语义地图的模型轨第一次真跑 —— 机制成立，模型质量没跑通**

- 机制链路全部跑通：任务书（只给事实 + 封闭模板目录）→ 模型（独立 agent，
  只读任务书）→ 编译四道闸 → 真跑 → **advisory 报告**。
- **隔离性做成了可执行证明**（不是设计声明）：把模型写的 `mission` / `label`
  全换成垃圾，重新编译 —— 承重部分（`check`）**逐字节一致**。
  台账层也验证过：模型轨跑完后 `all_findings.json` 仍是 **100 条**，
  含 `OB-MODEL` 的条目 **0 条**，人工语义地图的 2 条完好。
- **质量结论（实测负结果）**：同一个模型、同一批 12 个单元，任务书迭代 5 版
  （代码事实 → +fixture 目录与打桩样例 → +同类已跑通的用例 → +harness 默认行为
  → +实体完整字段表），**每一轮都是 0 条可晋升**。
  逐轮定位到的假失败根因：仓储没打桩（harness 默认 `Optional.empty()` → 抛异常
  → 方法「返回 null」）→ 打桩形状错（裸实体而非 Optional）→ 实体字段不全
  （MapStruct 映射出 null）。但**最后一条是根子上的**：模型只会挑 `not_null`
  —— 它不知道该期待**什么**（业务语义不在代码事实里）。
  ⇒ 下一步不该再加工具书（第 3/4/5 轮的改进都没改变结论），
  该换**任务形态**（问「应该拒绝什么」而不是「应该返回什么」）。
- 详见 `pilot/SEMANTIC_MODEL_TRACK.md`。
- **顺带修掉两个编译器/闸门的洞**（都是模型轨撞出来的）：
  ① 空的 bound（`{}`）被当成「有 bound 但类型不认识」而拒 —— 现在视为「没有 bound」；
  ② 缺「返回值类断言用在 void 方法上」的闸（结构合法、语义荒谬）。

---

## 14.2 第四轮验证记录（2026-11-08 晚）

**第四轮（补完）：覆盖率指标修正 —— 五层的结论终于都记到单元上了**

- **第二个原因（比第一个更根本）**：**只有函数层的结论能关联到单元**。
  文件层/类层/服务层那 70 条结论一条都没被记 —— 文件单元按路径后缀关联、
  类单元按文件名反查类名、服务单元按层归集。
  （排查时踩到一个坑：`file_by_rel` 用 `setdefault`，而**函数单元排在文件单元前面**
  且共享同一批文件路径 → 路径映射到了函数单元的 key 上；只收文件层单元才修好。）
- **结果**：已判定 19 → **34（3.57% → 6.39%）**；按层：函数 19 / 文件 11 / 类 3 / 服务 1；
  义务三值 满足 22 / 违反 63 / 未知 2067；
  `root_causes_with_multiple_carriers` 0 → 12（同一个根因在多个单元上有证据点）。
- **jiuyan 流水线** exit 0；发现 100 → 根因 72（未变）；硬违规 1（未变）；
  契约缺口 9（未变）；集权层门禁 GREEN；涌现维度如实报 INCOMPLETE；残留守卫通过。

---

## 14.3 第四轮验证记录（2026-11-08 晚）

**第四轮：覆盖率指标修正 —— 项目最关键的那个数字之前是错的**

- **问题**：单元状态只看「有没有用例」，**完全不看用例的判定结果**。
  一组 17 条全 PASS 的用例被记成「有观测但义务未定」，「满足」恒为 0 ——
  于是「被动态验证过的单元数」被系统性低估。
- **修法**：
  · 用例文件声明「每个用例分组验的是哪一类义务」（`group_categories`，
    类别取自人工标注表）；
  · `fn_dynamic.py` 把类别盖到每条用例上，观测里带着它；
  · Agent 图构建器按**判定结果**定状态：有违规或 FAIL 用例 → FAIL；
    有 PASS 用例 → PASS；只有观测 → OBSERVED；
  · 义务三值随之重算（满足/违反按类别计，未知是其余）；
  · 护栏：义务类别必须与单元所属层一致 —— 函数层单元不能记类层的义务
    （demo 的 LSP 用例就是这种：它是类层属性的函数级观测）。
- **结果**：已判定 **6 → 19（1.13% → 3.57%）**；「有观测但义务未定」**13 → 0**；
  义务三值 满足 0 → **20**、违反 5 → **10**、未知 2066 → 2101。
  **这 3 倍不是新查出来的，是本来就在那儿、之前没被算进去。**
- **顺带的影响**：「最该先验的单元」榜现在只排真正未查的单元，
  第一名从「BanquetOrder（中心性 46、0 分支 0 副作用）」变成
  「OrderService（文件层，56 分支 16 副作用）」—— 前者测它没意义。
- **jiuyan 流水线** exit 0；发现 100 → 根因 72（未变）；硬违规 1（未变）；
  集权层门禁 GREEN；涌现维度如实报 INCOMPLETE；残留守卫通过。
- **demo 流水线** exit 0；涌现 FDR 门禁 GREEN（FDR 1.000 / Attr_acc 1.000）；
  新增**特性覆盖矩阵**：demo 6 个特性 → 覆盖 1 / 部分 4 / 盲区 1，
  其中 **rpc_boundary 被覆盖**（跨仓库载体）—— 这是跨仓库能力第一次被测量。

---

## 14.4 第三轮验证记录（2026-11-08 下午）

**第三轮：五层差分判定层全部通用化 + demo 文件层接上 + 归属翻转到最低层**

- **jiuyan 流水线** `bash pilot/run_pipeline.sh` → **exit 0**；集权层门禁 GREEN；
  涌现维度如实报 INCOMPLETE；残留守卫通过。发现 100 → 根因 72（未变），
  硬违规 1（未变），契约缺口 9（未变）。
- **demo 流水线** `bash demos/shop/run_pipeline.sh` → **exit 0**；
  涌现 FDR 门禁 **GREEN**（FDR_inj 1.000 / Attr_acc 1.000）；残留守卫通过。
  发现 16 → **20** 条 → 根因 13 → **15** 条，硬违规 **0**，契约缺口 **0**。
- **等价性（两份都逐字节一致）**：
  · 函数层：`engine/fn_diff.py` + `fn_groups.json` 接管后，jiuyan 的
    `fn_findings.json` / `.md` 逐字节一致。
  · 文件层：`engine/file_diff.py` + `file_findings_config.json` 接管后，jiuyan 的
    `file_findings.json` / `.md` 逐字节一致。
  过程中被等价性判据抓到 7 个问题并全部改正（排序时点、HTTP 子场景路径语义、
  布尔值的字符串化写法、positive() 补的四个空字段、`http.get` 的两种语义等）。
- **归属翻转（这轮最实质的变化）**：demo 的
  `order.quota_checked_on_quoted_amount_ledger_on_settled` 由**文件层 own**、
  服务层改为 **cite**。理由：额度检查与记账在**同一个文件**里，但跨了两个类 ——
  函数层看不到（两处单看都对）、类层也看不到，只有文件层能同时看见。
  这与 jiuyan 的先例一致（`FN-APPLY-AMOUNT-INVARIANTS` own /
  `BI-DEPOSIT-LTE-FINAL-AMOUNT` cite）。门禁的原话：
  「own 单 FILE-QUOTA-BASIS-QUOTED-VS-SETTLED 在 **file 层**认领，层序最低、类别合法」。
- **契约变更**：`layers.yaml` 的文件层新增 `contract_alignment` 类别
  （「同一文件内两处对同一业务量的口径不一致」）；`agents.total_roles` 22 → 23。
  jiuyan 这一侧如实报该类别零覆盖（它没查过这个面）。
- **顺带修掉一个潜伏 bug**：契约解析器（`engine/findings_schema.py`）是逐行正则，
  `categories` 块里出现**注释行**会让它后面的类别**静默丢失** ——
  在 layers.yaml 里加一行注释就能悄悄改掉契约。已修在解析器里（注释行跳过）。
- **demo 的两条文件层用例改成断言「应有行为」**：原先断言的是现状（把缺陷写成了期望），
  所以 24 条判定全 PASS、开不出单。改成应有行为后两条变 FAIL，demo 拿到 2 条真实发现：
  额度口径不一致（file.contract_alignment）+ 跨仓库汇率兜底量纲错（file.error_handling_pattern）。

---

## 14.5 第二轮验证记录（2026-11-08 中午）

**第二轮：服务层通用化 + demo 拿到服务层 → 涌现门禁 RED 转 GREEN**

- **demo-shop 流水线** `bash demos/shop/run_pipeline.sh` → **exit 0**；
  涌现 FDR 门禁 **GREEN**：FDR_inj **1.000** / Attr_acc **1.000**
  （分母 2 / 检出 2 / 归属正确 2），门禁指纹 `861cc2f7121554a1`；
  残留守卫通过（容器 0 / `ai_` 行 0 / 非种子上传 0）。
  demo 侧：发现 16 → 18 条 → 根因 13 → **14** 条，硬违规 **0**，契约缺口 **0**。
- **阳性对照（证明这个绿不是调松阈值换来的）**：把服务层的两条 finding
  从 `all_findings.json` 抽掉再跑门禁 → 立刻变回 **RED**
  （FDR_inj 0.500 / 漏检 `INJ-PROFILE-VIP` / 退出码 1）。
- **jiuyan 流水线** `bash pilot/run_pipeline.sh` → **exit 0**；集权层门禁 GREEN；
  涌现维度如实报 INCOMPLETE；残留守卫通过。发现 100 → 根因 72（未变 ——
  业务不变式的 findings 本来就在证据源里，这轮只是让流水线**能复现**它们）。
- **服务层通用化的等价性**：`contract_diff.py` 去掉 4 处项目耦合后，
  jiuyan 的 10 条 finding（id/severity/endpoint/title/detail/repro/evidence）
  **逐条一致**。过程中踩到一个坑：`authz.get("method") and authz.get("path")`
  返回最后一个真值而不是元组 ⇒ 证人端点匹配失效 ⇒ NO_AUTHZ 被静默吞掉 ——
  「结论一条都不变」这条判据当场抓住了它。
- **业务不变式判定通用化**：原 `business_probe.py` 210 行里，
  「调用 → 读行 → 判定 → 开单」抽到 `engine/business_verdict.py`；
  jiuyan 的 4 条违反 + 2 条成立、场景映射、违反次数、入口拒绝的场景全部一致。
  引擎纯净度守卫当场抓出引擎文档示例里写死的项目脚本名，已改。
- **demo 的三个差分场景**（这是涌现性质的直接证据）：
  普通客户+会员装配 → 口径一致；VIP 客户+标准装配 → 口径一致；
  **VIP 客户+会员装配 → 口径分裂**。前两个是「单条件不违反」的对照组。
- **归属判定**：口径分裂 owner = service（own，差分证据）；
  VIP 拿不到会员价 owner = class（**cite**，不重复开单）。
  三层根因各归其位：class（结构原因）/ service（口径分裂）/ integration（端到端后果）。
- **顺序修正**：demo 流水线原先把「归一化 + 聚合」放在涌现门禁之后 ——
  门禁读的是上一轮的结论，服务层开对了单它也看不见。已改为先归一化再判门禁。

---

## 14.6 第一轮验证记录（2026-11-08 上午）

**本轮（2026-11-08）：接手被上下文撑爆中断的会话后继续**

- **jiuyan 全量流水线** `bash pilot/run_pipeline.sh` → **exit 0**；
  集权层门禁 **GREEN**（红 0 / 绿 0 / 没查完 0；对齐项 52→52，攻击链确认 5→5）；
  残留守卫通过（容器 0 / `ai_` 行 0 / 非种子上传 0）。
  涌现维度**如实报 INCOMPLETE**（本目标无注入目录 —— 没查完，不是绿灯）。
- **demo-shop 流水线** `bash demos/shop/run_pipeline.sh` → **exit 1**（预期）：
  涌现 FDR 门禁判红（演示目标里确实有「根因只被引用、无人 own 开单」的缺陷
  INJ-PROFILE-VIP）；候选集指纹 `4ad5d478ba218655`、裁决指纹 `b146ff57eb2fd281`、
  门禁指纹 `b89aa52606d938f9`，全部两轮一致；残留守卫通过。
- **类层补齐**：jiuyan 接入 `class_shape` 通用探针 → 类层 16 → 23 条，
  新开 7 条真实发现（6 实体缺值相等 + 1 封装泄漏），类别与归属都正确；
  收口后 jiuyan 总发现 **93 → 100**、根因 **70 → 72**，UNMAPPED 仍是 9 条、
  硬违规仍是 1 条（没有新增越界开单或重复开单）。
- **第二载体复核**：文件载体 10 条观测 → 被类层结论独立复现 7 条、
  项目声明不适用 3 条（Spring Data 仓储方法返回实体是框架既定用法）、
  **真缺口 0 条**（jiuyan）；demo 侧真缺口 3 条（`profile_assembly` ×2、
  `outbound_http` ×1），已列进 `class_carrier_gap.json` 等下一轮定夺。
- **等价性独立复核**（不是读文档，是拿基线逐条比）：
  文件层 18→18、类层 16→16，结论级字段（id/severity/layer/detail/title/
  category/claim）**0 差异**；仅有的两处差异是实测值（EXPLAIN 耗时 ms、
  日志截取方式）。基线已从 `/tmp` 挪进 `pilot/baselines/`，永远可复核。
- **涌现链三态与防作弊独立实测**：
  demo 退出码 1（红）/ jiuyan 退出码 2（没查完）；
  删掉检不出来的注入后 FDR 跳到 1.0，但分母哈希与集合哈希双双漂移 → 照样判红。
- **守卫**：`engine/check_engine_purity.py` 通过（19 个引擎文件零项目耦合）；
  `check_class_probe_purity.py` 通过（10 个类层通用文件零项目耦合）；
  `probe_residue_check.py --selftest` 通过；Java harness 编译零错误零警告。
- **中断本身没有造成损坏**：所有 Python 过 `py_compile`、所有 JSON 可解析、
  无 stash、无遗留进程、`xrepo_probe.py` 实跑通过。

---

## 14.7 更早一轮验证记录（2026-11-07）

**涌现候选链 + FDR 门禁（2026-11-08，详见 `pilot/EMERGENCE_CANDIDATES_RESULT.md`）**

- 候选生成（`pilot/engine/emergence_candidates.py`，固定种子 20261107）：demo 关系网
  37 节点 / 29 边 + 2 条不变式锚 → **候选 2 条**（模式 T3 ×2，T1/T2/T4/T5/T6/T7 = 0），
  选中 2 / 没跑 0；候选集指纹 `4ad5d478ba218655`（两轮一致）。
- 召回：注入目录里的已知链 `EMERG-QUOTA-LEDGER-DIVERGENCE` **命中 1/1，
  条件覆盖 full**（候选 `C0000` 的条件槽 data + instance + request 全覆盖真链需要）。
- 真跑（`demos/shop/emergence_candidate_run.py`，真容器 + 真库）：裁决
  **`EMERGENT_CONJUNCTIVE`**，owner=`integration`(own)，category=`integration.end_to_end_business`，
  最小核心集 `INJ-TIER-VIP+INJ-PROFILE-VIP`；裁决指纹 `f10753a38f7ef780`
  （与人工链那一轮**逐位相同**）；探针残留 **0 行**。
- FDR 门禁（`pilot/engine/emergence_gate.py`）：分母 **2**（active ∧ 活性通过；inactive 1 条
  不进分母）｜检出 **2**｜归属正确 **1** → **FDR_inj = 0.500**、**Attr_acc = 0.500**，
  分类别 `layer:service` FDR 0.500；分母哈希 `6cd68a40b6a5d3b9`、集合哈希 `8661e3c30899ed59`、
  门禁指纹 `b89aa52606d938f9`（两轮一致）；**结论 RED，退出码 1**。
- 漏检 1 条：`INJ-PROFILE-VIP`（根因 `order.quota_checked_on_quoted_amount_ledger_on_settled`）
  —— service 层根因**只被集成层引用、没有任何 own 单**，按「可归属」口径不算检出。
- 防作弊实测：删掉 `INJ-PROFILE-VIP` 后 FDR 跳到 1.0，但分母哈希与集合哈希双双漂移 → 判红；
  `--init` 拒绝静默重钉（须 `--accept`）。
- 三态语义：jiuyan 绑定无注入目录 → **退出码 2（没查完，不是没问题）**。
- 守卫：`engine/check_engine_purity.py` 通过（19 个引擎文件零项目耦合）；
  `probe_residue_check.py --selftest` 通过；demo 残留 0 行
  （容器 0 / `ai_` 行 0 / 非种子上传 0）。

**文件层载体通用化（2026-11-08，详见 `pilot/FILE_GENERIC_RESULT.md`）**

- 通用 harness（`file_harness/GenericFileHarness*.java` 2829 行）+ 通用容器探针
  （`file_http_probe.py`）：**零项目字面量**（自查 grep 零命中）；项目知识全在
  `pilot/file_cases.json`（jiuyan 21 场景）/ `demos/shop/file_cases.json`（demo 8 场景）。
- **等价性**：jiuyan 22 用例 / 58 判定，判定值逐条一致；18 条 finding 的 id 集合与
  结论级 10 字段 **0 差异**；整条不同的只有 1 条（`FILE-XFF-CLIENT-IP-SPOOF` 的
  `evidence.log_segment` 截取方式变了：日志尾巴 → 围绕标记的行，含 `clientIp=9.9.9.9`，
  结论不变）。连续两次跑归一化后 **0 差异**（确定性）。
- **demo-shop**：9 用例 / 25 判定 / **不符 0 条**（没崩）；埋的 6 类缺陷命中 5 类
  （「缓存放可变对象」实测不成立，如实记为负结果），另挖出 2 个真缺陷
  （额度校验拿报价去比、汇率回退量纲错）；容器侧两个部署结算 888888 vs 799999
  证明「客户等级被绑到部署 profile」。
- 通用化顺带修掉 6 个真问题（其中 **2 个是通用件本身的缺陷**）：
  ① `Method.invoke` 虚分派 ⇒ 覆写后置条件检查器恒空（改用 `MethodHandles` 非虚调用）；
  ② `docker logs --since` 毫秒/微秒竞态 ⇒ 日志探针时好时坏（窗口回看 2 秒）；
  ③ 日志证据取尾巴可能取不到目标（改围绕标记截）；
  ④ class 文件正则扫 URL 带出二进制尾巴（新增 `ClassConstPool` 解析常量池）；
  ⑤ `file_static.py` 产物写死脚本目录 ⇒ 换目标会覆盖别人的产物（改 `artifact_dir()`）；
  ⑥ cleanup 把 psql 的 `DELETE 0` 标签行当成删了 1 行。
- 守卫：`engine/check_engine_purity.py` 通过；`probe_residue_check.py --selftest` 通过；
  jiuyan 与 demo 两边残留均 **0**（容器 0 / `ai_` 行 0 / 非种子上传 0）；
  demo 的 `shop_order` 回到探针前水位（16 行 / max(id)=268）。
- **未做**：判定层 `file_diff.py` 仍是 jiuyan 专属（demo 跑它会 `KeyError`）——
  下一步若要 demo 出缺陷单，把差分规则配置化（类层已有 `diff_rules` 派发做法可抄）。

**负载实测层加入后重跑（最新）**

- `bash pilot/run_pipeline.sh` → exit 0；门禁 GREEN（对齐 52→52，攻击链确认 5→5）。
- `perf_load.py --scales 0,2000,10000,50000 --rounds 10` → 判定 `PARTIALLY_CONFIRMED`；
  探针行 50000 行全部删除，商品表回到 30 行基线。
- `normalize_findings.py` → 88 条，10 必填字段结构齐备 88/88；结构性违规 **0**；
  契约缺口 9 条（退出码 2，不阻断）。
- `aggregate_findings.py` → 88 条发现归并为 **53 条根因**；硬违规 1 条；归并提示 4 条；
  零覆盖类别从 10 降到 **8**（`service.service_latency` 与
  `integration.cross_service_performance` 本轮补上）。
- 补两个零覆盖类别：`numeric_props.py`（property_based_test，3 条，其中 1 条正面结论）、
  `concurrency_props.py`（concurrency_test / chaos_experiment，3 条，其中 1 条正面结论）；
  零覆盖类别 8 → **5**。发现 91 → 94 条、根因 55 → 58 条。
- 引擎自检：`engine/check_engine_purity.py` 通过（3 个文件零项目耦合）；
  `test_portability.sh` 通过（换绑定跑出 30 条、产物落在合成目标目录、不碰原产物）。
- `repro_cmd` 不可直接执行的从 **14 条降到 3 条**（case 8 / scenario 9 / request 30 /
  layer 36 / command 2 / reference 3）；
  `python3 integration_dynamic.py --scenario SC01_CLIENT_CONTROLLED_PRICE` 单场景重放
  复现成立（FAIL，status=200）。
- 回归套件 `python3 -m pytest -q` → 70 passed / 8 xfailed，**且跑完零残留**
  （本轮修掉了套件自身漏清理的 33 行）。
- 残留已归零：`ai_` 前缀行 products/packages/categories/package_types/orders 全为 0；
  DB 回到种子基线（products 30 / packages 4 / orders 8 / categories 8 / package_types 6 /
  package_items 40）；非种子上传文件 0。

**跨层收口层加入后重跑（对照）**

- `bash pilot/run_pipeline.sh` → exit 0；门禁 GREEN（对齐 52→52，攻击链确认 5→5）。
- `normalize_findings.py` → 86 条，10 必填字段结构齐备 86/86；结构性违规 **0**；
  契约缺口 9 条（退出码 2，不阻断）。
- `aggregate_findings.py` → 86 条发现归并为 **52 条根因**；硬违规 1 条；归并提示 4 条；
  零覆盖类别 10 个。
- `python3 fn_dynamic.py --case RFP-05 --case RFP-07` → 重放成立，两条断言均 FAIL
  （证明 `repro_cmd` 是真能跑的命令，不是编的）。
- 残留已归零：`ai_probe_` 订单 0；DB 回到种子基线（products 30 / packages 4 / orders 8 /
  categories 8 / package_types 6 / package_items 40）；非种子上传文件 0（清理删掉 4 个孤儿上传）。
- `/Users/june/jiuyan` 源码 0 审计改动（`git status` 里 9 个改动文件全是用户自己的 OTel/部署改动，
  已逐项核对无审计痕迹）。
- `layered-audit/` 已 `git init` + `.gitignore`（运行产物不入库）+ 首次提交 `06ea0ab`（53 文件，22034 行）；harness 仓库提交 `3b0fae6`。两者**均未 push**。

**加入跨层收口层之前（对照）**

- harness `python3 -m pytest -q` → 70 passed / 8 xfailed。
