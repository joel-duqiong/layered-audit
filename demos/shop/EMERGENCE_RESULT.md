# 涌现裁决结果（demo-shop 真跑）

> 证据边界（2026-11-15 更新）：判据现在**有实现依据**了 —— V3 迁移建了 `shop_quota_occupancy` / `shop_ledger_entry` 两张记录表，`QuotaService.occupy` 按报价写占用、`LedgerService.book` 按实付写分录，两条记录真的落库（夹具契约锁在 `test_d6_fixture.py`）。在此之前（2026-11-14 的记录）额度检查只做比较、记账只写订单字段，判据没有实现依据，本文的合取真值表是编造的 —— 那段历史保留在 `../../CLAUDE.md` 硬约束 5 里。
>
> **仍然成立的边界**：规则由本夹具自己的设计文档声明（README D6），所以它依然是**合成案例** —— 能证明「这套系统能判出这一类涌现」，不能当作真实项目缺陷的证据，也不能用作盲测召回的金标准。见 `../../EXPERIMENT_PROTOCOL.md`。

> 这份文档记录**真实裁决结果**，不是设计说明。
> 产物：`demos/shop/artifacts/emergence_chains.json`（证据全在里面）
> 引擎：`pilot/engine/emergence.py`（通用，零项目字面量）
> 探针：`demos/shop/emergence_probe.py`（项目侧，真 HTTP + 真 DB）
> 自证：`demos/shop/emergence_selftest.py`（35 项断言全过）

---

## 一、一句话结论

**`EMERG-QUOTA-LEDGER-DIVERGENCE` 判为 `EMERGENT_CONJUNCTIVE`（涌现 · 组合型）**
—— 单独注入任何一个都不触发「额度占用额 == 账本记录额」，
**两个合起来才触发**，偏离量 **90000 分（报价的 10%）**。
归属层：**integration（own）**，根因指纹 `order.quota_ledger_amount_divergence`，
三态：**verified**。

---

## 二、真实裁决结果（数字）

目标不变式 `BI-QUOTA-LEDGER-PARITY`：`quoted_amount_cent == settled_amount_cent`
（额度按报价占，账按实付记，两者必须相等）。

| # | 场景 | 注入集合 | 服务实例 | 客户等级 | 报价(分) | 实付(分) | 目标不变式 | 偏离量 |
|---|------|----------|----------|----------|-----------|----------|------------|--------|
| 1 | 基线 | — | default | NORMAL | 900000 | 900000 | 成立 | 0 |
| 2 | **只注 A** | `INJ-TIER-VIP` | default | VIP | 900000 | 900000 | **成立（不触发）** | 0 |
| 3 | **只注 B** | `INJ-PROFILE-VIP` | vip | NORMAL | 900000 | 900000 | **成立（不触发）** | 0 |
| 4 | **A+B** | 两个都注 | vip | VIP | 900000 | **810000** | **违反** | **90000** |

- **F(∅) = 0**（基线不违反，违反确实是注入造成的）
- **F({A}) = 0**、**F({B}) = 0**（单独注都不触发 —— 这是「涌现」的定义性条件）
- **F({A,B}) = 1**（合起来违反）
- leave-one-out：去掉 A → 不违反；去掉 B → 不违反 ⇒ **最小核心集 = {A, B} = 缺陷集**，
  没有冗余注入 ⇒ 报 `EMERGENT_CONJUNCTIVE`（不是 `..._CORE`）

### 两条注入各是什么

| 注入 | 层 | 代表的低层根因 | 条件怎么施加 | 单独注入时的实测 |
|------|----|----------------|--------------|------------------|
| `INJ-TIER-VIP` | service | `pricing.member_tier_bound_to_deployment_profile` | 数据行：客户等级改 `VIP` | default 部署里 VIP 客户**拿不到九折**（实付 900000），会员价不变式被违反 |
| `INJ-PROFILE-VIP` | service | `order.quota_checked_on_quoted_amount_ledger_on_settled` | 装配上下文：请求打到 **vip profile 实例**（8091） | vip 部署里**普通客户**不打折（实付 900000），会员价不变式成立 |

合起来：vip 部署 + VIP 客户 ⇒ 打九折 ⇒ 实付 810000 < 报价 900000
⇒ **额度按 900000 占住，账上只有 810000，差 90000 无对应账目**。

---

## 三、为什么不是 `NOT_EMERGENT_INDEPENDENT`（也不是别的六种）

裁决按 DESIGN.md §4.2 的七种分类，优先级从高到低逐条排除：

| 裁决 | 本例是否成立 | 依据 |
|------|--------------|------|
| `FALSIFIED_CHAIN` | ❌ | 基线 F(∅)=0（不违反），合起来确实违反，链的前提成立 |
| `EMERGENT_MASKING` | ❌ | 没有「单注触发、合起来反而不触发」的现象：两个单注都不触发 |
| `EMERGENT_AMPLIFIED` | ❌ | 没有单注能触发，谈不上「错得更狠」；合起来偏离量 90000 不与任何单注比较 |
| `EMERGENT_CONJUNCTIVE_CORE` | ❌ | leave-one-out 求出的最小核心集 = 缺陷集（2 条 = 2 条），没有更小的核心 |
| **`EMERGENT_CONJUNCTIVE`** | ✅ | **合起来违反、每条单独都不违反** |
| `NOT_EMERGENT_ADDITIVE` | ❌ | 需要「单注各自也能触发」——本例单注都不触发，前提不成立 |
| `NOT_EMERGENT_INDEPENDENT` | ❌ | 需要「A 和 B 各有缺陷但互不相干」——本例 **A 和 B 单独都不产生任何缺陷**，它们只有在**组合**时才共同构成一个缺陷 |

**关键区别**：`NOT_EMERGENT_INDEPENDENT` 是「两个错各错各的，集成层不用管」；
本例是「两个条件单独都是无害的，凑一起才出错」——
**缺陷不在任何一个单点，而在它们的组合上**，所以必须由集成层开单，
低层两个根因只能作为佐证引用（`related_root_causes`），不得重复开单。

---

## 四、三态（ground_truth）怎么定的

| 三态 | 含义 | 什么时候判成它 |
|------|------|----------------|
| `verified` | 结论**已由实测证据确认** | 链跑通、基线不违反、合起来违反、单注都不违反，且所有参与裁决的注入都通过活性检查 |
| `unverified` | 链**判不了**（不是「没问题」，是「没结论」） | 没有注入通过活性检查 / 基线场景执行失败 / 基线本身就违反目标不变式（违反不是注入造成的） |
| `mutant_survivor` | 链判出来了，但**有注入在这个环境里是死的** | 有 active 注入的活性检查不通过 —— 结论只对「活着的那些注入」成立 |

本例 = `verified`：两条 active 注入都通过活性检查（见下节），基线干净。

> 降级是**自动且不静默**的：死掉的注入会被**剔除出分母**（不进核心集、不参与单注判定），
> 同时在 `warnings` 里点名告警，产物里原样保留。

---

## 五、活性检查（alive_check）的语义

**「活」= 单独施加这条注入，能在它自己的层上看到症状**；看不到 = 这条注入在当前环境里是死的。

| 注入 | 活性检查场景 | 探针不变式 | 实测 | 判定 |
|------|--------------|------------|------|------|
| `INJ-TIER-VIP` | default 实例 + VIP 客户 | `BI-VIP-MEMBER-PRICE` | **violated**（实付 900000，应为 810000） | 活 |
| `INJ-PROFILE-VIP` | vip 实例 + VIP 客户 | `BI-VIP-MEMBER-PRICE` | **satisfied**（实付 810000） | 活 |

两条都活：说明「VIP 等级」这个条件真的进了定价路径（default 实例上它被部署环境吞掉了），
「vip profile」这个装配条件也真的换了策略实现（vip 实例上会员价生效）。

活性检查的两种「期望」都在目录里显式写着（`expect: violated` / `expect: satisfied`），
不是探针自己猜的。

---

## 六、敏感性分析：第三条注入为什么 `active=false`

目录里还有一条 `INJ-FX-NONBASE`（请求外币 → 跨仓库汇率 92）。
它**单独就能**让报价 ≠ 实付（实付 828000，偏离 72000）——
**单注即可触发的缺陷不是涌现**。所以它 `active=false`，不参与本链裁决，
只在敏感性分析里跑一次做对照：

| 注入 | 单注 F | 偏离量 | 反事实裁决 | 含义 |
|------|--------|--------|------------|------|
| `INJ-FX-NONBASE`（汇率） | **1** | 72000 | `NOT_EMERGENT_ADDITIVE` | 单注就触发 ⇒ 这条一旦激活，缺陷就不「涌现」了 |

对照结论：**本链的「涌现性」不是随便凑出来的** —— 换一个注入位置（汇率），
同样的不变式就退化成普通缺陷。涌现性来自**具体的合取方式**，不是来自不变式本身。

---

## 七、怎么复跑 / 产物在哪

```bash
cd demos/shop
python3 emergence_probe.py                                  # 裁决全部链，落产物
python3 emergence_probe.py --chain EMERG-QUOTA-LEDGER-DIVERGENCE
python3 emergence_probe.py --alive-check INJ-TIER-VIP       # 单跑一条注入的活性检查
python3 emergence_selftest.py                               # 自证（35 项断言）
```

产物：`demos/shop/artifacts/emergence_chains.json`
- `chains[].steps[]`：每一步的方法 / 路径 / 状态 / 证据文本（活性检查 2 步 + 场景 4 步）
- `chains[].observations[]`：每个注入集合的 F 值与偏离量（裁决的原始输入）
- `chains[].sensitivity[]`：敏感性分析
- `findings[]`：符合 10 字段证据 schema 的发现，可被归一化器直接吃
- `decision_fingerprint`：**裁决指纹**（只含裁决相关字段，不含自增 id / 时间戳）

---

## 八、零残留与确定性（机器校验，不是口头保证）

| 约束 | 怎么保证 | 本次实测 |
|------|----------|----------|
| DB 无残留 | 探针数据一律 `ai_probe_` 前缀；跑完按 `starts_with(列, 'ai_probe_')` 自删；**用同一个谓词**复查计数，残留时打印残留 id 并以退出码 1 失败 | **0 行**（外部独立复查：探针表 0 行 / 客户表只剩 2 行种子数据） |
| 无残留容器 | 本探针**不起任何容器**，只复用已有的 `demo-shop-backend` / `-vip` | 0 新增容器；本任务排查期间遗留的调试容器 `demo-dbg`（8092，vip profile）已 `docker rm -f` |
| 确定性 | 同一输入连跑两次，比对 `decision_fingerprint` | `f10753a38f7ef780` == `f10753a38f7ef780` |
| 不改被测代码 | 注入只改「请求字段 / 数据行 / 装配上下文」，一行业务代码都没动 | `git diff` 里 demo 业务代码 0 改动 |

> 订单 id（160/161/…）每次跑都会变 —— 那是数据库自增，不进指纹。
> 指纹只覆盖：verdict / owner / category / 核心集 / 三态 / 每个注入集合的 F 值与偏离量。

**第一次实现的残留校验是假的（已修）**：清理用的谓词是
`customer_name LIKE 'ai_probe_\_%'`，而探针前缀 `ai_probe_` **本身就以 `_` 结尾**，
拼上转义的下划线后图案变成「`ai_probe_` + 字面下划线」，要求名字里出现**两个**
下划线 —— 于是 `DELETE` 一行都删不掉，而计数查询同样匹配不到任何行，
返回 0，「零残留」就成了自证的假象（实测：库里留了 3 个客户行 + 1 个订单行，
探针仍然报 0 行）。现改用 `starts_with()`：纯前缀比较，没有通配语义，
写错会立刻暴露成残留行而不是静默通过。见判断记录 14。

---

## 九、接线（这次为了让结论能被审计系统收口，改了什么）

| 位置 | 改动 | 为什么 |
|------|------|--------|
| `layers.yaml` | 新增集成层类别 `integration.emergent_masking`（+ oracle `differential_injection`）；角色数 19→22 | 掩盖型裁决要开单，契约里必须先有这个类别；**契约变更走提交**，不在运行期动态改 |
| `pilot/engine/emergence.py` | 通用裁决引擎（七裁决 + 活性门控 + leave-one-out + 出单） | 新增，零项目字面量 |
| `pilot/engine/invariants.py` | 新增 `deviation(detail)`：从判定细节算偏离量 | 放大效应裁决要比较量级，需要通用算术，不认识业务 |
| `demos/shop/emergence_injections.json` | 注入目录（1 条链 / 3 条注入 / 2 个实例 / 活跃性声明） | 项目侧配置 |
| `demos/shop/invariants.json` | demo 的不变式声明（2 条不变式 + 实体读法） | 见判断记录 2 |
| `demos/shop/emergence_probe.py` | 项目侧执行器 + 驱动（真 HTTP、真 DB、敏感性、清理） | 项目侧 |
| `demos/shop/emergence_selftest.py` | 自证脚本（35 项断言） | 自证用可执行脚本，不新建 pytest 套件 |
| `demos/shop/target.json` | 登记证据源（layer=integration）+ `emergent_masking` 义务模板 + `carriers`（保真度） | demo 目标的绑定 |
| `demos/shop/finding_meta.py` | demo 的人工标注表（新建） | demo 的绑定原本就指向它，但文件不存在 —— 绑定是断的 |
| `pilot/finding_meta.py` / `pilot/target.json` | **未改** | 见判断记录 6 |

归一化 + 归并（用 demo 绑定跑）：

```
1 条发现 → 1 条根因（order.quota_ledger_amount_divergence，owner=integration）
硬违规 0 条；保真度 container；跨根因关联 2 条（两个低层根因作为引用）
```

---

## 十、判断记录（遇到的问题自己拍了板，理由写在这里）

1. **注入必须是「条件注入」，不能改代码** —— 所以第二个合取项用**装配上下文**
   （换 profile 实例，8090 default ↔ 8091 vip）表达，而不是「改一行 Java」。
   目录里 `mutation.kind` 只有三种：`data`（数据行）/ `request`（请求字段）/
   `instance`（装配上下文）；换实例就是「同一份代码、同一请求、换个部署维度」。
   被测系统全程只读。

2. **不变式声明放在 `demos/shop/invariants.json`，没放 `pilot/invariants.json`** ——
   pilot 那份是**单实体**格式（一个实体 + 一批不变式），demo 需要**实体列表** +
   两条跨层不变式（一条服务层 `BI-VIP-MEMBER-PRICE`、一条集成层
   `BI-QUOTA-LEDGER-PARITY`）。硬塞进 pilot 那份会破坏 jiuyan 的配置，
   所以在 demo 侧放等价声明，判定器（`pilot/engine/invariants.py`）完全复用，没写第二套。

3. **「活性」的判定标准自己定了**：单独施加注入后，在**它自己的层**上能看到症状
   （被违反的不变量）就算活。理由：注入的目的是激活某个缺陷条件；
   如果这个条件在当前环境里根本不生效（改了个没用的字段、请求打错实例），
   它对「组合是否触发」没有任何贡献，留在分母里只会污染裁决。
   两种期望（violated / satisfied）都在目录里显式声明，不靠探针猜。

4. **汇率注入 `INJ-FX-NONBASE` 设 `active=false`** —— 它单独就能触发目标不变式
   （偏离 72000）。留在 active 里会直接改变本链的裁决结论（变成 `NOT_EMERGENT_*`），
   而且掩盖真正想演示的组合效应。改成敏感性分析里的对照项，价值反而更大：
   它证明**涌现性来自具体的合取方式**，换个注入位置同样的不变式就退化成普通缺陷。

5. **`_CORE` 与普通 `CONJUNCTIVE` 的分界**：只有 leave-one-out 求出的最小核心集
   **严格小于**缺陷集时才报 `_CORE`。本例核心集 = 缺陷集（2 = 2），故报
   `EMERGENT_CONJUNCTIVE`（DESIGN.md §4.2 的字面口径）。

6. **证据源登记在 `demos/shop/target.json`，没登记进 `pilot/target.json`** ——
   `pilot/target.json` 是 **jiuyan** 的绑定（`"target": "jiuyan"`），把 demo-shop
   的发现登记进去会让 jiuyan 的 `all_findings.json` / `root_causes.json` /
   覆盖矩阵混进**另一个仓库**的结论，把已有的 71 条根因基线弄脏。
   框架本来就是「一个目标一份绑定 + 一份标注表」（demo 的 `target.json` 里
   `annotation_table` 指向的就是 `demos/shop/finding_meta.py`），
   按同一套机制登记在 demo 侧才是对的。`demos/shop/finding_meta.py` 原本**不存在**
   （绑定是断的），这次补上。

7. **契约变更走提交**：`integration.emergent_masking` 是新类别，
   `check_category()` 会在契约里没有该类别时直接报错（不开单、不静默降级）。
   掩盖型裁决还额外校验了「掩盖者所在层」的那个类别。契约改动随本次提交一起落库。

8. **`NOT_EMERGENT_ADDITIVE` 与 `NOT_EMERGENT_INDEPENDENT` 的分界按 DESIGN.md §4.2
   的字面口径**：「相交」→ ADDITIVE，「互不相交」→ INDEPENDENT。
   组合额外引入了新不变式违反这件事，只写进裁决理由、不改变分类
   （单注已经能触发，就不是涌现）。

9. **owner = integration（own），两个低层根因只引用** ——
   attribution.rule：「owner = 能给出可复现证据的最低层」。
   本例里额度检查那层和记账那层**各自都给不出**「两者口径分裂」的可复现证据
   （单层测试全绿），能给出证据的最低层就是集成层。
   低层两个根因（`order.quota_checked_on_quoted_amount_ledger_on_settled`、
   `pricing.member_tier_bound_to_deployment_profile`）以
   `related_root_causes` 引用，不重复开单。

10. **放大阈值 1.5× 写死在引擎常量里**（`AMPLIFICATION_RATIO`）——
    契约（layers.yaml）没定义量级门槛，也没人为它定过值；
    先取一个显式可见、可改的常量，而不是藏进判断逻辑里。

11. **三态里的 `mutant_survivor` 只看 active 注入** ——
    `active=false` 的条目只是敏感性素材，它没被激活**不等于**它是死的；
    一开始把所有条目都算进去，导致本例三态被误降级，已修。

12. **探针自证用可执行脚本（`emergence_selftest.py`），没有新建 pytest 套件** ——
    仓库的回归资产在 `/Users/june/tests/auto-api`，按要求没动。

13. **引擎纯度**：本次新增/改动的 `pilot/engine/emergence.py`、
    `pilot/engine/invariants.py` **零违规**（自证脚本里逐文件断言）。
    写这份报告期间，仓库级 `check_engine_purity.py` 一度为红，违规文件是
    `pilot/engine/store.py`（并发会话在写的分片存储，硬编码了 jiuyan 的容器名与
    SQL 字面量）；那是别的工作的产物，现已由其作者修正，最新一次全仓检查
    13 个引擎文件**全部通过**。

14. **残留清理的谓词从 `LIKE` 换成 `starts_with`** —— 原来的
    `LIKE 'ai_probe_\_%'` 是个**静默失败**：前缀 `ai_probe_` 末尾已经有一个下划线，
    LIKE 里下划线是单字符通配符，要按字面量匹配还得再转义一个，图案最终要求
    「两个下划线」，`DELETE` 删 0 行、计数也是 0，机器校验看着是绿的，库里其实留着
    探针数据。判定：**校验必须和删除用同一个谓词，而且谓词不能有通配语义** ——
    宁可漏删被当场发现，也不能「静默通过」。改完重新跑：
    外部独立复查 `shop_order` / `shop_customer` 的 `ai_probe_` 行数均为 0。

15. **顺手清掉了本任务排查期间遗留的调试容器 `demo-dbg`**（8092 端口，vip profile，
    与 `demo-shop-backend-vip` 重复）—— 按「临时容器用完 `docker rm -f`」的约束，
    它既不在目录（`emergence_injections.json` 的实例表只有 8090/8091）里，
    也不在文档里，留着就是残留。
