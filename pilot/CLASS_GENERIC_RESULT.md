# 类层探针通用化 —— 结果

> 目标：让类层（class）能审计任意 Spring Boot 项目，换项目只改配置不改引擎。
> 被测系统 jiuyan 只读；demo-shop 是第二个目标，用来证明「通用」不是口号。
> 口径与守卫：`python3 check_class_probe_purity.py`（类层通用层零项目耦合）
> + `python3 engine/check_engine_purity.py`（跨层引擎零项目耦合）
> + `python3 probe_residue_check.py --selftest`（残留守卫自检）。

---

## 0. 一句话

类层现在是**声明式**的：引擎（`class_probe.py` / `class_rules.py` / `class_contract_diff.py` /
`class_diff.py` / `class_contract.py` / `class_static.py` / `class_shape_probe.py` +
两个 Java harness）不含任何项目字面量，项目要「看什么」全部写在项目侧的
`class_scenarios.json` 里。jiuyan 的结论改前改后 **16 → 16 条完全等价**；
demo-shop 第一次跑就出了 **15 条**，demo 埋的 5 个缺陷全部命中，探针造的行 **0 残留**。

---

## 1. 改了什么

### 1.1 通用层（引擎）—— 改的是「怎么跑」，不是「跑什么」

| 文件 | 改动 | 为什么 |
|---|---|---|
| `class_probe.py` | 新增 `sql_fixture` 探针类型 + 跨探针夹具（`self.fixtures`）+ `id_paths`（响应 id 取值路径可配）+ `instance_pair` 的 `ignore_fields` + 实例创建的行记账清理 | 订单必须引用一个已存在的客户，而订单 DTO 里没有能自由填的字符串字段 —— 探针造不出这种行。加了 SQL 夹具后，夹具行自带前缀、自己清理、外键顺序由项目配置给（引擎不猜） |
| `class_probe.py` | `Database.cte_rows()`：把任意语句包进 CTE 再取回行 | Postgres 不允许 `SELECT ... FROM (INSERT ... RETURNING ...) x`，夹具插入直接报语法错 |
| `class_rules.py` | 规则派发化：`RULES` 字典（规则 id → 实现），文案/表名/文件名全在配置里；`render_json` 保住证据的 JSON 类型 | 原来规则里写着「address」「finalPriceCent」这类项目字段 —— 那不是规则，是项目的知识 |
| `class_contract_diff.py` | 三方差分（A 可空性 / B 规则分布 / C 写入足迹 / D 错误契约）全部改成吃配置：`dto_target` / `server_derived` / `field_notes` / `rule_notes` / `rule_excludes` / `rule_missing` / `write_footprint` / `error_contract` | 差分逻辑是语言级的，**差分对象是项目的** |
| `class_diff.py` | 差分调度改为按配置的 `diff_rules` 逐条派发到 `class_rules.RULES`；顺序即报告顺序 | 换项目只改配置顺序 |
| `class_contract.py` | `src_roots()` 可配（默认 `dto,entity`） | 不同项目 DTO 目录不同（demo 是 `web/dto`），引擎不该假设目录布局 |
| `class_static.py` | 列名按 Spring 默认命名策略推导（`@Column` 没写 `name` 时 CamelCase → snake_case）；`extends_base_entity` 改成通用判据「父类是不是本项目的 `@MappedSuperclass`」 | 原来解析出的列名是字段名本身，demo 的 `customerName` → 误报「列在 DDL 里不存在」5 条；`"BaseEntity" in src` 是把项目类名刻进了通用层 |
| `class_shape_probe.py`（新） | 类形状反射探针的驱动：编译 harness、喂类清单、收 JSON | 形状探针不碰 DB、不碰容器，审计不该顺手改业务数据 |
| `class_harness/ClassShape.java` | 输出 `field_states`（逐字段给出覆写方 / 继承方的实际取值） | 报告里要能说清「哪里不一样」，而不只是「不一样」 |
| `check_class_probe_purity.py`（新） | 类层通用代码纯净度守卫 | `engine/check_engine_purity.py` 只扫 `engine/`，类层通用骨架在 `pilot/` 下，扫不到 |

### 1.2 项目层（声明）—— 换项目只动这里

| 文件 | 内容 |
|---|---|
| `pilot/class_scenarios.json` | jiuyan 侧：探针前缀、哪些字段带前缀、5 个探针、5 条差分规则、三方差分配置（10 个 DTO→表映射、3 条字段结论、规则分布、写入足迹、错误契约） |
| `demos/shop/class_scenarios.json`（新） | demo 侧：7 个探针（sql_fixture / contract_boundary / nullability / db_invariants / defense_audit / class_shape / instance_pair）、3 条差分规则、三方差分配置 |
| `demos/shop/target.json` | 加 `contract_src_roots: "web/dto,entity"`；`evidence_sources` 加类层（`artifacts/class_findings.json`） |
| `demos/shop/finding_meta.py` | 类层 12 条人工标注（category / claim / owner / confidence / root_cause_key）+ `CLASS_FILE_HINTS` |

### 1.3 顺带修掉的两个真问题（不是通用化顺手改的，是通用化暴露的）

1. **JPA ↔ DDL 列级差分误报**：demo 上 5 条「列在 DDL 里不存在」全是假阳性 ——
   `@Column(nullable=false)` 没写 `name` 时列名是 `customer_name`，解析器却拿字段名去比对。
   修法：按 Spring 默认命名策略推导列名（`class_static.py`）。
   **对 jiuyan 零影响**：jiuyan 所有多词列都显式写了 `@Column(name=...)`，推导结果完全相同。
2. **`@Profile` 用错维度在类层查不出来**：`instance_pair` 原来只比对响应字段，
   但没记账它造出来的行 —— 两个实例各下一单，行就留在库里了。现在实例创建的行走
   `_track()`，探针结束按 id 删干净。

---

## 2. 通用 / 项目特定怎么切

| 文件 | 归属 | 项目相关的部分放在哪 |
|---|---|---|
| `class_probe.py` | 通用 | 无。DB 通道（容器/库/用户）、HTTP base_url 来自 `target.json`；造什么行、查什么表、走哪个端点来自 `class_scenarios.json` |
| `class_rules.py` | 通用 | 无。规则只认「结构事实」（继承层次、值相等、封装泄漏、缓存可变容器、条件装配分叉），文案与 id 在配置里 |
| `class_contract_diff.py` | 通用 | 无。DTO→表映射在 `contract_diff.dto_target`，字段级结论在 `contract_diff.field_notes` |
| `class_diff.py` | 通用 | 无。规则清单与顺序在 `diff_rules` |
| `class_contract.py` | 通用 | 无。反射扫哪些源码根在 `target.json` 的 `contract_src_roots` |
| `class_static.py` | 通用 | 无。迁移目录 / 实体目录来自 `target.json` |
| `class_shape_probe.py` + `class_harness/*.java` | 通用 | 无。类清单来自 `class_scenarios.json`（stdin 喂进去） |
| `class_scenarios.json` | **项目** | 全部：探针、前缀字段、差分规则、三方差分 |
| `target.json` | **项目** | 路径、DB、源码根、证据源 |
| `finding_meta.py` | **项目** | finding id → category/claim/owner/confidence/root_cause_key |

**切分的三条线**（我在做的过程中一直按这三条判）：

1. **「事实的形状」归引擎，「事实的内容」归项目。**
   「子类覆写后后置条件变了」是形状 → 引擎判；
   「`ShopOrder` 覆写 `AuditableEntity.touch`」是内容 → 项目声明。
2. **「怎么测」归引擎，「测什么」归项目。**
   探针类型（sql_fixture / contract_boundary / instance_pair …）是引擎；
   「造一个 VIP 客户夹具、下单、比对两个 profile 实例」是项目。
3. **「判定口径」归引擎，「判定对象」归项目。**
   「可空性三方一致」这个口径是引擎；「哪些列该一致」是项目。
   D1（JPA ↔ DDL 列级差分）是唯一的例外：它是纯语言级的，所以留在引擎里。

**验证方式**：守卫 `check_class_probe_purity.py` 从**当前绑定的 target.json + 项目自己的
迁移文件/实体类名**里抽出「这个项目特有的词」（jiuyan 44 个 / demo 22 个），再回头扫
10 个通用文件；同时禁绝对路径、内网地址、`/api/` 字面量。
**检查器自己也不写死任何项目名** —— 词是从项目里读出来的。

```
$ python3 check_class_probe_purity.py
类层通用代码纯净度检查通过：10 个文件零项目耦合（class_static.py、class_contract.py、
class_dynamic.py、class_diff.py、class_contract_diff.py、class_rules.py、class_probe.py、
class_shape_probe.py、class_harness/ClassShape.java、class_harness/ClassContract.java）
  已比对的项目特有词 44 个（项目名 / 绑定项 / 表名 / 实体与 DTO 类名）
```

写这个守卫时它当场抓出 3 处耦合：`class_static.py` 里的 `"BaseEntity"`、
`class_rules.py` 文档示例里的 `"OrderStatus.java"`、`class_probe.py` 文档里的 `"products"`。
三处都已改成通用写法 —— 前者是判定逻辑（改成 `@MappedSuperclass` 判定），后两处是文案。

---

## 3. 等价性证据（jiuyan 改前 vs 改后）

**基线**：`/tmp/class_generic_baseline/class_findings.json`（改前的 16 条）
**新版**：`pilot/class_findings.json`（改后的 16 条）

| 项 | 结果 |
|---|---|
| 条数 | 16 → 16 ✅ |
| 每条的 `id` | 全部一致 ✅ |
| 每条的 `severity` | 全部一致 ✅ |
| 每条的 `layer` | 全部一致 ✅ |
| 每条的 `detail`（结论正文） | 全部一致 ✅ |
| 每条的 `title` | 16 条里 15 条一致，1 条不同（**实测值**，见下） |
| `evidence` | 3 组不同（**实测值**，见下） |

逐条差异解释（**没有一条是结论变化**）：

| # | finding | 差异 | 为什么是噪声而不是回归 |
|---|---|---|---|
| 1 | `INDEX_UNUSABLE_FOR_QUERY_PATTERN` | title 里 `8.019ms vs 0.013ms，差 617 倍` → `7.783ms vs 0.014ms，差 556 倍`；evidence 同两项 | EXPLAIN ANALYZE 的实测耗时，每次跑都不同。**结论不变**：EXPLAIN 显示走 Filter 全量过滤而非 Index Cond，索引依然不可用（差 500 倍以上，两次跑都在这个量级） |
| 2 | `SOFT_DELETE_RESURRECTS_ROW` | evidence.category `ai_probe_cat_8412a7` → `ai_probe_cat_9a682b`；db_rows `[['225','f']]` → `[['235','f']]` | 探针自己造的随机类目名与自增 id。**结论不变**：软删后同名重建仍然复用了旧行的 id 与 sort_order |
| 3 | `CLASS-PUT-WIPES-UNSET-FIELD` | evidence.final_row 的 `id` / `created_at` / `updated_at` | 探针造的行的 id 与时间戳。**结论不变**：不传的 `imageUrl` 仍然被抹成 NULL |

### 确定性（同输入两次跑）

连续两次跑 `class_dynamic.py` + `class_diff.py`：

```
第1次 16 条 / 第2次 16 条
结论元组（id/severity/layer/detail）完全一致: True
id 序列一致: True
两次跑有差异的字段: INDEX_UNUSABLE_FOR_QUERY_PATTERN(title,evidence)
                    SOFT_DELETE_RESURRECTS_ROW(evidence)
                    NO_DATA_LIFECYCLE(title,evidence)
                    CLASS-PUT-WIPES-UNSET-FIELD(evidence)
```

差异全部落在**实测值**上（EXPLAIN 耗时、表体积、探针行的 id/时间戳），
**结论字段（id / severity / layer / detail / category）两次跑逐字节一致**。

> 判断记录：标题里嵌实测耗时（ms）本来就不是严格确定的。我没有改这一点，
> 因为改了 jiuyan 的 title 就与基线不等价了，而等价性是硬要求。
> 代价是「同输入两次跑标题可能不同」—— 已在 §6 记为已知取舍。
> 如果以后要把它做成严格基线门禁，正确的做法是给实测值分桶（例如「差 500 倍以上」），
> 那是一次独立的、需要重新拍基线的改动。

### 零残留

jiuyan 与 demo 两个库在跑完后探针行都是 0：

```
jiuyan:  products/orders/packages/categories 的 ai_probe% 行数 = 0
demo-shop: shop_order/shop_customer/shop_product 的 ai_probe% 行数 = 0
           shop_order 总行数 16（= 种子基线）
```

清理日志：demo 本轮 7 条清理语句，0 失败；jiuyan 本轮清理语句 0 失败。
`python3 probe_residue_check.py --selftest` 通过。

---

## 4. demo-shop 的类层运行结果

**没崩**。7 个探针全部执行成功，0 个探针报错，产物落在
`demos/shop/artifacts/class_{declaration,contract,observation,findings}.json`。

**15 条发现（HIGH 5 / MEDIUM 8 / INFO 2）**：

### demo 埋的 5 个缺陷 —— 全部命中

| demo 埋的缺陷 | 命中的 finding | 实测证据 |
|---|---|---|
| 3 层继承 + 覆写违反里氏替换 | `CLASS-LSP-POSTCONDITION-CHANGED`（HIGH） | `BaseEntity ← AuditableEntity ← ShopOrder`；`AuditableEntity.touch(actor)` 承诺 `updatedBy=actor`，`ShopOrder` 覆写后置成 null。反射实测：覆写方 `updatedBy=null`，继承方 `updatedBy=actor` |
| `@Profile` 条件装配 | `CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE`（HIGH） | 同一个请求（报价 1000 分、客户等级 VIP）：default 实例 `settledAmountCent=1000`，vip 实例 `=900`。**VIP 是客户等级（业务维度），却用 `@Profile`（部署维度）表达** —— 非 vip 部署里会员客户全部按标准价结算 |
| 实体没有值相等 | `CLASS-ENTITY-WITHOUT-VALUE-EQUALITY`（MEDIUM）×4 | `ShopOrder` / `ShopCustomer` / `ShopProduct` / `ShopCategory` 都没声明 `equals/hashCode` |
| 缓存里放可变对象 | `CLASS-CACHE-HOLDS-MUTABLE-CONTAINER`（MEDIUM） | `ProductService.listCached()` 的 `@Cacheable("products")` 直接缓存了 `List<ProductResponse>` 实例本身（元素是 record、不可变，风险全在容器上） |
| public 方法返回内部可变集合 | `CLASS-ENCAPSULATION-LEAK-ENTITY-COLLECTION`（HIGH） | `ProductService.snapshot()` 返回 `List<ShopProduct>`（JPA 实体本身），调用方改一个字段就改了服务层内部状态 |

### 顺带查出来的（demo 没埋的）

| finding | 级别 | 说了什么 |
|---|---|---|
| `CLASS-PRIMITIVE-NULLABLE-BY-DESIGN` | MEDIUM | `quoted_amount_cent` 是 NOT NULL，DTO 用原始类型 `long`。缺省与显式 `null` **都**变成 0，而 0 恰好满足 `@Min(0)` —— 「报价必须大于 0」这条规则三层都没人声明。实测：不传该字段 → HTTP 200，库里落 0 |
| `CLASS-BUSINESS-ERROR-AS-500` | HIGH | 客户不存在是业务错误，实测却返回 **HTTP 500**，响应体里没有业务错误码，只有一句 Internal Server Error |
| `CLASS-RULE-MISSING-IN-ALL-LAYERS` | HIGH | 3 条业务不变量在 DB / DTO / 服务层三层全都不存在（报价可为 0 / status 任意字符串 / 结算额与报价额无关联） |
| `CLASS-CHECK-WITHOUT-DTO-COUNTERPART` | MEDIUM | 3 条 DB CHECK 在 DTO 层找不到同义约束（规则只写在迁移文件里，Java 侧读不到） |
| `NO_DATA_LIFECYCLE` | MEDIUM | `shop_order` 表没有软删/归档，永久增长（当前 56 kB）；且订单只用 `customer_name` 字符串引用客户，没有外键兜底 |
| `CLASS-NULLABILITY-ALIGNED` / `CLASS-CHECK-DTO-ALIGNED` | INFO | 通过结论：可空性三方一致 1/2 列；1/4 条 DB CHECK 在 DTO 层有同义约束 |

### demo 的类层发现能直接进跨层引擎

`demos/shop/target.json` 的 `evidence_sources` 加了类层之后，
`engine/normalize_findings.py` + `engine/aggregate_findings.py` 在 demo 上跑通：
16 条记录（15 类层 + 1 集成层）、**结构性违规 0 条**、类别全部落地（exact 10 / stretched 6），
归并出 12 个根因，报告落在 `demos/shop/audit_report.md`。

其中 `CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE` 的根因键
`pricing.member_tier_bound_to_deployment_profile` 正是 demo 原有集成层涌现发现
（`EMERG-QUOTA-LEDGER-DIVERGENCE`）引用的低层根因之一 —— **低层根因与高层根因对上了**，
这正是「归属唯一、高层引用低层」这条规矩要的效果。

---

## 5. 剩余缺口

| # | 缺口 | 影响 | 我的判断 |
|---|---|---|---|
| 1 | **类层没有进入 `engine/`**：通用骨架在 `pilot/` 下，不在 `pilot/engine/` | `engine/check_engine_purity.py` 扫不到类层 → 所以我补了 `check_class_probe_purity.py`。但严格说这是两套守卫 | 暂不搬。函数层（`fn_harness/GenericHarness.java`）、服务层（`contract_probe.py`）的通用件也都还在 `pilot/` 下，**三层的通用件一起搬**才合理，单独搬类层会破坏一致性 |
| 2 | **实测值进标题 ⇒ 标题不是严格确定的** | 同输入两次跑，1 条 title 的 ms 与倍数会变（见 §3） | 已记录。彻底解法是给实测值分桶，但要重新拍 jiuyan 基线 |
| 3 | **`instance_pair` 比的是原始响应体**：响应体里若含 timestamp 之类每次都变的字段，会被算成「分叉」 | 误报风险（demo 踩过一次：夹具没造成功时，两个实例都返回 500 错误体，差异字段是 `timestamp`） | 已用 `ignore_fields`（项目侧声明）兜住，但**「哪些字段是随机量」目前靠项目声明，没有自动识别** |
| 4 | **`class_static.py` 的 DDL 解析是正则**：`CREATE TABLE` 必须以 `\n);` 结尾、CHECK 约束必须写在表体里 | 非标准写法的迁移文件（`CREATE TABLE ... AS`、`ALTER TABLE ADD CONSTRAINT`）解析不到 | jiuyan / demo 都够用。没做通用 SQL 解析器是刻意的（不过度设计） |
| 5 | **类层的 finding id 可能重复**：`CLASS-ENTITY-WITHOUT-VALUE-EQUALITY` 对 4 个实体开了 4 条，id 相同 | 跨层引擎把它标成「id 层内重复」，按 `root_cause_key` 归并成 1 个根因 | 语义上正确（4 个实体、1 个根因）。要让 id 唯一就得给标注表加通配机制，收益不大 |
| 6 | **demo 的类层只有一条 `write_footprint` 缺失**：demo 没有 PUT 语义 | C 维度（列级写入足迹）在 demo 上没被验证 | demo 本来就没有「改价」接口，属于目标本身的能力缺口，不是引擎缺口 |
| 7 | **`@Profile` 分叉只测了两个 profile** | 三种以上 profile 的组合没覆盖 | 当前配置（default / vip）已经能暴露「业务维度绑到部署维度」这一类问题 |

---

## 6. 判断记录

我在做的过程中做的判断，都记在这里（有的可以推翻，但请给理由）：

1. **通用骨架留在 `pilot/`，不进 `engine/`。**
   理由：函数层（`GenericHarness.java`）与服务层（`contract_probe.py`）的通用件都还在
   `pilot/` 下，类层单方面搬进 `engine/` 会让「引擎 = 跨层能力」这条边界变模糊。
   代价是守卫要写两套（`engine/check_engine_purity.py` + `check_class_probe_purity.py`），
   我认为这个代价可接受。

2. **差分规则改成「配置派发」而不是「配置覆盖」。**
   `class_rules.RULES` 是 id → 实现的字典，配置里写 id + 参数。
   理由：只做「文案模板化」的话，每加一类判定就要改引擎文件；派发化之后，
   **新增一个判定 = 配置里加一条 + 引擎里加一个函数**，项目仍然不用改引擎。

3. **规则 id 保持稳定，文案可以自由改。**
   `finding_meta.py` 按 id 标注，改文案不影响标注表的对应关系；
   但改 id 会让标注表失配。所以配置里的 id 我一个都没动。

4. **通用规则（`CLASS-NULLABILITY-THREE-WAY-MISMATCH` 这类兜底）留在引擎里，项目不能改。**
   理由：兜底文案是判定口径的一部分，项目能改口径 = 判定不可信。
   项目要定制结论，走 `field_notes`（配置里的专用结论），而不是改兜底。

5. **`ignore_fields` 放在项目侧，不做自动识别「随机量」。**
   自动识别（比如「字段名像 id/timestamp 就忽略」）是猜；项目声明是知识。
   我选择让项目声明，代价是多写一行配置。

6. **demo 的夹具客户建成 VIP 等级。**
   demo 的会员价策略只在 `customer.getLevel() == "VIP"` 时生效 —— 用 NORMAL 客户去比两个
   profile 实例，两边都是原价，**测不出 @Profile 分叉**。夹具建 VIP 是为了让差异只来自
   profile 维度。（这一点是踩出来的：第一版夹具用 NORMAL，实例对比只测出 `timestamp` 差异。）

7. **修 `class_static.py` 的列名推导时，只改推导、不改 jiuyan 的行为。**
   验证方式：jiuyan 所有多词列都显式写了 `@Column(name=...)`，推导结果与之逐字相同，
   所以 jiuyan 的 `class_declaration.json` 语义不变、findings 不变（§3 已逐条验证）。

8. **`extends_base_entity` 键名改成了 `extends_mapped_superclass`。**
   原判定是 `"BaseEntity" in src` —— 那是把项目类名刻在通用层里（守护器当场抓到）。
   新判定是「父类是不是本项目的 `@MappedSuperclass`」，语义更准也更通用。
   这个键**当前没有任何消费方**（我全仓搜过），改它不影响任何产物。

9. **没有为了让 demo 好看去调规则。**
   demo 出的 15 条里有 2 条我一开始觉得「不是缺陷」：
   `CLASS-CHECK-WITHOUT-DTO-COUNTERPART`（shop_product / shop_customer 的 CHECK 在 DTO 层没有对应 ——
   因为这两个表根本没有写入型 DTO）和 `CLASS-NULLABILITY-ALIGNED`（只有 1/2 列可比较）。
   我保留了它们：前者是事实，后者是**通过结论** —— 差分引擎必须同时能说「哪里没问题」。

10. **没有把 demo 的类层结论塞进 jiuyan 的 `finding_meta.py`。**
    demo 有自己的一份标注表（`demos/shop/finding_meta.py`），理由写在那个文件的注释里：
    两个仓库的 finding 共用一套 category/owner 判定会让归属串台。

11. **没有动 jiuyan 的代码。**
    被测仓库 `jiuyan` 全程只读（只 cat 实体与迁移文件）。
    jiuyan 侧被改的只有 `pilot/` 下的类层通用件与 `pilot/class_scenarios.json`（项目声明）。
