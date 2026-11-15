# 文件层探针通用化 —— 结果

> 目标：让文件层（file）能审计任意 Spring Boot 项目，换项目只改配置不改引擎。
> 被测系统 jiuyan 只读；demo-shop 是第二个目标，用来证明「通用」不是口号。
> 口径与守卫：`python3 engine/check_engine_purity.py`（跨层引擎零项目耦合）
> + `python3 probe_residue_check.py --selftest`（残留守卫自检）
> + 对 `pilot/file_harness/` 自查「零项目字面量」。

---

## 0. 一句话

文件层的**动态侧**现在是声明式的：通用骨架
（`pilot/file_harness/GenericFileHarness*.java` 2829 行 + `pilot/file_http_probe.py`）
不含任何项目字面量，项目要「看什么」全部写在项目侧的 `file_cases.json` 里；
jiuyan 的结论改前改后 **18 → 18 条完全等价**（结论级字段 0 差异）；
demo-shop 跑通 **9 用例 / 25 判定 / 不符 0 条**（没崩），
demo 埋的 6 类缺陷里 5 类命中（第 6 类「缓存放可变对象」实测**不成立**，如实记为负结果），
另外挖出 2 个 demo 源文件里没标出来的真缺陷，探针造的行 **0 残留**。

判定层（`pilot/file_diff.py`）**故意保持项目专属、一行未改** —— 它是「这个项目哪些行为算缺陷」
的业务知识所在，和函数层（`fn_diff.py` 硬编码 finding 文案）是同一种分工。
通用的是「怎么测」，不是「什么算缺陷」。

---

## 1. 改了什么

### 1.1 通用层（怎么测）

| 文件 | 行数 | 职责 |
|---|---|---|
| `pilot/file_harness/GenericFileHarness.java` | 651 | 入口：读用例配置 → 逐场景执行 → 落 JSON |
| `pilot/file_harness/GenericFileHarnessBase.java` | 726 | 反射基建：整类实例化、依赖图递归构造、日志捕获、Mock 请求/响应、工具方法 |
| `pilot/file_harness/GenericFileHarnessValues.java` | 587 | 值构造：夹具 / 模板 / 打桩 / 枚举 / record / 实体 / 各种 `$t` 参数形式 |
| `pilot/file_harness/GenericFileHarnessDetectors.java` | 546 | 6 个检查器（形状事实判定，见 §1.3） |
| `pilot/file_harness/GenericFileHarnessObserve.java` | 240 | 观测原语：`measure`（count / contains / lookup / all_of / same_as / log_contains …） |
| `pilot/file_harness/ClassConstPool.java` | 79 | 解析 .class 常量池取 UTF8 常量（本轮新增） |
| `pilot/file_http_probe.py` | 278 | 容器探针插件：真实 HTTP / 真实 DB / 裸 socket + 探针自己清理 |
| `pilot/file_dynamic.py` | 203 | 驱动：编译项目 → 跑 harness → 跑容器探针 → 归一化 → 逐条判定 |

**harness 提供的能力**（全部由用例配置驱动，零项目字面量）：

- 反射整类实例化：目标类 → 递归构造依赖图 → 深度上限防环 → 接口用动态代理打桩
- 打桩表：`类名#方法` → 返回值（`$list` / `$const` / `$optional` / `$echoArg0` …）
- 构造参数规格：`ctor` 段按「类名 → 位置 → 值」给，含嵌套依赖
- 日志捕获：logback `ListAppender` 挂到指定 logger，观测里可断言「日志里出现了什么」
- 调用计数：记录 `repo_calls`，能证明「这一步真的调到了哪个仓库」
- 6 个检查器：形状事实的机械判据（覆写后置条件 / 条件装配分叉 / 实体值相等 /
  缓存里放的东西可不可变 / 封装泄漏 / 跨仓库 HTTP）
- 容器探针：`http_get/http_put/http_post/raw_header/db_compare/cors`，
  外加「探针造的行自己删干净」（`cleanup` 段，主键水位兜底）

### 1.2 项目层（测什么）

| 文件 | 内容 |
|---|---|
| `pilot/file_cases.json`（jiuyan） | 21 个场景（模板 / 夹具 / 打桩 / 构造规格 / 观测视图）+ `http` 段（容器与 DB 探针） |
| `demos/shop/file_cases.json`（demo） | 8 个场景 + 6 个检查器 + 2 个容器探针 + `cleanup` 段 |
| `pilot/target.json` | 加 `paths.file_cases: "file_cases.json"` |
| `demos/shop/target.json` | 加 `paths.file_cases: "file_cases.json"` |
| `pilot/file_diff.py` | **未改**（判定层仍项目专属，见 §6 判断记录 1） |

### 1.3 通用化顺手挖出来的真问题（不是顺手改的，是通用化暴露的）

1. **`Method.invoke` 是虚分派 ⇒ 覆写后置条件检查器恒为空。**
   拿父类的 `Method` 去 `invoke`，跳的仍然是子类覆写 ⇒「父类行为」和「子类行为」永远一样，
   `override_postcondition` **不可能**报出任何差异。demo 的 Liskov 缺陷在检查器层面被漏掉
   （场景 `ENTITY-TOUCH-AUDIT-LOST` 抓到了，但检查器抓不到）。
   修法：`MethodHandles.privateLookupIn(...).unreflectSpecial(...)` 做**非虚调用**。
   **对 jiuyan 零影响**：jiuyan 的用例配置不开这个检查器（§4 已验证）。

2. **`docker logs --since` 的毫秒/微秒竞态 ⇒ 日志探针时好时坏。**
   docker 的日志条目时间戳只到毫秒，而 `--since` 收的是微秒 ⇒ 同一毫秒里写下的日志
   被判成「早于窗口」而丢掉。表现为 `u2028_in_logs` 这次 True 下次 False。
   修法：窗口往前留 2 秒（`LOG_LOOKBACK_S`）。

3. **日志证据取「日志尾巴」⇒ 证据里可能根本没有要找的那条。**
   修法：围绕标记截（`around()`），并只截到标记那一行结束 —— 再往后是容器里
   碰巧同时发生的别的事（实测混进过一条 OTel 导出报错），那部分每次跑都不一样，
   证据就不确定了。需要更多上下文用 `log_lines_after` 显式要几行。

4. **class 文件按文本正则扫 URL ⇒ 证据里出现二进制尾巴。**
   原来正则会吃进常量后面的控制字符，拿到过
   `"http://127.0.0.1:8098}\u0001\u0000\u0004rate\u0001\u0000"`。
   修法：`ClassConstPool` 解析常量池，URL 从干净的 UTF8 常量里取。

5. **`file_static.py` 的产物写死在脚本目录 ⇒ 换目标时覆盖别人的产物。**
   我自己踩了：跑 demo 的静态声明，把 jiuyan 的 `file_declaration.json` 覆盖成了 demo 的。
   修法：写 `project.artifact_dir() / "file_declaration.json"`
   （jiuyan 的 `artifacts_dir` 就是本目录 ⇒ **jiuyan 路径逐字不变**）。

6. **cleanup 计数把 psql 的 `DELETE 0` 标签行当成删了 1 行** ⇒ 报告里的删除数会骗人。
   修法：正则取 `DELETE n` 的 tag 行。

---

## 2. 通用 / 项目特定怎么切

| 文件 | 归属 | 项目相关的部分放在哪 |
|---|---|---|
| `file_harness/GenericFileHarness*.java` | **通用** | 无。类名、样本、打桩、期望全在用例文件 |
| `file_harness/ClassConstPool.java` | **通用** | 无（纯字节码解析） |
| `file_http_probe.py` | **通用** | 无。容器名 / base_url / SQL 全部来自用例文件的 `http` 段 |
| `file_dynamic.py` | **通用** | 无。用例文件路径来自 `target.json` 的 `paths.file_cases` |
| `file_static.py` | 通用（静态侧） | 无。包路径 / 目录来自 `target.json`；产物路径改成 `artifact_dir()` |
| `file_diff.py` | **项目** | 全部：场景 id、finding 文案、严重级、根因文件、修复顺序 |
| `file_cases.json`（jiuyan / demo 各一份） | **项目** | 全部：类名、夹具、打桩、构造规格、断言、期望、容器与 DB |
| `target.json` | **项目** | 源码根、包路径、容器、DB、产物目录、用例文件 |

**切分的三条线**（和类层同一条思路）：

1. **「事实的形状」归通用，「事实的内容」归项目。**
   「子类覆写后后置条件变了」是形状 → 通用检查器判；
   「`ShopOrder` 覆写 `AuditableEntity.touch`」是内容 → 项目声明。
2. **「怎么测」归通用，「测什么」归项目。**
   `http_post` / `db_compare` / `raw_header` 是通用探针；
   「POST /api/orders 让 VIP 客户走两个部署」是项目。
3. **「判定口径」归项目。**
   这条和类层相反 —— 文件层的判定口径（什么算缺陷、严重级、修复顺序）是业务知识，
   所以 `file_diff.py` 保持项目专属。通用化的只是「怎么把行为跑出来并记成可比的数据」。

**通用代码零项目字面量的自查**（自查规则：`jiuyan|banquet|com.demo|127.0.0.1|localhost|demo_shop` 等）：

```
$ grep -rn -E "jiuyan|banquet|com\.demo|127\.0\.0\.1|localhost|demo_shop" file_harness/
零命中
```

框架名（`org.springframework.*` / `ch.qos.logback.*`）只以反射字符串出现 —— harness
**不编译期依赖 Spring**，换 Spring 版本只需要改反射查找逻辑，不需要改用例。

---

## 3. 等价性证据（jiuyan 改前 vs 改后）

**基线**：`/tmp/baseline_file_findings.json`（18 条）、`/tmp/baseline_file_observation.json`（改造前的观测）

| 项 | 结果 |
|---|---|
| 用例数 | 22（21 场景 + HTTP-PROBES）→ 22 ✅ |
| 判定条数 | 58 → 58 ✅ |
| 判定不符数 | 1 → 1（**同一条**：`ADVICE-ENVELOPE.null_body_result_is_null`，基线里就是故意不符的） |
| 每条判定的 `expected` / `actual` / `pass` | 58/58 逐条一致，**0 差异** ✅ |
| finding 条数 | 18 → 18 ✅ |
| finding 的 `id` 集合 | 完全一致 ✅ |
| 结论级 10 字段（severity/kind/layer/cwe/title/detail/root_cause_file/related/note/supersedes） | **0 差异** ✅ |
| 整条 finding（归一化后逐字比） | 仅 **1 条** 不同：`FILE-XFF-CLIENT-IP-SPOOF`，见下 |

**逐条差异解释（没有一条是结论变化）**：

| finding | 差异位置 | 原因 | 结论是否变 |
|---|---|---|---|
| `FILE-XFF-CLIENT-IP-SPOOF` | `evidence.real_container.log_segment` | 截取方式从「日志尾巴」改成「围绕标记的行」。基线里的尾巴是 `n-urllib/3.14\n2026-…request end traceId=…`（从半句开始）；现在是完整的一行 `2026-…Z INFO 1 --- [banquet-backend] … request end traceId=… clientIp=9.9.9.9 …` | **否**。`spoofed_ip_in_logs=true` 不变，`clientIp=9.9.9.9` 仍在证据里，`severity/title/detail` 全部逐字一致 |

**原始观测（raw）的差异**：22 个场景里 5 个场景的 raw 与基线不同，
逐条看过，差异**只有**三类：应用自己写进响应体的墙钟时间戳、
链路追踪 UUID、Tomcat 线程名（`io-8080-exec-N`）。判定值没有一条因此变化。

**确定性**：连续两次跑 jiuyan 的文件层，归一化（时间戳 / UUID / 哈希 / 线程名）后
**0 差异** —— 包括日志探针那一段（§1.3 第 2、3 条修完之后）。

---

## 4. demo-shop 的结果（第二个项目，通用性的证据）

命令：`LAYERED_AUDIT_TARGET=demos/shop/target.json python3 file_dynamic.py`

```
文件层动态 -> demos/shop/artifacts/file_observation.json
  用例 9 个 / 判定 25 条 / 不符 0 条
  检查器 -> demos/shop/artifacts/file_detectors.json
```

**没崩、25 条断言全部成立**。断言是按「缺陷确实存在」写的，所以 pass = 缺陷被复现。

demo 埋了 6 类缺陷：里氏替换违反 / `@Profile` 条件装配 / 实体无值相等 /
缓存放可变对象 / 公开方法返回内部可变集合 / 跨仓库 HTTP 调用。
**5 类命中**；「缓存放可变对象」实测**不成立**（缓存里是不可变 record），
源文件注释与实现矛盾 —— 如实记为负结果，不改判定口径去迎合。
另外 2 条是 demo 没标出来、但实测确实存在的缺陷：额度校验拿报价去比、汇率回退的量纲错。

### 4.1 场景层（8 个场景，各自触发同文件内多方法的配合）

| 场景 | 实测到的事实 | 对应的缺陷 |
|---|---|---|
| `ENTITY-TOUCH-AUDIT-LOST` | `ShopOrder.touch("auditor-42")` 后 `updatedBy=null`；同继承层次的 `ShopCategory.touch("auditor-42")` 后 `updatedBy="auditor-42"` | **里氏替换违反**：子类覆写把父类承诺的审计信息抹掉 |
| `ENTITY-VALUE-EQUALITY` | 同字段的两个 `ShopProduct` 实例 `equals=false`，`hashCode` 不同 | 4 个实体（Product/Customer/Order/Category）都没有值相等 |
| `PRICING-PROFILE-DIVERGENCE` | 同一接口两实现：`StandardPricingStrategy` 对 VIP 收 10000，`MemberPricingStrategy` 对 VIP 收 9000、对 NORMAL 收 10000 | 「谁是会员」这个**客户维度**被写进了**策略实现**里 |
| `PRODUCT-SNAPSHOT-ENCAPSULATION-LEAK` | `snapshot()` 返回的是仓库里的实体本身；改实体名后返回值跟着变成 `HACKED` | **封装泄漏**：公开方法把内部可变对象交出去 |
| `PRODUCT-CACHE-VALUE-SHAPE` | 缓存里的元素是不可变 record `ProductResponse`，`.toList()` 也不可变；改实体不影响缓存值 | **诚实负结果**：源码注释说「缓存里放的是可变对象」，实测不成立 |
| `ORDER-QUOTA-CHECKS-QUOTED-NOT-SETTLED` | 报价 10000、额度 9500 → 抛 `IllegalStateException("超出授信额度")`；而实际结算 9000 **在额度之内** | **校验用错了量**：拿报价去比额度，用户被拒了但业务上根本不超限 |
| `ORDER-FX-FALLBACK-SCALE` | 汇率拿不到回退成 1 → 结算 90（报价 10000 的 1%） | **静默的量纲错误**：回退值 1 被当成「比例」而不是「汇率 1.0」 |
| `FX-CLIENT-FALLBACK` | `FxClient.rate()` 吞掉所有异常回退 `1L` | 跨仓库调用失败时**没有声音**，故障变成金额错误 |

### 4.2 检查器层（6 个通用检查器）

| 检查器 | 结果 |
|---|---|
| `override_postcondition` | 报出 `ShopOrder.touch` vs `AuditableEntity.touch` 的 `divergent_fields: ["updatedBy"]`（父类状态 `auditor-42` / 覆写状态 `null`）—— **修完虚分派 bug 才有**（§1.3 第 1 条） |
| `profile_assembly` | `MemberPricingStrategy`(`@Profile("vip")`) ↔ `StandardPricingStrategy`(`@Profile("!vip")`) 是同一接口的两条互斥装配路径 |
| `entity_value_equality` | 4 个实体全部 `has_equals=false` / `has_hashcode=false` / `two_instances_equal=false` |
| `cacheable_mutable` | `ProductService.listCached`：`element_mutable=false`、`mutation_rejected=true`、`same_instance_twice=false` ⇒ **缓存里不是可变对象**（与源码注释矛盾，如实记录） |
| `encapsulation_leak` | `ProductService.snapshot` 返回 `List<ShopProduct>`（实体），封装泄漏成立 |
| `outbound_http` | `com.demo.shop.client.FxClient` 内嵌 URL `http://127.0.0.1:8098` + 持有 `java.net.http.HttpClient` ⇒ 跨仓库 HTTP 调用 |

### 4.3 容器层（真实部署对比）

| 部署 | profile | 同一请求（demo-vip / 888888 / CNY） | 结算 |
|---|---|---|---|
| `demo-shop-backend` : 8090 | default | HTTP 200 | **888888**（原价，没打折） |
| `demo-shop-backend-vip` : 8091 | vip | HTTP 200 | **799999**（= 888888 × 9/10） |

同一个客户、同一个金额，两个部署结算结果不同 —— **「客户等级」被绑到了「部署 profile」上**。
这是 `profile_assembly` 在真实部署上的落地证据。

### 4.4 零残留

```
_cleanup: DELETE FROM shop_order WHERE id > 268 AND quoted_amount_cent = 888888 → deleted 2
残留守卫通过：容器 0 / ai_ 行 0 / 非种子上传 0
DB 复核：quoted_amount_cent=888888 的行 0 条；shop_order 共 16 行，max(id)=268（回到探针前水位）
```

---

## 5. 剩余缺口

| # | 缺口 | 影响 | 我的判断 |
|---|---|---|---|
| 1 | **判定层仍是 jiuyan 专属**：`file_diff.py` 硬编码场景 id 与 finding 文案，demo 跑它直接 `KeyError` | demo 只能拿到「观测 + 检查器结论」，拿不到「缺陷单 + 严重级 + 根因文件 + 修复顺序」 | 与用户要求一致（「必须保持可用不变，使结论可比」），也与函数层同构。下一步若要 demo 出缺陷单，应把**差分规则也配置化**（类层已经这么做了，可直接抄它的 `diff_rules` 派发做法） |
| 2 | **检查器需要项目给样例入参**：`override_postcondition` 没有样例入参就不下结论 | demo 要在 `detectors.args` 里写 `类名#方法 → 入参` | 检查器判的是「形状」，但**入参取值是项目知识**，引擎不该猜。代价是配置里多几行 |
| 3 | **等价性只在 2 个项目上验证过** | 第三个项目可能还会暴露新的框架签名差异 | 已经踩到 5 个签名差异（Spring 6 `MethodParameter`、`MockHttpServletRequest.addHeader`、logback 1.5 `ListAppender.getList`…），都做成了「按名字 + 兼容度查找」 |
| 4 | **证据体里含墙钟时间** | 文件级 finding 做不到字节级确定性（结论字段确定，证据体里的 timestamp 每次变） | 与函数层/服务层同源。彻底解法是给实测值分桶，但要重拍基线 |
| 5 | **demo 观测里唯一非确定的原始值是 `order_id`**（DB 序列自增，每次 +1） | 归一化后两次跑一致；原始 JSON 不一致 | 保留：它是「真的建了一行订单」的证据，清理记录 `deleted: 2` 与它对得上 |
| 6 | **通用件仍散在 `pilot/` 下**（`file_harness/`、`file_http_probe.py`、`check_class_probe_purity.py`…），不在 `pilot/engine/` | 两套守卫（`engine/check_engine_purity.py` 扫 `engine/`，通用件在 `pilot/` 扫不到） | 与类层判断一致：函数层/服务层/类层/文件层四层的通用件**一起搬**才合理，单独搬一层会破坏一致性 |
| 7 | **harness 反射调用了 Spring 的包私有方法**（`MethodParameter.setParameterType`） | Spring 改版可能失效，表现为某个场景构造不出实例 | 已用「找不到就跳过、不下结论」兜住；换 Spring 大版本时优先看这里 |
| 8 | **没有「真实容器内的类装配」探针** | `@Profile` 在 demo 上是靠「两个部署对比」绕过去的，属环境技巧，不是通用能力 | 这是通用化之后最值得补的一块：容器内拿 `ApplicationContext` 的实际 bean 表，比两个部署更通用 |

---

## 6. 判断记录

我在做的过程中做的判断，都记在这里（有的可以推翻，但请给理由）：

1. **判定层（`file_diff.py`）保持项目专属，不通用化。**
   理由：用户明确要求「必须保持可用不变，使结论可比」；而且文件层的判定口径
   （什么算缺陷、多严重、根因在哪个文件、先修哪个）本质是业务知识，写进通用引擎就会变成
   下一个项目的隐性包袱。通用化的目标是把「怎么把行为跑出来并记成可比的数据」这件事做干净。
   代价：demo 拿不到缺陷单（缺口 1）。这个代价我认为可接受 —— 类层已经把「差分规则配置化」
   这条路走通了，真要做的时候照抄即可。

2. **检查器留在通用层，项目只声明「开不开」和「样例入参」。**
   理由：检查器判的是语言级形状事实（覆写后置条件、条件装配分叉、值相等、封装泄漏…），
   这些口径不应该由项目自己定义 —— 否则每个项目的「缺陷」定义都不一样，跨项目就不可比。
   但**样例入参**（`touch("auditor-42")`）是项目知识，所以放在配置里。

3. **修 `Method.invoke` 的虚分派 bug 时，没有试图保持「旧行为」。**
   理由：旧行为（检查器恒返回空）是错的，不是「口径」。jiuyan 不开这个检查器，
   所以修了它 jiuyan 的结论一条都不变（§3 已逐条验证）。这正是「先通用化、再对比」的价值：
   通用化把 jiuyan 一直没跑过的检查器变成了真能跑的东西。

4. **`around()` 默认只截到标记那一行结束（不截 +600 字符尾巴）。**
   理由：证据要**确定性**。多截的尾巴是容器里同时段发生的别的事（实测混进过 OTel 导出报错），
   两次跑不一样。要更多上下文就用 `log_lines_after` 显式要几行 —— 让「要多一点」成为显式决定，
   而不是默认吃到多少算多少。

5. **日志窗口往前留 2 秒，宁可多拿日志也不要漏。**
   理由：docker 的毫秒/微秒竞态是真实存在的（同一毫秒写下的日志会被丢掉）。
   多拿 2 秒的代价是「可能夹带无关日志」，而无关日志已经被第 4 条的「围绕标记截」挡住了。

6. **demo 的 `cacheable_mutable` 检出「其实不可变」，我如实记录，没有改判定口径去迎合 demo 埋的缺陷。**
   demo 源码注释写「缓存里放的是可变对象」，实测是不可变 record + 不可变 List。
   我把这条记成**诚实负结果**（`element_mutable=false`、`mutation_rejected=true`），
   冲突本身是更值钱的发现 —— 一个连注释都和实现对不上的地方，通常还有别的对不上。

7. **`file_static.py` 的产物路径改成 `artifact_dir()`，jiuyan 的路径保持逐字不变。**
   理由：jiuyan 的 `target.json` 没有 `artifacts_dir`，默认就是脚本所在目录 ⇒ 行为不变；
   demo 指向自己的目录 ⇒ 两个目标的产物不再互相覆盖（我自己踩过这个坑：demo 的静态声明
   覆盖了 jiuyan 的 `file_declaration.json`，已重新生成 jiuyan 的并验证结论不变）。

8. **删掉了 `pilot/file_harness/_old_FileHarness.java.bak`。**
   理由：它是旧的 jiuyan 专属 harness，含大量 `com.jiuyan.banquet.*` 字面量。
   留在通用目录里会让「通用代码零项目字面量」的自查说不清（它不会被编译，但会被 grep 到）。
   需要查旧实现可以看 git 历史。

9. **demo 的 25 条断言全部按「缺陷确实存在」来写，所以「不符 0 条」= 全部缺陷被复现。**
   理由：断言的语义是「期望看到缺陷行为」。如果写成「期望一切正常」，那 demo 反而会绿，
   探针的证明力就没了。判定与期望分离这件事本身也是这套系统的一贯做法。

10. **没有动被测仓库 `jiuyan`。**
    全程只读（只 cat 源码）。jiuyan 侧被改的只有 `pilot/` 下的通用件与
    `pilot/file_cases.json`（项目声明）。

11. **等价性判在「结论级」而不是「字节级」。**
    理由：基线证据里嵌着 `OffsetDateTime.now()` 与 identity hashCode，
    字节级一致在物理上不可能（两次跑必然不同）。
    所以我用三档判定：① 58 条判定的值逐条一致 ② 18 条 finding 的结论级字段 0 差异
    ③ 整条 finding 只有 1 条的 `evidence` 不同，且原因写清楚了。
    这是我能给出的最强证据，不是「差不多」。
