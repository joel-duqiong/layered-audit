# 共享经验册 —— 踩过的坑与有效做法

> **每个 Agent 开工前读这份,交付前把你新踩的坑追加进来。**
> 目的:同一个坑不许踩第二次。重复踩坑是浪费资源,不是勤奋。
>
> 规矩:每条必须带**实测证据**(文件:行 / 数字 / 可复现命令)。
> 没有证据的「经验」不收——那是猜测,不是经验。
> 好的做法也写,不只写坑。

---

## 一、同一个坑在三个独立地方重复出现(最贵的一课)

### 坑 1:子串匹配当证据 —— 三处独立复发

| 出处 | 具体形态 | 实测后果 |
|---|---|---|
| `capability_map.py` | `if symbol in test_text`,测试根是 **Python** 套件、源码是 **Java** | **41/42 条 `confirmed_test` 是假的**。`list` 命中 `-> list[dict]`、`update` 命中 `dict.update`、`ok` 命中 `hookwrapper` |
| `relation_net.py:183` | `any(h in method for h in WRITE_HINTS)` | `findByIdAndIsDeletedFalse` 含子串 `delete` → **11/26 条 WRITES 是只读方法**,污染下游 38.6% 的涌现候选 |
| `oracle_harvest.py` | 结论匹配用子串 | 「**不一致**」包含「一致」→ 分级判反 |

**根因**:子串匹配没有词边界,且跨语言/跨上下文时命中完全无关的东西。

**防复发守卫**:
- 一律 **token 边界匹配**,永不子串。大小写可归一化,但**不得退化成子串**。
- 跨语言证据必须**可核实**(如测试真的请求了该细胞对应的 HTTP 路径),不是「字符串出现过」。
- 回归守卫必须同时锁两条:「大小写不同仍匹配」与「子串不得匹配」。

**通用检查**:写任何 `in` / `contains` / `startswith` 判据前,先问「这个词会不会是另一个词的一部分」。

---

### 坑 2:同义反复的测试守卫(看起来绿,其实没守)

**证据 A(V4 变异测试 M7,决定性)**:把 OpenAPI reference **修成合法 JSON Pointer,测试反而变红**。
说明测试断言的是**引擎当前的错误输出**,而不是正确行为。

**证据 B(V5)**:`capability_map.verify_links` 的 `field_write` 分支**根本没有跨实体检查**——
它声称守住的东西,代码里不存在。

**防复发守卫**:
- 测试必须锁**正确行为**(如「reference 是合法 JSON Pointer 且能在原文解析到该字段」),
  不是锁字符串等于引擎当前产出。
- **每个新守卫必须做变异测试**:故意改坏判定,看它是否变红。不红就是同义反复,删掉重写。
- 变异测试要在**同进程内 monkeypatch**(subprocess 会重新导入,变异不生效)。

**证据 B 已修 + 更锐利的形态(W1 实测,2026-11-15)**:第一次补的检查**仍是同义反复**——
`matched = [(ent,fld) for (ent,fld) in scopes if …]`,再断言 `ent in {e for e,_ in scopes}`:
**从被验集合自身迭代出来的成员,永远属于该集合**,这个检查在数学上不可能失败。
改成从**边自己声称的作用域**(`evidence` 里的 `作用域 X.Y`)独立回查原始关系网:
声称的实体不在判据作用域内 → 跨实体;声称的字段该细胞没写 → 复核不过。
变异实测:篡改 evidence 的作用域 `PackageType.name`→`Category.name` → 跨实体变红;
篡改 `unit_key` → 复核不过变红。**通用纪律**:独立复核的输入必须**外生于被验对象**
(产物自己声称的内容 + 原始数据),不得从被验对象的构造路径里再推导一遍。
W1 本轮 12 个变异全部被守卫抓住、基线 0 失败,才算守卫成立。

---

### 坑 3:聚合指标掩盖分项失败

**证据**:W4 报「reference 行号命中率 **100%**」。V4 自建独立校验器实测 **74.58%**——
ddl 73/73、annotation 66/66、document 37/37 都是 100%,但 **openapi 0/60**。
四类混在一起报,把一类的完全失败藏进了平均值。

**防复发守卫**:
- 分项指标**必须分开报**,不得只报聚合值。
- 报「100%」之前先问:分母里有几类?每类各是多少?
- 独立验证者**不得复用被验方的校验脚本**(V4 自建映射与判据,才发现这个)。

---

## 二、验证与协作的坑

### 坑 4:验到移动靶

**证据**:V4 Read 的第 401 行与它后来 grep 的第 401 行内容不同——W4 正在实时改同一批文件。
V5、V7 同样遇到。V7 还发现 `edges.json` 从 786 变成 **840**(W6 加了同类边),
导致 arch_rules 声称的「59 unknown」复现不出,**实测 363**。

**防复发做法**:
- 验证前**冻结快照 + 报 sha256**,在快照上验。
- 交付方**改完再报**,不要边改边让验证者追。
- 上游产物变了,**下游必须重新基线**,不得引用旧数字。
- **W8 实测两例**:任务书说修前「287 passed」,开工实测 **382 passed + 5 subtests**(并发 Agent 持续加测试,交付时 485);W8 交付后重跑桥,47/236 已变 **14/236**——`oracles_harvested.json` 被上游改了发射词汇。引用任何数字前先重测,报数字带时间戳。
- **W1 实测(反向:任务书的指控是陈旧快照)**:任务书称「你的文件有 8 个红,交付前必须闭环」并点名一个测试。按同一命令重测当前磁盘:**我的文件 0 红**,全量 **3 failed / 595 passed**,3 个红全在 `test_arch_rules.py`(`arch_rules.py:125 KeyError: 'method'`,W3 在飞);稍后 W3 修好,全量 **621 passed / 0 failed**。**正确动作是重测 + 把红归到其拥有者,而不是为了变绿去删断言或改正确的代码**。同时把产物 sha256 与三个输入哈希钉进产物 `reproduce` 块,让「是否陈旧」变成可比对的客观量,而不是口头争论。

### 坑 5:把在飞中间态当成回归(我自己犯了两次)

**证据**:先误判 W3 的 `KeyError: 'rules_in_scope'` 为跨 Agent 集成回归(实为它自己在飞);
又误报「34 failed」为回归(实为 W2/W4 重写中间态,重测后 0 failed)。

**防复发做法**:**断言回归前必须重测并列出失败名**。单次快照不是证据。
**W8 第二轮归因实例**:全量 3 红,逐个取证——1 红是 W7 交集守卫的设计内漂移警报
(W8 扩原语、桥未同步,预期红);2 红来自并发新增的 `test_contract_seams.py`,
与 W8 diff 无关:`frame_check.py` 只 import oracle_bridge(不 import 两个求值器),
确认门控白名单 `("user","document")` 在 W8 diff 里零改动。**新守卫暴露的既有设计缺口
不是我的回归,但必须逐条给证据归因,不得笼统说「全绿」或笼统背锅。**

### 坑 6:cwd 漂移导致假失败

**证据**:Bash 调用之间 cwd 会重置。在错误目录跑 `check_engine_purity.py` 得到 **rc=1**,
看起来像纯净度违规,实际是路径不对。另一次 `&&` 链在崩溃处短路,purity 根本没跑。

**防复发做法**:命令一律用绝对路径或显式 `cd`;**rc≠0 先确认命令真的跑了**。

### 坑 11:封闭集有三个物理载体,改一个不同步另两个(W8)

**现象**:W8 给两个求值器加了 3 个原语、79 条测试全绿,但桥重跑仍 47/236,差值 0;
随后采集器改发射求值器原语名,桥又不认,**33 条原可映射判据凭空消失**(47→14)。

**实测证据**:
- `/tmp/w8_before.json` 与 `/tmp/w8_after.json` 逐字节相同(`report identical: True`)——
  求值器扩了,`oracle_bridge.py:19-22` 的硬编码 `CLOSED_PRIMITIVES`/`_NO_COUNTERPART`/`_CMP_OPS` 没扩。
- `oracles_harvested.json` 改发射 `field_gte_const`(25 条)/`field_in_set`(8 条)后,
  桥报「未知原语」28 条 confirmed 被拒(另 5 条 needs_confirmation 被正确排除);
  同一命令同一输入,可映射数 47/236 → 14/236。

**根因**:「封闭集」同时活在三个地方——求值器 dispatch 分支、桥的转录表、采集器的输出词汇,
且分属不同 Agent 的文件所有权。任何一处单方面变更,端到端计数就静默漂移或倒退。
**单测全绿 ≠ 端到端解锁。**

**防复发守卫**:
- 扩原语集前先 `grep CLOSED_PRIMITIVES` 枚举**所有**载体与消费者;验收用端到端计数(桥输出),不只单测。
- 跨所有权边界时,交付报告必须写出对方的**精确改动清单**(文件:行 + 映射条目),不许只说「请更新」。
- 采集器/桥/求值器三方原语词汇必须同一时刻可逐条转录;报差值带时间戳与输入 sha256(见坑 4)。

**W7 补充(2026-11-15,修复 + 把守卫 mechanize)**:

修复实测(**同一输入**跑修前/修后两版桥,避免坑 8 的归因重复计算):

| 输入 sha256(前 8) | 总数 | 修前可映射 | 修后可映射 | 差值 | 丢失 |
|---|---|---|---|---|---|
| `57b04c9f` | 236 | 14 | 177 | +163 | 0 |
| `181007d6` | 271 | 45 | 203 | +158 | 0 |

`181007d6` 上 gained=158 / **lost=0**,分解:`not_null` 136 + `not_blank` 18 + `field_cmp{op:>}` 4。
`57b04c9f` 上 +163 = 新原语 135 + **直通 28**(22 `field_gte_const` + 6 `field_in_set`)。

**新发现:上游词汇会来回摆,只修当前快照等于没修。** `57b04c9f` 有 34 条已是封闭原语的断言,
`181007d6` 有 **0** 条(全摆回采集器侧名字)。所以桥必须**两套词汇同时认**:
采集器侧走映射,已是封闭原语走**直通(逐字复制,不重写语义)**。只按当前快照补映射表,
下次上游一摆就又静默丢判据。

**已 mechanize 的守卫**(`pilot/tests/test_oracle_bridge.py`,135 条全绿):
- `test_closed_set_is_exactly_the_evaluator_intersection`:用正则从**两个求值器源码**抽 dispatch
  分支求交集,断言 `CLOSED_PRIMITIVES == 交集`。任一侧增删原语立刻变红 —— 把「三个物理载体」
  的漂移从人工 grep 变成机器判红。
- `test_both_vocabularies_converge_to_identical_assertion`:6 对采集器侧/求值器侧词汇必须
  产出**逐字相同**的封闭断言。
- `test_mixed_vocabulary_corpus_loses_nothing`:两套词汇混合语料,`unmappable == []`。
- `test_passthrough_copies_and_does_not_alias_input`:直通必须 copy,下游改断言不得污染采集产物。

**变异测试 5/5 变红**(同进程 monkeypatch,坑 2 要求):M1 去掉直通(复现本回归)→ **24 红**;
M2 把 `>` 放宽成 `>=` → 4 红;M3 直通改写语义 → 15 红;M4 直通别名输入 → 21 红;
M5 缩小封闭集 → 3 红。基线 135 绿。

**我自己踩的二次坑**:旧测试把「`not_null` 永久不可映射」「`>` 不可映射」当真理锁住,
补原语后 **9 条守卫变红**。那是**调节器的属性,不是判据的属性**(见坑 7 W8 行)。
守卫要锁**语义等价与极性**(> 只能到 `field_gt_const`,不得到 `field_gte_const`),
不锁「当前封闭集有多大」。锁后者 = 把 diversity deficit 固化成回归。

**W8 第二轮补充:封闭集还有第四个载体——守卫里的提取正则。**
W7 的交集守卫用 `re.findall(r'"(field_\w+)"', …)` 从 rule_assessment 源码抽原语名
(`test_oracle_bridge.py:404-407`)。W8 新增 `text_not_contains`(无 `field_` 前缀)后,
**该正则看不见它**:即使 W7 把 CLOSED_PRIMITIVES 同步到 13,交集仍少一个,守卫照样红。
实测:W8 第二轮交付时该守卫红,差集里就有命名前缀不在提取模式里的原语。
**根因**:守卫把「原语都叫 field_*」这个命名约定硬编码进了提取模式——约定本身成了
无人声明的第四份契约。**防复发**:扩封闭集时枚举所有守卫的提取模式一并核对;
跨 owner 同步清单必须包含「守卫提取正则也要认新命名」这一条,不只 CLOSED_PRIMITIVES。
**(解决状态:W7 已把提取正则放宽为 `([a-z][a-z_]+)` 并同步 CLOSED_PRIMITIVES=13,
桥侧 182 守卫全绿;本条记录发现过程与防复发纪律。)**

---

## 三、判断与归因的坑

### 坑 7:未经穷尽就宣布「做不到」(被撤回 3 次以上)

| 谁 | 原断言 | 取证后 |
|---|---|---|
| W1 | 「校验侧**永远**连不上」 | 查文献(traceability link recovery / Trustrace)+ 实测:**121/125 条判据可被 Bean Validation 级联演绎证明执行** |
| W3 | 「接口 mediated 调用只能判 unknown」 | `UNRESOLVED#` 引用**带声明侧类型**,是编译期客观事实。**受体身份确定,只有运行时占位者未知** → 22 条盲边可判 |
| W4 | 「14 行里 10 行是散文,转录不了」 | 字段名/错误码**逐字在原文里**,可零猜转录。求值器暂不支持是「unknown(待补原语)」,**不是「转录不了」** |
| W8 | 「114 not_null + 18 not_blank + 6 `>` 永久不可映射」 | 是**封闭集多样性赤字**,不是判据属性。补 3 个原语后 138/138 可转录可执行(无观测→UNKNOWN,零假绿);「封闭集无 X 原语」描述的是调节器,不是被判据 |

**规矩**:「做不到」和「做到了」一样是**需要证据的断言**。宣布不行之前必须穷尽:
已检索的论文、相邻学科新方法、跨学科迁移,且独立验证确认。
**不得作为理由**:某篇博客说难、现有代码没做、不在现有方案里、看起来贵、以前没人做过。

理论依据:Ashby 必要多样性定律用在**搜索本身**——只试一种方法就宣布不可能,
是搜索的多样性赤字,不是问题的属性。

### 坑 8:归因重复计算 / 抢上游功劳

**证据**:W2 报「上游修正 44→27,我的过滤 27→9」。V6 实测:**旧网 + CRUD 过滤同样得 9**,
上游移除的 17 对完全被过滤包含 → 分解重复计算。
反面好例子:W2 发现候选 44→27 时**主动拒绝算成自己的功劳**,去核实是上游 relation_net 修正所致。

**防复发做法**:报差值前**独立重算两个因子**,确认它们不重叠。上游修正与自己的改动必须分开归因。

### 坑 9:覆盖率分母灌水

**证据**:**182/532 细胞是 Java record 编译器合成成员**(232/532 无源码声明),
全部 unknown 且 references 为空。覆盖率 15.23% 的分母里塞了大量本来就没有业务使命的合成成员。

**防复发做法**:分母构成必须报清楚;合成成员要么排除,要么**两种口径都报**。

**补充证据(W6,2026-11-15,同族新形态——合成成员长假边)**:枚举的编译器合成
`$values()` 被 javap 抽进声明后,长出 2 条**无源码证据的自调用假边**
(`E-000072`/`E-000074`,OrderStatus/ErrorCode 的 `$values` 自环)。
已在 `fn_static.py` 主循环剔除 `<init>/<clinit>/$values`,守卫
`test_synthetic_enum_values_not_a_unit` 锁死。合成成员不只灌分母,还会凭空造边。

### 坑 10:排序/去重破坏语义顺序

**证据**:W1 的 `emit` 对 evidence 做 `sorted()`,**把级联链路顺序打乱了——而链路顺序就是语义本身**
(`Controller#create(@Valid …)` → `DTO.addedItems @Valid` → `quantity @Min(1)`)。

**防复发做法**:凡是**顺序即语义**的数据(链路、证明步骤、调用栈),用保序去重,不用 `sorted()`。
并加结构化字段(如 `chain`)让复核不必解析字符串。

---

## 四、有效做法(值得复制)

1. **0 命中必须证明是「真合规」而不是「引擎没接上」**。W3 用三重证据:
   8 个角色全配到真实类(`rules_unmatched=0`)、**注入合成禁止边做阳性对照**(6 规则全命中)、
   **绕开引擎独立 grep 源码**。还用**故意写反的规则**验证行号定位,24/24 解析且回源码逐字对上。

2. **无观测 → UNKNOWN 是正确行为,不得编造观测**。W7 把 47 条判据真喂给两个求值器,
   47/47 UNKNOWN、`gate_eligible` 全 False。它没有为了看到 PASS/FAIL 而造数据。

3. **拒绝 invent 语义的映射**。桥拒绝用 `field_gte_const` 表达非空(「会 invent 语义」),
   拒绝把 `>` 放宽成 `>=`(D4 极性红线),拒绝映射带 `unit:"MB"` 的 `<=`(无量纲原语需先绑定单位)。
   **不可映射就逐条带原因报出,零静默丢弃。**

4. **更严的语义没有制造假一致,就如实报 0**。W2 修对 δ⁰ 后真实数据仍 0/9 consistent,
   它没有为了好看放宽判定。而且主动标注唯一新增覆盖是**语义过挂**
   (HTTP `status_code` ≠ 订单 `status` 字段)。

5. **阴性结果也是结果,不编成发现**。W1 普查所有输入 DTO 的 `@Valid` 级联齐全,
   唯一缺级联的是输出 DTO(输出侧不适用校验)→ **没有缺级联缺陷**,如实报阴性。

6. **纠正任务书而不是照着做**。W7 读源码发现我给的封闭原语集是错的
   (`field_eq_const` **不存在**,实际交集只有 6 个);W2 纠正我说的「枚举 147153 对」
   (实际只枚举 8385 对,147153 是公式分母)。**以源码为准,不以 brief 为准。**

7. **诊断别人的 bug 但不越权修**。W7 定位到 `capability_map.py:28` 只 import 了
   `Counter, defaultdict` 而 936 行调 `collections.Counter`,**明确拒绝去修**(不是它的文件),
   把精确诊断交给拥有者。

8. **算法归约前先验证行为保持**。W2 加倒排索引后先确认候选逐条不变(44==44),
   再加新守卫。实测 **72× 加速**(1.355ms→0.019ms),8385→56 比较。

9. **放大调节器多样性时,语义守卫与端到端差值一起交付**(W8)。加 3 个原语解锁 138 条判据,
   同时交付 79 条语义锁:缺字段→UNKNOWN 非 PASS、null→FAIL、类型不兼容→UNKNOWN、
   严格 `>` 边界等于常量必须 FAIL(与 `>=` 对照测试锁死 D4 极性)、双求值器 13 组同构断言。
   差值**分层如实报**:求值器层 138 条可执行是实测,桥层差值 0 也如实报(映射表归 W7),
   不把预期解锁数冒充成端到端实测(见坑 8 的分层归因)。

---

## 五、跨学科理论怎么用来判断(不是装饰)

| 理论 | 判断用法 |
|---|---|
| **Ashby 必要多样性**(1956) | 6 条手写判据管不住 533 细胞 = **多样性赤字**,不是算力问题。放大原语集/采集判据 = 放大调节器多样性 |
| **Conant–Ashby 良好调节器**(1970) | 审计器**必须**建模被审系统。Bean Validation 是被审系统自身调节器的一部分,只建模业务方法数据流等于漏掉整个校验子系统 |
| **Beer VSM 递归** | 函数→类→服务→仓库→跨仓库系统→生态,每层自身也是个活系统,五功能递归 |
| **分离逻辑 frame rule**(2016 哥德尔奖) | 旁条件 `mod(C)∩fv(R)=∅`。**足迹重叠 = 失效 = 涌现**。读-读重叠旁条件仍成立,不报候选 |
| **层上同调 / Čech 粘合障碍** | 局部截面在重叠上**取值相等**才能粘成全局。**「双方都有 finding」≠「取值相同」**。二元顶点导出边恒为 coboundary,环一致性退化为恒一致——要检出非零障碍必须有独立测量的成对判定 |
| **受体-配体特异性** | 架构约束绑的是**身份(声明类型)**,不是运行时占位者。所以接口 mediated 调用可判,不该留 unknown |

**三态铁律**:`unknown` 不得变 `pass`,**也不得变 `impossible`**。
缺字段 → UNKNOWN(字段名拼错不得得到一张通过的成绩单)。

---

## 六、怎么追加

交付时若踩了新坑或发现新做法,按上面格式追加:**现象 + 实测证据(文件:行/数字)+ 根因 + 防复发守卫**。
没有证据的不收。重复的坑合并到已有条目下,不要新开一条。

---

## 七、W6 补充(2026-11-15,同类自调用边修复)

### 坑 12:Edit 替换吞掉结构边界行 —— 7 个红被误当集成回归

**现象**:用 Edit 替换代码块时,`old_string` 末尾带上了**下一个测试方法的 def 行**,
`new_string` 没保留它 → 定义行丢失、方法体孤儿化,全量跑出 7 个红,
编排者一度以为是跨 Agent 集成回归(坑 5 的又一次实例)。

**实测证据**:`pilot/tests/test_self_call_edges.py` 一次 Edit 的 old_string 以
`def test_comment_and_string_calls_do_not_produce_edges(self):` 结尾而 new_string 未含;
Read 回见第 110-111 行方法体直接接在上一方法尾部,def 行消失。

**根因**:替换范围跨越结构边界时,边界行被当成可丢弃的上下文。

**防复发守卫**:
- old_string 含边界行(def/class/分隔符),new_string 必须**原样保留**;
- 结构编辑后**立即跑目标测试文件**,并 `git diff` 核对只改了预期行;
- 断言回归前先列失败名并重测(坑 5)。

### 坑 13:正则可选组回溯伪影 —— 包前缀被吞成方法名

**现象**:修「javap 同类 invoke 无 owner」时把 owner 改可选组,立刻长出新伪影:
`com/jiuyan/...` 的 `com` 被当成方法名,产出假调用 `OrderService.com`。

**实测证据**:修复中间态解析 OrderService 得
`com.jiuyan.banquet.service.OrderService.com`(触发行为
`// Method com/jiuyan/banquet/dto/PageResponse."<init>":...`);
加右边界断言 `(?=[:(])` 后伪影 = [](fn_static.py:30-32,守卫在
test_self_call_edges.py::FnStaticExtractorGuard)。

**根因**:可选组匹配失败后回溯,mname 无右边界,吞掉包路径前缀。

**防复发守卫**:放宽正则(必需→可选)必须**同时补边界断言 + 阴性用例**
(「包前缀不得成为方法名」),修一个丢失不得引入一个伪影。

### 做法 10:修根因不修症状 —— 沿生成链追到第一个丢失点

**证据**:同类边 0/343。第一层调查停在消费端 build_agent_graph.py:153
(它只读 internal_calls,不是丢失点);继续上溯到 **fn_static.py:30-32**——
CALL_RE 要求 `owner.method`,而 javap 同类 invoke 省略 owner
(实测字节码行 `10: invokevirtual #129 // Method generateOrderNo:()...`)。
修抽取器后 internal_calls 343→397、同类 0→54,**声明层/边表/关系网输入三处同时恢复**;
只在下游打补丁只能救 edges.json,救不了 relation_net。

**守卫**:派生产物缺事实时,沿生成链上溯到**第一个丢失点**,用原始证据
(字节码行/源码行)核实丢失机制再动手;下游补丁只能当止血,必须标注。

### 做法 11:因果归因先在冻结快照上独立重算(坑 8 实例)

**证据**:简报称「W6 的 54 条同类边使 arch_rules unknown 59→363」。
独立重算(同一 units/config,`arch_rules.audit` 分别吃基线 786 与新 840 边表):
**unknown_evaluations 两侧都是 363**;新增 54 边 × 8 规则 = 432 次评估
**全部 not_applicable,零 unknown 贡献**。59 是 W3 在飞配置/版本的旧数(坑 4 移动靶),
差值不能归因于新边——已如实退回,不背不属于自己的锅,也不抢功。

**守卫**:接受「A 导致 B 变化」之前,冻结两侧输入独立重算;
复现不出就如实报「因果不成立/待重新基线」,不得照单全收。

**补充裁定(W3,2026-11-15,2×2 控制变量实验)**:上面「59 是 W3 在飞配置/版本的旧数」
这句**归因错了**,但「54 条新边贡献 0 unknown」这句**对**。冻结两侧边表 + 同一 units/config,
只切换 members 输入(`arch_rules.audit` 直接调用,四次):

| 边表 | members | unknown | compliant | not_applicable |
|---|---|---|---|---|
| 786 `bc880f7040f62a94` | 无 | 363 | 104 | 5821 |
| 786 | 有 | **59** | 408 | 5821 |
| 840 `eb571788012da50d` | 无 | 363 | 104 | 6253 |
| 840 | 有 | **59** | 408 | 6253 |

- 边表 786→840:unknown 差值 **0**(两侧一致),n/a 差值 **432 = 54×8** → W6 的因果判断成立。
- members 无→有:unknown 差值 **-304**(两侧一致),原因全是 `member_declarations_not_supplied`。
- **59 与 363 的唯一因子是 `--members` 是否供给**,不是边数、不是在飞版本;59 当下可复现。

**根因**:W6 两侧都用 `members=None`,所以两侧都得 363,于是把差异误归给「版本在飞」。
**这是坑 3 的镜像**——不是聚合掩盖分项,而是**两侧共缺同一个输入,使对照实验失去区分力**。

**防复发守卫**:对照实验必须枚举**所有输入维度**并逐一冻结;两侧共有的缺失输入会让差值恒为 0,
看起来像「因果不成立」,实际是「实验没区分力」。报 unknown 必须带**原因分解**,不得只报总数。

---

## 八、W3 补充(2026-11-15,架构禁止边)

### 坑 14:三态闸门只绑一个字段 —— unknown 换个位置编码就漏

**现象**:arch_rules 的三态闸门只看 `resolution != "static"`。W5 修 WRITES 误标时把 unknown
编码进 `access` 字段(而非新边类型),这种边若 resolution 仍是 static,就会被判成合规/违规。

**实测证据**:`relation_net.json` 边字段已出现 `access`(39 条:24 read / 15 write),
WRITES 26→15、READS 13→24;`edges.json` 当时无 access 字段 → 失效面为 0 但**已武装未触发**。
补 `unknown_markers` 配置闸门后 6 例守卫全绿:`access="unknown"` + `resolution="static"`
→ resolved 与 declared **两个通道都记 unknown**,visibility 同样阻断;`access="write"` 正常判定。

**根因**:把「unknown」当成某个字段的取值,而不是**边的一种状态**。上游换编码位置,下游闸门就空转。

**防复发守卫**:三态闸门必须可配置地扫描**多个字段**(`unknown_markers: [{field, values}]`),
命中即两通道都 unknown;并加「未声明的字段不猜测」的显式测试,把盲区写成可见行为而非静默。

### 坑 15:文件级事实被重复归属到细胞级 —— 计数放大 11.5 倍

**现象**:扫「源码有、图里无」的 dispatch 形式时,按**每个调用方细胞**扫它所在的整个文件,
同一处 `CategoryMapper::toResponse` 被该文件的每个方法各报一次。

**实测证据**:引擎报 **127** 处,独立 `grep -rno "::" src/main/java | wc -l` 只有 **11** 处。
还产生错误关联:`CategoryService.java:33` 被归到 `#create`,并匹配上 `#create` 自己在 51 行的
直接调用边 → 假「已投影」。改为按文件去重 + `enclosing_cell` 恒 null + 只给候选细胞后
**11/11 与独立 grep 一致**;另补数组构造器引用 `String[]::new`(正则漏 `[]`)与全 63 文件扫描(原只扫 28 个调用方文件)。

**根因**:事实粒度(文件:行)与归属粒度(细胞)不一致时,按细胞迭代文件会做笛卡尔积。

**防复发守卫**:跨粒度统计先按**事实的天然粒度**去重再谈归属;归属不确定就显式给候选集 +
`enclosing_cell: null`,不许挑一个。任何「源码扫描计数」必须与独立 grep 对账后才报。

### 做法 12:上游权威证据优先于本地启发式(同类无点号调用 0/47 → 47/47)

**证据**:arch_rules 原用文本匹配 `\.\s*方法名\s*\(` 定位行号,对**同类无点号调用**完全失效。
上游 `edges.json` 新增 `evidence`(file:line)后改为优先消费:同一批 47 条 service 同类边,
唯一行号 **0/47 → 47/47**,`line_status` 从 `not_found` → `upstream_evidence`。
4 条回源码逐字核对:`OrderService.java:71 parseDateScope(dateScope)`、`:72 orderSpec(...)`、
`:73 normalizePage(page), normalizePageSize(pageSize)`。多处 evidence 不一致 →
`upstream_evidence_ambiguous`,不给唯一行。

**守卫**:本地启发式只作**兜底**,上游权威事实优先;两者都要三态(唯一/多命中/零命中),
且必须用「剥掉上游证据」的对照跑量化差值,不许只说「更准了」。

### 做法 13:调节器多样性可从系统自身接线归纳,但必须配阴性对照

**证据**:8 条规则全手写 = Ashby 多样性只来自配置作者。改为对观察到的角色对做 BFS,
判据是**受体-配体特异性**:配体 B 在别处真实结合、A 到 B 有长度≥2 通路、但无直接边。
840 边 / 10 个观察角色对 → 归纳出 **2 条候选**(controller↛entity、controller↛mapper,support 36)。
三个阴性对照全部按预期变化:①注入 controller→entity 直接边 → 该候选消失;
②删掉全部指向 entity 的边 → 该候选消失(mapper 候选保留);③删掉全部 controller→service 边 → 候选全灭。
resolved 通道只出 1 条,因为 ARCH-002 已在该通道覆盖 controller→mapper → 跨通道去重按 basis 精确匹配。

**关键纪律**:候选 `confirmation=unconfirmed`,**不进 findings、不进 rules、不自动升级**。
归纳出的是「系统目前这么接线」,不是「系统必须这么接线」——后者要人确认。
resolved 通道下若 A→B 只有 unknown 边,缺席不成立 → `status=unknown`,不计入候选。

**守卫**:归纳器必须配**注入/删除两类阴性对照**证明它是数据驱动;候选与判定必须物理隔离
(独立字段 + 独立计数),否则假设会偷偷变成 oracle。

### 做法 14:把「必需输入」机械化,不要只写在文档里(坑 3/坑 4 的防复发)

**证据**:59 vs 363 争议(见做法 11 补充裁定)的根因是**交付没写清必需输入**。已机械化:
缺输入时报告产出 `input_warnings` 并在 stdout 前置打印,实测缺 `--members` 口径输出
`输入告警: visibility 规则 ['ARCH-004'] 未供给成员声明输入：304 条边不可判定（unknown），不得读成合规`;
报告同时自带 `repro_cmd`(完整命令)与全部输入 `sha256`。4 例守卫锁死告警行为。

**守卫**:凡「缺了会让指标变形」的输入,必须在**产物里**告警 + 带复跑命令与哈希,
不能只在交付散文里提一句。文档会被略读,产物字段不会。

**V11 证伪 + W3 第二轮修正(2026-11-15):上面「repro_cmd(完整命令)」与「零手工中间文件」的声称成立,
但「可复现」整体被证伪三处。教训:声称的可复现性必须自己在干净环境逐字跑一遍(坑 4 移动靶的变体)。**

| V11 实测 | 根因 | 修法与实测 |
|---|---|---|
| `repro_cmd` 逐字跑 **permission denied** | `" ".join(sys.argv)` 的 argv[0] 是脚本路径,**缺解释器前缀**,被当可执行文件 | 改 `shlex.join(["python3"] + sys.argv)`;另给 `repro_cwd` 与自带 `cd` 前缀的 `repro_cmd_anywhere`。从临时干净目录逐字跑三条命令 **全 rc=0**;相对命令从异目录跑 rc=2(如实区分) |
| fresh clone **FileNotFoundError** | `fn_declaration.json` 被 `.gitignore:7 pilot/*_declaration.json` 忽略;且**我的三个文件本身未提交**(`git ls-files` 为空) | 不再声称零前置。产物新增 `repro_prerequisites`(5 条,含「三文件未提交」「口径 A 需 `cd pilot && python3 fn_static.py`,见 run_pipeline.sh:50,需被测后端已编译」)。`git archive HEAD` + 拷入三文件实测:口径 A **rc=1 FileNotFoundError**(与声明一致),口径 B **rc=0** |
| 第二口径 **rc=1** | `member_source` 写死 `records:"functions"`,而 relation_net 容器名是 `nodes`;上轮我只在 scratch 脚本里覆盖配置,**从没给过可跑命令** | `member_source` 支持**候选形状列表**,`select_source` 按容器存在性选中并把 `source_records` 写进 `member_coverage`。同一配置只换 `--members` 即可跑两口径,实测 **A rc=0 / B rc=0** |

**两口径必须各自给命令、分开报数**(坑 3):产物 `repro_variants` 为每个 `member_inputs` 生成一条完整命令,
输出文件名带 label 后缀**互不覆盖**。当前快照(`edges 09a87675ccda9f99` / `units 87236ab8c43261ed`,842 边):
口径 A `source_records=functions`、declarations 458、judged 304、missing 0、**unknown 59**;
口径 B `source_records=nodes`、declarations 119、judged 41、missing 263、**unknown 322** + 覆盖告警。
两个数都对,**不可混比**;配置里的历史计数已删除,改为指向活字段 `member_coverage`,防数字腐烂。

**机械化守卫(新增 5 例,`CliReproducibilityTest` / `ReproBuilderTest`)**:
- `test_repro_cmd_is_verbatim_executable`:subprocess **从另一个目录**跑 `repro_cmd_anywhere` → rc=0 且 counts 与首跑逐字相同;同时断言相对命令从异目录跑**必须失败**(锁住 cwd 语义,不含糊)。
- `test_both_member_variants_run_verbatim_with_one_config`:两条变体命令都从干净目录逐字跑 → 都 rc=0,`source_records` 分别为 functions/nodes,members 的 sha256 不同。
- `test_interpreter_prefix_and_non_clobbering_variant_outputs` / `test_variant_appends_members_when_absent` / `test_source_shape_list_selects_matching_container`。

**通用纪律**:① 交付里的每条命令,**自己在一个干净目录逐字跑一遍**再写进报告,不许只跑「自己构造的等价命令」;
② 「可复现」必须区分**口径**并各给命令,前置依赖(含「本文件尚未提交」)写进产物字段而不是散文;
③ 配置里不得写死单一上游容器名——上游产物是异构的,形状要可声明成候选列表;
④ 说明性文字里的计数会腐烂,指向活字段。

### 坑 16:采集器越权对齐发射词汇,把桥端到端计数砸出回归(W4 实测)

**证据**:W4 为「让判据可执行」在 `oracle_harvest.py` 里把 `field_cmp`/`in` 自行对齐成
`field_gte_const`/`field_in_set`。但映射到消费方封闭集是 `oracle_bridge.py` 的职责,
桥认的是采集器**原始词汇**(`_CMP_OPS` 读 `field_cmp.op`)。改发射词汇后桥认不出,
可映射数 **47→14** 静默倒退。撤回对齐、恢复原始词汇后 **14→177**,再补接口清单表
(真实字段校验要点)→ **203/278**。

**根因**:跨独占文件的契约缝(采集器↔桥)被单方面改动。采集器供原始词汇,桥做映射,
求值器判定——三层各属不同 owner,任何一层越权改词汇都会让端到端计数漂移。

**守卫**:① 采集器**不**对齐词汇,只发原始原语 + 字段可核实性降级(散文字段→needs_confirmation);
② 可执行分子分母由桥报出,不在采集器里另算一套;③ 引用任何端到端数字前先重测
(桥/求值器会被并发 agent 扩封闭集,如本轮 `field_gt_const`/`field_not_null` 新增),
不得用缓存认知断言「不可映射」。

### 做法 15:撤回未经穷尽的「做不到」——Ashby 用在搜索本身(W4 三次实测)

**现象**:三次宣布「转录不了 / 不可执行」,取证后全部撤回。

**证据**:① 「10 行散文转录不了」→ 读求值器封闭集 + 逐字核原文,发现 envelope/错误码/
快照的字段名**逐字在原文里**,零猜转录成 `has_fields`/`set_covers`;② 报「L0 可执行 4→8」
→ 字段可核实性重核,确认表全是散文字段(金额字段/桌数),实际 **0 可绑定**,撤回虚报;
③ 接口清单表当「散文」跳过 → 校验要点含真实字段 priceCent/items/page,采到 **32 条 L0 可绑定**。

**根因**:只试一种方法(锚定两个正则模式)就宣布其余不可能 = 搜索的多样性赤字,
不是问题的属性。「现有代码没做 / 看起来难」都不是「做不到」的证据。

**守卫**:写「做不到」前必须穷尽(读消费方契约 / 逐字核原文 / 相邻学科迁移)并给实测证据;
unknown 不得变 pass,也不得变 impossible。

### 做法 16:不把异常当正常——隔离因子再下结论(W4 实测)

**现象**:端到端喂求值器,合法值返回 UNKNOWN、违反值却返回 FAIL。

**证据**:没当成「求值器就这样」放过。单规则单观测隔离重测 → `price_cent=5→PASS`、
`-1→FAIL` 完全正常。根因是测试脚本把每条规则对**所有**观测求值,另一条观测缺该字段
→ UNKNOWN 混入聚合(`rule_assessment.py` `assess` 的 items 推导式对全部 rows 求值)。

**根因**:异常信号是测试 setup 的交叉污染,不是消费方缺陷;若当正常放过,会误判桥/求值器
不可用,或反过来把 UNKNOWN 当 PASS。

**守卫**:合法值/违反值**双向**都测;遇异常先隔离因子(单规则单观测、重测当前态)再下结论,
不拿异常当正常,也不拿异常当「不可能」。

### 坑 17:跨文件词汇契约断裂——改上游导出没同步下游导入与映射表(W4 实测,与坑 16 互补)

**现象**:同一 agent 独占 `oracle_harvest.py` 与 `test_oracle_harvest.py`,改上游时删了
导出常量 `CONSUMER_PRIMITIVES`,没同步下游测试的 `import`(:19)与使用(:362)→
`ImportError: cannot import name 'CONSUMER_PRIMITIVES'`,**整个测试模块一条也收集不了**,
之前报的「全绿」无法复现。同源的词汇越权对齐(坑 16)让桥可映射 **47→14**。

**实测证据**:`grep CONSUMER_PRIMITIVES` 上游 0 命中、下游仍在 import;`pytest pilot/tests`
报 ERROR collecting。修复后全量 **568 passed**(本模块 28 绿),桥回升 **203/278**。

**根因**:跨文件契约(上游导出 ↔ 下游导入 ↔ 桥映射表)三方必须同步;单方面改一方,
另两方静默断裂。常量在两个文件各复制一份更是漂移温床。

**防复发守卫(已机械化)**:`test_harvester_bridge_vocabulary_contract` 锁两条——
① 采集器发射的每个 `assert.type` **不得**落在 `oracle_bridge.CLOSED_PRIMITIVES`(映射是桥的职责);
② 桥必须**认识**每个采集器原语(映射成功或显式不可映射,不得「未知原语」)。
契约权威取自桥的 `CLOSED_PRIMITIVES`(单一来源),不在采集器里复制常量。
变异实测:让采集器越权发射 `field_gte_const` → 该守卫红。
**通用纪律**:改任何跨文件导出前,grep 下游全部 import/使用点同步改;交付前跑**全量**收集
(不只跑自己的模块),ImportError 会让「全绿」假象掩盖整模块失效。

**同类第三形态:产物 schema 漂移,下游无声退化(W1 实测,2026-11-15)**:上游把 CHECK 判据的
字段操作数从 `assert.field` 改成 `assert.left`(`field_gte_const`/`field_in_set`),下游
`capability_map._oracle_fields` 只读 `field`/`fields` → 取不到字段 → 实体作用域为空 →
落进「表级兜底」→ **字段级判据扇出到所有写该表的细胞**:
`ddl:products.chk_products_price_cent` 连到 `ProductService#updateStatus`(只改 isActive)
与 `#delete`(只改 isDeleted),**8 条可证伪边**,跨实体计数 0→8。
与坑 17 的 import 断裂不同:**这里没有任何报错**,测试全绿,只有独立复核才看得见。
修三处:补读 `left`/`target_field`;`_enforces` 认新断言类型;
**兜底收紧为「判据真的没有字段操作数」才用**,有字段但作用域解析不出就进 unlinked。
**防复发(已机械化)**:产物新增 `counts.unrecognized_assertion_types` —— 有字段操作数但
下游认不出的断言类型及条数,把无声漂移变成可见信号。上线当场抓到**第二次漂移**
(`field_in_set` 已变成 `in`,共 48 条),这些判据 enforcement 保持 unknown,绝不声称已执行。
**通用纪律**:消费上游产物时,「认不出的词汇」必须计数并外露,不得静默跳过或退化成更宽的兜底。

### 坑 18:能力合法扩展后守卫过期——更新断言形式,保留意图,不得删测试(W4 实测)

**现象**:W8 给求值器/桥加了 `field_gt_const`,严格 `>` 从「不可映射」变成「合法可映射」。
写于旧能力期的守卫 `test_strict_gt_not_fabricated_to_gte` 断言 `specs == [] and "算子" in reason`
(> 必须不可映射)→ 新能力下变红。**红的是过期断言形式,不是代码错。**

**关键区分**:守卫的**意图**(「不得把 `>` 伪造成 `>=`」,极性红线)仍然有效;过期的只是
**断言形式**(「必须不可映射」)。一律当「测试过期」放宽、或一律当「代码回归」改代码,都错。

**实测证据与修法**:`tests/test_oracle_harvest.py:363` 改名 `test_strict_gt_maps_to_strict_not_relaxed`,
断言更新为 `specs[0]["type"] == "field_gt_const"`(:372)**且** `!= "field_gte_const"`(:373)——
极性红线原样保留,只把「不可映射」换成「映射到严格原语而非放宽原语」。测试不删。

**防复发守卫**:能力扩展(新原语进封闭集)时,grep 所有断言「不可映射/未知原语」的守卫,
逐个区分「意图过期」还是「形式过期」;形式过期→更新断言保留意图,意图过期→需独立裁定。
**不得删测试**(删了就丢了极性红线),也不得为变绿放宽判定。

### 坑 19:上游用可靠过近似编码 unknown(新字段携带三态),下游只读旧字段 → unknown 被洗成确定结论(V7 实证,W2 修)

**现象**:W5 把「不知道是不是写」编码为 `WRITES` 边 + `access="unknown"`(may-write 可靠过近似),
因为下游只认精确边类型。W2 的 `frame_check.py:107-110` 把 WRITES 边直接计入 `fp.writes`,
**完全不看 `access`** → 「可能写」被当成「确定写」→ 产生假涌现候选。这是「unknown 不得变 pass」
红线的同类:**unknown 不得变确定违反**。

**实测证据(V7 注入,W2 复现)**:向冻结快照注入 1 条 `access="unknown"` 的 WRITES 边
(`OrderService#apply → table:categories`),修前 → **5 个新候选全部 `b_writes=True` +
确定 `frame_violation`,记录里无任何 access 标记**(V7 报 4,注入细胞/表不同所致)。
修后同样注入 → 确定违反数 **9→9 不变**,5 个新候选全标 `evidence_strength="may_write_unknown"`、
`frame_violation=[]`、provenance 带 `access="unknown"`。

**根因**:把「unknown」当成某个旧字段的隐含取值,而不是**边的一种显式状态**。上游换编码位置
(新字段 `access`),下游只读旧字段(`type`)就空转。与坑 14 同族(三态闸门只绑一个字段),
但发生在**跨文件契约接缝**:上游 relation_net(W5)↔ 下游 frame_check(W2)。

**契约接缝的调和(关键,不得二选一)**:W5 的守卫 `test_relation_net_writes.py:190` 要求
may-write **保留在 `fp.writes`**(足迹不得丢弃);V7 红线要求 may-write **不得计入确定写**。
两者可同时满足:`writes` = 全部写足迹(含 may),`may_writes` = 不确定子集,
**`definite_writes = writes - may_writes`** 唯一驱动确定 `frame_violation`。
只把 may-write 移出 `writes`(W2 第一版修法)会违反「足迹不得丢弃」契约,弄红 W5 的守卫。

**防复发守卫(已机械化,`test_frame_check.py`)**:
- `test_unknown_may_write_not_definite_violation`:注入 unknown 边 × 确定读者 → 候选必须
  `may_write_unknown`、`frame_violation=[]`、`b_writes=False`、`b_may_write=True`。
- `test_unknown_may_write_vs_definite_write_still_unknown`:一方确定写 × 另一方仅 may-touch →
  重叠本身不确定 → 仍 `may_write_unknown`(确定违反需「确定写 × 确定触碰」,不是「一方写即可」)。
- `test_injected_unknown_edge_adds_no_definite_violation`:基线全确定,注入 unknown 边后确定违反数不得增加。
- 变异实测(同进程 monkeypatch,坑 2):洗掉 `may_writes` 标记 → **3/3 红**;基线 577 绿 + 5 subtests。
- **通用纪律**:消费上游边/记录前,先枚举它携带三态的**所有字段**(`type`/`access`/`resolution`…),
  不得只读旧字段;跨所有权边界改 API 时,先 grep 下游全部消费点(含别人的守卫)再定契约,
  交付前跑**全量**套件(不只自己的模块),契约接缝的红会暴露在别人的测试里。

### 坑 20:聚合分母与分项集合口径不一致——桥三桶之和 ≠ counts.total(W4 实测,V4/W7 查清)

**现象**:桥报「可映射/不可映射/排除」三桶之和 **282**,采集器 `counts.total` **278**,差 **4**。
不是丢判据,是**口径**:4 条 `doc:undefined:*`(refuted)在独立顶层键 `explicitly_undefined`,
**不在 `oracles` 数组**。`counts.total` 只数 oracles(278),桥把 refuted 单列排除桶(278+4=282)。
行为正确(refuted 永不进判定),但两个分母没显式声明,下游一眼看不出差异来源。

**实测证据**:`oracles_count=278`、`explicitly_undefined_count=4`、`total_declarations=282`、
`denominators_disjoint=True`(两集合 id 交集为空)。桥 `mapped 203/278`。

**根因**:与坑 3 同族(聚合掩盖分项)——一个 `total` 字段背了两个集合的口径,
refuted 独立成桶却没在产物里写明「total 不含它」。

**防复发守卫(已机械化)**:产物显式报 `oracles_count` / `explicitly_undefined_count` /
`total_declarations`(=前两者和)/ `denominators_disjoint`,并带 `denominator_caliber` 文字说明;
`test_denominators_disjoint_and_explicit` 锁「两分母不重叠 + 和可核 + total==oracles_count」。
**通用纪律**:产物含多个不重叠集合时,每个集合给独立 count + 总和 + 不重叠声明,
不得只用一个 `total` 让下游猜口径;跨所有权交付时把口径定义随产物告知下游(本轮告知 W7)。

**W7 补充(2026-11-15,消费侧闭合 + 对账守卫)**:

W4 定义口径后,桥(消费侧)照此报分母。**修前实测**:三桶之和 `203+46+33=282`,
而 `counts.total=278`,差 4 —— 行为正确但产物看起来像丢了 4 条判据。
**修后实测**(输入 `dcc61eb1…`):`counts.total = total_declarations = 282`、
`oracles_count = 278`、`explicitly_undefined_count = 4`、`three_bucket_sum = 282 == total`,
CLI 同时打印两个口径 `203/278`(oracles 数组)与 `203/282`(全部声明),rc=0。
可映射数仍 **203**,直通与 W8 新原语映射未受影响
(`not_null→field_not_null` 136 / `not_blank→field_not_blank` 18 / `field_cmp{>}→field_gt_const` 4)。

**消费侧新增纪律(补 W4 的生产侧纪律)**:
- 消费方必须**独立重算**两个分母(从实际数组长度),再与上游 `counts` 声明**对账**
  (`caliber_crosscheck.agrees`),不得只信上游 `total`。对账不一致 → **CLI rc=1**,不静默吸收。
- 三桶之和必须恒等于**处理总数**(含 refuted 独立桶),不是 oracles 数组数;
  产物里给 `three_bucket_sum` 字段让下游一眼可核,不用自己加。
- 两个口径**都报**,不选一个让下游猜(坑 3:聚合掩盖分项;坑 9:分母灌水)。

**已 mechanize**(`pilot/tests/test_oracle_bridge.py`,158 条全绿):
`test_both_denominators_are_explicit_and_close`、`test_three_bucket_sum_always_equals_processed_total`
(5 组参数化)、`test_overlapping_denominators_are_reported_not_hidden`、
`test_upstream_caliber_drift_is_flagged_not_absorbed`(4 组)、
`test_cli_reports_both_denominators_and_closes`、`test_cli_fails_loudly_when_upstream_caliber_drifts`。

**变异测试 4/4 变红**(同进程 monkeypatch,坑 2 要求):
M6 `total` 只数 oracles(复现本坑)→ **5 红**;M7 静默丢掉 refuted 桶 → **9 红**;
M8 掩盖分母重叠 → 1 红;M9 对账恒返回 agrees → 5 红。基线绿。
上一轮 5 个词汇契约变异(M1–M5)重跑仍全红,无回归。

**W7 自己在本轮踩的子坑:把「上游没声明」当成「上游不同意」。**
第一版对账把 `upstream_counts.get(k)` 的 `None` 直接拿去和桥算出的值比,
于是**任何还没采用新口径字段的旧产物**(只有 `counts.total`)都被判 `agrees=False` → CLI rc=1。
实测:`{"oracles":[],"counts":{"total":0}}` 这种空仓库产物被硬判红,
而 W6 的端到端测试跑真实流水线,会被我这条新 rc=1 路径打断。
**缺失 ≠ 冲突**(与坑 19「unknown 被洗成确定结论」同族:这里是把「无声明」洗成「有冲突」)。
修法:只对上游**真的声明了**的字段做比对,未声明的列进 `upstream_undeclared` 可见报出,
`mismatches` 只收真冲突。守卫 `test_upstream_absent_caliber_fields_are_not_treated_as_disagreement`
+ `test_cli_succeeds_on_empty_and_undeclared_caliber`(空 / 无 counts / 部分 counts 三种都 rc=0)。
**通用纪律**:跨所有权对账时,先分清「对方没这个字段」与「对方字段值和我不同」;
前者是版本差,后者才是漂移。把前者判红会让下游无法渐进升级。

---

## 九、W1 补充(2026-11-15,能力图测试证据三态)

### 坑 21:解析器漏抽被当成真实覆盖缺口报出去(与坑 9 互补:坑 9 是分母灌水,这里是把工具缺陷算成被测系统缺陷)

**现象**:能力图报「2 个 HTTP 端点没有测试证据」。V5 独立数 Java 侧真实端点后指出:
那 2 条是**解析器漏抽**,不是真缺口——测试用自建 helper `api.upload(path, …)` 发请求,
抽取器只认标准 HTTP 动词,于是把「我没抽到」报成了「人家没测」。
同族第二形态:路径匹配只做字面量相等,`api.delete("/api/categories/999999999")` 这种
**具体值**调用匹配不上 `/{id}` 模板,同样会被误报成没测试。

**实测证据**:修前 `http_endpoints_without_test_evidence: 2`(且早期版本把 7 个 controller
**文件级**细胞也算进端点,报成 9 —— 指标定义本身错了);修后
`http_endpoints_total 25 / tested 25 / parser_miss 0 / no_test_evidence 0`,
`confirmed_test` 23→25,新增 4 条上传调用点全部回源码核实
(`test_uploads.py:5-6` 等,`helpers.py:124` 的 `upload` 转发 `_send("POST", path, …)`)。

**根因**:把「证据缺失」当成单一状态。实际至少三态,且**归因对象不同**:
① tested —— 被测系统真有测试;② parser_miss —— **我的工具**没抽到;③ no_test_evidence ——
被测系统真没测。混报会把工具缺陷写成被测系统的缺陷,方向完全反了。

**防复发守卫(已机械化)**:
- helper 别名**从测试自身代码推断**:某函数的 path 参数被转发给字面量动词
  (`_send("POST", path, …)`)或标准动词方法(`self.post(path)`)→ 建立别名→动词映射。
  不写死任何 helper 名字;推不出动词的**不许猜**(守卫 `test_alias_resolution_does_not_invent_a_verb`)。
- 路径按**段**匹配,模板 `*` 段匹配任意具体值。
- 产物把三态分开计数 + 分别列出明细(`endpoint_parser_miss` / `endpoint_no_test_evidence`),
  parser_miss 带未解析的 callee 名与行号,便于直接修抽取器。
- 变异实测 12/12 被抓住,其中「已知动词也算漏抽」「路径模板退化成字面量相等」
  「漏抽与真缺口混报」三条专门锁这个坑;基线 0 失败。

**通用纪律**:报任何「覆盖缺口」前先问——**这是被测系统的缺口,还是我的观测工具的缺口?**
两者必须分桶,且工具缺口要带可复现的定位信息。观测工具的失败不得记到被测系统账上。

### 做法 17:合成细胞(lambda)的足迹归因要回源码核,不能因为名字生成就怀疑它

**现象**:V5 质疑 8 条边落在 `lambda$create$0` 这种合成细胞上,怀疑足迹该归外层方法。

**实测**:回 Java 源码核实——`CategoryService#create` 里
`orElseGet(() -> { next.setName(name); next.setSortOrder(…); return next; })`,
lambda **确实**写 `Category.name`/`sortOrder`;而外层 `create` 写的是 `isDeleted`。
两者足迹不同,**归因正确,不能合并**(合并会抹掉真实足迹区分)。

**但同时暴露一个真缺口**:扫描器不解析 lambda 体,21 个 lambda 细胞使命**全部 unknown**。
做法是**显式报出**(`lambda_cells` / `lambda_cells_without_declaration`),不编造使命,
也不因为「名字是生成的」就否定关系网给的足迹。**通用纪律**:质疑归因时回原始证据核,
核完该确认的确认、该报的缺口报,两件事分开做。

### 坑 22:表级约束聚合把不同列的约束当成同一个值求交 → 假障碍(W2 实测)

**现象**:用判据约束集做多值截面时,若把「表」当成一个资源、把该表所有列的约束求交,
`table:orders` 的 status 字符串枚举 {PENDING,…} ∩ deposit_cent 数值区间 [0,∞) = ∅(类型不同无公共值),
被判成「不相交 = 非零障碍」。实测原型在 table:orders 上凭空产出 **6 个假不相交对 + 4 个假三元障碍**,
涉及的全是 repository CRUD 细胞——纯属把不同列的独立约束错误地当成同一值的约束。

**根因**:事实粒度(列)与资源粒度(表)不一致。表级足迹无字段粒度(见 frame_check 既有局限),
把列约束聚合到表级求交,等于假设「同一个值既要满足 status 枚举又要满足 deposit_cent 区间」,这是编造。

**防复发守卫(已机械化)**:约束集**只按字段粒度**算(`_cell_section` 对 `identity[0]!='field'` 直接返回 None);
表级资源一律 unknown。`test_table_level_no_false_obstruction`:表级两列约束(status 枚举 + amount 区间)
→ 断言 `cycle_obstructions==0` 且 `unknown==1`。变异 M3(去掉字段粒度闸门、表级也聚合)→ **1/1 红**。
**通用纪律**:约束求交前确认所有约束作用于**同一个值**(同字段);跨字段/跨列的约束是乘积关系,不是交集关系。

### 做法 18:多值截面(判据约束集)让 Čech 障碍路径可 firing,不必外部测量(W2)

**证据**:二值顶点导出的边恒为 coboundary,环一致性退化为恒一致(已证明)。改用**多值截面**——
细胞在重叠资源上的截面 = 它对该资源施加的约束集(`field_gte_const`→[v,∞)、`field_in_set`→有限域、
`range`→区间),经 `oracle_bridge.map_assertion` 只读映射到封闭原语后解释为区间/域(尊重 >/>= 极性,不自己映射词汇)。
**两约束集不相交 = 重叠上不一致 = 非零障碍**;相交 = consistent;任一方缺约束集 = unknown(绝不默认 consistent)。
合成 [5,∞)∩(-∞,3] → inconsistent(firing 证明);变异 M1(_is_empty 恒 False)→ 3/3 红,M2(缺截面默认全域)→ 2/2 红,
M4(> 放宽为 >=)→ 1/1 红。成本仅区间/集合运算,无求解器/LLM/容器。

**真实数据如实报**:278 判据下环一致性 **2 consistent(quantity,setStatus)/ 0 inconsistent / 12 unknown**,
约束截面覆盖 4 细胞×资源、2 桶。0 inconsistent 的可复现原因:① 候选重叠资源 isDeleted/isActive **零约束判据**→unknown;
② status 7 条判据全是同一枚举、quantity 的 >=1 与 >0 相容,且**对称挂载**(共享字段资源的两个细胞继承同一批匹配判据)→consistent;
③ 全目录无任何字段含不相交约束。**理论局限**:对称挂载下约束路径检的是「资源全局约束矛盾(空不变式)」,
不是「细胞间不对称分歧」;后者需 DTO 字段→实体字段的数据流投影,不猜(不编造)。
**通用纪律**:机制能 firing 要用合成不相交约束证明 + 变异验证;真实数据不 firing 要给可复现原因(逐资源约束分析),不得只说「数据不足」。

---

## 十、W3 第二轮补充(2026-11-15,可复现性 / 契约缺口 / 编码形态)

### 坑 23:同一语义有多种上游编码,解析器只认一种 → 类名被切成包名(V7 实证,W3 修)

**现象**:上游 `UNRESOLVED#` 引用有两种形态:`fqcn.method` 与 `fqcn#method`。
arch_rules 只按最后一个点号切,遇到第二种把**包名当成类名**,角色判不出来 → 边落 unclassified。

**实测证据**:对 `fn:UNRESOLVED#com.jiuyan.banquet.repository.CategoryRepository#save`,
修前 `class=com.jiuyan.banquet.repository`(包)、`role=unclassified`;
修后 `class=...CategoryRepository`、`role=repository`。
向冻结快照(`edges eb571788012da50d`)注入这 1 条边:
**declared_type findings 0 → 2**(ARCH-007 + ARCH-008),修前该边两条规则均记 `role_unclassified`。
同进程 monkeypatch 旧解析器复现修前值,非口头断言。
**当前快照该形态 0 条**(39 条 UNRESOLVED 全是点号形态),所以真实数据差值为 0——
如实报「已武装未触发」,不拿注入差值冒充真实差值(坑 8 分层归因)。

**根因**:把「上游当前恰好这么写」当成「上游只会这么写」。编码形态是跨所有权接缝,上游随时会加变体。

**防复发守卫**:`UnresolvedEncodingTest` 5 例锁两种形态都解析出声明侧类型、都拿到角色、
都可在 declared 通道判定,且无类名的退化形态仍记 unknown。
**通用纪律**:解析上游标识符时,先枚举**已见过的所有分隔形态**并各写一例;
新增形态用注入阳性对照量化差值,同时报「当前快照该形态条数」,避免把武装当战果。

### 坑 24:归一化函数不幂等 —— 校验过的配置再喂回校验器就炸

**现象**:CLI 先 `validate_config(raw)` 取输入形状,又把**已归一化的 cfg** 传进 `audit()`,
而 `audit()` 内部再校验一次;`edge_kinds` 第一次已从 list 变 tuple,第二次 `isinstance(list)` 失败 →
`ValueError: 规则 ARCH-001 的 edge_kinds 必须是非空字符串数组`,**90 例单测全绿但 CLI 直接崩**。

**实测证据**:`python3 engine/arch_rules.py --units units.json ... --members fn_declaration.json`
修前 rc=1 抛上述 ValueError;改为把 `raw_config` 传进 `audit()` 后 rc=0。
单测全绿是因为它们都直接调 `audit(raw)`,**没有一条走 CLI 路径**。

**根因**:归一化有方向性(list→tuple),校验器却假设输入永远是原始形态;
且测试只覆盖函数入口,不覆盖可执行入口。

**防复发守卫**:新增 `CliReproducibilityTest`(subprocess 跑真实 CLI)2 例:
完整输入 rc=0 且产物含 repro_cmd/sha256;缺必需输入 rc≠0 且**不产出 out.json**。
**通用纪律**:凡有 CLI 入口的引擎,必须有至少一条**走 subprocess 的端到端守卫**;
「函数级全绿」不等于「命令能跑」。归一化函数要么幂等,要么明确只接受原始输入。

### 做法 19:必需输入必须来自仓库已有产物,缺失时硬报错而不是给另一个数(做法 14 的加强)

**证据**:V7 复现 59 unknown 必须自己用 javap 重建 members,因为我的 members 是 `/tmp` 里手工归一化的中间文件。
修法不是「落盘一个新文件」(超出文件所有权),而是**让引擎直接读已有产物**:
配置声明形状 `member_source={records:functions, class, method, modifiers}` → `--members fn_declaration.json`;
`role_fact_source={records:nodes, class:cls, mechanism:type}` → `--role-facts relation_net.json`(dict 容器也支持);
`source_root_key="paths.backend"` → 源码根从 target.json 派生。
**复现命令现在零手工中间文件、零绝对路径参数**,实测 rc=0、unknown=59、`member_coverage={declarations:458, judged:304, declaration_missing:0}`。

**两个必须分开报的口径**(坑 3):`fn_declaration.json` 被 `.gitignore:7 pilot/*_declaration.json` 忽略,是流水线产物;
仅用已跟踪产物时改喂 `relation_net.json`,declarations 458→119、judged 304→41、
**unknown 59→322**(多出 263 条 `member_declaration_missing`)。两个数都对,但不可混比。

**从告警升级为硬报错**:原先缺 members 只告警并给 363,与 59 不可比却长得像同一指标。
现在配置含 visibility 规则而 members 缺失 → **抛 ValueError 拒绝出数**,CLI rc≠0 且不写产物。
异构容器(关系网混着 TABLE 节点)跳过不合格行,但跳过**不静默**:它会变成
`member_coverage.declaration_missing` 与输入告警(守卫 `test_heterogeneous_rows_skip_but_surface_as_missing_declarations`)。

**守卫**:缺必需输入 → 硬报错 + 不产出;两种产物形状各一例;覆盖率进产物字段。
**通用纪律**:「可复现」= 别人用**仓库里已有的东西**跑得出同一个数。
靠交付散文说明「要喂 X」不算可复现;必需输入缺失时**宁可拒绝出数,也不要出一个看起来可比的另一个数**。

### 做法 20:契约缺口提案必须带 oracle 绑定论证与跨载体同步清单

**证据**:7/8 规则 UNMAPPED。提案不是「请加一个类别」,而是逐条给出**为什么现有类别装不下**的客观判据:
本引擎证据种类是 `static_analysis`,layers.yaml 里含该 oracle 的类别只有
function.boundary/complexity/exception_path、file.state_consistency/error_handling_pattern/cohesion、
class.encapsulation_leak/inheritance_design、service.authz/resource_leak;
逐条比对 desc 后无一描述「细胞间禁止边」。特别地 `service.data_consistency` 虽含「事务边界错」
(绕过 service 的后果),但其 oracle 绑定是 `[concurrency_test, property_based_test]`,
**用静态边开这张单等于伪造 oracle 绑定**;`integration.cross_layer_correlation` desc 明写「归并不新增检测」,不能承载新检测。

**提案内容**:class 层新增 `architecture_contract`(oracle `[static_analysis, contract_test]`),
7 条规则共用一个类别(避免同根因开两类单),ARCH-004 保持 `class.encapsulation_leak` 精确映射不动。
**跨载体同步清单**(坑 11 的教训):`agents.total_roles` 语义是 category 总数
(function 4 + file 4 + class 4 + service 6 + integration 5 = 23),新增 1 必须同步改 **23→24**,
否则调度器 `diversity_source` 的分母漂移;`agents.by_layer` 的 class count 是否调整需人工裁定。

**纪律**:提案写进自己独占的配置(`contract_revision_proposal`)并由引擎透传进产物 `contract_gaps`,
**不碰 layers.yaml**;UNMAPPED 而无提案 → `status=needs_decision_no_proposal` + 输入告警,不得静默堆着。
CWE 编号不猜,显式写「待人工裁定」。

---

## 十一、W1 第二轮补充(2026-11-15,复核工具自身的缺陷)

### 坑 25:独立复核脚本自己的 bug,会伪装成被验方的缺陷(坑 3 的补集:坑 3 讲「别复用被验方脚本」,这里讲「自己的脚本要先自证」)

**现象**:W1 自建独立复核脚本回查能力图的每条证据,报出「4 条 test reference 不符」+
「1 条写边无写证据」,看起来是引擎缺陷。**逐条追下去,5 条全是复核脚本自己的 bug,引擎 0 缺陷。**

**实测证据(三次,同一族)**:
1. `path.replace('*','')` 把模板 `/api/orders/*/status` 变成 `/api/orders//status`(双斜杠),
   拿去 grep 原文当然命中不了 → **4 条假指控**。改成「取第一个 `*` 之前的段前缀」后 84/84 全对。
2. 复核脚本只读 `assert.field`,漏了 `assert.fields`(复数)→ `ddl:orders.uk_orders_order_no`
   被误判「无写证据」。引擎的 `_oracle_fields` 本来就读了复数形式,**引擎是对的**。
3. 粗测「方法体 guard」用 60 行正则窗口,在当时的快照上报 **25/59**;改成参数级绑定 +
   精确 body 跨度后,在当前快照上是 **8/67**。**两个数分母不同(上游 relation_net 改过),
   不得直接相减当假阳性数**。可辩护的结论只有一条:粗测**至少含 1 条已证实的跨方法假阳性**
   ——`finalPriceCent` 的 guard 在 `resolveFinalPrice` 里、经局部变量流入 `apply`,
   窗口把它算进了 `apply`。**若按粗测发布,就会把「别的方法的 guard」写成「本细胞已执行」。**
4. 变异测试脚本里写了 `break`,**静默跳过 3 个变异**,却报「4 个被抓住」——
   分母被自己截断了,看起来像高通过率。

**根因**:复核脚本和被验引擎一样是代码,一样会有 bug;但复核脚本的 bug
**方向是单向的**——它只会制造「被验方有罪」的假阳性,不会制造假阴性,
所以特别容易被当成真发现。**测量工具的失败不得记到被测对象账上**(与坑 21 同族,
坑 21 是抽取器,这里是复核器)。

**防复发守卫(已机械化)**:
- 复核脚本必须**双向校准**:先跑一批**已知真**样本(必须 0 不符)和一批**已知假**样本
  (篡改 evidence 的作用域 / 篡改 unit_key,必须变红)。只跑真样本的复核器没有判别力。
- 报「N 条不符」时**必须逐条给出原文行**,不能只给计数——本轮正是靠贴出原文才发现双斜杠。
- 变异矩阵必须报**「跑了几个 / 抓住几个」两个数**,不得只报抓住数;
  W1 最终交付 **12/12 与 7/7,基线 0 失败**,分母可核。
- 粗测(正则/窗口)只能当**上界线索**,不得当结论发布;发布前必须换成可辩护的精确判定
  (参数级绑定、精确语法跨度),并把两者的差额如实报出来。

### 坑 26:CLI 默认空集使一个证据通道静默报 0,看起来像数据缺失实为参数缺失(W2 修,编排者自踩实证)

**现象**:`frame_check.py` 的 `business_levels` 默认空集,而 `business_oracles` 按 `level in business_levels` 过滤。
**不传 `--business-levels` 时 business-oracle 永远报 0**。编排者独立复核时自踩:看到 business-oracle 0,
差点误报「业务判据没挂上」;实际传 `--business-levels L0_business_rule` 后 **5 个候选资源里 3 个有业务判据**
(setStatus 挂 12 条,含 L0 `doc:AGENTS:286.status` 枚举 {PENDING,DEPOSIT_PAID,COMPLETED})。

**根因**:把「参数未供给」与「测量结果为 0」混为一谈。空集过滤使通道恒空,输出 0 看起来像「无业务判据」,
实为「没测」。违反「缺口必须可见、不得静默给误导性数字」(做法 14/19 同族)。

**防复发守卫(已机械化,`test_frame_check.py`)**:
- CLI `--business-levels` 默认 `L0_business_rule`(常用开箱即用);
- `run()` 在 `business_levels` 为空时产 `input_warnings` 且 `variety_deficit.business_oracle_channel="INCOMPLETE"`,
  CLI 前置打印告警、业务通道显示 INCOMPLETE 而非 0;`plumbing_types` 为空同样告警(CRUD 过滤未启用)。
- 守卫:`test_business_levels_empty_reports_incomplete_not_zero`、`test_cli_default_business_levels_not_incomplete`
  (端到端,不传参数断言不报 INCOMPLETE)、`test_cli_explicit_empty_business_levels_incomplete`。
- 变异实测:强制 business_levels 空 → 1/1 红;抹掉告警/INCOMPLETE → 2/2 红;CLI 默认改回空串 → 端到端守卫红。
- **通用纪律**:任何「空集/缺省参数会让某通道恒报 0」的设计,必须把「未测量」与「测得 0」物理区分
  (INCOMPLETE 标记 + 告警),不得静默输出误导性 0。

---

## 十二、W9 补充(2026-11-15,零配置编排入口)

### 坑 28:「引擎全部接受显式路径」只对支柱引擎成立,上游图生成器仍项目绑定(W9 实测)

**现象**:任务书称「新引擎全部零项目字面量、接受显式路径」,据此以为整条链都能零配置跑。
核实源码发现**只有五个支柱引擎**是显式路径 CLI;上游图生成器不是:
- `relation_net.py:622` main 只有 `--out`,输入靠 `artifacts_dir()`/`project_dir()` 读 `target.json`
  (`findings_schema.load_target`),吃 fn_declaration/class_declaration/openapi/invariants/surfaces。
- `build_agent_graph.py:633` main 无路径参数,读 `target.json` + PROJECT_DIR 固定产物。
- `fn_static.py` `ensure_compiled()` 跑 `mvn dependency:build-classpath` + javap 读编译产物。

**实测后果**:零配置入口对业务判据支柱(harvest+bridge)可全自动发现输入;但 capability/arch/frame
三个图支柱需要 units/edges/relation_net 上游产物,这些由项目绑定流水线生成,**入口不伪造**,
缺则 INCOMPLETE。jiuyan 零配置实跑:oracle 117/163 INCOMPLETE,三图支柱全 INCOMPLETE,退出 2;
显式供给工作区已有产物后四支柱全激活,emergence RED(9 确定违反),退出 1。

**根因**:把「支柱引擎通用」误推成「整条链通用」。契约接缝要逐文件核 CLI,不能凭描述归类。

**防复发守卫**:编排入口对每个引擎先核 `add_argument` 与 main 的输入来源(显式路径 vs load_target);
图支柱缺上游产物一律 INCOMPLETE + 写明缺什么,不得假绿。守卫 `test_missing_units_incomplete` 等锁死。

### 坑 29:契约接缝在飞,CLI/计数/报告字段必须每次重核(W9 三次实证,坑 4 同族)

**现象与实测**:W9 开工到交付期间,被调引擎持续被并发 agent 改:
- `arch_rules.py` 从 919 行长到 1016 行:新增「visibility 规则缺 --members 拒绝出数」
  (`arch_rules.py:684` ValueError)、`load_target()` 条件调用(:976,仅 --source-root 缺省且配置声明
  source_root_key 时触发)、报告新增 `contract_gaps`/`member_coverage`。基线那 1 个红
  (example config member_source → `arch_rules.py:125` KeyError)是 W3 在飞,交付时已被其修好。
- `frame_check.py` 新增 `input_warnings` 与 `variety_deficit.business_oracle_channel`(坑 26),
  `--business-levels` 默认改 `L0_business_rule`。W9 第一版 emergence 支柱没消费这两个字段,
  会把「业务通道未测量」当「无」——补消费后加守卫 `test_business_channel_incomplete_not_green`。
- `edges.json` 两次实跑间从 840 增到 842(W6 并发),arch 评估 4200→4210,unknown 恒 191。

**防复发做法**:调用任何引擎前重核当前 `add_argument`/counts 键/报告字段;报数字带时间戳与输入快照;
上游新增的三态字段(warnings/channel)必须纳入编排层三态,不得只读旧字段(坑 19 同族)。

### 做法 21:纯编排入口 audit() 返回 dict、main() 负责 I/O,守卫 monkeypatch 引擎锁三态(W9)

**证据**:`audit_entry.py` 把 `run_engine(script,argv,out)` 隔离成唯一引擎调用点,`audit()` 纯返回报告 dict,
`main()` 才写文件。守卫用 monkeypatch 替换 `run_engine` 喂 canned 报告,**不跑真引擎**即可锁四支柱三态
(41 条,0.25s)。变异实测 5/5 变红(同进程,坑 2):M1 INCOMPLETE 当 0、M2 缺输入静默 GREEN、
M3 引擎失败假绿、M4 arch unknown 洗 GREEN、M5 may-write 当确定 RED。基线 41 绿。
退出码聚合提成 `aggregate_exit(states)` 纯函数,三态可直接单测。

**守卫**:编排层守卫锁「缺输入→INCOMPLETE、引擎 rc≠0→INCOMPLETE、unknown→INCOMPLETE、
确定违反→RED、INCOMPLETE→退出 2」,不锁引擎当前输出字符串。

### 做法 22:缺口归因三分,不把工具/上游缺陷算给被测系统(W9,呼应 absence-attribution)

**证据**:jiuyan oracle 117/163 的缺口必须分桶——40 条不可映射是**桥封闭集多样性赤字(工具)**,
141 条未转录是**散文需物化(上游/工具)**,6 条排除是 needs_confirmation/refuted(上游判据质量),
「未发现 OpenAPI」是**发现 scope**(系统把 spec 放运行期 springdoc 生成,仓库内无静态文件)。
capability 44 条 unlinked 含**系统真缺实现 / 工具连边失败 / 上游 relation_net 缺边**三种,不得合并成一个数。
architecture 191 unknown 是**上游接口 dispatch 未解析(工具/上游)**,不是系统违反。
emergence 9 确定违反才是**系统真缺陷**。入口报告把每支柱的 numerator/denominator 与 reason 分开列,
缺输入/unknown/确定违反物理区分,正是为了让归因可分桶。

### 坑 27:「零静默丢弃」只锁到行粒度 —— 行内子句与无提示词散文仍在丢(V8 实测,2026-11-15)

**现象**:W4 第二轮声称「4 表 54 行零静默丢弃」,行级普查确实成立(V8 独立解析器复核 54/54 覆盖)。
但「采到一行」≠「转录了该行全部规则」,三个更细粒度的丢失通道全部实测存在:

**实测证据**(产物 sha256 `c5c2ba21…`,语料 `/Users/june/tests/auto-api/docs/接口清单.md`):
1. **管道符分裂截断**:`oracle_harvest.py:442` 朴素 `split("|")`,L46/47 校验要点含字面 `|`
   (`image/jpeg|png|webp`)→ 5 列表被切成 7 段,`dict(zip(…))` 静默截断。产物里
   `doc:接口清单:46.iface` 的 `source.text` 止于「Content-Type 必须 image/jpeg」,
   **「最大 5MB」整段从未被看见**(独立 split 复现 7 段)。reference 指向的行与转录文本不再逐字对应。
2. **行内子句丢失**:13 个已采行里 **13 个可执行子句**无断言也无 untranscribed 登记,
   含「name 不超过 64 字符」×2(`_RE_IF_SIZE` 要求字段名紧贴「不超过」,「必填且不超过」不匹配——
   措辞敏感,坑 1 同族)、「productId/packageId 必须存在」、「useFinalPrice=false 时 finalPriceCent=planTotalCent」。
   **变异 M4 实证**:`_interface_asserts` 改成只转录第一个子句 → **30 测试全绿**,无一守卫变红。
3. **无提示词散文静默丢弃**:`oracle_harvest.py:617-626` 兜底只登记含 `_RE_RULE_HINT` 词的行。
   `API.md:183-184`「page 默认 1 / pageSize 默认 50」无任何提示词 → 直接丢弃,
   且 openapi/DDL 均无此默认值,**全语料唯一载体丢失**(围栏内重复 247-248/298-299 同丢)。
   对照:AGENTS.md:274-277 外键散文同被丢,但 DDL 已有 4 条 references 判据——**冗余丢失,不是知识丢失**,
   归因要分开(坑 21 的桶纪律)。

**根因**:守卫的粒度(行)粗于声称的粒度(规则)。「每行有着落」被当成「每条规则有着落」报出;
子句级与无提示词通道没有守卫,变异全绿证明是同义反复盲区(坑 2 同族)。

**防复发守卫(建议,未机械化——V8 只读不改)**:
- 表格解析按列数 `split("|", ncols-1)` 或识别转义 `\|`;cell 段数 > 列数时必须告警不得 zip 截断。
- 已采行的**每个可执行子句**要么有断言、要么进 untranscribed(子句级 reference),并加守卫:
  变异「只转录第一子句」必须变红。
- 「零静默丢弃」的验收必须报**粒度**(行/子句/散文行),并各配一个变异测试;
  散文兜底丢弃前至少登记「无提示词未分类行」计数,不得直接 continue。

### 做法 23:约束集覆盖缺口要三分归因,余单例域可补但跨行/条件约束是真模型局限(W2)

**背景**:多值截面 firing 后,203+ 可执行判据只有 4 个细胞拿到约束集。不是一句「数据不足」,要分解。

**实测分解(222 可执行判据,桥映射后)**:区间/有限域原语 **69**(gte 46/in_set 15/gt 6/lte 2),
余单例域 not_null/not_blank **155**,其他 0。覆盖低三分归因:
1. **类型缺口(可补)**:155 条 not_null/not_blank 曾被排除(返回 None)。它们是**余单例域** V\{null}/V\{null,""},
   抽象域里单例的补仍是合法域元素 → 可补。补后覆盖 **4→18 细胞、2→8 桶**,consistent 2→8、unknown 12→6。
2. **归因缺口(结构性)**:69 条区间/有限域判据多数挂在 depositCent/priceCent 等 DTO 字段,
   而这些字段**不是任何 ≥2 细胞共享的桶资源** → 挂不上。涌现只发生在共享资源上,这不是 bug。
3. **粒度缺口(正确 unknown)**:6 个表桶无字段粒度 → unknown(坑22:聚合不同列约束会造假障碍,不得回归)。

**余单例域的理论边界(证明,非「做不到」)**:无封闭原语强制 null/""，故余单例域与任何非空约束的交仍非空
→ **not_null/not_blank 单独永不致 inconsistent**(只升覆盖,不升障碍检测力)。守卫 `test_not_null_never_causes_inconsistent`;
notblank ∩ {""} = ∅ 可致障碍(`test_not_blank_filters_empty_string`,变异 M2 红)。变异 M1(排除余单例域)→ 2/3 红。

**真模型局限(需更富模型,不是当前单值交集框架可补)**:
- `unique`:跨行单射约束(∀ 行 r1≠r2: f 不同),不是单值域 → 需关系/基数抽象域。
- `default`:条件契约(缺省→d),非无条件域 → 需条件/蕴含域(桥已显式排除)。
- `references`:引用完整性 f∈他表键集,是域约束但键集**动态**(依赖 DB 态)→ 静态 unknown,除非键集已知。
- `field_*_field`:字段间二元关系 → 需关系抽象域(当前可执行集 0 条)。
**通用纪律**:覆盖率低先按「类型/归因/粒度」三分归因再下结论;可补的(余单例域)用抽象域论证补,
真局限的(跨行/条件/动态/关系)指明所需更富模型,不得笼统宣布「做不到」也不得为升覆盖造约束。

---

## W6 补充(2026-11-15,javap -v 缺口修复:@Query 原文 + :: 方法引用边)

### 坑 30:自报的工具局限被证伪 —— javap -v 本来就有注解原文与 MethodHandle(W5 自报,V7 证伪,W6 修)

**现象**:W5 自报「javap 不抽 @Query 注解」当成工具局限;V7 证伪:`javap -v -p`
完整输出 @Query 原文与常量池 MethodHandle。**实现缺口,不是工具局限**。

**实测证据**:改用 -v 后抽到 **3 条 @Query 原文**(CategoryRepository/PackageItemRepository/
PackageTypeRepository,与源码逐字一致,含 text block 多行语句)、24 条同类方法引用事实;
2 个孤儿细胞(`OrderService#packageItemSnapshot`、`PackageMapper#toItemResponse`,
修前入边 0)各得 1 条带双证据(源码 file:line + 常量池 cp#932/cp#146)的入边。

**根因**:只用了 `javap -c`(仅反汇编),没试 `-v`(verbose 含常量池/注解/BootstrapMethods)。
未穷尽工具参数面就宣布「做不到」(坑 7/做法 15 同族)。

**防复发守卫**:宣布工具局限前,枚举工具完整参数面逐一实验;局限必须带可复现反例。
守卫已锁:`test_query_raw_text_extracted`、`test_method_reference_attributed_via_bootstrap_with_cp_evidence`。

### 坑 31:换输出格式必须配段落闸门 —— 否则新段落污染旧统计(W6 自查抓住)

**现象**:javap -c → -v 后,注解段索引行 `0: #17(#18=s#19)` 形如字节码行 `^\s*\d+:`,
无闸门会被数进 bytecode_size。

**实测证据**:无闸门口径下 `ApiResponseAdvice.beforeBodyWrite` 多数 1(=注解索引行数);
加 in_code 闸门(只在 Code 段计数)后,新解析器 **-c vs -v 全字段一致 PASS**
(459 函数 × bytecode/branch/return/null_guard/calls/constants/caught 7 字段)。

**根因**:解析器隐含假设「输入只有 Code 段」,换格式后假设失效。

**防复发守卫**:换/升级输入格式必须加段落闸门 + **跨格式一致性校验**
(同一对象新旧格式各解析一遍,全字段相等才放行)。

### 坑 32:旧统计可能本来就是错的 —— 非回归校验要分辨「修旧缺陷」与「引新回归」

**现象**:切 -v 后 132 个方法的 bytecode_size 与旧声明不一致,第一反应是回归。

**实测证据**:旧解析器对 invoke/ldc/常量行 `continue` 跳过,**bytecode_size 系统性漏计
132/459 个方法、共 1200 条指令**。新口径与**独立指令计数**(直接数 -c 输出 Code 段
指令行,不经过任何解析器逻辑)一致 **458/458**——唯一「例外」是校验脚本自身对重载
`ok` 按名撞键的伪影,按签名核对两个 ok 均 11=11。

**根因**:旧基线自带缺陷;「与旧值一致」不是正确性判据。

**防复发守卫**:非回归校验出现差值时,先用**第三方独立口径**重算正确值;
旧值错则按「修复」报差值并给证据,不回滚到错误基线,也不静默改数。

### 做法 24:多通道证据佐证同一事实 —— 按事实计数,不按通道计数

**证据**:同一处 `::` 调用点有源码 file:line 与字节码 cp#932 两条证据,初版
call_count=2(centrality 被抬到 2);修正为按调用点计数后 call_count=1、evidence=2。
守卫:`test_dual_channel_evidence_one_site_not_double_counted`。

**守卫**:多证据通道指向同一事实时,证据合并、计数不合并;计数以最细粒度事实单元
(调用点)为准。与坑 8(归因重复计算)同族,发生在证据层。

### 做法 25:下游影响要按下游的完整契约输入重测(59-vs-363 悬案解决)

**证据**:做法 11 里无 members/role-facts 测得 arch_rules unknown=363;本轮 W3 契约已升级
(visibility 规则缺 members **拒绝出数**)。按契约供齐 `members=fn_declaration.json` +
`role-facts=relation_net.json` 重测:**基线 786 / 上轮 840 / 当前 842 三份边表 unknown 均为 59**,
findings 均 0——W6 的 +56 条自调用边对 unknown **零贡献**(全 not_applicable)。
59-vs-363 之差是**契约输入口径差**,不是边表变化;两个数各自在其口径内都真。

**守卫**:测下游影响必须用下游声明的完整输入(读它的 main/CLI 参数面);
契约在飞则每次重测,不引用旧数(坑 4/坑 29 同族)。

### 坑 33:「验证者测到 0」≠「产物没重生」—— 先对指纹与字段路径,再下结论(W6 实测)

**现象**:编排者连续几轮测 `fn_declaration.json`「带 query 的函数 0、constants 里含
MethodHandle 0」,怀疑 W6 被环境阻塞、产物一直没重生。

**实测证据**:产物指纹 `247c98b9…` 与 W6 交付**逐字节一致**(未被覆盖);
实际字段:`annotations` 459/459 函数都有、其中 @Query **3** 条、
`method_reference_calls` 非空 **27** 个函数。测到 0 的原因是**字段路径不匹配**:
Query 原文在 `annotations[].type/raw`,不在名为 `query` 的字段;MethodHandle 证据在
`method_reference_calls[].cp`,**不在 constants**(constants 只装 ldc/int 字面量,
这是刻意分离)。另一可能:fn_declaration.json 是 **gitignore 的流水线产物**
(arch_rules.example.json reproducibility_note 明示 `pilot/*_declaration.json` 被忽略),
fresh clone/副本环境里测到的必然是旧版或缺失。

**根因**:验证口径(字段名/环境)与交付编码不一致,被误读成「产物未更新/环境阻塞」。
与坑 4 同族但机制不同:不是移动靶,是**测错了地方**。

**防复发守卫**:
- 验证前先 `shasum -a 256` 对交付方报的指纹;不一致才谈「被覆盖/未重生」。
- 报「0 命中」必须附**实际读的字段路径 + 文件指纹 + 测量环境**(本仓/fresh clone)。
- gitignore 产物跨环境验证时,先确认产物存在与来源,不得把缺产物当成抽取器缺口。
- 交付方报数字时给出**可复制的一行验证命令**(字段路径写明),减少口径漂移。

---

## 十三、W9 第二轮补充(2026-11-15,守卫加固与验证者旧快照)

### 坑 34:验证者冻结旧快照,误报「交付物不存在」(V9 实测,W9 被误报;坑 4/33 验证者侧实例)

**现象**:V9 证伪报告称「`pilot/tests/test_audit_entry.py` 不存在,无守卫的交付不算交付」,
并报 audit_entry.py 23370B、全量 640 passed。

**W9 重测当前文件系统(证据)**:
- `ls -la` + `shasum`:测试文件**存在**,18038B,sha256 `75633a474a21dd37…`,41 守卫全绿;
  audit_entry.py 23842B(非 23370),sha256 `0576d300e5107ac6…`。
- 全量重测 **702 passed + 5 subtests,0 failed**(非 640)。
- 即 V9 冻结的是 W9 **创建测试文件之前**的快照,三个数字全是旧态。

**根因**:验证者在交付方仍在写时冻结快照(坑 4 移动靶),把「我这一刻没看到」当成「不存在」。
与坑 33 同族,但发生在验证者侧。

**防复发守卫**:
- 验证「不存在/未重生」前必须 `ls` + `shasum -a 256` 当前态,不得凭冻结快照断言缺席。
- 交付方报产物 sha256 + 一行验证命令;验证者指纹不一致才谈「被覆盖/未重生」。
- 交付方**改完再报**(本轮 W9 加固后重测 715 绿、报新 sha256 `a1e119e3…`/`1aba4cf7…`),不边改边让验证者追。

### 做法 26:常设机械化变异测试——把「守卫有牙」写成永久绿测试(W9)

**证据**:W9 把 6 个变异写成常设测试(`TestMutationGuards`),每个测试 monkeypatch 改坏实现
(INCOMPLETE→0、缺输入静默绿、引擎失败假绿、arch unknown 洗绿、may-write 当确定红、audit 层 2→0),
再断言对应守卫断言**会红**(`_caught(guard)==True`)。6/6 绿 = 6/6 变异被咬住。

**为何非同义反复**:若守卫无牙(恒过),`_caught` 返回 False → 变异测试本身红。故变异测试可证伪:
它随全量每次跑,回归时自动重验守卫有牙,区别于一次性手工变异(上轮 W9 手工 5/5,本轮升级为常设 6/6)。

**配套加固**(V9 六点要求落地):只读红线升级为**内容 sha256 指纹 + git status 双重对比**
(`_fingerprint` + `TestReadOnlyGit`);新增**无 target 绑定守卫**(bogus `LAYERED_AUDIT_TARGET` 下
oracle/arch 仍出数,证明五支柱显式路径调用不读项目绑定);缺输入显式缺口、供给上游激活支柱均有守卫。
守卫数 41→54。无 target 测试还暴露并修复一处隐藏耦合:`pillar_arch` 写默认规则前未自建工作目录
(原依赖 main() mkdir),已让 audit()/pillar_arch 自建目录。

### 坑 35:验收粒度粗于声称粒度——行级零丢守卫掩盖规则级子句丢失(W4 实测,V8 M4 机器证明)

**现象**:声称「覆盖缺口可见、零静默丢弃」,守卫却只锁**行级**(每行采到或登记)。
V8 的 M4 变异「只转录每行第一个子句」→ **30 测试全绿**。行内后续子句
(「name 必填**且**不超过 64 字符」的 size、「packageId 必须存在」、
「useFinalPrice=false 时 finalPriceCent=planTotalCent」)共 **13 处**无断言也无登记。
**声称粒度(规则/子句)粗于验收粒度(行),守卫盲区。**

**实测证据**:`_interface_asserts` 旧版只返回匹配子句,未匹配子句静默消失;
`_RE_IF_SIZE` 要求字段紧贴「不超过」,「必填且不超过」漏配。修后:返回 (断言, 未匹配子句),
未匹配子句逐条登记 untranscribed;`_RE_IF_SIZE` 改 `.*?不超过`;加 `_RE_IF_EXISTS`。
子句级登记 35 处,name size×2 / packageId references / 条件子句登记全捕获。

**根因**:与坑 3 同族但更隐蔽——不是聚合掩盖分项,是**验收单位的粒度低于声称单位**。
行级「零丢」为真,规则级「零丢」为假,两者不矛盾但后者才是承诺。

**防复发守卫(已机械化,同进程变异验证)**:`test_interface_clause_level_no_silent_drop`
锁真实语料形态(订单行 6 子句),断言第 2/4 子句产原语、第 3/6 子句被登记;
M4「只转录首子句」monkeypatch → **红**。另:管道符截断守卫(末列吸收字面 |,
朴素 split 变异→红)、无提示词默认值守卫(禁用 default 转录→红)、分桶守卫(洗白→红)。
**通用纪律**:声称「X 粒度零丢」就必须有 X 粒度的守卫;写守卫前问「我的验收单位
和声称单位一致吗」;每个守卫做同进程变异,改坏不红就是同义反复。

---

## 十四、V9 独立证伪补充(2026-11-15,零配置入口:路径接缝 / 聚合口径 / 变异基线)

> 验证快照:`audit_entry.py` sha256 `a1e119e3bd223ce5…`(23932B)、
> `test_audit_entry.py` `1aba4cf7737958f8…`(27391B),07:45 冻结,07:48 复核未变。
> 全部结论在此快照上重验过。只读 + 纯单测 + /tmp 沙箱变异,未改任何交付文件。

### 坑 36:子进程 cwd 与用户相对路径不一致 + 「路径真值」当「读取成功」→ 缺输入洗成 GREEN(V9 实测,最严重)

**现象**:两个独立缺陷串成一条红线违规链,结果是**供给的输入静默失效、缺口消失、判 GREEN**。

**实测证据(三段,各自可复现)**:
1. `audit_entry.py:131` `subprocess.run(..., cwd=str(ENGINE_DIR))`,但 `:397/:401` **只 resolve 了
   `repo` 与 `out`**,其余 12 个路径参数原样传进子进程。从仓库根跑
   `--units pilot/units.json --edges pilot/edges.json --relation-net pilot/relation_net.json`
   → capability/arch/emergence **三个支柱全 FileNotFoundError**(`pilot/units.json` 相对
   `pilot/engine/` 解析)。三态诚实(报 INCOMPLETE 不报绿),但**最自然的供给调用方式直接不可用**。
2. `oracle_harvest.py:750` `if args.openapi and args.openapi.is_file():` —— 文件不存在就**静默跳过**;
   `:771` 仍把该路径写进 `inputs.openapi` **声称已供给**。
3. `audit_entry.py:186` `if not d["openapi"]:` —— 判的是**路径真值**,不是读取成功。

**串联后果(实测数字)**:同一批 docs,`--openapi pilot/openapi.json`(相对)→
`by_source={"annotation":66,"ddl":73,"document":79}`,**openapi 键消失**,总数 278→218,
**60 条 L2 接口契约判据凭空蒸发**;报告 `discovery.openapi.paths=["pilot/openapi.json"]` 声称有,
oracle reason **不再提 L2 缺口**,全链路**零告警**。改绝对路径 → `openapi:60` 回来,278/203。

**机制证明(沙箱,排除真实数据干扰)**:令其余缺口全 0,只把 `openapi` 设为不存在的路径 →
`state=GREEN`、reason=「采集判据全部可执行化,零未转录零排除」;
对照 `openapi=None` → `INCOMPLETE`「未发现 OpenAPI」。**缺输入 → pass,三态铁律被破。**

**根因**:与坑 19 同族(unknown 洗成确定结论),但载体是**路径**而非边字段:
「用户给了路径」≠「输入被读到」。跨进程 cwd 变换让二者脱钩,而下游用真值检查代替成功检查。

**防复发守卫(建议,尚未机械化)**:
- 入口**对所有路径参数 `.resolve()`**(或子进程 cwd 设为用户 cwd),并加一条
  「从仓库根用相对路径供给 → 三支柱必须出数」的 subprocess 端到端守卫(坑 24 要求)。
- 缺口判据一律绑**读取成功**,不绑路径真值:`openapi_supplied_and_read` 由采集器回传,
  入口消费该布尔;采集器对「给了路径但读不到」**硬报错或显式登记缺口**,不得静默跳过。
- 变异探针:把 `--openapi` 指向不存在文件且其余缺口为 0 → **必须 INCOMPLETE**。当前该变异**仍绿**。

### 坑 37:入口层 numerator = evaluations − unknown,把 not_applicable 算进分子 → 99.1% 假合规(V9 实测)

**现象**:架构支柱头条报 **6677/6736(99.12%)**,非专家读者(「公司内很多人」的目标用户)
会读成「99% 合规」。真实分项完全不同。

**实测证据**(`/tmp/v9_b3/arch_rules.json` 逐规则 status_counts 求和):

| 分项 | 条数 | 占 6736 |
|---|---|---|
| compliant | **408** | 6.06% |
| violation | 0 | 0% |
| unknown | 59 | 0.88% |
| **not_applicable** | **6269** | **93.06%** |

`audit_entry.py:268/281` 用 `c["evaluations"] - c["unknown_evaluations"]` 当分子 →
6269 条**不适用**被当成「已判定通过」。**真实适用域只有 467/6736 = 6.93%。**
另:ARCH-001/002/005/006/007 **5 条规则 compliant=unknown=violation=0、842 全 n/a**,
即 **5/8 规则在本仓库完全空转**,而 `rules_unmatched=0`(角色匹配上了),
入口的缺口判据只看 `rules_unmatched`,**空转规则不触发任何告警**。

**根因**:坑 3(聚合掩盖分项)+ 坑 9(分母灌水)在**编排层**的新形态:
引擎产物里有正确分项,入口自己造了一个口径错误的比值,把引擎的诚实抹掉了。

**防复发守卫**:入口分子必须用 `compliant`(或 `compliant+violation`),分母用**适用域**;
`not_applicable` 独立成字段;新增「适用域为 0 的规则数」并计入缺口。
报任何比值必须随附分项(坑 20 的口径纪律)。

### 坑 38:变异测试「恢复基线」自己红了 → 5 个「抓住」是假阳性(V9 自己踩,当场纠正)

**现象**:V9 第一轮 16 个变异报「16/16 抓住」。但输出末行 `恢复基线: rc=1 | 1 failed, 36 passed`
—— **基线本身是红的**,M4/M6/M7/M11/M12 的「1 failed, 36 passed」与红基线**逐字相同**,
即这 5 个「变红」是**假阳性**,变异根本没被咬住。

**实测证据**:冻结的 `test_audit_entry.py` sha256 `31bd3321…` 是交付方**在飞中间态**:
`_fingerprint` 已被调用但尚未定义 → `NameError: name '_fingerprint' is not defined`。
重新冻结当前态 `1aba4cf7…`(54 passed 全绿)后重跑 → **真实结果 14/16**,
M6(arch 只取首个源码根)、M7(多 OpenAPI 静默择一)**仍绿**。

**根因**:坑 4(移动靶)+ 坑 5(在飞中间态)在**变异测试**上的复合:
变异测试的判据是「相对基线由绿变红」,**基线不绿则整个实验失去区分力**
(与做法 11 补充裁定「两侧共缺同一输入使差值恒为 0」同构)。

**防复发守卫(已纳入 V9 流程)**:
- 变异 harness **必须在恢复基线后断言 rc==0**,不绿则整轮结果作废、重冻结重跑;
- 每个变异的失败数/失败名要与红基线**逐条比对**,相同即判假阳性;
- 报「N/M 抓住」必须同时报**恢复基线的 rc 与 passed 数**。

### 坑 39:「不读项目绑定」守卫只覆盖简单路径,泄漏路径未覆盖(V9 证伪做法 26 的声称)

**现象**:做法 26 称 bogus-target 守卫「证明五支柱显式路径调用不读项目绑定」。**该声称不成立。**

**实测证据(哨兵探针,决定性)**:`arch_rules.py:976-982` —— `--source-root` 缺失
**且** rules 配置含 `source_root_key` 时,走 `findings_schema.load_target()`。
令 `LAYERED_AUDIT_TARGET=/tmp/v9_NOPE.json`,审计一个**无 `src/main/java` 的合成仓库**、
`--rules` 用含 `source_root_key:"paths.backend"` 的配置 →
`FileNotFoundError: 找不到项目绑定配置: /private/tmp/v9_NOPE.json`。
去掉哨兵则读 `pilot/target.json`,其 `paths.backend = /Users/june/jiuyan/backend`
→ **审计 A 仓库时源码根取自 jiuyan 的项目绑定**(跨项目污染)。

**守卫为何没咬住**:`test_arch_default_runs_with_bogus_target` 传 `java_roots=[tmp_path]`(非空)
→ 入口必传 `--source-root` → 永远到不了 `load_target()`;且 `rules=None` 走默认配置,
而默认配置**不含 `source_root_key`**。**两个条件都被守卫的 setup 排除掉了。**

**同族两处静默择一(变异 M6/M7 仍绿)**:
- `audit_entry.py:253` arch 只传 `d["java_roots"][0]`,而 `:206` capability 传**全部**根
  → 多模块仓库下**两支柱口径不一致**,arch 静默丢弃其余模块,无告警。
- `discover` 对多个 `openapi*/swagger*` 候选**取遍历序第一个**(实测 3 候选选中
  `openapi-v2.json`),返回值无歧义字段 → 多规格仓库下**静默按任意一份规格审计**。

**防复发守卫**:bogus-target 测试必须**枚举到达 `load_target()` 的分支**
(空 java_roots × 含 source_root_key 的 rules);多根/多候选要么全用要么显式报歧义,
并各加一条变异可咬的守卫。**通用纪律**:声称「不依赖 X」的守卫,
必须构造**会触发 X 的输入**,否则守的是「我这条路径恰好没走到 X」。

### 坑 40:零配置声称成立,但「可选供给」路径三处硬缺口(V9 实测)

**纯零配置路径确实可用**(仅 `--repo --out` → 117/170、rc=2、仓库零写入,已复现 3 次)。
但用户一旦想补上游,踩三处:

1. **`--members`/`--role-facts` 对仓库自有产物直接不可用**:`default_arch_config`
   **不含 `member_source`/`role_fact_source`**(grep 计数 0),arch_rules 退到默认容器名 `members`,
   而 `pilot/fn_declaration.json` 的记录容器是 `functions` →
   `ValueError: members 输入里找不到记录容器 'members'`。
   必须改用 `--rules pilot/arch_rules.example.json`(含 `member_source.records="functions"`)才跑得通
   → **「零手写配置」在 visibility 通道上不成立**(做法 19 的契约要求未被入口继承)。
2. **`--out` 指向仓库内部无 containment 守卫**:实测 `--out <repo>/.audit` →
   仓库内写出 3 个产物文件。docstring「不向仓库写一个字节」是**无条件声称**,实际只在默认路径成立。
   `find_artifact` 又恰好在 `repo/.audit/` 找产物,该目录是**自然用法**,踩中概率不低。
3. **入口报告无 `repro_cmd`、无输入 `sha256`、无 `cwd`**(顶层键只有
   schema/repo/work_dir/discovery/pillars/exit_code/limitations)。违反做法 14。
   **后果已实证**:仓库内既有 278 产物的 `inputs.openapi` 是裸相对路径 `"openapi.json"`,
   无 cwd 记录 → 该数字**无法凭产物自身复现**(V9 必须自己 find 到 `pilot/openapi.json`)。

**另:分母供给缺口未披露**。基线报 117/170,全供给报 203/295,差 125 条
(document +65、openapi +60)**确实是仓库外 PRD/规格供给缺口**(归因已独立重算,不重叠)。
但报告里 `grep 278/分母/denominator_incomplete` 全 0 命中,limitations 只有一句泛泛散文。
读者看到 oracle 117/170 与 capability 178/295,**两个分母无法对账**(坑 20 在入口层复发)。

### 做法 27:移动靶下验证者的正确处置(V9 本轮实操,三次重冻结)

**证据**:本轮 `audit_entry.py` 在 15 分钟内变了 **4 次**
(`2d34fe09`→`5e4bdf8e`→`0576d300`→`a1e119e3`),测试文件 3 次(`不存在`→`3656e36a`→`31bd3321`→`1aba4cf7`)。
V9 的处置:① 每次发现哈希漂移**立即重冻结到 /tmp 并重验全部关键发现**,不作废也不照抄旧结论;
② 区分「我首读时的真缺陷」与「已被交付方修好的」——`aggregate_exit` 死代码、
只读守卫只比路径列表(M4)、缺 docs/openapi 无守卫(M11/M12)**四项在验证期间被交付方自行修好**,
如实记为「已修」,不冒充成当前缺陷;③ 在飞中间态(`31bd3321` 的 NameError)**不报为交付缺陷**(坑 5);
④ 所有数字带时间戳 + 输入 sha256(163/278 是 07:34 态,170/295 是 07:48 态,
差异来自上游 `oracle_harvest.py` 被并发改动,**203 可映射稳定不变**)。

**关于坑 34 的事实纠正**:V9 接受「冻结了测试文件创建前的快照」这一纠正。
但坑 34 记的「V9 报全量 640 passed」**不是 V9 报的**——V9 本轮只跑
`test_audit_entry.py`(40→54 passed),从未跑全量、从未报 640。
按做法 11「不背不属于自己的锅」如实退回,请登记方核对来源。

**通用纪律**:验证者与被验方并发时,**报告必须写明快照哈希与冻结时刻**,
每条结论标注「在哪个快照上成立」;交付方改完再报。双方都不许用「我这一刻看到的」当永恒事实。

---

## W4 交接（安全停下,编排者预算将尽;下一会话先读这段）

**稳定态实测(停下时重测)**:全量 **720 passed + 5 subtests, 0 failed**;
`test_oracle_harvest.py` **35 passed**;purity **rc=0**;产物逐字节确定(同代码连跑一致)。
产物 sha256:`4f0314f4d74523a1ca1ca451f92693fd54d6d3279d9205ead30adb3b652734e2`
(`pilot/oracles_harvested.json`)。判据 **295**,桥 **203/299**(299=295 oracles+4 refuted;
`counts.total=295` 仅 oracles),中文 confirmed 假字段 **0**,untranscribed **211**
分桶 noise 65 / business_rule_pending 131 / untranscribable 15。

**已完成(带证据,勿重做)**:
1. 四原始缺陷:reference 行号(严格全首行 218/218)、反引号污染枚举/验收载荷、
   markdown 表格采集、P1-P5 分级。OpenAPI pointer 60/60 真解析到字段 spec。
2. **V8 三个子句级缺陷已全部修复**(不是未完成):
   - 13 处行内子句丢失 → `_interface_asserts` 返回 (断言,未匹配子句) 逐条登记;
     `_RE_IF_SIZE` 改 `.*?不超过`;加 `_RE_IF_EXISTS`。守卫
     `test_interface_clause_level_no_silent_drop`,M4「只转录首子句」变异→红。
   - 管道符截断藏「最大 5MB」→ `_split_row` 用 `split("|",ncols-1)` 末列吸收字面 |;
     列数不匹配显式登记。守卫 `test_pipe_in_cell_not_truncated`,朴素 split 变异→红。
   - 无提示词散文丢失 → `_RE_DOC_DEFAULT` 转录 page=1/pageSize=50;兜底拓宽
     `_RE_VALUE_SEMANTICS`。守卫 `test_no_hint_default_prose_transcribed`,禁用 default 变异→红。
   以上三守卫均同进程变异验证「改坏→红」,非同义反复。
3. 词汇契约:采集器发原始原语,桥(W7 `oracle_bridge.py`)映射,**不越权对齐**
   (曾砸 47→14,见坑 16/17)。契约守卫 `test_harvester_bridge_vocabulary_contract`。
4. 分母口径显式:`oracles_count`/`explicitly_undefined_count`/`total_declarations`/
   `denominators_disjoint`,守卫 `test_denominators_disjoint_and_explicit`。

**未完成(诚实列出,均非 W4 独占文件能单方闭环)**:
- 50 条不可映射判据:封闭集缺 size/pattern/default/unique/references/has_fields/
  set_covers/forbidden_terms 原语。**补原语属求值器 owner**(`invariants.py`/
  `rule_assessment.py`/`oracle_bridge.py`,非 W4 文件)。逐条原因已在桥 unmappable 桶。
- business_rule_pending 131 条散文字段(上传格式/文件大小/金额字段)待**物化绑定**到
  真实 schema(物化层,非 W4 文件)。
- 条件子句(useFinalPrice=false 时 finalPriceCent=planTotalCent)登记为 untranscribed,
  未伪造条件原语(不猜语义);需 when 原语支持。

**下一步具体怎么修(给对应 owner)**:
- 求值器扩原语:`pilot/engine/invariants.py` evaluate() 加分支 + `rule_assessment.py`
  evaluate_observation() 同步 + `oracle_bridge.py` `_NO_COUNTERPART`/`_CMP_OPS` 扩映射
  (W7/W8)。扩后 W4 的 size/pattern 等判据自动可映射,无需改采集器。
- 物化绑定:把 untranscribed business_rule_pending 的散文字段绑到 DDL/DTO/OpenAPI 真实列
  (新物化层文件)。绑定后 demote_unverified_fields 会自动升 confirmed。
- W4 采集侧无待修红测试;若新增语料表格形态,按坑 35 加**子句粒度**守卫并做变异。

**纪律提醒**:数字以当前盘上重测为准(编排者快照常陈旧,如 702/236/177 均为旧态);
改跨文件导出前 grep 下游全部 import;守卫粒度必须匹配声称粒度;unknown 不得变 pass/impossible。

---

## 十五、V10 独立证伪补充(2026-11-15,多值截面:跨实体过挂 / 反向三态 / 覆盖≠检测力)

**冻结快照**:`frame_check.py e8a410cb6b5cec13`、`test_frame_check.py 29af03be72ed8894`、
`relation_net.json c0acee4ff0aed74f`、`units.json 87236ab8c43261ed`、`oracle_bridge.py 5406deb6524751`。
判据在验证期间漂移两次(`c5c2ba21`→`ed986164`→`4f0314f4`,278→295),
**两个快照上结论逐项一致**(18 incidences / 16 distinct cells / 8 buckets / 70 unchecked mounts),非陈旧。
复现:全量 **720 passed + 5 subtests, 0 failed**(任务书 702 为旧态);`test_frame_check.py` **76 passed**;purity rc=0。
独立参考实现:BFS 二染色对 GF(2) 并查集 **400 随机图 0 mismatch**(证实前轮声称)。

### 坑 41:跨实体守卫只认 entity/schema 不认 table —— DDL 判据按裸列名扇出到所有实体(V10 实测,最严重)

**现象**:`_cell_section` 声称「判据的 entity/schema 须出现在细胞足迹里」防跨实体过挂,
但 DDL 判据的 target 只有 `table`,没有 entity/schema → 守卫恒不触发。

**实测证据**:`frame_check.py:470` `owner = target.get("entity") or target.get("schema")`(**无 `table`**);
`_matching_oracles:196` 把任何缺 `entity` 的判据当裸字段判据 join。
全索引上表级判据挂到字段资源 **246** 处,其中按被测源码 `@Table` 注解可证跨实体 **74** 处:
- `Category.isDeleted`(Category→`categories`)收到 `package_types`/`packages`/`products` 三张无关表的约束;
- `PackageType.isDeleted` 同样收到 4 张表;`BanquetPackage.isActive` 收到 `products`;`Product.isActive` 收到 `packages`。
- 8 个约束桶内部可证跨实体挂载 **36** 处。守卫实际只拦下 schema 类 **26** 处,entity 类 **0** 处
  (entity 分支数学上冗余:`_matching_oracles` 已要求 `entity == 资源 owner`,而 owner 必在 ents 里)。

**注入阳性对照(证明已武装且会 firing)**:向冻结快照加两条 DDL 判据
`products.is_deleted ∈ {true}` 与 `packages.is_deleted ∈ {false}` →
**consistent 8→6、inconsistent 0→2**,障碍落在 `Category.setIsDeleted` 与 `PackageType.setIsDeleted`,
而这两个细胞的足迹**根本不含 products/packages**(`CategoryService#create` 足迹只有
`CreateCategoryRequest.name` + `Category.setIsDeleted`,回源码 `CategoryService.java:48` 逐字核实)。
真实数据 0 inconsistent 只是因为四张表的 is_deleted 约束恰好全是 `not_null`+`default`——
**是数据同质,不是机制健全**。与坑 17 的 capability_map 145 条跨实体边同族,发生在约束截面层。

**守卫本身无守卫**:变异 M4(整段删掉 ents 守卫)→ **76 passed / 0 failed**,9 个变异中唯一漏网。

**防复发守卫(建议,未机械化——V10 只读不改)**:
- `owner` 必须取 `entity or schema or table`,且判据是**资源级**:要求 `owner == ident[1]`(或经权威
  实体→表映射等价),不是 `owner ∈ 细胞足迹实体集`(见坑 42)。
- 表级判据挂到字段资源前必须过实体→表映射;映射不可得 → 该判据不进截面(unknown),不得按裸列名 join。
- 必须加「两条不同表同列名、值域不相交」的注入守卫,断言**无关实体桶不得 inconsistent**;
  并加 M4 变异(删守卫)必须变红。

### 坑 42:守卫判据是细胞级而非资源级 —— 别实体的约束造出「细胞间」假障碍(V10 最小反例)

**现象**:即使守卫认了 schema,它问的是「这个实体在细胞足迹里吗」,而不是「这个实体是**该资源**的实体吗」。
于是细胞 A 因为**另外**触及 S,就把 S 的约束套到 X 的资源上,再与只触及 X 的细胞 B 判不相交。

**最小可复现反例**(合成,3 行足迹):
A 足迹 `{X.setStatus, S.setStatus}`,B 足迹 `{X.setStatus}`;
判据 `o_X: entity=X,field=status ∈{PENDING}`、`o_S: schema=S,field=status ∈{OTHER}`。
实测 `section_A(X.status)=∅`、`section_B(X.status)={PENDING}` →
**报 A×B 在 X.status 上 inconsistent**,而 **B 从不触及 S**。
矛盾是 A 自身内部(且源于过挂),不是细胞间分歧 —— Čech 障碍的语义被伪造。

**根因**:守卫的作用域(细胞)粗于被守护对象的作用域(资源×判据)。与坑 15「事实粒度与归属粒度不一致」同族。

**防复发守卫**:守卫判据必须是 `owner == 资源 owner`;并加上述最小反例为守卫
(断言 B 不触及 S 时不得产生 A×B 障碍)。变异「把资源级判据换成细胞级」必须变红。

### 坑 43:definite_writes = writes − may_writes 是资源级集合差 —— 一条不确定边抹掉全部确定证据(V10 实测,坑 19 的镜像)

**现象**:坑 19 修的是「may-write 不得洗成确定写」。修法 `definite_writes = writes - may_writes`
(`frame_check.py:82`)是**按资源**做集合差,不是按证据。同一资源上只要**新增一条**不确定边,
该资源上**已有的确定证据全部失效** → 确定违反被静默删除。

**实测证据(真实候选注入,非合成)**:选 `test_contract_seams.py:103` 自己挑中的同一注入点
`CategoryService#create → Category.setIsDeleted`(该细胞**已经**经 `taint.sinks` 确定写该资源)。
加 1 条 `access="unknown"` 的 WRITES 边后:
- 确定候选 **27→26**,`frame_violation` 总数 **36→34**;
- 该资源 `a_writes` 由 True 变 **False**,候选降为 `may_write_unknown`、`frame_violation=[]`;
- provenance **同时保留** `{"source":"taint.sinks"}` 与 `{"source":"WRITES","access":"unknown"}`
  —— 确定证据在产物里可见,却被判定丢弃。
- 另在 `OrderService#apply → BanquetOrder.setStatus`(FC-0020)复现:确定 9→8、违反 18→16。

**真实数据注入守卫是单向的,所以全绿**:`test_contract_seams.py:146-154` 只断言
①注入后存在 may-write 资源 ②may-write 资源不得标 definite。
两条在「确定写被抹掉」时**都成立** → 守卫 PASS。它锁住了坑 19 的方向,没锁反方向。
真实数据 `may_write_edges=0`,故**已武装未触发**;但任何上游新增一条粗粒度边就会静默删掉已确认违反。

**正确语义**:资源是确定写 ⟺ **存在任一**确定证据;may-write ⟺ **全部**证据都不确定。
即 `definite_writes` 应由 provenance 逐证据判定,不能由 `writes - may_writes` 的资源级差得出。

**防复发守卫**:①注入守卫必须**双向**——加 unknown 边后「确定违反数不得减少」与「may-write 不得标 definite」同时断言;
②加「同一资源上确定证据 + 不确定证据共存 → 仍确定写」的合成守卫;
③变异「把逐证据判定换回资源级集合差」必须变红。
**通用纪律**:三态红线有两个方向,「unknown 不得升为确定」与「确定不得降为 unknown」;
只锁一个方向的守卫会让另一个方向的回归全绿通过(坑 2 同族)。

### 坑 44:覆盖增益 ≠ 检测力增益 —— 6/8 新桶是纯余单例域,可证永不 firing(V10 实测)

**现象**:交付报「约束截面细胞 4→18、桶 2→8、consistent 2→8」,读起来像检测能力大幅提升。
实测**能 firing 的桶数 2→2,零增益**。

**实测证据**:8 个约束桶按域类型分解——
`PackageItemRequest.quantity`(区间)、`BanquetOrder.status`(有限域)是**仅有的 2 个**非余单例域桶,
且**正是修前就有的那 2 个**;新增 6 桶(productId / StatusRequest.isActive / BanquetPackage.isActive /
Category.isDeleted / PackageType.isDeleted / Product.isActive)**全部是纯 `nonnull`**。
做法 23 自己已证明「无封闭原语强制 null/空串,故余单例域单独永不致 inconsistent」——
所以这 6 桶在数学上**不可能**产出障碍,只贡献 consistent 标签。
修前可 firing 桶 **2/2**,修后 **2/8**。

**且 8/8 consistent 都是「双方约束集完全相同」**:实测 8 桶两侧 section 逐一相等,
6 桶两侧 ents 也相等(对称挂载 → 构造上必然相同);另 2 桶(IsDeleted×2)ents 不同但 section 仍相同,
**恰因坑 41 的跨表过挂把判据集变成实体无关**。
「consistent」在此数据上检的是「资源全局约束集可满足」,**不是「两细胞取值一致」**(做法 18 已声明该局限)。
「一方无约束」未被混为 consistent:实测部分覆盖桶 = 0;合成部分覆盖 → `status=unknown, basis=binary`,路由正确。

**口径问题(坑 3/9 同族)**:`constraint_section_cells = sum(sections_present)` 是
**(细胞×资源) 关联数 18**,去重后**distinct cells = 16**;修前的 4 恰好两种口径都是 4,
所以「4→18」混了口径。诚实表述:**4→16 个细胞(4→18 个细胞×资源关联)**。

**防复发守卫**:报覆盖增益必须**同时报检测力增益**——按域类型分桶,
标出「可证永不 firing」的桶数,并给出「能 firing 的桶数」修前/修后两个数。
`constraint_section_cells` 必须同时给 incidences 与 distinct_cells 两个口径。
**通用纪律**(Ashby 用在指标上):不能区分状态的调节器多样性不是多样性;
覆盖率上升而可 firing 桶数不变,必须如实报「检测力零增益」,不得让覆盖数字冒充能力。

### 坑 45:余单例域过滤只排 "" 不排 null —— 与自己的域定义不符(V10 独立枚举参考实证)

**现象**:`nonnull` 定义为 V\{null}、`notblank` 为 V\{null,""},但 `_filter_set_by_co`
(`frame_check.py:390-394`)只在 notblank 时过滤 `""`,**从不过滤 `None`**。

**实测证据**:独立值枚举参考(13 个候选值 × 可达约束原子)——
144 个可达二元组 **4 处不符**、1728 个三元组 **18 处不符**,
**全部**是同一根因:`nonnull ∩ {null}` 与 `notblank ∩ {null}` 引擎返回 `{null}`(非空),参考判空。
端到端复现:向真实 `PackageItemRequest.productId` 注入 `in_set [null]` →
该桶报 **consistent**(应为 inconsistent,因该字段已有 not_null)。
方向是**假阴性(漏报障碍)**,与坑 41 的假阳性相反。

**武装状态(如实报)**:当前 295 条判据中 `in_set` 含 null 的 **0** 条、
非数值区间上下界 **0** 条、字符串域与数值区间同桶混挂 **0** 处 → **已武装未触发**。
另:`_intersect` 对「字符串区间 × 数值有限域」会抛
`TypeError: '>' not supported between 'int' and 'str'`(:447 与 :422),同样 0 条真实判据可触发。

**防复发守卫**:`_filter_set_by_co` 对 nonnull/notblank 都必须过滤 `None`,notblank  additionally 过滤 `""`;
加「in_set 含 null × not_null → inconsistent」守卫;混合类型比较必须先判类型可比,不可比 → unknown(不猜、不崩)。

### 做法 28:验证者的参考实现必须先自证「输入可达性」(坑 25 本轮实例,V10 自己踩)

**现象**:V10 第一版独立枚举参考报 **12 处**引擎不符,看起来像重大发现。
自证后 **8 处是参考自己的错**:它把 `('iv',5,True,3,True)`(上下界矛盾的空区间)当原子喂进去,
而引擎里 `_prim_constraint` 只发**单侧**区间、`_intersect(iv,iv)` 会把空区间**归一化成 `('set',∅)`**
(`_intersect(gte5,lte3)` 实测 = `('set', frozenset())`),所以空区间原子**不是可达状态**。
剔除不可达原子后:144 对 → **4 处真不符**(即坑 45),1728 三元组 → 18 处同根因。

**纪律**:参考实现与被验引擎的**契约边界**必须显式对齐——
只喂引擎自己会产生的状态(可达性),否则不符计数里混着参考的越界输入。
报「N 处不符」必须**分解为「引擎缺陷 / 参考越界 / 口径差」三桶**并各给证据,
不得把总数当缺陷数发布(坑 25:复核器 bug 方向单向,只会制造「被验方有罪」的假阳性)。

### 做法 29:表级「一律 unknown」是数据条件成立,不是结构不变量(V10 核)

**实测**:6 个表桶当前全 `unknown / basis=binary / sections=0`,坑 22 的假障碍红线**成立**——
表级注入两条不相交列约束后 `table:orders` 仍 unknown(约束路径对表级恒返回 None)。
但**结构上表级可以 consistent**:给 `table:package_items` 的两个 in-units 细胞注入
相同 verified `root_cause_key` 后 → `status=consistent, basis=binary, edges_known=1`。
即「表级一律 unknown」依赖「当前无 finding 归属到表桶」,不是代码保证。
表级**永不 inconsistent**(二值 `_pair_label_from_findings` 只产 consistent/unknown)——
这条才是真不变量,也是坑 22 要守的那条。
**纪律**:报「一律 X」前先分清是**结构不变量**还是**当前数据恰好如此**;
前者可用变异证明,后者必须写明依赖条件,否则上游一动就静默失效。

---

## 十六、W9 第三轮补充(2026-11-15,入口诚实性三修)

### 做法 30:入口层诚实性三修——可读性+消费校验 / 适用域分子 / --out 越界拒绝(W9,修 V9 坑 36 等)

**证据(修前→修后,jiuyan 实测)**:
1. **输入校验从「路径真值」升级为「读到且产出预期类别」**。修前:供给不存在 OpenAPI 路径,
   `pillar_oracle` 只判 `if not d["openapi"]`(真值)→ 60 条 L2 凭空消失却判 GREEN(决定性复现:
   canned 全可映射数据 + openapi=不存在路径 → GREEN)。修后:`_unreadable_inputs` 校验每个供给路径
   exists/is_file/is_dir,再核 harvest `counts.by_source` 消费(document/ddl/openapi 供给但 0 产出 → 缺口)。
   实测不存在 OpenAPI → INCOMPLETE「OpenAPI 供给但不可读」+「未产出 L2」。**unknown 不得变 pass。**
2. **arch 分子只算 compliant,not_applicable 单列**。修前分子=`evaluations-unknown`(含 not_applicable)
   → jiuyan 报 4019/4210=95.5% 虚高合规。修后从 per-rule `counts` 求和:分子=compliant 41,
   分母=适用域 232(compliant+unknown+violation),not_applicable 3978 单列,适用域占比 5.5%,
   空转规则数(idle_rules)显式报。**坑 3 入口层形态:聚合不得掩盖分项。**
3. **--out 落被测仓库内 → 拒绝**(rc=2,零写入)。修前 --out 指仓库内真写 3 文件;修后 main() 判
   `out==repo or repo in out.parents` 拒绝。实测 --out /Users/june/jiuyan/.audit_test → 拒绝,目录未创建。

**配套**:所有 CLI 路径参数 resolve 为绝对(修子进程 cwd=ENGINE_DIR 与用户相对路径不一致);
默认架构配置显式声明 member_source/role_fact_source,使仓库自有 members 产物可用(形状不匹配 → INCOMPLETE 不假绿)。

**守卫**:54→69,新增 8 个常设变异测试(含 M6 不存在 OpenAPI 洗绿、M7 not_applicable 进分子),
8/8 咬住;元证伪:正确实现下守卫断言通过 → 守卫无牙则变异测试红。只读红线用内容 sha256 指纹 + git status 双重锁。

**附带观测(非本文件权限,报给 owner)**:并发新建 `test_contract_seams.py::test_confirmation_gate_covers_every_emitted_kind`
红——桥发射的 annotation/ddl/openapi kind 被 `rule_assessment.py` 确认门控拒收(只认 user/document)。
这是 W4(桥)↔W8(求值器)契约缝,与 audit_entry 无关,未越权修。

---

## 十七、V11 独立证伪补充(2026-11-15,架构禁止边:闸门粒度 / 空域绿灯 / 变异站点普查)

被验快照:`arch_rules.py 380cb37a`(1074 行)、`test_arch_rules.py 6b456aaa`(96 绿)、
`arch_rules.example.json 069e2ada`、`edges.json 09a87675`(842 边)、`layers.yaml bb9f6e2c`。
**验证期间 W3 实时改了三次**(引擎 6b06fd47→380cb37a、配置 b87e56f9→1423f0bd→069e2ada、测试 91→96),
下面每条都注明在哪个快照上成立,交付前已重测(坑 4/34 纪律)。

### 坑 46:硬报错只锁「参数缺席」,不锁「参数给了但退化」—— 声称已消除的 363 仍可复现(V11 实测)

**现象**:W3 做法 19 声称「配置含 visibility 规则而 members 缺失 → ValueError、rc≠0、不写产物,
不再静默给出误导性的 363」。**缺席分支确实硬**(实测 rc=1、`arch_rules.py:711` ValueError、out.json 不存在),
但**供给一个形状合法却零声明的容器**就绕过了闸门。

**实测证据(当前快照 380cb37a)**:`--members /tmp/v11b_empty.json`(内容 `{"functions":[]}`)
→ **rc=0、写出产物、unknown=363**、`member_coverage={supplied:true, declarations:0, judged:0, declaration_missing:304}`。
363 正是做法 19 宣布已消除的那个数。告警有,但 rc=0 + 产物落盘 = 下游可当正常口径消费。

**根因**:守卫粒度(`members is None`)窄于声称粒度(「缺成员声明拒绝出数」)。
`declarations==0 且 declaration_missing>0` 是**引擎自己已经算出来的可判别状态**,与 `None` 同样退化,却不同待遇。

**防复发守卫(建议,未机械化——V11 只读不改)**:硬报错条件应为
`members is None or (len(members)==0 and 存在 visibility 规则)`;并加一例「空容器 → rc≠0 且不写产物」。
**通用纪律**:把「必需输入缺失」写成守卫时,必须枚举**缺失的所有物理形态**——
参数缺席 / 容器不存在 / 容器存在但空 / 容器有行但合格行 0。只锁第一种等于留了三个后门。

**已机械化(W3,2026-11-15,V13 重验命中后修)**:四种物理形态现在全部硬报错或显式计数,实测:

| 情形 | rc | 产物 | 报错/计数 |
|---|---|---|---|
| 文件缺失 | **1** | 不写 | `FileNotFoundError` |
| 未供给 `--members` | **1** | 不写 | `ValueError: 必须供给成员声明输入` |
| 容器空 `{"functions":[]}` | **1** | 不写 | `ValueError: 成员声明输入不可用：容器为空` |
| 容器有行但合格行 0 | **1** | 不写 | `ValueError: …容器有 1 条记录但全不合格（跳过 1 条）` |
| 部分合格(1/2) | 0 | 写 | `records_seen=2, records_valid=1, records_skipped=1`;`declaration_missing` 计数 + 告警 |

修前实测(V13 与 W3 各自复现一致):空容器 **rc=0、unknown=365**(旧 363 随边表增长)、declarations=0。
实现:`audit()` 加 `if visibility_rules and not members: raise`;`main()` 用新函数 `member_record_stats()`
区分「容器空」与「有行但全不合格」并给不同报错文案;`member_coverage` 新增
`records_seen / records_valid / records_skipped` 三个活字段。
守卫 `EmptyMemberContainerTest`(6 例)+ `EmptyContainerCliTest`(6 例,含 subprocess 与 in-process 双入口)。
变异 **M14**(去掉 audit 空容器闸门)→ **3 红**;**M15**(去掉 main 空容器闸门)→ **1 红**。
全套 **18/18 KILLED,0 存活**;全量 **1027 passed + 5 subtests, 0 failed**;purity rc=0。

### 坑 47:「路径不存在」与「路径未供给」不同待遇 —— 整条证据通道静默报 0,且交付文档声称会告警(V11 实测)

**现象**:`repro_prerequisites[4]` 逐字写「源码根…缺失时这两项降级**并告警**,判定数字不受影响」。
实测**只有 `source_root is None` 告警,路径给了但不存在时零告警**。

**实测证据(其余输入全同,只切换源码根,三次)**:

| source_root | unknown | scanned_files | unprojected | input_warnings |
|---|---|---|---|---|
| `/Users/june/jiuyan/backend`(存在) | 59 | 63 | 3 | `[]` |
| `None` | 59 | 0 | 0 | 1 条「未供给源码根」 |
| `/tmp/v11b_no_such_root`(不存在) | 59 | **0** | **0** | **`[]`** |

代码位置:`arch_rules.py:882` 只判 `if source_root is None`;`:534` `if path is None or not path.is_file(): continue`
静默跳过全部文件。**在任何别的机器上 `/Users/june/jiuyan` 不存在,走的就是第三行**——
未投影 dispatch 通道报 0 处 / 0 文件,与「测得 0」不可区分。

**根因**:坑 26 的同族第三形态(坑 26 是空集默认参数,这里是**路径真值当可读**)。
W9 做法 30 已在入口层修过同一族(`_unreadable_inputs` 校验 exists/is_file),**引擎层没同步**。

**防复发守卫**:告警条件改为 `source_root is None or not source_root.is_dir()`,
并在 `unprojected_dispatch` 里给 `channel_status: INCOMPLETE`(区别于测得 0);
加一例「源码根指向不存在路径 → 必须告警」。**判定数字不受影响这句是对的(unknown 恒 59),
但「并告警」这半句是假的**——文档声称与实测行为必须逐句对账,不得只核前半句。

**已机械化(W3,2026-11-15)**:告警条件改为 `source_root is None` / `elif not source_root.is_dir()` 两分支,
并给 `unprojected_dispatch.channel_status` 四态:`OK` / `NO_SOURCE_ROOT` / `SOURCE_ROOT_MISSING` / `NO_SCANNABLE_FILE`
(另有 `NO_PATTERN`)。非 OK 时追加告警「occurrences 为空不得读成『测得 0 处』」。
守卫 `SourceRootChannelTest` 5 例,其中 `test_judgment_numbers_identical_across_all_three_root_states`
锁住「三种根状态下判定数字恒同(findings 1 / unknown 0),但 channel_status 三者互异」——
即**判定不受影响是真的,通道不可用必须可见**。
变异 **M16**(去掉不存在路径告警)→ 1 红;**M17**(去掉通道状态告警)→ 2 红;**M18**(去掉 SOURCE_ROOT_MISSING 状态)→ 2 红。

### 坑 48:空域被判成「域内确实没有违规边」—— 5/6 条 clean 规则的适用域是空的(V11 实测)

**现象**:引擎 docstring 明写「命中 0 条不等于绿灯…`NO_VIOLATION_IN_SCOPE` 才是『域内确实没有违规边』」,
并用 `RULE_UNMATCHED` 挡「角色一个类都没配到」。**但还有第二种不可证伪:角色配到了类,两个角色之间一条边都没有。**

**实测证据(当前快照)**:8 条规则里 6 条报 `NO_VIOLATION_IN_SCOPE`,其中 **5 条 `in_scope_edges=0`**
(ARCH-001/002/005/006/007),只有 ARCH-008 真判了 63 条。`counts.rules_clean_in_scope=6` 把 5 条空域
与 1 条实测混成一个数;`counts.rules_unmatched=0` 被 LESSONS 做法 1 当作「规则可证伪」的证据,
而这 5 条在本快照上同样不可证伪。V11 另写 12 条真实上行/反向依赖规则(dto→entity、mapper→service、
service→controller…),**12/12 全部 in_scope=0 且全部报 NO_VIOLATION_IN_SCOPE**。

**下游影响(已核,不夸大)**:`audit_entry.pillar_arch` 因 unknown>0 判 INCOMPLETE,**不会假绿**;
但它的 `idle_rules = counts.rules_unmatched` 同样只数第一种空转,5 条空域规则既不算 idle 也不进分子。

**根因**:verdict 判定链(`arch_rules.py:849-859`)在 `in_scope==0 且 unknown==0` 时落进 `else: CLEAN`,
把**空真(vacuous truth)**当成**实测合规**。与坑 26/47 同族:未测量 ≠ 测得 0。

**防复发守卫**:新增 `NO_EDGE_IN_SCOPE` verdict(或 `CLEAN` 要求 `in_scope>0`),
`rules_clean_in_scope` 只数真判过的;`idle_rules` 应含「角色配到类但零适用边」。
加一例「角色有类、零边 → 不得报 NO_VIOLATION_IN_SCOPE」。
**通用纪律**:报「域内零违规」必须同时报**域的大小**;域为 0 时结论是「没测」,不是「合规」。

### 坑 49:三态闸门有多个物理站点,守卫只锁代表站点 —— visibility 的 resolution 闸门无牙(V11 变异实证,最严重)

**现象**:三态红线在引擎里有**三个独立 resolution 闸门**:visibility(`arch_rules.py:455`)、
layer_skip(`:496`)、forbidden_call/dependency_direction(`:503`)。
把 visibility 那一个单独关掉,**96 条测试全绿**。

**实测证据(判别性用例校准,证明不是等价变异)**:
取 `resolution="unknown"` 且 callee 是**已声明 private 成员**的边
(`com.jiuyan.banquet.config.TraceIdFilter#clientIp`,modifiers=`private`):
- 原引擎 `evaluate_visibility` → `('unknown', 'edge_resolution_unknown', True)`
- M1b 变异体(`if False:`)→ **`('violation', 'callee_visibility:private', True)`**

**unknown 变成确定违反,96 全绿。** 对照:同一矩阵里关掉另两个闸门(M1c)→ 5 红;
三个全关(M1a)→ 5 红;`unknown_markers` 闸门(M5)→ 3 红,**其中有一条专门叫
`test_unknown_access_blocks_visibility`**——作者给 visibility 锁了 `unknown_markers` 闸门,
**却漏了同一函数里的 resolution 闸门**。

**真实数据状态(如实报,不拿注入当战果)**:当前 842 边里非 static 的 39 条,
callee 落在 members 中的 **0 条** → 本洞**已武装未触发**(坑 23 纪律)。
但上游一旦把某个 UNRESOLVED 的 callee 解析进声明表(Java 9+ 接口私有方法、或单位匹配口径变化),立刻触发。

**根因**:守卫按「概念」写(「unknown 不得变 pass」),实现按「站点」分布。
一个概念有 N 个物理站点时,只锁代表站点 = 其余 N-1 个无牙。**坑 11(封闭集三载体)的三态版。**

**防复发守卫**:先 `grep RESOLUTION_STATIC` 枚举**全部闸门站点**,每站点各一条守卫;
并把「关掉任一闸门必须变红」写成常设变异测试(做法 26 形态)。
**通用纪律**:变异矩阵的分母是**站点数**,不是**概念数**。

### 坑 50:穷尽性论证里的普查本身不完整 —— 「含 static_analysis 的类别只有…」漏了一个(V11 独立解析实证)

**现象**:UNMAPPED 提案的 `oracle_binding_argument` 声称「layers.yaml 里含 static_analysis 的类别**只有**
function.boundary/complexity/exception_path、file.state_consistency/error_handling_pattern/cohesion、
class.encapsulation_leak/inheritance_design、service.authz/resource_leak」(10 个),并以此作为
「客观的契约多样性缺口,不是映射失误」的判据。

**实测证据**:V11 独立正则解析 `layers.yaml`(不复用被验方脚本,坑 3 纪律)得 **11 个**,
漏的是 **`file.contract_alignment`**(`layers.yaml:45` 逐字:
`- {id: contract_alignment, desc: "同一文件内两处对同一业务量的口径不一致", oracle: [static_analysis, differential_injection]}`)。
`why_existing_categories_do_not_fit` 的 6 条也没覆盖它。

**结论是否被推翻(分开判)**:**没有**。该类别 desc 是「同一文件内两处对同一业务量口径不一致」,
确实装不下「细胞间禁止边」。**但论证的穷尽性声称是假的**——而穷尽性正是这个提案的全部说服力来源。
`service.data_consistency` 的 oracle 逐字核对**完全成立**(`layers.yaml:67` = `[concurrency_test, property_based_test]`,
不含 static_analysis),`total_roles` 23 = 4+4+4+6+5 **成立**,23→24 算术**成立**。

**根因**:手写普查当客观判据。类别是会被并发 agent 新增的(`contract_alignment` 的注释就写着
「之前这一类只能塞进 UNMAPPED 堆着…现在给它一个正式的家」),**手写清单必然滞后**。

**防复发守卫**:把普查机械化——从 `layers.yaml` 解析出「含某 oracle 的类别全集」,
与提案清单做差集,非空则报错;提案里不写死清单,写「由脚本生成 + 契约 sha256」。
**通用纪律**:凡用「只有 X 个」「无一符合」支撑的论证,那个集合必须**机器枚举**,不得手抄。

### 做法 31:变异矩阵按「站点普查」构造,每个变异用判别性用例校准行为真的变了(V11,含我自己踩的坑)

**我自己踩的(坑 25 本轮实例,当场纠正)**:第一版矩阵用**字符串模式**替换,
`applied=2` 而我以为是 3——因为文件已被 W3 改过,forbidden 闸门的缩进从 8 空格变 4 空格,
模式静默失配,**一个站点根本没被变异**,我却报「M1 已覆盖三态闸门」。
第二版改为**先按 `strip()` 后的语句文本 + 缩进普查全部站点行号**(实测 vis=[455]、skip=[496]、pair=[503]),
再按行号替换,并 `assert text != pristine`(变异必须真的改了文件)。

**第二个校准**:M1b 报「未咬住」时**不能直接下结论**——需要证明它不是等价变异。
构造判别性用例(non-static + callee 已声明 private)实测原引擎 unknown / 变异体 violation,
才确认是真守卫洞。**「变异后全绿」有两种解释:守卫无牙,或变异等价;必须用判别性用例区分。**

**结果(当前快照,基线 96 绿 / 恢复后 96 绿)**:9 个变异 **8 咬住 1 漏**,
漏的那个就是坑 49。M2 硬报错→静默 3 红、M3 hash 编码回退 3 红、M4 declared 回填 14 红、
M5 unknown_markers 3 红、M6 缺声明洗合规 2 红、M7 机制事实优先关掉 1 红。
**报「跑了几个 / 咬住几个」两个数**(坑 25 纪律)。

### 做法 32:移动靶下验证者报「哪个快照上成立」,交付方把陈旧数字换成指向产物活字段(V11 本轮实操)

**验证者侧**:本轮 W3 在我验证期间改了三版。我的处置是**每条发现都带快照 sha256,交付前全部重测**:
- 我在旧快照(6b06fd47)上发现的两条——`repro_cmd` 缺 `python3` 前缀导致逐字执行 permission denied、
  第二口径必须手改 `member_source` 才能跑——**重测时已被 W3 修掉**
  (`build_repro` 用 `shlex.join(["python3"]+argv)` + `repro_cmd_anywhere` 带 cd 前缀;
  `member_source` 改成候选形状列表 + `select_source` 自动选容器)。
  **如实撤回,不当成现存缺陷**(坑 4/34:不得拿旧快照指控当前态)。
- 重测后仍成立的四条(坑 46/47/48/50)才写进报告。

**交付方侧的好做法(值得复制)**:W3 把 `member_inputs[].prerequisite` 里的硬编码计数
(「458 条声明 / 304 条边可判」「119 个节点 / 41 条边」)**删掉**,改成
「活数字见产物 `member_coverage`,勿引用本段里的历史计数」。
**散文里的数字必然陈旧,产物字段不会**——这是做法 14「把必需输入机械化」的推广:
**把会漂移的数字也从散文里机械化掉**。

**但同一文件里 `reproducibility_note` 仍留着「204/223 掉到 38/223」**(坑 50 同族),
与 `member_inputs` 的口径描述并存且互相矛盾。**改一处不改另一处 = 陈旧数字换了个地方活着。**
纪律:删硬编码数字必须 grep 全文件同族表述,一次删净。

### 坑 51:技术约束被满足但业务意图未满足——NOT NULL 对空串判 PASS,需派生待确认 not_blank 候选(W4 实测,编排者端到端跑出)

**现象**:端到端真求值后 `ddl:orders.address.not_null` 对真实观测 `address:""` 判 **PASS**
——空串确实非 NULL,技术约束满足了,但业务意图(地址必须非空)没满足。这正是台账
`db.notnull_bypassed_by_empty_string` 的本质。**纯技术约束抓不到这类缺陷,必须有 PRD 业务意图。**

**实测证据**:采集集只有 not_null(DDL 只写 NOT NULL),没有 not_blank。对 NOT NULL 字符串列
派生 not_blank 候选 **13 条**,全部 `confirmation=needs_confirmation` + `source.kind=inferred`,
**0 误标 confirmed**;address 候选指 V1__init_schema.sql:57。桥实测 13 条全排除(不进判定)。

**根因**:NOT NULL 与「非空白」是两层约束;DDL 只声明前者。把技术约束当业务意图会漏掉
空串绕过;但把推断的业务意图自动标 confirmed 又是上周两条假 HIGH 的根因。

**防复发守卫(已机械化,同进程变异)**:`test_not_null_string_derives_needs_confirmation_not_blank`
锁:仅字符串列派生(数值列不派生)、必须 needs_confirmation + inferred + gate_eligible=false、
桥排除不进判定。变异:误标 confirmed→红;数值列过度派生→红。
**通用纪律**:技术约束派生的业务意图候选**只能 needs_confirmation**,reference 指原 DDL 行
并注明「派生候选非原文」;让缺口可见,确认前不得开单。这是「业务那半不能被技术采集替代」
的具体实例,也是 Ashby——调节器(判据)多样性必须覆盖扰动(空串绕过)的多样性。

---

## 十八、W8 第二轮补充(2026-11-15,再扩封闭集:锚定正则 / 条件契约第四态)

### 坑 51:锚定正则用 re.search 实现 —— 尾随换行假绿,跨语言 $ 语义漂移(W8 实测)

**现象**:给 `field_pattern` 实现「按原文锚定语义」时,若用 `re.search` 代替
`re.fullmatch`,两处放宽:① 部分匹配放过前后缀杂质;② **Python 的 `$` 允许尾随换行,
JS 原文的 `$` 不允许**——判据原文是前端 JS 正则 `/^1[3-9]\d{9}$/`,
`"13812345678\n"` 在 search 下假绿。

**实测证据**:`re.search('^1[3-9]\\d{9}$', '13812345678\n')` → **匹配(True)**;
`re.fullmatch` 同输入 → **不匹配(False)**。守卫
`test_closed_primitives.py::test_pattern_fullmatch_not_relaxed` 含尾随换行阴性用例;
常设变异 M3(search 替换 fullmatch)被该用例咬红。

**根因**:「全匹配」不是「search 加锚点」——锚点本身有语言方言($ 与尾随换行),
实现必须逐字对齐原文语言的语义,不得用宿主语言的近似。

**防复发守卫**:锚定正则一律 fullmatch;阴性用例必须含尾随换行/前后缀杂质;
变异(search 放宽)必须红。**通用纪律**:转录跨语言判据时,先枚举两侧语义差异点
(锚定、换行、Unicode、大小写),每个差异点配一个判别用例。

### 做法 33:条件契约给第四态 not_applicable,模型边界如实报(W8)

**证据**:11 条 default 判据(「不传该字段时落默认值」)转录成
`field_default_when_absent`:字段存在 → **NOT_APPLICABLE**(契约没被考验,
不是 PASS 也不是 UNKNOWN);字段缺失 → UNKNOWN(契约被触发,但「落没落默认值」
需结果态观测,单行输入观测核验不了)。汇总层全不适用 → unknown,ok=None。
17/19 条新原语判据带字段绑定、双求值器无观测全 UNKNOWN(另 2 条 pattern/forbidden
采集时未带 field,留绑定层,不猜字段)。161 条守卫 + 6 个常设变异全绿。

**纪律**:① 「不适用」必须与 pass/unknown 物理隔离(第四态 + 汇总按跳过计),
否则条件契约会被洗成假绿或假 unknown;② 单行观测判不了条件契约的 firing,
就如实报「需输入/结果成对观测」的模型边界,**不得为看到 PASS/FAIL 编造观测或放宽语义**;
③ 字符数 vs 元素数、全匹配 vs 部分匹配这类「两种计数/两种语义」的原语,
detail 里带种类标签(size_kind),守卫锁标签,变异锁混淆。

### 坑 52:三态红线有两个方向 + 跨实体守卫必须按资源实体(W2 修 V10 证伪)

**方向二(反向红线,与坑 14/41 相反)**:`definite_writes = writes - may_writes` 的**降级语义**——
一个资源既有确定写又有 may-write 时,确定写被减掉。V10 实证:注入 1 条 may-write 边 →
**definite 27→23**(无 CRUD 过滤口径)。坑 14 是 unknown 洗成确定写(假阳性),这里是确定写洗成 unknown(假阴性),
**两者都违反三态**。修法:`definite_writes` 独立存储确定写证据(sinks + access≠unknown 的 WRITES),
**确定写优先**——may-write 只是额外信息,不得降级已有确定证据。`writes` 仍保留全部写足迹(契约接缝,坑19)。
守卫 `test_definite_write_not_downgraded_by_may_write`/`test_definite_count_stable_under_may_write_injection`;变异 M1 → 2/2 红。

**跨实体守卫细胞级→资源级**:旧 `_cell_section` 用「判据 entity/schema ∈ **细胞足迹**」过滤,
且 `_matching_oracles` 裸字段分支允许带 table 的 DDL 判据按字段名挂到异实体字段资源。
V10 注入反例:products 表 DDL 判据挂到 Category.name,造 2 个假障碍(细胞足迹根本不含 products)。
修法:① 裸字段分支要求 `not entity and not table`(真正全局判据才按字段名 join);
② `_cell_section` 守卫按**资源实体** `owner != ident[1]` 排除,不用细胞足迹。
守卫 `test_cross_entity_disjoint_ddl_no_false_obstruction`/`test_resource_guard_not_cell_footprint`;变异 M2 → 2/2 红、M3 → 1/1 红。

**null 漏洞(坑45)已修**:`_filter_set_by_co` 对 nonnull/notblank 都排 None(notblank 另排 "");
`_intersect` 非数值区间界 → None(unknown,不崩不猜)。变异 M4 → 3/3 红、M5(去类型守卫)→ 1/1 红(TypeError)。

**覆盖权衡(如实报)**:跨实体修复把无 @Table 映射依据的「DDL 表级判据→实体字段资源」挂载移除,
覆盖 18→14 细胞(2 个 setIsDeleted 桶失去 ddl:*.is_deleted.not_null)→ 这是**正确性优先于覆盖**的正确后果,非回归。
要合法挂回需上游 @Table 实体→表映射(V10 证实源码 @Table 是权威源),不猜。
**通用纪律**:三态守卫必须锁**两个方向**(unknown→确定 与 确定→unknown);跨实体/跨粒度归属守卫按**事实的天然粒度**(资源实体)判,不是按消费方(细胞足迹)判。
---

## 十九、W7 第三轮补充(2026-11-15,判定忠实性:适用域 / 口径混用 / 对账盲区)

### 坑 52:映射路径重建断言时销毁适用域 `when` —— 把「不适用」判成「违反」= 制造假缺陷(V12 实证,W7 修)

**现象**:桥有两条路径。**直通**逐字复制断言,不丢字段;**映射**只挑自己认识的键重建,
把求值器消费的判据级一级字段静默丢弃。`when` 是适用域,丢了它,一条非 VIP 行被判 FAIL。

**决定性实测**(合成夹具,可复现):
判据 `when={field:vip,op:eq,value:true}` + `field_cmp{discount>=0}`,观测行 `{vip:false, discount:-1}`:

| | rule_assessment | invariants |
|---|---|---|
| 修前(丢 when) | **FAIL** ← 假缺陷 | **fail** ← 假缺陷 |
| 修后(保留 when) | **NOT_APPLICABLE** | **unknown**(skipped=1, checked=0) |

**当时语料 `when` 是 0/308、采集器也不发射 → 属「已武装未触发」**。
但判据本来就有适用域,上游一旦开始发射 when 就会**批量**制造假缺陷。
不得因为「当前触发不了」就降级成低优先(与坑 19 同族:武装未触发 ≠ 不存在)。

**根因**:两条路径对「额外字段」的处理不对称。直通=复制,映射=重建;
重建时没有**从求值器源码枚举**它到底读哪些字段,于是只保留了自己想到的那几个。

**防复发守卫(已机械化)**:
- 读源码枚举两个求值器消费的判据级字段:`invariants` 读 `when/assert/id/claim/severity/category/why/oracle`,
  `rule_assessment` 读 `when/assert/id/claim/scope/source`。桥用 `_PRESERVED_FIELDS` 逐字带过去,两个消费函数都发射。
- `test_when_survives_both_mapping_and_passthrough`(7 组原语参数化)、
  `test_non_applicable_row_is_not_applicable_not_fail`、`test_non_applicable_row_is_not_fail_in_invariants_either`、
  `test_when_is_carried_verbatim_not_rewritten`(含 `when:[]` 不得改写成 None —— 求值器对空列表判 UNKNOWN)、
  `test_every_field_the_evaluators_read_is_emitted`(从源码正则抽字段名,断言两个消费函数全覆盖)。
- **变异 6/6 变红**:M10 映射丢 when → 13 红;M11 when 改写成 None → 12 红;
  M12 三桶改数规则 → 6 红;M13 旧式 total 不对账 → 2 红;M14 to_rule_pack 丢 when → 12 红;
  M15 to_invariants 丢 when → 11 红。基线 182 绿。

**通用纪律**:**转录器有两条路径时,必须对「保留哪些字段」用同一份契约**,
且该契约从**消费方源码**枚举,不靠转录方记忆。复制路径天然安全,重建路径天然危险 ——
危险的那条要加守卫,不能因为另一条安全就以为整体安全。

### 坑 53:三桶闭合混用两种计数单位 —— 复合 range 让合法输入被硬判红(V12 实证,W7 修)

**现象**:一条双边界 `range` 判据映射成 **2 条规则**。三桶之和用 `len(mapped)`(规则数)
加 `unmappable`/`excluded`(判据数),于是打印「三桶之和 4 == 处理总数 2」并对合法输入 **rc=1**。

**根因**:与坑 20 同族但更隐蔽 —— 坑 20 是**两个集合**口径不一致,这里是**同一集合内两种计数单位**混用。
`mapped_rules >= mapped_oracles` 是合法且必然的(复合断言),不是数据错误。

**修法**:三桶**按判据数**闭合(`bucket_caliber="oracles"` 显式声明),
`mapped_rules` 另报不参与闭合;CLI 打印 `mapped_rules=N >= mapped_oracles=M` 并说明原因。
实测复合 range:rc=**0**,「三桶之和 1 == 处理总数 1」,规则数 2 另报。

**防复发守卫**:`test_compound_range_closes_by_oracle_caliber`、
`test_many_compound_ranges_still_close`(1/2/5 条参数化)、
`test_compound_range_mixed_with_other_buckets_closes`、
`test_compound_range_cli_does_not_fail_on_legal_input`、
`test_compound_range_rules_keep_distinct_ids_and_same_when`(两条规则都必须带同一份 when)。
变异 M12(三桶改回规则数)→ 6 红。

**通用纪律**:**一个聚合等式里所有项必须同一计数单位**;单位不同就分开报,
不得把「一条声明出多条产物」当成不闭合。判红前先问:这是数据错了,还是我的口径错了?

### 坑 54:对账只覆盖新口径字段,旧式 `counts.total` 从不参与 —— 999 静默吸收(V12 实证,W7 修)

**现象**:上一轮加的对账只比 `oracles_count/explicitly_undefined_count/total_declarations/denominators_disjoint`。
旧生产者只发 `counts.total`,于是 `total=999` 而数组只有 1 条 → **rc=0 静默吸收**。

**修法**:把 `total` 纳入对账(上游口径 `counts.total == oracles_count`,不是 total_declarations)。
实测 `{"total":999}` + 1 条判据 → `mismatches={"total":{"upstream":999,"bridge":1}}`、**rc=1**。
**同时守住上一轮自己的教训(缺失≠不一致)**:只比上游真的声明了的字段,
未声明的列进 `upstream_undeclared`;`{"total":0}`、无 counts、部分 counts 三种旧产物都仍 **rc=0**。

**防复发守卫**:`test_old_style_total_is_crosschecked_not_absorbed`、
`test_old_style_total_agreeing_is_accepted`、
`test_upstream_absent_caliber_fields_are_not_treated_as_disagreement`、
`test_cli_succeeds_on_empty_and_undeclared_caliber`。变异 M13 → 2 红。

### 坑 55:我自己的契约守卫正则漏了一类原语名 —— 封闭集少算 1 个还自称「精确交集」(W7 自踩)

**现象**:`test_closed_set_is_exactly_the_evaluator_intersection` 用 `"(field_\w+)"` 抽
`rule_assessment` 的原语名。W8 新增 `text_not_contains` 不以 `field_` 开头 → **被正则漏掉**,
交集算成 12 而真实是 13。守卫名义上锁「精确交集」,实际锁的是「我正则能看见的那部分」。

**根因**:与坑 1(子串/前缀匹配当证据)同族 —— **用命名前缀当分类依据**,
新原语换个前缀就静默漏掉。守卫的抽取器本身也是被守卫对象的一部分。

**修法**:改成抽所有小写 snake_case 引号串再与 `invariants` 的 dispatch 集求交(前缀无关),
并断言 `len == 13` 且 `text_not_contains in CLOSED_PRIMITIVES`。

**通用纪律**:**契约守卫的抽取器必须前缀无关 / 形态无关**,否则它会在上游合法扩展时静默失效,
而且失效方向是「少算」——看起来仍然绿。给抽取器本身也加一条「已知新增项必须被看见」的断言。

### 坑 58:可映射 ≠ 可判定 —— 下游确认门控的 kind 白名单使 84% 判据结构上无法产出结论(编排者端到端实测,W8 修)

**现象**:桥报「203 条可映射」,端到端喂真实观测后 **203 全 UNKNOWN,0 PASS,0 FAIL**。
「可执行」是过度声称:可映射到封闭原语 ≠ 能产出最终判定。

**实测证据(编排者端到端,只读)**:桥 `--out-rules` 203 条 + `class_observation.json`
真实 db_row 5 条观测 → `observed_status` 已有 **13 PASS**,但 final `status` 全 UNKNOWN。
根因定位到行:`rule_assessment.py` 确认门控只认 `kind ∈ (user, document)`,而桥发射
**openapi 60 / annotation 56 / ddl 55 / document 32** → **171/203(84%)结构上永远过不了门**,
无论观测多完整。剩下 32 条 document 的 scope 是 `接口清单.md:42` 这类,与 DB 观测 scope 对不上。

**根因**:把「技术约束」与「业务规则」混用同一个确认标准。DDL NOT NULL / @NotNull /
OpenAPI required 是作者写下即生效的客观事实,不需业务方确认;要求它们走业务确认门,
等于把 84% 的客观约束永久锁在 UNKNOWN。**分级认识论:两个 tier 都是合法 confirmed,
但必须分开标记报告**(业务 tier: user/document;技术 tier: ddl/annotation/openapi)。

**修法与实测(W8,双 tier 门控)**:修后同一输入 **13 PASS / 0 FAIL / 190 UNKNOWN**,
13 条判定全部技术 tier(ddl:orders,scope 匹配)。190 UNKNOWN 三分归因:
**181 scope 不匹配**(第二根因:绑定层缺口,annotation/openapi 的 scope 是 DTO 名/来源引用)
+ **9 字段缺失**(三态正确行为)+ 0 其他。0 FAIL 是诚实阴性:该样本满足全部可判约束。
守卫:编排者的 `test_confirmation_gate_covers_every_emitted_kind` 变绿(未删断言);
双 tier 可区分、refuted 永不进判定、缺字段仍 UNKNOWN、4 个常设变异全咬住。

**防复发守卫**:① 「可执行/可映射」声称必须端到端验证到 final status,不得停在映射计数;
② 确认门控的 kind 白名单必须覆盖上游实际发射的全部 kind(契约缝守卫已机械化);
③ 报 UNKNOWN 必须带原因分解(门控/scope/字段缺失),不得只报总数(坑 3 同族)。

**所有权交接注记**:W8 的双 tier 实现已在工作区(rule_assessment.py 双 tier 门控 +
test_closed_primitives.py tier 守卫),编排者已将两文件转给 W11;W8 停止写入并交出
逐行改动清单,由 W11 决定采纳或重做。**在飞改动不得静默回退,也不得双写者冲突。**

---

## 十五、W10 补充(2026-11-15,观测适配器:第一次让可执行判据对真实观测出判定)

### 做法 28:观测包必须按 (batch, scope) + 行形切分 —— 求值器对每条规则聚合包内全部观测

**证据(读 `rule_assessment.py:189-194` assess() 源码)**:每条规则对包内**所有**观测求值,
`observed = FAIL if any FAIL else UNKNOWN if any UNKNOWN else PASS if any PASS else NOT_APPLICABLE`。
而 `evaluate_observation` 在 `obs.scope != rule.scope` 时返回 UNKNOWN(:48-50)。
**后果**:一个包里混两个 scope,或同 scope 混两种行形(完整行 vs 单字段行),
缺字段/异 scope 的观测会把本来 PASS 的规则全部洗成 UNKNOWN。

**实测**:contract_boundary 的 5 条完整 orders 行单独成包 → 13 PASS;
若把 transition 的 9 条单字段 `{status}` 行混进同一包,address/deposit 等 12 条 PASS 全被洗成 UNKNOWN
(单字段行缺这些字段 → UNKNOWN → 聚合 UNKNOWN)。故适配器一包一 scope 一行形,
跨批合并只在 scope 一致的批间做,且保持 FAIL>UNKNOWN>PASS 保守序(`observation_adapter.summarize`)。
**通用纪律**:消费「对全部观测聚合」的求值器前,先核它的聚合量词(∀/∃)与 scope 匹配语义;
观测包的同质性是判定正确的前提,不是可选优化。与做法 16(隔离因子)同族,但这里是包结构层面。

### 坑 25 补充(复核判据必须匹配行形 —— 投影行的首指针指向源条目,不是投影行)

**现象**:独立复核适配器证据时报「9/28 证据不符」,看起来是适配器缺陷。
**逐条追下去:9 条全是复核判据过严,适配器 0 缺陷。**
transition 批用 `row_map={"status":"db_status"}`,行是**投影** `{status:"PENDING"}`,
首条证据指针指向源条目 `{http,accepted,db_status}`(整体),本就不等于投影行。
复核脚本用 `resolve(首指针)==row` 判,对投影行恒假 → 9 条假指控。
**修法**:投影行改逐字段核 `row[out]==源[src]` + 每个源字段指针可解析 → **28/28 全一致**。
**通用纪律**(坑 25 同族):复核判据必须按被验对象的**真实形态**写;
报「N 条不符」前逐条贴原文,先排除复核器自己的判据错。测量工具的失败不得记到被验对象账上。

### 三态铁律实例:零 FAIL ≠ 真合规 —— 真缺陷可因判据未确认而对判定层不可见

**实测(输入 `oracles_harvested.json` ea4a7099…)**:28 条真实观测、7 批,
合并 **7 PASS / 196 UNKNOWN / 0 FAIL**。零 FAIL 必须三分归因,不得读成「系统合规」:
1. **真合规(绑定子集)**:观测行确实满足全部已确认、可映射、可绑定的判据
   (status∈枚举、金额>=0、桌数>0、记录字段非 null)——7 PASS 是真的。
2. **真缺陷但判据未确认(上游判据质量)**:`address:""` 在 5 条 orders 行真实存在并被转录
   (证据 `class_observation.json#/contract_boundary/0/db_row`)。已确认的 `ddl:orders.address.not_null`
   对它判 **PASS**(空串不是 null —— 这正是台账 `db.notnull_bypassed_by_empty_string` 的「绕过」本身)。
   能判 FAIL 的 `ddl:orders.address.not_blank_candidate` 是 `needs_confirmation`+`kind:inferred`,
   桥正确排除(不进判定)。**反事实演示**(原样保留未确认状态喂求值器):observed=FAIL、status=UNKNOWN、
   5 条 FAIL 观测、reference `V1__init_schema.sql:57`。即:缺陷已观测,只等业务确认非空白意图。
3. **工具/观测覆盖缺口**:phone 正则判据 confirmed 但封闭集无 pattern 原语(不可映射);
   deposit>final 的 BI 规则被文档明确 refuted(永不进判定);163 条规则的 scope 无观测
   (categories/package_items/package_types 表、全部请求 DTO、document 占位 scope)。
**通用纪律**:报零 FAIL 必须随附「哪些真缺陷因判据未确认/不可映射/被 refuted 而不可见」的逐条归因;
不得为凑 FAIL 把未确认候选提升成 confirmed(破三态),也不得把零 FAIL 包装成合规。

### 坑 4/18 实例:确认闸门是移动靶 —— 求值器 owner 会中扩成两 tier

**实测**:开工时 `rule_assessment.assess` 只认 kind∈{user,document} 的 confirmed,
ddl/annotation/openapi 即使 observed=FAIL 也强制 status=UNKNOWN。交付前重测,owner(W8)已扩成
两 tier(`rule_assessment.py:27-28,183-198`):business(user/document)+technical(ddl/annotation/openapi),
confirmed 的 technical 也放行 status。我的守卫 `test_..._status_stays_unknown` 与正式汇总数字随之变
(7 PASS 的 status 从 UNKNOWN 变 PASS)。**处置**:按坑 18 更新断言形式(锁两 tier 契约 + 未确认仍 UNKNOWN)、
保留意图(闸门有效、observed 可见、unknown 不洗),重跑正式管线刷新数字;不照旧认知报数。
他人文件里锁旧闸门的 `test_oracle_bridge.py:346 test_non_document_kind_is_capped_at_unknown_by_evaluator`
因此变红 —— 归其 owner(W7/W8),不越权修(做法 7)。

### 坑 59:双向守卫的两个方向可能自相矛盾 + 守卫内两次读同一文件遇移动靶假红(W2/V10 本轮,编排者认领)

**现象一(守卫太强且自相矛盾,坑 2 的变体)**:注入守卫同时断言
①「有 may_write 标志的资源不得 definite」(方向一)与 ②「确定写不得被 may-write 降级」(方向二)。
当资源**同时有确定写与 may-write** 时,②要求 definite、①禁止 definite —— **同一输入上互斥**。
具名反例:FC-0019 / `Category.setIsDeleted`,注入 unknown 边后 `a_writes=True, a_may_write=True, write_strength='definite'`
—— 这是**确定写优先的正确行为**,却被方向一判红。W2 拒绝改正确语义去迎合守卫,独立复现证明守卫互斥;
编排者认领并修方向一为「**仅** may-write 证据(无确定写,用 a_writes/b_writes 排除)的资源不得 definite」。
**纪律**:写双向守卫必须核两方向在**同一输入**(尤其边界:两证据并存)上可同时满足;
守卫太强把正确行为判违规,与守卫太弱(同义反复)同样有害。不得为变绿删断言或放宽被测方正确语义。

**现象二(守卫内移动靶假红,坑 4 变体)**:同一守卫内先跑 base 再跑 injected,两次都读 `oracles_harvested.json`;
上游 W4 在两次读之间改文件 → base/new 输入不同 → 方向二报「17 个确定写被降级」的**假红**(实测 lost=17,
数据稳定后重跑 lost=0、守卫绿)。W2 手动冻结快照复现 lost=0 证伪了该红。
**纪律**:守卫的 base/new 必须读**同一冻结快照**(先 copy 到 tmp 再分别喂),不得两次读活文件;
报「N 个降级」前先确认两侧输入 sha256 相同,否则是移动靶不是回归。

---

## 二十、W7 第四轮补充(2026-11-15,两 tier 门控:守卫过期与报告同步)

### 坑 56:能力扩展后守卫过期,但「意图」与「形式」必须分开判 —— 删测试就丢了红线(W11 改门控,W7 修守卫)

**现象**:W11 把确认门控改成两 tier(业务 `user/document`;技术 `ddl/annotation/openapi`,
两者都是合法 confirmed)。我写于旧门控期的守卫
`test_non_document_kind_is_capped_at_unknown_by_evaluator` 断言「ddl 必须被压成 UNKNOWN」→ 变红。

**按坑 18 区分**:
- **意图**(未确认来源不得被当成 confirmed)**仍然有效**;
- **形式**(拿 `ddl` 当「未确认来源」的例子)**过期了** —— ddl 现在是合法技术 tier。

**修法(不删测试、不放宽三态)**:原测试改名为
`test_technical_tier_is_legitimately_confirmed_and_can_decide`,断言 ddl/annotation/openapi
→ `status=FAIL`、`confirmation_tier=technical`、`gate_eligible=False`;
**原意图用真正未确认的来源重新锁住**,新增 4 组守卫:
`test_unrecognized_kind_is_still_capped_at_unknown`(kind=rumor → tier None → UNKNOWN)、
`test_missing_provenance_is_still_capped_at_unknown`(抽掉 reference / confirmed_by)、
`test_non_confirmed_is_capped_at_unknown_under_new_gate`(needs_confirmation/refuted/unconfirmed/"" 四组)、
`test_needs_confirmation_and_refuted_never_reach_the_gate_at_all`(桥侧就排除,根本不进 pack)。

**实测差值**(输入 `ea4a7099…`,203 条映射规则):
`source_confirmation` 从 **32 declared_confirmed / 171 unconfirmed**
变成 **203 declared_confirmed / 0 unconfirmed**;tier 分开计数 **业务 32 / 技术 171 / 未识别 0**。
无观测时仍 **203/203 UNKNOWN**、`gate_eligible` 全 False(三态未放宽)。
编排者带真实观测端到端跑出 13 PASS + 190 UNKNOWN —— 那 13 条正是原先被旧门控压死的通道。
**归因**:解锁来自 W11 改门控,不是桥的功劳;桥只逐字转录 `source.kind`,由门控派生 tier。

**报告必须同步,否则 ships 假陈述**:桥的 limitations 里原有一条
「rule_assessment 只承认 user/document,ddl/annotation/openapi 即使判出 PASS/FAIL 仍被压成 UNKNOWN」
—— W11 之后**变成假话**,已改写。并新增 `counts.by_confirmation_tier` 分开计数 +
CLI 打印「业务 32 / 技术 171 / 未识别 0(FAIL 必须连同 tier 一起读)」。
理由:门控自己的 limitation 写明「a FAIL must be read together with confirmation_tier」,
生产判据包的桥若只报一个聚合数,使用者就分不清 FAIL 是违反业务规则还是违反技术约束(坑 3 同族)。

**防复发守卫(已机械化)**:`BUSINESS_TIERS`/`TECHNICAL_TIERS` 与
`rule_assessment.BUSINESS_KINDS`/`TECHNICAL_KINDS` 由
`test_tier_split_is_reported_and_matches_the_evaluator` 断言相等 —— 门控再改 tier 立刻变红,
不靠人工记得同步(坑 11 的「多个物理载体」在 tier 上重演)。
**变异 4/4 变红**:M16 抽掉技术 tier → 1 红;M17 tier 区分塌成 business → 1 红;
M18 报告去掉 tier 分项 → 5 红;M19 把所有 kind 伪造成 document → 2 红。基线 192 绿。
上一轮 M10–M15(适用域/口径/对账)与 M6–M9 重跑仍全红,无回归。

**通用纪律**:上游**合法**扩展能力后,先 grep 所有断言旧能力的守卫,逐个判「意图过期」还是「形式过期」;
形式过期→换断言留意图,**并把原意图用仍然成立的反例重新锁住**(否则红线随测试一起消失)。
同时 grep 自己产物里的**文字陈述**(limitations / 原因串 / docstring)——
能力扩展会让它们静默变成假话,而假话比红测试更危险,因为它看起来是绿的。

---

## 二十一、W11 补充(2026-11-15,采纳双 tier 门控 + 补守卫:交接冲突与基线污染)

### 做法 34:在飞交接的采纳与加固 —— 合并而非覆盖,补洞而非重做(W11)

**背景**:坑 58 的所有权交接注记落地执行。W11 开工首读是旧门控,跑完全量基线后
首次 Edit 即撞「file changed since last read」——W8 的完整双 tier 实现 + 17 条测试
已落盘(test_closed_primitives.py 161→178)。**处置**:重读当前态,采纳 W8 实现
(保留其命名契约 BUSINESS_KINDS/TECHNICAL_KINDS,对方测试已引用),删掉自己抢跑插入的
重复常量,不覆盖不重做。

**W11 补的洞(26 条新测试,文件 178→204 绿)**:
- needs_confirmation 参数化 6 kind(含 inferred 双重锁)——坑 51 纪律在求值器侧机械化;
- 技术 tier 的 PASS 方向(做法 16 双向)、无观测 UNKNOWN、未到达/无证据/scope 不匹配
  三态闸门不因 tier 放宽;
- 常设变异 M11–M15(同进程 monkeypatch,做法 26):M11 门控缩回业务 only(复现坑 58
  阻塞)、M12 技术 tier UNKNOWN 洗 PASS、M13 needs_confirmation 洗 confirmed、
  M14 tier 常量漂移、M15 门控字面元组漂移(后两个咬结构守卫
  test_gate_union_matches_tier_constants)。**15/15 变异全咬住,基线 204 绿**。

**验收实测(2026-11-15 08:1x,输入 oracles_harvested.json + /tmp/q3obs.json)**:
purity rc=0;全量 **964 passed + 5 subtests,0 failed**;契约缝守卫
test_confirmation_gate_covers_every_emitted_kind 绿(门控保留字面元组可解析形态)。
端到端 final:**13 PASS / 0 FAIL / 190 UNKNOWN**(修前沙盒重建旧门控:203 全 UNKNOWN);
190 分解 = 181 scope 不匹配 + 9 缺字段,与坑 58 一致。13 PASS 全为 ddl 技术 tier。

### 坑 4/5 的交接形态:所有权交接 ≠ 在飞写入已清空(W11 实测)

**现象**:任务书宣布两文件所有权已转 W11、W8 停写;实际 W8 的在飞交付在 W11 首读之后、
首写之前落盘。若按首读认知直接重写,就是双写者冲突(坑 58 注记明令禁止)。
**证据**:Edit 工具报「file changed since last read」;测试数 161→178;
W11 抢跑插入的 BUSINESS_CONFIRMED_KINDS 与 W8 的 BUSINESS_KINDS 重复,合并时删除。
**防复发**:① 接手方首读后、首写前再核一次文件指纹;Edit 报「file changed」是并发信号,
必须重读再动手;② 交接必须附「在飞写入已清空」确认或 sha256 对账;
③ 合并时保留对方测试已引用的命名契约,补洞不重做。

### 做法 11 实例:「修前基线」被并发落盘污染 —— 沙盒重建旧门控才得真差值(W11)

**现象**:W11 首跑「修前」端到端得 13 PASS / 190 UNKNOWN,与任务书「203 全 UNKNOWN」
不符,一度以为任务书陈旧。**隔离因子后真相**:首跑时 W8 新门控已落盘,「修前」跑的
其实是修后代码。把旧门控元组 ("user","document") 在沙盒模块重建(不改文件),
同一输入独立重算 → 真修前 **203 全 UNKNOWN**(171 unconfirmed + 32 confirmed 无匹配观测),
与任务书一致;13 条 UNKNOWN→PASS 差值全为 ddl 技术 tier。
**附带纠正**:W11 曾误认 13 PASS 是 document kind,沙盒逐规则核 kind 证伪——
报差值前每个因子都要独立重算,不得凭首次测量下结论(坑 8/做法 11)。
**纪律**:并发环境下「修前/修后」必须冻结两侧代码版本(沙盒重建或 git 快照),
活文件上跑出的「修前」可能已是修后。

---

## 二十一、W7 第五轮补充(2026-11-15,已武装未触发的语义键 /  stale 快照判定)

### 坑 57:求值器不消费的语义键 —— 直通静默忽略、映射静默销毁,两条路都放宽判据(V12 提出,W7 实证并修)

**现象**:断言上带 `exclusive` / `negate` 这类**修饰键**时:
- **直通**逐字复制 → 求值器只读自己认识的键,**忽略修饰键** → 语义被静默放宽/反转;
- **映射**只挑认识的键重建 → 修饰键**被销毁**,且 `reason=""` 报成完全可映射。

**决定性实测**(合成夹具,可复现):
| 断言 | 观测 | 放行后求值器实际返回 | 应为 |
|---|---|---|---|
| `field_gte_const value=1 exclusive=true` | `f=1` | **PASS / pass** | 严格 > 语义下应 FAIL |
| `field_in_set value=["A"] negate=true` | `f="A"` | **PASS** | 取反语义下应 FAIL |

即 **D4 极性红线在效果上被破**:不是桥算错,是桥把「求值器读不懂的约束」当成「没有约束」。
`range` 带 `exclusive` 时映射路径同样销毁该键(V12 说的「上界整个消失」在当前代码**不成立**,
实测 `maximum` 正常产出 `field_lte_const`;真正消失的是 `exclusive`)。

**修法**:从两个求值器源码**枚举它们真正读取的断言键**
(`left/right/value/min/max/items_path/equals/value_field/qty_field/ref_path/id_field/ref_id_field`),
建 `_CONSUMED_KEYS`(封闭原语)与 `_HARVESTER_KEYS`(采集器原语)两张白名单;
**表外键一律拒收并逐条报原因**,两条路径共用同一道闸。`unit` 保留其专用理由(无量纲比较),
不被通用理由覆盖。实测 9 组直通 + 7 组映射全部拒收,干净断言零过度拒收,**203 条映射数不变**。

**防复发守卫(已机械化,216 条全绿)**:
`test_unconsumed_keys_rejected_on_passthrough`(11 组)、`test_unconsumed_keys_rejected_on_mapping_path`(7 组)、
`test_exclusive_would_relax_polarity_so_it_must_be_rejected`(先证明放行会判 PASS,再断言拒收)、
`test_negate_would_invert_semantics_so_it_must_be_rejected`、
`test_consumed_keys_table_only_allows_keys_evaluators_really_read`(白名单 ⊆ 源码实读键,防止我自己发明键)、
`test_range_upper_bound_is_emitted_not_destroyed`、`test_clean_assertions_are_not_over_rejected`。
**变异 4/4 变红**:M20 关掉键闸 → 20 红;M21 只让映射路径失守 → 7 红;
M22 改成静默剥离键而非拒收 → 21 红;M23 白名单里塞进 `exclusive` → 3 红。

**「已武装未触发」必须如实标注,不得当战果**:实测语料 `exclusive`/`negate`/`when` **全 0 发射**,
`unit:"MB"` 是 **6 条不是 1 条**且**全 needs_confirmation**(根本没走到 unit 闸门)。
所以这些闸门当前未被真实语料触发,修的是**语义风险**不是已发生的假缺陷。
报告 limitations 里显式写明这一点。

**通用纪律**:**转录器的白名单必须从消费方源码枚举,而不是从自己的记忆里列**。
「消费方读不懂的键」有三种处理方式,只有第一种诚实:①拒收并报原因 ②静默忽略 ③静默销毁。
②③ 都会把「有约束」变成「没约束」,而且产物看起来完全正常(`reason=""`)。

### 坑 58:验证者钉的是旧快照,把已修缺陷当成仍在 —— 收尾前必须先核当前 sha256(V12 判定 PARTIAL,W7 复核)

**现象**:V12 最终判定列了两个「真缺陷仍在」,并给出文件行号(`:222` 的 `len(mapped)`、`:267-270` 的 pairs)。
但它钉的快照是 **`5406deb6`**,而当前是 **`f70af439`** —— 中间隔了两轮交付。

**实测复核**(不重做它已证实的部分,只核这两条):
- 复合 range(V12 点名的 `doc:接口清单:30.iface.2` / `:35.iface.2`,把 confirmation 改成 confirmed):
  当前 **rc=0**,打印「三桶之和 2 == 处理总数 2(按判据数闭合)」+「mapped_rules=4 >= mapped_oracles=2」。
  `:275` 已是 `mapped_oracles + len(unmappable) + len(excluded)`,`:287` 有 `bucket_caliber="oracles"`。
- `counts.total` 对账:当前 pairs **已含** `("total", oracles_count)`;
  1 条判据 + `{"total":999}` → **rc=1**,`mismatches={"total":{"upstream":999,"bridge":1}}`。

**根因**:与坑 4 同族但方向相反 —— 坑 4 是「验证者追移动靶」,这里是「验证者钉了过期靶」。
跨多轮交付时,**行号与快照 hash 必须一起报**;只报行号会让下游把上一轮的代码当成本轮的证据。

**防复发做法**:收到「缺陷仍在」的判定,先 `shasum -a 256` 核当前产物与对方钉的快照是否同一个;
不同则**逐条重放对方给的复现命令**,用当前输出判定,而不是直接照改(照改会把已修的东西改坏)。
本轮两条重放都证明已修,故**未改动这两处逻辑**,只把证据回给编排者。

### 坑 60:跨实体守卫只取 entity/schema 不取 table —— 表级判据借裸字段 join 过挂到无关实体(W2 修 V10 最终判定)

**现象**:资源级守卫 `_cell_section` 只取 `entity or schema`,不取 `table`;`_matching_oracles` 裸字段分支
把任何缺 entity 的判据按字段名 join。后果:Category.isDeleted(→categories)收到 products/packages/package_types
三张无关表的约束;V10 注入 products.is_deleted∈{true}+packages.is_deleted∈{false} → Category/PackageType.setIsDeleted
造 2 个假障碍(细胞足迹根本不含 products/packages)。V10 旧代码报 74 处过挂。

**修法(已落地,frame_check.py)**:① `_matching_oracles` 加 table-via-@Table 分支:表级判据经 `entity_meta`
(来自 class_declaration.json 的 jpa @Table 权威映射,entity→{table,columns})只挂到 `t.table==资源实体@Table`
的字段;② 裸字段分支排除 `source.kind=='ddl'`(DDL 必须带 table 经映射挂载,不得借裸字段挂无关实体;
真实 64 条裸字段判据全是 document,不受影响);③ CLI `--declarations` 加载映射。

**实测**:跨实体过挂 **74→0**;V10 表级注入与裸字段 DDL 注入 inconsistent 均 **0**(阳性对照:同实体 Product
两条不相交 products DDL → 真障碍 1,机制仍 firing);覆盖恢复 8 桶。变异 M1(去table映射)3/3 红、
M2(裸字段允许DDL)1/1 红、M3(table跨实体)2/2 红。

**两个口径必须都报**:`constraint_section_cells`=18 是**细胞×资源关联数**,去重 `constraint_section_unique_cells`=16 细胞。
**consistent 信息量限定**:8/8 consistent 桶两侧约束集逐一相等(对称挂载→构造上必然相同),
检的是「资源全局约束集可满足」,**不是「两细胞独立取值一致」**(做法18 已声明,交付数字必须带此限定)。

---

## W2 交接(2026-11-15,安全停下,frame_check.py + test_frame_check.py 独占)

**已完成(全绿,无半成品)**:frame_check 四机制——① frame rule 旁条件(倒排索引 8385→56 比较,72× 加速);
② Čech 环一致性 GF(2) 并查集 + 多值截面(约束集求交,经 oracle_bridge 只读映射);③ CRUD 噪音过滤(27→9);
④ 三态两方向(unknown 不升 definite、definite 不降 unknown,确定写优先)+ 跨实体 @Table 守卫 + null/混合类型安全。
**实测**:我的测试 93 passed;全量 1003 passed + 5 subtests **0 failed**;purity rc=0。
真实数据:候选 27→9(definite 9),cycles 8 consistent/0 inconsistent/6 unknown,覆盖 assoc 18/unique 16/8 桶,
symmetric 8/8。产物 `/tmp/fc_v10final.json` sha256 `0ab7a500d48bcc3b…`;输入 relation_net `c0acee4f`/units `87236ab8`/
oracles `ea4a7099`/class_declaration `60b2750d`。复跑:`python3 engine/frame_check.py --net relation_net.json
--units units.json --findings all_findings.json --oracles oracles_harvested.json --plumbing-types REPO_OP --declarations class_declaration.json`。

**V10 跨实体过挂:已修(非未完成)**。编排者交接模板列为「未完成」是基于旧快照;本轮已落地修复并变异验证(见坑60)。
重放 V10 两条注入均 inconsistent 0,过挂 74→0。**下一个会话不必重修此处**,除非上游 entity_meta/oracles 快照变了(先核 sha256)。

**真正未完成(已知理论局限,非半成品代码)**:① 高阶 Čech H¹(跨资源环/三重以上交集)算不了,只算 1-骨架;
② unique(跨行)/default(条件)/references(动态键集)/field_*_field(关系)需更富抽象域,当前 unknown;
③ δ⁰ 0/9 consistent——finding 通道粒度(endpoint/class_method)与候选资源粒度(field)不相交(Ashby 赤字),
需 field 粒度差分 finding 或独立 judgments 才能 firing;④ 2 个 setIsDeleted 桶现经 @Table 挂回 DDL not_null,
但 DTO→实体数据流投影仍不猜(不对称截面需上游投影映射)。

**下一步(若继续)**:给 `_cell_section` 供 DTO→实体投影映射(类似 entity_meta),让 schema 判据经投影挂到实体字段,
扩大不对称截面覆盖;或接差分执法层供 `judgments` 让环一致性 firing。两者都需上游映射输入,不猜。

---

## 二十二、W3 第三轮补充(2026-11-15,守卫洞系统性排查 / 未测量≠合规)

### 坑 60:`in_scope=0` 被报成「测得合规」——与坑 21 互为镜像(V11 指出模式,W3 自查证实)

**现象**:arch_rules 对域内 0 条边的规则给 `NO_VIOLATION_IN_SCOPE`,与「域内有边且全部合规」**同一个 verdict**。
于是 8 条规则里 5 条什么都没判,却在报告里和 3 条真判过的一起支撑「0 命中 = 真合规」的结论。
坑 21 是把「我没抽到」报成「人家没测」;**这里是把「我没判」报成「判了合规」**,方向同样反了。

**实测证据**(快照 `edges 09a87675ccda9f99` / `units 87236ab8c43261ed`,842 边):
修前 8 条规则 verdict 只有 `NO_VIOLATION_IN_SCOPE` / `PARTIAL_UNKNOWN` 两种,`rules_clean_in_scope=6`;
修后新增 `NO_EDGE_IN_SCOPE`,实测 **5 条 measured_edges=0**(ARCH-001/002/005/006/007)、
**3 条产生判定**(ARCH-003=41、ARCH-004=304、ARCH-008=63),`judgments_made=408`、`unknown=59`。
即:**「真合规」的引擎内证据只来自 3 条规则的 408 次判定**,另外 5 条贡献 0 次判定,
它们的合规性只能由绕开引擎的独立 grep 支撑,不得混算。

**根因**:把「扫描覆盖了全部边」当成「产生了判定」。扫描 842 条 ≠ 判定 842 条;
`not_applicable` 与 `in_scope=0` 都是**未测量**,不是阴性结果。

**防复发守卫(已机械化 + 变异验证)**:
- 每条规则产出 `edges_examined`(扫了多少)/ `in_scope_edges`(域内多少)/ `measured_edges`(= violation + compliant,**不含 unknown**)三个独立字段。
- 新增 verdict `NO_EDGE_IN_SCOPE`,与 `NO_VIOLATION_IN_SCOPE` 物理分开;产物新增 `measurement` 段,显式列出 `rules_with_measurement` / `rules_without_measurement` 与「findings=0 不得读成全局合规」。
- `MeasurementHonestyTest` 5 例;变异 **M11**(把 `NO_EDGE` 塌回 `CLEAN`)→ **5 红**;**M12**(把 unknown 计入 `measured_edges`)→ **1 红**。
- **通用纪律**:任何「0 缺陷 / 全合规」结论,必须同时报**产生了多少次判定**与**哪些规则一次都没判**;分母为 0 的项单列,不得进合规分子。

### 做法 35:被点名一个守卫洞时,系统性变异**所有**同类闸门,不只补那一个(W3)

**证据**:V11 报 M1b(关掉 visibility 的 resolution 闸门 → 96 测试全绿)。
我没有只补 M1b,而是写源码级变异脚本,把**全部三态闸门逐个关掉**跑同一套测试:
首轮 10 个突变体 **9 KILLED / 1 SURVIVED**,存活的正是 M1b(与 V11 独立结论一致);
补 4 例守卫后 M1b **KILLED(3 红)**。再加 2 个针对新字段的突变体,又抓出 **M12 SURVIVED**
(`measured_edges` 把 unknown 算进判定数,无人守卫),补 1 例后 **12/12 全 KILLED,0 存活**。
测试 96 → **105**,全量 **1003 passed + 5 subtests, 0 failed**,purity rc=0。

**关键实现细节**(坑 2 要求同进程):变异用**源码文本替换 + `exec` 进新模块 + 替换 `sys.modules['arch_rules']`**,
每轮 `sys.modules.pop('test_arch_rules')` 强制重导入;用 `pytest.main` 同进程跑,不用 subprocess(会重新导入,变异不生效)。
锚点必须**逐字核对源文件**——我第一次写 M11 锚点时把注释里的「没产生任何判定」记成「没产生任何合规判定」,
脚本静默跳过该突变体并差点报成「全部 killed」。**锚点未命中必须显式报「跳过」,不得计入 killed**(与坑「变异脚本 break 静默跳过」同族)。

**守卫**:M1b 正反两面都锁——同一设置(声明已供给、确为 private、跨顶层类)只把 resolution 换成 unknown 必须 unknown;
只换回 static 必须命中(证明不是恒 unknown);callee 为 public + unknown 也不得变 compliant;resolution 字段缺失同样 unknown。

**通用纪律**:验证者点名一个洞,说明**这一类**守卫可能都缺牙。按闸门锁一个清单逐个变异,
报「N 个突变体 / KILLED 数 / SURVIVED 名单」,而不是只报「已修被点名的那个」。
真实数据 0 条触发的路径**照样必须有守卫**(已武装未触发 ≠ 可以没牙)。

### 补:repro 声明精确化(承做法 14 的 V11 修正)

`repro_prerequisites` 与 `member_inputs[B].prerequisite` 已改成**条件式**表述:
口径 B 是「**在三文件已提交的前提下** fresh clone 可零上游前置复现(实测 rc=0)」,
不再出现无条件「前置为零」。口径 A 明确写 `fn_declaration.json` 被 gitignore、
上游命令 `cd pilot && python3 fn_static.py`(run_pipeline.sh:50)、需被测后端已编译。
三条生成命令(`repro_cmd_anywhere` + 两个变体)本轮再次从干净临时目录逐字跑,**全 rc=0**。

### 坑 65:契约提案被采纳后,只改自己的配置不够 —— 别的默认规则生成器仍发 UNMAPPED(坑 11/17 同族,W3 实测)

**现象**:用户采纳 W3 的契约提案,layers.yaml 新增 `class.architecture_contract`(:61)、`total_roles` 23→24(:107)。
W3 把自己 7 条规则映射过去后 `contract_gaps` 归零,但**零配置入口的默认规则仍是 UNMAPPED**——
类别集不只活在 layers.yaml 和 W3 的配置里,还活在 `audit_entry.py` 的默认规则生成器里。

**实测证据**(同一快照 `edges 911881eb9229efdb` / `layers 5f2aeef097217106`,同一份成员声明):
- W3 的 `arch_rules.example.json`:8 条规则 `category_fit` **全 exact**,`contract_gaps=0`,
  分布 `class.architecture_contract ×7 + class.encapsulation_leak ×1`。
- `audit_entry.default_arch_config(...)` 产出的 6 条默认规则:**ARCH-D1..D5 全 UNMAPPED,`contract_gaps=5`**;
  只有 ARCH-D6(visibility)是 exact。根因在 `audit_entry.py:95-99` 的 `convention()` 助手:
  它给每条规则塞 `unmapped_reason="默认层契约无架构分层/依赖方向类别…"`,**却不带 `layer`/`category`**。
- `findings_schema.validate_finding` 对 `class.architecture_contract` **零问题**;
  对照 `class.nonexistent` → 「不在 class 的允许列表」,`UNMAPPED` → 「契约装不下」。
  `normalize_findings.py:413` 动态调 `load_layer_categories()`,自动认新类别,无需改。

**根因**:与坑 11(封闭集三载体)、坑 17(跨文件词汇契约)同族——**「可用类别集」有多个载体**:
契约文件、各 Agent 的配置、以及**代码里生成默认配置的助手函数**。契约修订只同步了前两个。
提案被采纳 ≠ 端到端解锁;`audit_entry` 是零配置入口,它发的 UNMAPPED 会直接进支柱报告。

**防复发守卫(已机械化)**:
- W3 侧新增 `test_mapping_breaks_if_contract_category_is_removed`:把 `architecture_contract` 从活契约里剔掉再跑,
  必须重新出现 **7 个 contract_gaps** 且 7 条规则退回 UNMAPPED。变异 **M13**(让 `resolve_mapping` 跳过
  「类别是否在活契约里」检查)→ **2 红**,证明该守卫锁的是活契约而不是硬编码字符串。
- 全套变异 **13/13 KILLED,0 存活**;全量 **1005 passed + 5 subtests, 0 failed**;purity rc=0。
- **待修(不属 W3 所有权,已交精确诊断)**:`audit_entry.py:95-99` 的 `convention()` 应改为带
  `"layer": "class", "category": "architecture_contract"`,并删除那句已过期的 `unmapped_reason`。
  按做法 7 诊断但不越权修。

**通用纪律**:契约/封闭集被修订后,**grep 出所有生成默认配置的代码路径**逐个实测,
不能只改自己独占的那份配置就报「缺口归零」。报差值时必须区分「我的配置归零」与「端到端归零」。

### 坑 61:把「需要字节码」误当成「需要重新编译」——先核产物是否已存在再宣布环境阻塞(W6 轮实测)

**现象**:编排者认定 W6「声称需 maven 编译、产物未重生」,实测证伪:
`target/classes` 下已有 **64 个 .class**,`javap -v -p` 直接输出 @Query 原文——
只读即可抽取,无需编译。

**实测证据与归因(三方都要核)**:
- W6 的重生路径**从未用 maven**:复用已有 classes + 只读 javap,连续两轮报告明示
  「无阻塞、不需 maven、产物已重生」——「需 maven」是把条件句(「若 classes 缺失才需编译」)
  误读成现状声明。
- 编排者指的 `target/classes` 是 **Jun 6 陈旧产物**(1 个源文件新于它);
  W6 用的 `/tmp/jiuyan_fn/classes` 是 Nov 15 新鲜编译(0 源文件新于它)。
  两者字节码语义一致:63/64 反汇编逐字相同,MethodHandle/@Query 计数 64/64 相同,
  唯一语义差 `SeedAssetInitializer` 恰是 Jun 6 后改过的源文件。
- 但本轮压力测试揪出**真缺口**(见坑 62):声明里 SeedAssetInitializer 整类缺失。

**根因**:「抽取器需要字节码」≠「需要重新编译」;字节码可能已在多处存在,新鲜度各异。

**防复发守卫**:宣布环境阻塞前先核三件:产物**是否存在、在哪个目录、是否新鲜**
(源文件 mtime 对比);报阻塞必须附「缺什么、哪个路径缺」;验证方指的 class 目录
也要核新鲜度,陈旧产物上的测量不能推翻新鲜产物上的交付。

### 坑 62:签名正则缺 throws 子句 —— 整类方法静默丢失(W6 实测,坑 61 压力测试揪出)

**现象**:`fn_static.METHOD_RE` 只认 `...);` 结尾,带 `throws` 的合法 Java 签名
整行不匹配 → 方法静默丢失,**SeedAssetInitializer 整类 4 个方法 + TraceIdFilter
doFilterInternal 共 5 个方法**不在声明里,其自调用边全部不存在。

**实测证据**:修前 functions=459、自调用边 56;正则补 `(?:\s+throws\s+[\w.$, ]+)?`
后 functions=**464**、自调用边 **61**(+5 条全部回源码对上:SeedAssetInitializer.java
:34/:35/:43/:46、TraceIdFilter.java:35),**旧 459 函数字段零变更**。
守卫:`test_method_with_throws_clause_not_dropped`。

**根因**:签名正则没覆盖 Java 签名全语法面(throws 是合法后缀);丢失无报错。

**防复发守卫**:抽取器解析签名要枚举语法面(throws/泛型/varargs/注解);
加**类级对账**:声明的方法数 vs javap 实际方法数,整类缺失必须可检出
(本轮正是按类对账发现的)。

### 坑 63:判据采到了但缺作用域绑定,永远判不出结果——可执行≠可判定(W4 实测,编排者端到端查出)

**现象**:L0 业务规则 confirmed 54 条,`scope` 全 null。`rule_assessment.evaluate_observation`
要求 `obs.scope == rule.scope`,不匹配即 UNKNOWN。**54 条已确认业务规则永远判不出结果**
——不是观测不足,是判据本身没作用域。桥映射数漂亮(203),链路终点却全 UNKNOWN。

**实测证据**:观测 scope 是表名(orders/products/packages),行键是 snake_case DB 列。
L0 的 target 只有 {field} 无 table → 桥 `_scope` 退到 source.reference → 与观测 scope 不匹配。
修后:从 DDL 列→表客观归属绑定 target.table,端到端 **L0 document 规则 0 PASS → 25 PASS**,
全规则 PASS 13→55,桥规则 203→216。可绑定 L0 53(confirmed 中 35),多表歧义拆分 10,不可绑定 77。

**根因**:把「可执行」(原语可映射)当成「可判定」(能在真实观测上出 PASS/FAIL)。
**绑定(scope/字段→真实容器)也是可判定性的一部分**,缺它判据永远是 UNKNOWN。

**防复发守卫(已机械化,同进程变异)**:`bind_l0_scope` 单表绑定带 DDL 证据、多表按表拆分
各带 reference、非 DDL 列显式标 scope_bindable=false 不猜表、不改 confirmation。
守卫 `test_l0_scope_*`:不绑定变异→红;多表猜单表变异→红。
**通用纪律**:列→表归属是 DDL 客观事实(与 NOT NULL 同档),绑定不是猜业务意图;
但散文字段不得猜表,needs_confirmation 不得借绑定提升 confirmed。
报「可执行」时必须同时报「可绑定 scope 比例」与端到端 final status,不得只报映射数。

### 坑 64:派生候选前先查文档是否已明确否定——否则把文档否定的预期当待确认,重犯假 HIGH(W4 实测,编排者查出)

**现象**:W4 对 NOT NULL 字符串列派生 not_blank 候选,把 `orders.address` 标 needs_confirmation。
但 **`docs/API.md:307` 明写「`address` 可为空;为空时后端保存为空字符串,避免前端非必填地址
导致订单生成失败」**——非空白预期被文档**明确否定**,应是 **refuted**,不是待确认。
这与上周两条假 HIGH 同族(「定金≤成交价」文档明写未定义却被报 HIGH)。

**实测证据**:全语料扫「可为空/选填/非必填」仅 address 命中候选(package_id 是 BIGINT 无候选)。
修后 address 候选 `confirmation=refuted` + `source.kind=document` + `reference=API.md:307`,
桥排除不判缺陷;其余 12 候选无否定声明,仍 needs_confirmation。

**根因**:派生推断候选时只看了 DDL(NOT NULL),没查 PRD 是否已否定该意图。
**文档否定的预期留在待确认,会被后续当成「确认后即成立」开单**——正是假 HIGH 的温床。

**防复发守卫(已机械化,同进程变异)**:`doc_nullable_fields` 扫文档否定声明,
`refute_negated_candidates` 把被否定的候选直接 refuted(带文档 reference)。
守卫 `test_not_blank_candidate_refuted_by_doc_nullable`:有否定→refuted、无否定→needs_confirmation;
禁用 refute 变异→红。**通用纪律**:派生任何推断候选前,先扫文档有无「可为空/选填/非必填/未定义」
类明确否定;有则 refuted,不得先标待确认再等人确认。refuted 只保留观测,不判缺陷。

**台账冲突(供编排者/用户决策,W4 不改台账)**:台账 `db.notnull_bypassed_by_empty_string`
(MEDIUM,「orders.address NOT NULL 被空串绕过」)**很可能是假阳性**——API.md:307 证明空串是
故意设计。技术约束(NOT NULL)被满足≠缺陷,业务意图(地址非空)已被文档否定。

### 做法 36:subprocess 守卫杀不死同进程变异 —— 入口层必须另有 in-process 守卫(W3 实测,坑 2 的补集)

**现象**:坑 2 要求「变异测试要在同进程内 monkeypatch,subprocess 会重新导入,变异不生效」。
这条纪律有个**反向盲区**:如果你的守卫本身是 subprocess 跑的(CLI 端到端测试),
那么同进程变异**也杀不死它**——子进程读的是磁盘上的真文件,不是你变异后的内存模块。

**实测证据**:W3 给 `main()` 加了「空容器精确报错」闸门,并写了 subprocess CLI 守卫。
变异 **M15**(把该闸门条件改成 `if False:`)→ **SURVIVED,115 测试全绿**。
原因不是守卫没牙,而是变异根本没到达被测代码路径:CLI 测试 spawn 的
`sys.executable engine/arch_rules.py` 读磁盘原文。
补 3 例**同进程直调 `ar.main()`**(`mock.patch.object(sys, "argv", ...)` + 临时目录绝对路径参数)后,
M15 → **KILLED(1 红)**。全套 18/18 KILLED。

**根因**:守卫的**执行载体**与变异的**注入载体**必须一致。
subprocess 守卫验的是「磁盘上的文件能跑」,in-process 守卫验的是「这段逻辑正确」——两者互补,不可互相替代。

**防复发守卫**:凡有 CLI 入口的引擎,入口层闸门必须**同时**有
①subprocess 端到端守卫(锁 rc / 产物是否落盘 / 命令逐字可执行)与
②in-process 直调 `main()` 守卫(锁报错文案与分支,可被同进程变异杀死)。
变异报告里若出现「CLI 路径突变体 SURVIVED」,先判是**守卫洞**还是**载体不匹配**,再决定补守卫还是补 in-process 入口;
不得因为「subprocess 测试绿」就宣称该闸门有牙。

### 坑 65:散文兜底 default 解析把技术栈说明当字段默认值——字段须可核实,且区分合法中文描述字段与纯伪影(W4 实测,V13 查出)

**现象**:`doc:AGENTS:57.default` = `{field:"Java", value:"使用"}`。AGENTS.md:57 原文
「Java: 默认使用 Java 21;如本机或镜像阻塞,可降到 Java 17」是**技术栈说明**。
`_RE_DOC_DEFAULT` 见「默认」就把 colon 前词当字段、后词当值 → 纯解析伪影。
三态纪律兜住了(needs_confirmation 被排除判定),但**仍是不该存在的编造判据**。

**关键区分(不得一刀切)**:全量中文字段判据 20 条,**19+ 条是真规则**
(上传格式=[jpg,jpeg,png,webp]、文件大小=5、金额字段>=0、错误码列表),只是字段名是中文描述;
**只有 Java=使用 是伪影**。修法只门控 **default 类**断言的字段可核实性,
**不动 in/field_cmp 的合法中文主语规则**(一刀切会误删 19 条真规则)。

**实测证据**:`drop_unverifiable_defaults` 用扩展清单(DDL/DTO/OpenAPI schema **+路径参数**,
page/pageSize 是参数不在 schema)门控 default:Java 无处可查→进 untranscribed;
page/pageSize 可核实→保留 6 条;中文字段判据保留 20 条;总判据 342→341(仅减 1 伪影)。

**根因**:① 散文 default 正则把动词「使用」当值、技术栈词当字段;② 现有降级守卫
`demote_unverified_fields` 只把不可核实字段**降级** needs_confirmation,**未移除**——
降级≠不产出,荒谬断言仍存在。

**防复发守卫(已机械化,同进程变异)**:`test_default_unverifiable_field_not_produced`
(Java 不产出+进 untranscribed、page 保留)、`test_default_gate_preserves_chinese_subject_rules`
(空清单下 in/field_cmp 中文规则不被误删)。禁用门控变异→红。
**通用纪律**:字段断言(尤其 default)的字段名必须可核实于 DDL/DTO/OpenAPI(含参数);
不可核实→进 untranscribed,不产荒谬断言;但**合法中文描述字段是待物化真规则,不是伪影**,
门控须按断言类型精确区分,不得按「字段含中文」一刀切。

---

## 二十三、V13 独立重验补充(2026-11-15,四支柱修复复核:等价变异 / harness 伪影 / 检测力指标)

**冻结与漂移**:开工 14:35 全量 **1019 passed + 5 subtests**(任务书 1003 为旧态),收尾 **1043 passed**,purity rc=0(32 文件)。
验证期间上游漂了 4 轮:`arch_rules e2e3d56b→a33421e1→53e08a73→fc0f01c4`、
`oracle_harvest 763b2e82→ef3da16d→d25410dd→75a46b62`、`oracles ea4a7099(308)→761cbcd8(342)→7a9c5cfd(342)→82fa1e91(341)`、
`units 87236ab8→0367197b`、`edges 09a87675→911881eb(886 边)`。`frame_check fbdcf2da`/`oracle_bridge c796a7f3`/`audit_entry 36744c1a` 全程未变。
**变异矩阵合计 40 个突变体 / 35 KILLED / 5 SURVIVED,5 个存活逐个证明为「等价变异 / 不可达 / harness 伪影」,无一为真守卫洞。**
被测仓库 jiuyan **334 文件内容 sha 逐字未变**(c39ab761…)、96 目录未变、git status 14 项未变、HEAD df3c341b 未变。

### 坑 61:「等价变异」是绑定当前实现的临时结论 —— 声明的下一步扩展会让它变成真洞(V13 实测)

**现象**:frame_check 变异 M9(`cycle_consistency` 的 `elif len(have) == len(cells)` → `>= 1`)**93 测试全绿**。
按做法 31 构造判别性用例证明它**不是等价变异**:手工造不对称截面(A 有约束、B 无),
原引擎 `('unknown','binary',sections=1/2)`,变异体 `('consistent','constraint_set',1/2)` —— **unknown 被洗成 pass**,三态红线。

**为什么全绿(可达性证明,不是猜测)**:`_cell_section(fp, resource, oracles, entity_meta)` 的函数体
**从不读 `fp`**(源码正则核实 `\bfp\b` 在 docstring 之后 0 命中)→ 截面只由资源决定 →
同一桶内所有细胞截面恒等 → `have ∈ {0, len(cells)}`,`0 < have < len(cells)` **结构上不可达**。
唯一名字对得上的守卫 `test_constraint_missing_is_unknown_not_consistent`(`test_frame_check.py:689`)
喂的是**零判据**(have=0),M9 不改它的行为。真实 14 桶实测:8 桶 secs==cells、6 桶 secs==0,无中间态。

**根因**:守卫的可达性依赖「截面与细胞无关」这个**实现细节**,而 W2 交接明写下一步是
「给 `_cell_section` 供 DTO→实体投影映射」——那一步会让截面变成细胞相关,**M9 当场从不可达变成可达**,
且届时仍无守卫。与坑 49(闸门多站点)同族,但这里是**同一站点的输入域会扩张**。

**防复发守卫(建议,未机械化——V13 只读不改)**:加一例直接调 `cycle_consistency` + monkeypatch
`_cell_section` 造 `0<have<len(cells)` 的守卫,断言 `status=unknown, basis=binary`;
并把「`_cell_section` 是否读 fp」写成结构守卫:一旦开始读 fp,该不对称守卫必须同时存在。
**通用纪律**:判「等价变异」必须写明**依赖哪条可达性前提**,并把该前提本身机械化;
前提消失时等价变异自动升级为真洞。不得把「今天不可达」记成「永久无牙」。

### 坑 62:同进程变异 harness 对 subprocess 守卫恒失效 —— 存活必须先分三类再报(V13 自踩,坑 25 本轮实例)

**现象**:V13 用做法 35 的同进程模块替换跑变异,两个突变体存活:
`arch_rules` M13(CLI 空容器闸门 `if member_stats and records_valid==0` → `if False`)、
`audit_entry` M8(`--out` 越界闸门 `if out == repo or repo in out.parents` → `if False`)。
**两者都是 harness 伪影,不是守卫洞**:对应守卫(`EmptyContainerCliTest`、`TestOutBoundary`)
用 `subprocess.run([sys.executable, str(PILOT/"engine"/…)])` 跑**磁盘上的原文件**,
内存里的变异体根本没被加载(坑 2 的镜像:坑 2 说 subprocess 会让 monkeypatch 失效,这里是它让**文件级**守卫测不到)。

**实测证据(文件级沙箱重做,两个都 KILLED)**:把 engine+tests 拷进 `/tmp/v13cli/pilot/`,
按行替换闸门后跑同一套测试:M13 → `test_empty_container_exits_nonzero_and_writes_nothing` +
`test_main_rejects_empty_container_in_process` **2 红**;M8 → `test_out_inside_repo_refused` +
`test_out_equal_repo_refused` **2 红**。沙箱基线先跑绿(6 passed / 3 passed)才做变异(坑 38)。
**沙箱陷阱**:直接拷到 `/tmp/x/engine` 会让 `findings_schema.contract_path()` 解析到 `/private/tmp/layers.yaml`
而 FileNotFoundError(基线 2 红)——必须保留 `pilot/engine` + `pilot/tests` 的**相对层级**,否则基线不绿、整轮作废。

**另两个存活是真等价/不可达(逐个证明,不笼统归类)**:
- `arch_rules` M2(`audit()` 的 `if visibility_rules and members is None`)→ 紧邻下方还有
  `if visibility_rules and not members`,`not None` 为真 → 仍抛 ValueError,**冗余闸门,等价变异**。
- `arch_rules` M6(`evaluate_visibility` 的 `if members is None`)→ 直接调该函数,原引擎返回
  `('unknown','member_declarations_not_supplied')`,变异体 **TypeError: NoneType not iterable**(非等价);
  但 `audit()` 两道前置闸门使 `members=None` + visibility 规则**不可达**(实测抛 ValueError)。

**防复发做法**:变异 harness 必须**按守卫的执行形态分派**——同进程断言用模块替换,
subprocess/CLI 断言用文件级沙箱;报「N 个存活」时每个都附**三分类之一 + 证据**
(harness 伪影→文件级重做;等价变异→给出冗余站点;不可达→给出可达性证明与解除条件)。
**通用纪律**:「变异后全绿」有**四**种解释(守卫无牙 / 等价变异 / 输入不可达 / harness 没加载变异体),
做法 31 只列了前两种。第四种在混合测试形态的仓库里必然出现,不分类就会把伪影报成缺陷。

### 坑 63:独立普查器的两处自伤 —— 参考键形态不符 + 分母漏桶,把 61/61 全判成静默丢弃(V13 自踩)

**现象**:V13 自建接口表子句普查器首跑报 **61 子句全部 UNACCOUNTED**,连已核实采到的
`接口清单.md:46`「最大 5MB」都判丢;篡改判别力测试也失效(删一条登记,UNACCOUNTED 仍 61)。

**逐条追下去,两处全是普查器自己的 bug,采集器 0 缺陷**:
1. **参考键形态**:产物 `source.reference` 是 `接口清单.md:46`(带扩展名),我用 `doc.stem`(=`接口清单`)建索引 → 键恒不命中。改 `doc.name` 后 61 → **0 UNACCOUNTED**。
2. **分母漏桶**:产物有**三个**处置桶 `oracles` / `untranscribed` / `explicitly_undefined`,我只索引前两个 →
   `业务校验对照.md:62-65` 4 条(在第三桶,refuted)被误判丢。补第三桶后散文行普查 3 → **1 UNACCOUNTED**。

**自证后才报数(坑 25 纪律)**:① 已知真样本 L46 四子句逐个核 → 2 registered + 2 oracle,0 不符;
② 篡改一条登记 → UNACCOUNTED 0→1,**判别力成立**;③ 最终 61 子句 = 35 registered + 26 oracle,
多命中(13 处)逐条核实为**跨表扇出**(「name 必填且不超过 64 字符」→ categories/package_types/packages/products 各 2 条),不是漏转录。

**根因**:复核器的索引键与分母都必须**从被验产物的实际形态枚举**,不能凭印象。
方向单向(只会制造「被验方有罪」),所以首跑的惊人数字必须先自证再发布。
**通用纪律**:普查器上线前跑两个校准——已知真样本必须 0 不符、篡改必须变红;
两者任一不成立,报出的「N 处不符」一律作废。分母桶数要从产物顶层键枚举,不得只取自己知道的两个。

### 坑 64:检测力指标仍未机械化 —— 坑 44 的防复发守卫没落地(V13 独立枚举实证)

**现象**:frame_check 产物报 `constraint_section_cells=18`(assoc)/`constraint_section_unique_cells=16`/
`constraint_section_buckets=8`/`consistent_buckets_symmetric=8/8`,口径与信息量限定都齐(坑 44 的一半已修),
但**「能 firing 的桶数」没有任何字段**(grep `firing|detect|co_singleton` 在引擎里 0 命中)。

**实测证据(独立按域类型枚举 8 个 constraint_set 桶)**:
`PackageItemRequest.quantity`=`iv`、`BanquetOrder.setStatus`=`set` → **可 firing 2 个**;
其余 6 个(productId / StatusRequest.isActive / BanquetPackage.setIsActive / Category.setIsDeleted /
PackageType.setIsDeleted / Product.setIsActive)全是 `nonnull` 余单例域 → 做法 23 已证**永不致 inconsistent**。
即 **2/8**,与 V10 修前修后的结论一致:**覆盖 8 桶,检测力 2 桶,零增益**。

**后果(面向公司内非专家读者)**:报告头条是「8 consistent」,读者读成「8 处两细胞取值一致已验证」;
实际 6 处数学上不可能不一致,另 2 处也是对称挂载(检的是资源全局约束可满足)。
`consistent_buckets_symmetric` 限定在,但**可 firing 桶数不在**,读者无法自行算出检测力。

**防复发守卫(建议)**:产物按域类型分桶计 `buckets_firing_capable` / `buckets_co_singleton_only`,
CLI 与覆盖数字并排打印;变异「把余单例域桶也算进 firing_capable」必须红。
**通用纪律**(坑 44 重申 + Ashby 用在指标上):报覆盖增益必须同报检测力增益,
且**检测力要按「可证永不 firing」的桶数显式扣除**,不能只给一句散文限定。

### 做法 36:移动靶下验证者的定稿前重放 —— 本轮 5 条发现里 4 条在验证期间被交付方修好(V13 实操)

**证据(每条都带前后快照与 rc,不凭首读定稿)**:

| V13 首测发现 | 首测快照 | 定稿前重放 | 处置 |
|---|---|---|---|
| 坑 46 空 members 容器 rc=0 出数 unknown=365 | `arch_rules e2e3d56b` | `53e08a73`/`fc0f01c4`:**rc=1、不写产物**,「容器为空」与「有 N 条但全不合格」分别给因 | **撤回**,记为已修 |
| 坑 47 源码根不存在零告警、prereq[4]「并告警」是假话 | `e2e3d56b`/`53e08a73` | `fc0f01c4`:**2 条告警 + `channel_status=SOURCE_ROOT_MISSING`** | **撤回**,记为已修 |
| `API.md:307` address 空字符串散文静默丢弃 | `oracles 761cbcd8` | `7a9c5cfd`:该候选改 **refuted**、`source.kind=document`、reference 指 API.md:307 逐字原文 | **撤回**,记为已修 |
| `doc:AGENTS:57.default` = `{field:Java, value:使用}` 语义伪影 | `oracles 7a9c5cfd` | `82fa1e91`(341 条):伪影**移除**并进 untranscribed,动词值伪影 0 | **撤回**,记为已修 |
| frame_check M9 不对称截面守卫洞 | `fbdcf2da`(全程未变) | 重放仍存活 | **保留**,附可达性前提 |

**同时如实报「不是缺陷的观测」**:14 条 DDL CHECK 判据 `target={table}` 无 `field`、操作数在 `assert.field`,
`_matching_oracles` 只读 `target.field` → 挂不上。独立量化:补上 target.field 后**只有 1 个资源截面变化**
(`BanquetOrder.setPlanTotalCent`,nonnull→[0,∞)),且它是**单细胞桶**(不进环一致性);
`chk_orders_status` 挂上后截面不变(枚举已由 openapi 提供)。**当前检测力后果 0**,故只记为
「无计数器的静默不可挂载通道」(坑 17 第三形态同族,capability_map 已修读 `left`,frame_check 未修),不夸大成回归。

**下游需重裁定(不是 W4 缺陷)**:`ddl:orders.address.not_blank_candidate` 现被 API.md:307 原文判 **refuted**
(文档明写「为空时后端保存为空字符串」是刻意行为)。坑 51 / W10「三态铁律实例」把它记为
「真缺陷已观测,只等业务确认非空白意图」——**该前提已被上游原文否定**,台账根因
`db.notnull_bypassed_by_empty_string` 需拥有者重裁定。另:id 仍带 `ddl:` 前缀而 `source.kind` 已是 `document`,命名与来源不一致(低)。

**通用纪律**:验证者定稿前必须把**每条**发现按当前快照重放一遍,逐条标「仍在 / 已修 / 我的工具错」;
首测发现不是永恒事实(坑 4),但也不是可以照抄的旧结论(坑 58)。
**撤回要写明在哪个快照上曾成立**,否则下游无法判断是验证者错了还是交付方修了。

### 坑 66:覆盖≠检测力——「N consistent」头条必须并排报「可 firing 桶数」,否则非专家读成已验证(W2 修 V13)

**现象**:产物报覆盖 8 桶 / symmetric 8/8,但无「能 firing 的桶数」字段。V13 独立按域类型枚举:
quantity=iv、setStatus=set → 可 firing **2**;其余 6 桶全是 nonnull 余单例域 → **数学上永不 inconsistent**。
即覆盖 8、检测力 2、零增益。头条「8 consistent」会被公司内非专家读成「8 处两细胞取值一致已验证」,
实际 6 处不可能不一致——与坑 3(聚合掩盖分项)、V9 99.1% 假合规同族,违反北极星「直接暴露」诚实性。

**修法(已落地 frame_check.py)**:① 环报告加 `domain_type`(截面域类型 iv/set/nonnull/notblank);
② run 计 `buckets_firing_capable`(iv/set,可不相交)与 `buckets_co_singleton_only`(nonnull/notblank,无封闭原语
强制 null/""→永不 inconsistent);③ CLI 与覆盖数字**并排打印**「覆盖 8 桶 ≠ 检测力 2 桶」。
**实测**:firing_capable **2** / co_singleton_only **6**(与 V13 独立枚举一致)。
**自己踩的 bug**:`domain_type=have[0][0]` 错——`have` 是**细胞名列表**不是截面,取的是细胞名首字符 'c';
应为 `sections[have[0]][0]`。守卫 test_firing_capable_iv_and_set 等抓住。
**变异**:M_firing(余单例域误算 firing)→ 2/2 红。**通用纪律**:报覆盖增益必须同报检测力增益,
按「可证永不 firing」的桶数显式扣除,不能只给散文限定。

### M9 处置(坑61 保留项):结构上不可达,但防御守卫已机械化

**可达性核实**:`_cell_section` 函数体**从不读 fp**(inspect.getsource 正则核实)→ 截面只由资源决定 →
同桶细胞截面恒等 → `have∈{0,len(cells)}`,`0<have<len(cells)` 结构上不可达 → M9(`==len(cells)`→`>=1`)当前无牙。
**但下一步 DTO 投影会让截面变细胞相关,M9 当场升级真洞**。已补:① `test_m9_partial_sections_is_unknown`
monkeypatch `_cell_section` 造 A 有/B 无 → 断言 status=unknown/basis=binary;临时改源码 `==`→`>=` 验证**变红**,恢复。
② `test_cell_section_is_cell_independent_structural`:一旦 `_cell_section` 开始读 fp 即红,提醒不对称守卫须生效。
**纪律**:判等价变异必须写明依赖哪条可达性前提,并把前提本身机械化(坑61)。

### DDL CHECK 无 field 缺口(低优先,显式报不静默)

14 条 DDL CHECK 判据 target={table} 无 field,`_matching_oracles` 字段分支读 target.field → 挂不上字段资源。
V13 量化:补上后仅 1 个单细胞桶截面变化(不进环一致性)→ **当前检测力后果 0**。
已加计数器 `coverage_gaps.ddl_table_only_no_field_oracles=14` + note,记为「无计数器的静默不可挂载通道」,
**不夸大成回归**。守卫 test_ddl_table_only_no_field_counter。

### 坑 67:分类计数必须守恒 + 「字段都是 0」先核快照再核逻辑(W2,编排者报 0+0)

**现象**:编排者实测 `buckets_firing_capable=0 / buckets_co_singleton_only=0` 而约束桶 5 个,疑分类没接上。
**核实**:同数据(assoc 12/unique 10/buckets 5)下,W2 当前源码(:590 `sections[have[0]][0]`)分类 **1+4=5 正确**;
编排者的 0+0 是**旧快照 `have[0][0]` bug**(domain_type 取细胞名首字符 'c',落不进任何域类)的症状(坑66 已修)。
**纪律**:报「新字段都是 0」先 `shasum` 核对方跑的快照是否含修复(坑4/58),再核逻辑;不得照旧快照改已修好的代码。

**守恒守卫(已机械化,坑3 同族)**:加 `buckets_other_domain`(域类型不在 iv/set/nonnull/notblank 的显式第三类),
产物与 CLI 并排报 `firing + co_singleton + other = 约束桶总数`。守卫 `test_detection_power_conservation`
(断言守恒且 firing=2/co=1/other=0)、`test_domain_type_is_valid_kind_not_cell_char`(domain_type 必须是合法域类型,
非细胞名首字符)。变异:退回 `have[0][0]` → **3 守卫红**(domain_type 非法/守恒分项/co_singleton)。
**通用纪律**:分项计数之和必须等于总数,余类显式报(不得静默丢桶);分类键取自正确的数据结构(截面 tuple 非细胞名)。

### 坑 68:验证移动靶——CLI 输出与所读源码格式不符是并发编辑警报,先重冻结再下结论(V14 实测)

**现象**:V14 开工冻结 `frame_check 4853825f`,Read 全文后跑 CLI,输出打印格式(「/ other 0 —— 守恒」)
与所读源码 line 868(「覆盖 X 桶 ≠ 检测力 Y 桶」)**逐字不符**,数字也对不上(任务引 5桶/1/4,实测 8桶/2/6)。
第一反应若是「源码有 bug」就错了——`shasum` 重核发现文件已漂移 `4853825f→243d88a8→c420d7e9`(mtime 晚于 Read),
**交付方 W2 正在并发编辑**。输出与所读源码不符 = 移动靶警报,不是缺陷证据。
**处置**:冻结当前版到 /tmp 作稳定参考(`c420d7e9`/test `1e1b2c63`),复验磁盘 sha 不再变,再逐项验;报数带所验 sha。
任务引用的「5桶/firing1/co4」是坑67 旧数据快照(assoc12/unique10/buckets5),当前数据 8桶/2/6,
**分类逻辑数据无关,两快照皆正确**——不得拿旧快照数字判当前代码错(坑4/58/67 同族)。
**纪律**:验证者 Read 与跑 CLI 之间文件可能被改;凡「输出≠所读源码」先重冻结再下结论;
定稿前按当前快照重放每条发现(做法36),移动靶下首读结论不是永恒事实。

**闭合 by construction 的反例核法**:守恒 `firing+co+other=约束桶` 不靠运行时断言,而是
`other = complement(firing∪co)`(frame_check:683)→ 任何域类型必落且仅落一类,**构造上闭合**。
核「不得静默丢桶」必须注入**域外值**(novel 域类型 / None),不能只注入域内值:
V14 monkeypatch cycle_consistency 追加 custom_novel/None/interval_v2 三桶 → 全落 other=3,守恒 1+0+3=4,零丢桶。
表桶反例:table 资源 `_cell_section` 返 None → basis=binary,**正确排除**在约束桶外(不是丢桶)。

**残留(坑61 同族,可达性前提扩张)**:`domain_type=sections[have[0]][0]`(:590)依赖「截面细胞无关」
(`_cell_section` 不读 fp,inspect 核 0 命中;真实 14 桶 0 中间态 have∈{0,len})。下一步 DTO 投影读 fp 时,
结构守卫 `test_cell_section_is_cell_independent_structural` 会红示警,但**混合域桶的 domain_type 代表性无独立守卫**
(have[0] 不再代表全桶)。fp 开始被读时须补「同桶细胞域类型一致」守卫。当前 fp 未读,不触发。

### 做法 37:变异 harness 必须报「哪个指名守卫红」,不能只 rc≠0(V14)

**现象**:V14 首版 harness 用 `pytest -rN` 抑制了 FAILED 行 → 8 变异全报「KILLED」(凭 rc≠0)
但 `reds=0`、**说不出哪个守卫咬住**。这分不清「指名守卫红」与「某个无关测试坏了」,
等于没验「守卫是否被改弱」(坑25 自证纪律的变异版)。
**修法**:改 `-rf --tb=no` 捕获 FAILED 测试名,逐个核对**指名守卫**在红名单里:
M9→test_m9_partial_sections_is_unknown、结构守卫→test_cell_section_is_cell_independent_structural、
M_firing→test_co_singleton_only_not_firing_capable、跨实体→test_ddl_maps_to_correct_entity_via_table、
三态两方向→test_definite_write_not_downgraded_by_may_write / test_unknown_may_write_not_definite_violation。
8/8 KILLED 且指名守卫逐个命中,才证「真修好非改弱」。
**harness 自证**:空变异(原样)必须 0 红(基线绿)、每个变异锚点逐字存在且改后文件≠原文件(防空变异)、恢复后复绿。
**载体匹配(坑62/做法36)**:frame_check 守卫均 in-process(import+monkeypatch/直调),文件级沙箱变异生效;
沙箱保留 pilot/engine+pilot/tests 层级,基线先绿(101)再变异;subprocess 守卫则须文件级、同进程变异无效。
**通用纪律**:报「N KILLED」必须附每个的指名守卫红名;rc≠0 不等于守卫有牙,可能是无关测试 collateral。

### 坑 69:产物记的出处依赖 cwd —— 判据本身全对,但消费者解析不了(主控实测,V15 复验)

**实测**:独立复核 341 条采集判据的 reference 能否回原文,得到 **281/341 可解析,60 条失败**。
逐层隔离因子后结论是:
- 60 条 OpenAPI 判据的 **JSON pointer 全部正确**(拿真快照 `pilot/openapi.json` 核,60/60 命中);
- 失败的唯一原因是 `inputs.openapi` 原样记录了调用者传入的裸文件名 `"openapi.json"`,**丢了目录**。

`oracle_harvest.py` 的四类输入都是 `str(p)` 原样转录。其余三类(migrations/java_roots/docs)
**只是恰好**被传了绝对路径才没暴露。**「恰好是绝对的」不是不变式。**

**修法**:四处统一 `.resolve()`。不变式是「**产物自足**:消费者不知道采集时的 cwd,
也能解析每一条判据的出处」。守卫 `test_provenance_is_cwd_independent_and_self_sufficient`
在 A 目录用相对路径采集、换到 B 目录当消费者,逐条回原文;
文件级变异(把 openapi 那处退回 `str(p)`)**已实测判红**。

**教训**:声明输入时用绝对路径,别转录调用者的字符串。校验「出处可核实」要核到
**消费者视角**(换 cwd、只拿产物),不是采集者视角。

### 做法 39:校验器报错先怀疑自己的 scope 与键形态,别急着报数(主控本轮连踩三次)

同一个复核任务里我的校验器错了三次,每次都是**先怀疑被测系统就会误报**:
1. 把 reference 当**仓库根相对路径** → 320/341 指控「文件不存在」。实际是 **basename:line**。
2. 把 **JSON pointer**(`openapi.json:#/components/...`)当行号解析 → 又一批假指控。
3. 搜索范围只覆盖被测仓库 `jiuyan`,而快照在**审计仓库** `pilot/openapi.json` → 60 条假失败。

三次都是坑 25(先自证参考实现)的变体,但**具体形态是「scope 与键形态」**:
- 键形态:行号 / JSON pointer / basename / 全路径,是**不同种类**,必须分类处理再计数;
- scope:产物自己声明了 `inputs`,**用它**,别自己猜搜索根。

**先分类,再计数;先自证校验器,再报被测系统的数。** 与坑 21(归因纪律)同源。

### 做法 38:`gate_eligible ≡ false` 是诚实,不是缺陷 —— 别把设计当洞报(主控)

复核时看到 341 条判据 `gate_eligible` **全为 false**,第一反应像「闸门没接上」。
回原文核实:`oracle_harvest.py:11` 与 `:1007` 明写
「产出 confirmation=confirmed 但 gate_eligible=false —— 采集到的是**声明**」
「判据权威性与观测有效性尚未独立验证」。

这是**有意的三态纪律**:采集到声明 ≠ 有资格开缺陷单。**报之前先读引擎自己声明的语义**,
否则会把正确的设计报成缺陷(反向的假阳性,一样损害信任)。

### 坑 70:共享经验册的序号是竞态资源 —— 编号已被撞车多次,别拿它当身份键(主控实测)

**实测普查**(不是推理):本册里 **坑 64 出现 2 次**(W4 / V13)、**坑 65 出现 3 次**
(W3 / W4 / 主控初稿)、**做法 36 出现 2 次**(W3 / V13)、**做法 37 出现 2 次**(V14 / 主控初稿)。

成因:多个 agent 并发「读尾部 → 取 max+1 → 追加」,**读和写之间没有原子性**。
V14 与主控本轮就撞了:V14 读到 2601 行、追加到 2639;主控随后追加到 2680,
两边都自认为拿了「下一个」编号。**追加本身没丢内容(append-only 保住了),丢的是编号唯一性。**

**规约(此后照此办理)**:
1. **编号只是显示标签,不是身份键。** 引用一条经验时用「标题原文 + 作者」,不要用「坑 NN」——
   会指到多条。
2. 新条目标题**必须带作者标记**(如 `(W3 实测)`、`(V14)`、`(主控)`),这是天然去重键。
3. 追加前 `grep -n "^### 坑 \|^### 做法 "` 取真实 max,**并且**接受仍可能撞车;
   撞了就改**自己刚加的那条**(自己的条目可以改,别人的永不动)。
4. 统一用 `###` 三级标题(历史上混用了 `##`,导致按 `^## ` 普查会漏掉大半条目 ——
   主控本轮第一次普查就因此误判「只有我的条目」)。**普查共享册要先核标题形态,再计数**(做法 39 同源)。

**为什么不重编号历史条目**:append-only 是并发安全的基石,为了美观去改写别人的条目,
会制造比编号重复严重得多的问题(他人引用失效、并发覆盖)。**宁可标签重复,不可内容丢失。**

### 坑 71:「产物自足」是有算法歧义的不变式 —— 不声明消费者算法,分子分母都是任意的(V15 实测)

复验主控的 `oracle_harvest.py` 四处 `str(p)` → `str(p.resolve())`(承坑 69「产物记的出处依赖 cwd」)。
**缺陷真存在、修法方向对、但声称的不变式没达成,守卫粒度也只有声称面的一半。**

**1. 分母必须先声明消费者算法。** 同一份冻结产物(341 条),两种都「只用产物、不知 cwd」的算法:
- **A 严格**:只在 `inputs` 字面列出的路径里按 basename 找 → 修前 **215/341**,修后 **275/341**;
- **B  rooted**:允许在 `inputs` 列出的**目录根**下递归找 → 修前 **281/341**,修后 **341/341**。

主控报的 281 是 **B**,可复现;但**守卫自己实现的是 A**(`test_oracle_harvest.py:738`
`match = [p for p in recorded if p.name == name]`,不 rglob)。
把守卫的算法原样搬到真实产物上,**修后仍红 66 条**,全是 annotation 类:
`inputs.java_roots` 记的是**目录** `/…/src/main/java`,而 migrations/docs 记的是**展开后的文件**。
`AddedItemRequest.java:7` 在 A 下无处可找。**「自足」在守卫自己的读法下仍不成立。**
最小修法已实测:`:983` 的 `args.java_root` → `java_files`(与 ddl_files/doc_files 同待遇),
A 立刻 **275→341/341**。夹具没供给 java 输入,所以守卫看不见这个洞。

**2. 守卫粒度要匹配声称的修复面(承做法 35)。** 声称「四处统一」,夹具只给 `--docs`/`--openapi`,
`migrations`/`java_roots` 恒为 `[]` → 文件级变异矩阵 **2/4 存活**:
M1 migrations、M2 java_roots **SURVIVED**(44 passed,0 红);M3 docs、M4 openapi、M5 四处全退 **KILLED**,
指名守卫逐个命中。空变异对照 0 红、恢复后复绿。
**存活必须再分一步**(坑 61):把夹具补齐四类输入(全用相对路径)后重跑,**5/5 全杀**
→ 证明是**夹具覆盖缺口**,不是等价变异。只报「2 存活」不定性,等于把可修的洞说成不可修。

**3. `.resolve()` 不是无损的:它把「依赖 cwd」换成「依赖物理路径」。** 实测(合成夹具,非推理):
- **符号链接下 basename 会撕裂**:`reference` 取 `path.name`(`:341`,调用者名 `link.json`),
  `inputs` 取 resolve 后的目标名 `spec.json` → **守卫自己的 basename 路由断了**。
  docs 同理(`doc.md:3` vs 记 `prd.md`)。**修前 basename 是一致的,修后不一致** —— 这是新引入的。
- **稳定名被钉死到版本**:`current.json -> releases/v1/spec.json`,记的是 v1 物理路径;
  v1 退役后 recorded 路径悬空,而调用者用的稳定名仍有效。
- **绝对路径也会被改写**:macOS 下 `/tmp/...` → `/private/tmp/...`,`samefile=True` 但字符串不等
  → 任何按字符串比对 inputs 的下游(如 `test_capability_map.py:559` 那种 `== str(p)` 守卫)会假红。
- **以下三条我假设过、实测被否,不许当缺陷报**:含 `..` 穿符号链接目录时
  `resolve() == os.path.realpath`(**正确**,反倒是 lexical `abspath` 会指错文件);
  不存在的路径 non-strict 不抛;符号链接环不抛、harvest rc=0。
- **只改 inputs 块已实测**:四类输入齐全的语料上,orig vs 五个变异体逐 top-level key 比对,
  **差异恒为 `['inputs']`,且各自只动自己那个子键**;counts/oracles/explicitly_undefined/untranscribed 全等。

**4. 更对的修法:房规已经在仓库里,别自己发明。** `arch_rules.py:1095`、
`observation_adapter.py:382/392/424` 记的都是 **`{"path": …, "sha256": …}`**,
`test_arch_rules.py:1148` 还守卫「每个 input 必须有 64 位 sha256」。
`oracle_harvest` 是唯一只记裸字符串、无内容哈希的。公司级多人服务里产物要在**另一台机器**被消费,
绝对路径跨机即失效 —— `.resolve()` 只做到「同机 cwd 无关」,做不到「跨机自足」。
建议:`inputs` 升级为 `{path, sha256}` + 声明 `repo_root` + **根相对路径**(绝对路径留作便利字段),
sha256 才是「回原文」的真正凭据(路径会漂,内容哈希不会)。

**5. 修生成器 ≠ 修已落盘产物,且要说清谁是消费者。** 冻结的 `pilot/oracles_harvested.json`
仍带 `"openapi": "openapi.json"`(本轮不许重生成),所以**今天存在的产物里那 60 条出处仍是断的**。
同时实测:`oracle_bridge.py:254` 只把 `reference` 当 **scope 字符串兜底**,从不 open 文件;
全仓没有别的机器消费者读 `inputs.openapi`。故严重度是**可追溯性/审计**,不是「流水线断了」——
报缺陷时把消费者是谁一并写清,别让读者自行升级严重度。

**教训**:声称一个不变式时,**同时声明判定它的算法**;否则「N/M 可解析」的分子分母都随算法漂,
而守卫可能恰好实现的是更严的那个读法、又恰好被夹具避开了失败类。

---

## 二十四、W15 补充(2026-11-15,台账高置信假阳性重裁定:第三扇开单门)

### 坑 72:硬约束 5 的 oracle 闸门有第三扇开单门没装 —— field_notes 从不问 oracle(W15 实测)

**现象**:闸门已装两扇(`class_contract_diff.rule_oracle_buckets` 规则三层全缺 /
`class_rules.oracle_gate` 规则派发层),但**差分 A 的 `field_notes` 路径**
(`class_contract_diff.py:161-176`,修前)只要「DB NOT NULL + DTO 无 NotNull/NotBlank」
就开单,从不消费 oracle。

**实测证据**:台账 `CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING`
(MEDIUM / confidence 0.92 / ground_truth=verified / kind=defect)断言
「orders.address NOT NULL 被空串绕过」,而 `docs/API.md:307` 明写有意设计。
采集侧早已把对应候选判 refuted(`oracles_harvested.json` 的
`ddl:orders.address.not_blank_candidate`,reference=API.md:307),**台账侧却照开单**
—— 同一份文档在两个子系统里得出相反结论。

**修前/修后实测差值**(冻结同一批输入,/tmp 双沙箱跑纯判定层
class_diff→normalize→aggregate):台账 **95→94**、defect **78→77**(MEDIUM 44→43)、
根因 **70→69**、class_findings **22→21**、refuted 旁路 **3→4**;
positive 15 / observation 2 / collapsed 25 / 硬违规 1 **全不变**;
按 `(layer,id,title)` 比对:消失 1 / 新增 0 / 其余 94 条里内容变化 **1**
(`CLASS-NULLABILITY-ALIGNED` 的 detail —— 连带的诚实性修复,见做法 40)。

**根因**:与「三态闸门有多个物理站点,守卫只锁代表站点」(V11 坑 49)同族,
但发生在**开单门**上:一条硬约束有 N 扇物理门,只装被点名的那两扇,第三扇继续编造预期。

**防复发守卫(已机械化)**:`pilot/tests/test_nullability_oracle_gate.py` **29 条**。
装闸门时先 grep 全部 `_finding(` 调用点枚举开单门,逐扇核有没有 oracle 消费;
**守卫按门写,不按概念写**。

**残留(未修,交拥有者裁定)**:该门默认口径仍是「没声明 oracle 就开单」(与派发层一致),
所以**删掉 oracle 声明缺陷就回来**。收紧成差分 B 的「缺 oracle 一律 unconfirmed」
会同时压掉另外两条未裁定的 note,不得静默改。其中
`CLASS-REQUIRED-CHECKED-IN-SERVICE-LAYER`(finalPriceCent「DTO 少一条 @NotNull」)
**疑是同族假阳性**:`openapi.json` 两个请求体 required 均不含 finalPriceCent、
`API.md:337-338` 明写 useFinalPrice=false 时后端补值、`接口清单.md:42` 必填清单也没有它;
但它同时捆绑了一个真客观事实(报错文案「小于 0」vs 真因「不能为空」,
已另有 `CLASS-ERROR-MESSAGE-MISMATCH` LOW 开单),需拆开裁定,不能整条压掉。

### 坑 73:「技术约束被满足」写成「约束被绕过」—— invent 一条比所有来源都更严的规则,还编造了业务消费者(W15 实测)

**现象**:断言「NOT NULL 被空串绕过」。空串不是 NULL,`NOT NULL` 与
`@Column(nullable=false)` **都被满足**,约束没被绕过。真正被违反的是
「address 必须非空白」,而这条规则**没有任何来源声明**,且存在一条 confirmed 的
**可执行** oracle 断言相反预期。

**机器枚举的九个载体(全部同向,穷尽性不靠手抄)**:

| 载体 | file:line | 原文/事实 |
|---|---|---|
| PRD | `/Users/june/jiuyan/docs/API.md:307` | 「`address` 可为空;为空时后端保存为空字符串,避免前端非必填地址导致订单生成失败」 |
| 接口清单 | `/Users/june/tests/auto-api/docs/接口清单.md:42` | 「address 为空保存空字符串」(由 `scripts/generate_api_docs.py:31` 生成) |
| **可执行验收用例** | `/Users/june/tests/auto-api/tests/test_orders.py:19-21` | `test_create_order_empty_address`:`make_order(address=None)` → `check_eq(api,"address 为空时保存为空字符串", order["address"], "")` |
| DDL | `V1__init_schema.sql:57` | `address VARCHAR(255) NOT NULL`;orders 的 CHECK 只有 table_count/plan_total_cent/final_price_cent/deposit_cent/status |
| 实体 | `BanquetOrder.java:42-43` | `@Column(nullable = false)` |
| DTO | `CreateOrderRequest.java:16` / `UpdateOrderRequest.java:16` | `String address,` 无注解,而四个同级字段都有 `@NotBlank`(刻意对比,不是漏写) |
| 实现 | `OrderService.java:135` | `order.setAddress(StringUtils.hasText(address) ? address.trim() : "");` |
| OpenAPI | `pilot/openapi.json` | 两个请求体 required 均不含 address,address schema 只有 `type:string` |
| 前端 | `src/App.jsx:2411` | `selectedOrder.address \|\| "无"` —— 显式把空地址渲染成「无」 |

**附加编造**:后果句「宴会执行单拿不到地址」——`grep -rl 执行单` 全仓
(排除 node_modules/.git/target)**0 命中**。业务消费者是凭空造的。

**与既有经验的关系(必须写清,否则下一个人继续引用错前提)**:
「技术约束抓不到这类缺陷,必须有 PRD 业务意图」(W4 坑 51)这句**在本例上被推翻**:
PRD 业务意图存在,且它说的是相反的话。W10「三态铁律实例」把它记成
「真缺陷已观测,只等业务确认非空白意图」是错的;V13 做法 36 已指出前提被上游原文否定
并要求台账拥有者重裁定 —— 本条完成台账侧的重裁定。

**防复发守卫(已机械化)**:oracle 声明带结构化 `corroborated_by`(每条 file:line + 原文),
守卫 `test_every_cited_reference_resolves_to_the_original_text` 逐条解析行号读原文,
断言引用文本**在该行**(不是全文件子串搜索),7 条引用全过、0 skip。

**通用纪律**:报「约束被绕过」前必须枚举该约束的**全部**来源
(DDL/注解/OpenAPI/文档/验收用例/前端),确认存在一条声明了更严预期的来源;
找不到就是 invent。后果句里的业务消费者必须能在仓库里 grep 到。
**文档否定的预期不是「待确认」,是 refuted**(承 W4 坑 64)。

### 坑 74:uid 含层内序号 —— 删一条会让后续 uid 整体平移,按 uid 做差集把 1 条删除报成 10 消失 + 9 新增 + 1 内容变化(W15 自踩)

**实测**:第一版复核脚本按 `uid` 比对修前/修后台账,报「消失 10 / 新增 9 /
共有 uid 内容变化 1」,看起来像大面积回归。逐条追下去全是**序号平移**:
`uid = class:<id>#<层内 index>`,删掉 index 11 后原 #12..#21 全变成 #11..#20。
改按 `(layer,id,title)` 比对 + 剔除 `uid`/`source.index` 后,
真实差值是「消失 1 / 新增 0 / 内容变化 0」。

**根因**:与「复核判据必须按被验对象的真实形态写」(W10 对坑 25 的补充)同族 ——
这次是**身份键里混进了位置信息**。

**防复发**:比对派生产物前先确认 key 的构成;含序号的 key 只能用于定位,不能用于差集。
**另需告知下游**:`units.json` / `agent_coverage.json` 里引用
`class:CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING#11` 的地方,重生成后序号会平移,
必须一起重生成,不得只重生成台账。

### 做法 40:撤销裁定落在「声明」上,并用双载体变异证明它不是按 id 拉黑(W15)

**修在哪**:① 机制 —— `class_contract_diff.diff_nullability` 复用
`class_rules.oracle_gate`(不新造闸门),被压下来的结论经 `run()` 汇进既有
`rule_refuted`/`rule_suspects` 旁路;② 裁定 —— `class_scenarios.json` 的
`field_notes[0].oracle` 声明 `confirmation=refuted` + reference + 原文 +
结构化 corroborated_by;③ 文案 —— title/detail 撤掉缺陷断言与编造后果,
severity MEDIUM→INFO(它现在是观测)。**`finding_meta.py` 一字未改**:
先例是 `NO_STATE_MACHINE` 被压后 meta 条目保留(台账 0 命中、meta 仍在),
照先例办,不发明新约定。

**④ 连带的诚实性修复(自己的改动自己收口)**:压住一列后,通过结论
`CLASS-NULLABILITY-ALIGNED` 的原文案「不成立的只有下面单列出来的那几列」**变成假话**
(4 列不成立、只列 3 列,第 4 列去哪了读者无从知道)。改成按 `held` 条件出文案
「不成立的 4 列里,1 列的业务预期未确认或被文档否定,只保留观测、不开单」,
并在 evidence 加结构化 `expectation_held` 计数(不让复核去解析字符串)。
**没被压住时文案与计数一字不变**(阳性对照守卫锁住)。
这是台账里唯一一条内容变化的共有条目 —— 差值必须如实报成 1,不是 0。

**证明不是拉黑(两条独立变异)**:同进程 `test_gate_is_not_an_id_blacklist`
—— 同一条 note 的 oracle 改成 confirmed 必须**重新开单**;
文件级 M6 在沙箱里把声明的 refuted 改成 confirmed → CLI 守卫**红**。

**双载体变异矩阵(7/7 KILLED,0 存活;基线先绿、恢复后复绿)**:

| 变异 | 载体 | 变红的常设测试 | 被咬住的守卫断言 |
|---|---|---|---|
| M1 拆掉闸门 | 同进程 monkeypatch | `test_mutation_gate_removed_is_caught` | `_guard_refuted_not_filed`(= `test_refuted_note_is_not_filed`) |
| M2 refuted 洗成 confirmed | 同进程 | `test_mutation_refuted_washed_to_confirmed_is_caught` | 同上 |
| M3 压下来但不留痕 | 同进程 | `test_mutation_bypass_dropped_is_caught` | `_guard_run_keeps_bypass` |
| M4 闸门过度扩大(无 oracle 也压) | 同进程 | `test_mutation_gate_over_reaches_is_caught` | `_guard_no_oracle_still_filed` |
| M5 源码删闸门 | **文件级沙箱 + subprocess CLI** | `test_file_level_mutation_reopens_the_defect` | `_assert_cli_retracts`(= `test_cli_retracts_the_defect_and_keeps_the_observation`) |
| M6 声明 refuted→confirmed | **文件级沙箱 + subprocess CLI** | `test_file_level_declaration_flip_reopens_the_defect` | 同上 |
| M7 通过结论藏起被压住的列(`gated_n=0`) | **文件级沙箱 + subprocess CLI** | `test_file_level_mutation_hiding_the_held_column_is_caught` | 同上(新增的 aligned 文案/计数断言) |

M5/M6/M7 必须文件级:CLI 守卫 spawn 的子进程读磁盘原文,同进程 monkeypatch 杀不死它
(承 W3 做法 36 / V13 坑 62)。沙箱只需拷 5 个模块
(class_diff/class_contract_diff/class_rules/project/file_carrier,全标准库依赖)
+ 最小 target.json + 合成观测,**不依赖任何 gitignore 的流水线产物**,fresh clone 可跑。
变异锚点逐字断言存在,未命中显式失败不计入 KILLED。

### 做法 41:并发验证者在飞时,判定层重算放双沙箱,不覆盖共享产物(W15)

**处境**:V14 正在独立验 `frame_check.py`(消费 `all_findings.json`),
任务书禁止跑流水线/探针。直接重生成台账会让 V14 追移动靶。

**做法**:`/tmp` 建 before/after 两份 pilot 拷贝,**其余输入 sha 逐字相同**
(class_declaration `60b2750d` / class_observation `28c22552` / class_contract `577e623d`
/ finding_meta `f60d11cd` / layers.yaml `5f2aeef0`),只把我改的三个文件在 before 侧
换成 `git show HEAD:` 版本(开工快照确认这三个文件当时未被他人修改)。
两侧各跑纯判定层 `class_diff.py`→`engine/normalize_findings.py`→`engine/aggregate_findings.py`
(三者都只读 JSON + 写产物,无 subprocess/DB)。

**保真度先自证**:before 沙箱产出的 95 条 findings 与盘上台账**逐条内容相同**
(剔除 uid/index/绝对路径后 `sorted(json.dumps(...))` 全等)—— 沙箱可信,
差值才可归因于我的改动。**注意 sha 不可跨沙箱比**:`all_findings.json` 顶层记
`contract`/`target` 绝对路径,沙箱路径不同必然 sha 不同,要比 findings 数组。

**红线实测**:交付时共享产物五个 sha 与开工指纹**逐字未变**
(all_findings `ffb072e8` / root_causes `43fb9d54` / class_findings `5a63c803`
/ class_rule_suspects `5ddafa58` / finding_meta `f60d11cd`);
`frame_check.py` 与其测试未触碰;被测系统 HEAD `df3c341b`、git status 14 项未变。

**代价必须显式说清**:盘上台账**今天仍带着这条假阳性**,直到判定层被重跑
(承 V15 坑 71 第 5 点「修生成器 ≠ 修已落盘产物」)。
重生成命令与连带产物见交付报告,由编排者在 V14 收工后调度。

**交付时的全量归因(坑 5 纪律,不背不属于自己的锅)**:15:47 全量 **1070 passed + 5 subtests / 0 failed**;
15:49 重测 **1070 passed / 1 failed**,唯一红是
`test_audit_entry.py:622 TestMutationGuards::test_mutation_unknown_overlap_as_definite_red_is_caught`
(「守卫没咬住『确定违反』头条」)。归因证据三条:① 该测试文件 mtime **15:49:11**、
`engine/audit_entry.py` mtime **15:47:16**,都晚于我那次全绿;
② `grep class_contract_diff|class_diff|class_scenarios|class_rules engine/audit_entry.py` **0 命中**,
无因果路径;③ 我的 29 条守卫单独重测全绿。**结论:并发 owner 的在飞新守卫,不是我的回归**,
按做法 7 诊断但不越权修。我的贡献用隔离法测准:`--ignore` 我的文件后 1041 passed,
加我的文件 1070 passed → **我 +29**,其余增量属并发 agent。

### 坑 75:零配置入口的「发现」静默丢仓库外输入 —— 341→177 的差是输入差,不是能力差(W16 实测)

**实测**:冻结采集 `pilot/oracles_harvested.json` 的 `inputs` 段自己声明了 6 份 docs,
其中 **4 份是仓库外 PRD**(`/Users/june/tests/auto-api/docs/*.md`),OpenAPI 也是显式供给。
而 `audit_entry.discover` 只扫仓库根 `*.md` + `docs/`(`audit_entry.py:65-67`),
OpenAPI 只认仓库内 `openapi*/swagger*` 文件名(`audit_entry.py:63`)。
`find /Users/june/jiuyan -maxdepth 3 -iname 'openapi*'` 实测为空 ——
OpenAPI 快照在审计仓库 `pilot/openapi.json`,不在被测仓库。
于是零配置跑 118/177,冻结采集 341(272 confirmed):**差值来自输入发现,不是引擎能力**。

**教训**:① 比较两次跑之前先 diff 产物自己声明的 `inputs`(做法 39 同源:用它,别猜)。
② 「未发现」要归因到系统/工具/上游三桶,别把上游供给缺失误报成引擎缺陷。
③ 入口的 limitations 已诚实写明「PRD 若在仓库外需 --docs 供给」(`audit_entry.py:412`),
读报告先读 limitations,再下「跑不起来」的结论。

### 做法 42:宣称「人工输入不可约」之前,先拆 A/B/C 并逐项给 file:line —— C 类常比直觉小一个量级(W16)

**实测**(四支柱零配置接入界定任务):
- frame_check 对 units 只消费 `unit_key`(`frame_check.py:90-91`);arch_rules 的角色来自
  包后缀 fnmatch + 注解机制事实(`arch_rules.py:100,153-174`),优先级 mechanism_over_name_pattern
  (`arch_rules.py:812`)—— **层归属与角色不是人工输入**。
- `oracle_bridge.to_invariants` 已实现(`oracle_bridge.py:413-421`)、`--out-invariants`
  参数已存在(`oracle_bridge.py:430`),但 `audit_entry.py:192-193` 没调用 —— B 类推导路径已建成一半。
- jiuyan `backend/target/classes` 实测已存在:javap 直读即可,**不需要 maven/javac**(坑 61 同源)。
- C 类残余是**仓库级几条**(1 个 PRD 路径 + ~6 条惯例规则确认 + 0~5 个角色 override),
  不是每类一条;每类一条的是动态层(finding_meta 106 条 / fn_cases 92 条),不在四支柱最小路径上。

**文献对应**:ACDC 全自动 vs ARC 需人工 concern 识别,精度权衡有实测
(Garcia, Ivkovic, Medvidovic, ASE 2013, pp.486–496;经 arXiv 2412.08352v1 ref[67] 核实)。
**自纠**:本轮凭记忆把该文的会议说成 ICSE,核对引文后是 **ASE** —— 报出处前先核(坑 25/39 同源)。

### 坑 76:足迹重叠(旁条件失效)是结构事实,不是缺陷 —— 「0 已证实 + N unknown」判 RED「确定违反」是三态纪律的对称面违规(W17 实测)

**现象**:零配置入口 `audit_entry.py` 的 emergence 支柱把 jiuyan 判成
`[RED] 确定 frame rule 违反候选 9 个`、退出码 1。但同一份 `frame_check.json`:
`candidates_delta0={consistent:0, unknown:9}`、`cycle_obstructions=0`、9 个候选
`business_oracles=[]`、两侧 findings 全空。**零个已证实不一致,9 个一致性未知。**

**逐条核源码(只读 jiuyan,9 条全是正常 CRUD/生命周期共写,无真缺陷)**:
FC-0019 CategoryService#create(:48 置 isDeleted=false)×#delete(:64 置 true);
FC-0020 OrderService#apply(:145)×#updateStatus(:113) 共写 setStatus(文档明写「当前允许任意状态切换」);
FC-0021/22/23 PackageService 共写 setIsActive;FC-0024 PackageTypeService;FC-0025/26/27 ProductService。
CRUD 噪声过滤只排 REPO_OP×REPO_OP 同表对(dropped_crud_noise=18),这 9 条是 SVC_OP×SVC_OP
字段级对,**按设计不是噪声**——服务层共写正是涌现可能藏身处,不滤是对的,错的是入口定性。

**根因(两个声称被混为一谈)**:`frame_violation`+`evidence_strength=definite` 证明的是
frame rule 旁条件 `mod(A)∩fp(B)≠∅` 失效——**局部判定不可孤立组合**这一结构事实(确定写证据);
它**不是**缺陷判定。缺陷相关的问句是「重叠上局部判定是否不一致(δ⁰≠0)」,而 δ⁰ 全 unknown、
环障碍 0。入口把前者(结构事实)写成「确定违反」+RED,等于把 unknown 洗成确定缺陷——
违反硬约束 3「unknown 不得变 pass」的**对称面**:unknown 也不得变「确定缺陷」。
`evidence_strength=definite` 的 definite 指**写证据**确定,不是缺陷确定(措辞陷阱,坑 66 同族:
头条声称超出证据支持的面)。

**修法(只改入口层 `audit_entry.py:343-388`,frame_check.py 一字未动——它刚被 V14 判 PASS)**:
RED 只认唯一可证不一致的机器通道 `cycle_obstructions>0`(约束集不相交);候选 δ⁰ 只有
consistent/unknown 两态,结构上给不出 inconsistent,不得当 RED 依据。头条恒并排报
「已证实不一致 N / 一致性未知 M」;0 已证实+N unknown 一律 INCOMPLETE(退出码 2,没查完),
N 条列为待查线索。实测 jiuyan:`[INCOMPLETE] 已证实不一致 0 / 一致性未知 9`,退出码 **1→2**。

**守卫 + 变异(文件级,/tmp 沙箱,坑 2/做法 37)**:新增 4 守卫(重叠 unknown→INCOMPLETE 非 RED、
环障碍→RED、头条并排且无「确定违反」、0 已证实+unknown→退出码 2)+ 1 in-process 变异守卫。
文件级变异 3/3 KILLED 且指名守卫逐个红:M1 `if proven`→`if proven or overlap`(unknown 洗 RED)
→ state/exit 守卫红;M2 头条退回「确定 frame rule 违反候选」→ 头条守卫红;M3 删 unknown 缺口
(洗 GREEN)→ state/exit 守卫红。空变异 0 红、恢复复绿。

**自踩(坑 2 实例,被变异 harness 揪出)**:首版「0 已证实+unknown→退出码 2」守卫用空仓库
`ae.audit(tmp_path,…,{})`,units/net 缺失 → pillar_emergence 走**缺输入早退**,根本没跑到候选
判定逻辑——守卫恒绿是同义反复。变异 M1/M3 下它不红才暴露。补供给 units/net 后真咬住。
**教训:断言「某判定逻辑正确」的守卫,必须确保夹具真的到达那段逻辑,早退路径上的绿不算数。**

**通用纪律**:报「确定违反/RED」前问一句——证据证实的是**结构事实**还是**缺陷判定**?
两者不同源时,头条必须并排报「已证实 N / 未知 M」,unknown 既不洗绿也不洗红(三态对称面)。

### 坑 77:夹具里放了符号链接 ≠ 守卫锁住了符号链接不变式 —— 断言没比对那个键,退回上一轮缺陷态仍 44/44 全绿(V15 实测)

复验主控第二轮修复(入口一次性 `root.resolve()` / `openapi.resolve()`,下游全用同一批 Path;
`inputs.java_roots` 改记展开后的 `java_files`)。**三处引擎修复全部为真,但第三处的守卫没有牙。**

**1. 引擎侧全部复核通过(独立重算,不照抄)**
- 严格算法 A(basename 只在 `inputs` 字面列出的路径里找,consumer cwd=`/tmp`):
  **341/341,FAIL=0**;同一算法在冻结的修前产物上是 **215/341**(annotation 66 + openapi 60)。
  三轮轨迹 215 → 275(只 resolve)→ 341(java_files)。79 条输入全绝对、全存在、basename 0 碰撞。
- **内容零漂移**:全量输入重采到 /tmp,341 条判据按 `id/level/confirmation/assert/source/scope`
  逐条比对 **完全相等**,`counts` 所有键相等,refuted 4=4、untranscribed 212=212。**只有 inputs 块变了**。
- 符号链接探针全清:文件链接、**目录链接**(三个 root 都试)、**rglob 发现的嵌套文件链接**、
  含 `..` 穿链接目录(`resolve() == os.path.realpath`,取到真文件不是 decoy)、链接环(不抛)。
  嵌套文件链接不撕裂的原因值得记:inputs 与 reference **用的是同一个未 resolve 但已绝对的 rglob 路径**,
  一致性来自「同源」,不是来自「都解析」。

**2. 守卫的洞:放了链接,但没断言 basename**
守卫 pointer 分支(`test_oracle_harvest.py:743`)直接 `json.loads(inputs.openapi)` 解指针,
**从不比对 `ref.partition(":")[0]` 与 `Path(inputs.openapi).name`**。于是:
- **M6**:`harvest_openapi(openapi)` → `harvest_openapi(args.openapi)`(只撕 basename,绝对性不变)→ **SURVIVED**,44 passed。
- **M7**:把 openapi 退回**上一轮的缺陷态**(入口不 resolve、只在 inputs 里 resolve)→ **SURVIVED**,44 passed。
  实测该变异体产物:`reference=link.json:#/…`、`inputs.openapi=/…/real/spec.json`,**TORN=True**。
- **不是等价变异**(坑 61 纪律):产物有可观测差异,已贴出原文。
- 主控自报的 M1–M5(三个 root 去归一化 / openapi 去归一化 / java_roots 改回记目录)**5/5 KILLED 属实**,
  我独立重跑逐个命中指名守卫、0 collateral、空对照 rc=0、恢复复绿。
- **最小补法已实测**:pointer 分支加一句
  `assert ref.partition(":")[0] == Path(report["inputs"]["openapi"]).name` → **8/8 全杀**,基线仍 44 passed。

**教训(与坑 66「覆盖≠检测力」同族,这是它的夹具版)**:
**夹具里出现某个场景,不等于守卫断言了那个场景的不变式。** 加了符号链接夹具却只断言「指针能解开」,
锁住的是「指针正确」,不是「出处 basename 一致」——而后者才是本轮声称修的东西。
**声称修了 X,就要有一个变异体精确退回 X 的缺陷态,并实测它判红**;否则修好的引擎随时可以被无声改回去。

**3. 归因后才报的两条既存洞(非本轮引入,已用去归一化变异体对照实测)**
- 供给不存在的 doc/sql/java 路径 → **未捕获 FileNotFoundError,rc=1,无产物**;
  只有 openapi 例外(`is_file()` 挡住,rc=0 但记一条悬空绝对路径,守卫的 `exists()` 会红)。
- **root 下的嵌套目录符号链接被 rglob 静默跳过**(Python 3.13+ `recurse_symlinks=False`):
  实测 `src/inner -> other/` 里的 `Hidden.java` **采不到,且无任何缺口登记**。
  与「任何 markdown 表格不得静默丢弃」同源,属覆盖缺口可见性问题。

**4. `java_roots` 语义漂移:实测无下游打破,但名字现在是错的**
全仓 grep:**唯一读 `inputs.java_roots` 的是该守卫自己**。
`audit_entry.pillar_oracle` 只读 `hdata["counts"]["by_source"]`;`capability_map.py:1335` 只读 `["oracles"]`;
`oracle_bridge` 零处读 `inputs`;`test_contract_seams` 只读 `["counts"]`。
`audit_entry` 自己的 `d["java_roots"]` 是**发现字典(目录)**,与产物 inputs 同名不同义 —— 两份产物里同一个键名两种语义,是未来的坑。
建议改名 `java_files`(现在改最便宜:0 个机器消费者)。体积代价实测 349,777 → 355,927 字节(+1.8%)。

**5. 跨机自足:单一声明根不可行,先上 sha256**
实测 79 条输入跨 **3 棵树**:jiuyan 74、tests/auto-api 4、审计仓 pilot 1。
`repo_root=jiuyan` 只覆盖 **74/79**;全体共同祖先是 `/Users/june`(家目录,当声明根毫无意义)。
**所以「记相对单一根」这个方案对本语料直接不成立**,必须多根。
最小可行增量是**每个 input 加 `sha256`**(房规已有:`arch_rules.py:1095`、`observation_adapter.py:382`、
`capability_map.py:1344` 都是 `{path, sha256}`)。全部输入合计仅 **203,624 字节**,哈希成本可忽略。
路径退化为线索、哈希成为契约:消费者拿任何一份副本都能判定是否采集时那一份。
**现在是最便宜的时机 —— 实测 0 个机器消费者读 inputs**,破坏性 schema 变更只需同步改守卫(+ `schema_version` 升 2)。

**6. 定稿前重放(做法 36)抓到并发补的断言「瞄错了靶」——既误红合法产物,又抓不到目标缺陷**
验证期间 `test_oracle_harvest.py` 的 sha 从 `2b882480…` 变成 `a8cc022d…`(交付方并发追加,不是我改的)。
新增的是 `assert p == p.resolve()`(「记录的必须是规范路径」)。**重放矩阵:M6/M7 仍 SURVIVED,44 passed。**
- **抓不到**:M7 的 inputs 记的正是 resolve 后的规范路径,该断言恒真;撕裂发生在
  **reference 的 basename** 与 inputs 的 basename 之间,不在「路径是否规范」上。**两个不同的不变式。**
- **反而误红**:实测合法产物 —— `--java-root src`,`src/Alias.java -> elsewhere/Real.java`(rglob 发现的文件链接)。
  引擎 rc=0、`reference=Alias.java:2`、inputs 记 `/…/src/Alias.java`,**basename 完全一致、零撕裂**,
  但 `p != p.resolve()` → 新断言判红。**这是把合法输入判成缺陷的反向假阳性。**
- **对照**:换成 `assert ref.partition(":")[0] == Path(inputs.openapi).name`,在**同一当前版本**上
  **8/8 全杀、基线 44 passed、无误红**。
**教训**:补守卫时先写出「要锁的不变式」的**精确键比对**,再挑断言。
「路径规范」是形式性质,「出处 basename 一致」是关系性质;**用形式性质冒充关系性质,会同时产生漏检与误红**。
与坑 59(双向守卫两方向自相矛盾)同族:断言的**粒度与对象**必须对准声称,不是越严越好。

### 坑 78:粗变异被杀 ≠ 守卫抓得住真缺陷 —— 变异必须精确复刻缺陷态,且要核守卫不误红合法输入(主控实测,V15 两轮查出)

**背景**:修「出处符号链接撕裂」(reference 用传入名、inputs 用解析名,basename 对不上)。
我加断言 `p == p.resolve()`(记录的输入必须是规范路径),自测变异 5/5 KILLED,以为修好了。

**V15 独立验证连判两轮 PARTIAL,两个方向都打中**:

1. **抓不到真缺陷**。它的变异比我的**细**:
   - 我的 M7:整处退回 `openapi = args.openapi` → inputs 变相对路径 → 被 `is_absolute` 断言杀掉。
     **杀掉它的是另一条断言,不是撕裂守卫。**
   - 它的 M6:**只撕 basename** —— `harvest_openapi(args.openapi)` 而 inputs 仍记解析后的路径。
   - 它的 M7:**精确复刻上一轮缺陷态**。
   两者在我的守卫下 **SURVIVED,44/44 全绿**,而产物实测 `TORN=True`。
   根因:守卫的 pointer 分支只断言「指针能解开」,**从不比对 reference basename 与 inputs basename**
   —— **这是两个不同的不变式**,我锁错了那个。M7 的 inputs 记的正是规范路径,我的断言恒真。

2. **反而误红合法输入**(反向假阳性)。rglob 发现的 `src/Alias.java -> elsewhere/Real.java`:
   reference `Alias.java:2`、inputs `/…/src/Alias.java`,**basename 完全一致、零撕裂**,
   但路径非规范 → 我的断言判红。**把合法输入判成缺陷。**

**正确修法**(V15 给的最小补法,已实测):
```python
assert ref.partition(":")[0] == Path(report["inputs"]["openapi"]).name
```
并**撤掉** `p == p.resolve()`。实测:V15 的 M6/M7 **双双 KILLED**,空对照 rc=0,恢复复绿;
合法符号链接探针 rc=0、零撕裂、路径确实非规范(证明旧断言会在此误 firing)。

**规约**:
- **变异要精确复刻缺陷态**,不是「改坏相邻的东西」。粗变异被别的断言杀掉,会给出虚假的安全感。
- **守卫必须双向验证**:既能杀缺陷态,又**不误红合法输入**。只测前者是半个证明。
- 声称修了不变式 X,就要有一条**直接断言 X** 的守卫;断言 Y 恰好也能杀掉某个变异,不算修了 X。
- 与坑 61(区分守卫无牙 vs 等价变异)、做法 37(报指名守卫红名)同源,本条补的是**变异精度**与**反向假阳性**。

### 坑 79:「X 没有生产者」是形状断言,不是能力断言 —— 先把原产物喂一次再宣布缺口(W18 实测)

**任务前提**:交接说「架构支柱报 visibility 未检查:缺成员声明(--members)」⇒ **members 没有生产者**,估 ~30 行适配器。

**实测推翻一半**:`arch_rules.example.json` 的 `member_source` 是**候选形状列表**
`[{records:"functions",…},{records:"nodes",…}]`,所以 `--members pilot/fn_declaration.json`
**直接就能跑**,无需任何适配器:

| 口径 | ARCH-004 verdict | in_scope | measured | declaration_missing |
|---|---|---|---|---|
| 不喂 members | 引擎 rc=1 拒绝出数 | — | — | — |
| 喂 fn_declaration.json 原产物 | PARTIAL_UNKNOWN | 343 | **306** | **0** |
| 喂 relation_net.json 原产物 | PARTIAL_UNKNOWN | 343 | 41 | 265 |

**真缺口在另一处**:`audit_entry.default_arch_config` 硬写 `member_source={"records":"members"}`
(规范形状),原产物容器叫 `functions` → `select_source` 抛
`members 输入里找不到任何已声明的记录容器 ['members']`,rc=1。
**所以缺的是「规范容器转录」,不是「成员事实的生产」。** 两者混为一谈会把 30 行的形状适配器
说成一条新测量能力。

**并集增益必须实测,本轮是 0**:fn(463 键)+ relation_net(119 键,真子集)→ 583 条记录、
**仍是 463 键、仍是 306 条判定**。三载体两两比对**零修饰符冲突**(463/119 共享键全等)。
file_declaration 只多 2 个键,且是编译器合成的 `OrderStatus.$values` / `ErrorCode.$values`,
**0 条在域内边用到它**。把「多载体并集」报成覆盖率提升就是编造。

**规约**:
- 宣布「缺生产者/环境阻塞」前,**先拿已有产物真跑一次消费者**(坑 61 同族:先核产物在不在,这里核消费者吃不吃)。
- 适配器的价值要**按实测增益归因**:形状打通 / 覆盖率提升 / 出处可核,是三件事,不许合并成一句「补上了能力」。
- 报「适用域分母」要连 `not_applicable` 一起报:本轮入口适用域 267→610(+343 恰为 visibility 域内边),
  分子 41→347(+306),`not_applicable` 4163→4706 单列,不进分子。

### 坑 80:空串 modifiers 是**真事实**,缺 modifiers 字段是**未知** —— 生产者补空串等于伪造判定(W18 实测)

`arch_rules.member_row_ok` 只要求 `modifiers` 是 `str`(**允许空串**),
而 `visibility_level("")` 返回 `package_private`。于是两种上游状态在消费者那边天差地别:

| 上游状态 | 消费者读到 | 对 levels=[private,package_private] 的规则 |
|---|---|---|
| `modifiers: ""` | package_private | **violation** |
| 无 `modifiers` 键 / 值为 null | 记录不合格 → 跳过 | `member_declaration_missing` → **unknown** |

**空串不是解析失败,已回源码核实**:被测仓库 `BaseEntity.java:19` `void onCreate()`、
`:26` `void onUpdate()` —— 无修饰符即包私有,javap 就是不打印。fn_declaration 里
464 条中恰好这 2 条空串,**与源码逐条对上**。

**所以生产者的诚实边界是**:缺字段/非字符串 → **跳过并计数**(`skipped_reasons.modifiers_missing`),
**绝不补 `""`**;真空串 → **原样转录 + 在 `coverage.empty_modifiers` 列出键供复核**。
补空串会把 unknown 造成一个确定的可见性判定(违规或合规,取决于 levels),
与「字段名拼错得到一张通过的成绩单」同族(硬约束 3)。

**变异实证**:M3「缺字段补空串」被 4 条指名守卫抓住,其中
`test_missing_modifiers_field_is_skipped_and_counted` 直接断言
「缺声明不得被补成 package_private 而伪造违规」(violation 必须 0、unknown 必须 1)。
反向变异 M6「把空串行整条丢掉」也被抓 —— **过度跳过同样是缺陷**,它丢掉真的包私有事实。

### 做法 43:生产者的合格判据复用消费者读取器;变异必须分打「载体内」与「跨载体」两条去重路径(W18)

**一、不另立形状判据**。生产者直接调 `arch_rules.select_source / extract_records / member_row_ok`,
自己只写一个 `skip_reason` **做归因、不做判定**。这样「什么算合格成员」只有一处定义,
不会重演坑 11(同一封闭集活在三个物理载体,改一处静默漂移)。
守卫 `test_accepts_exactly_what_consumer_accepts` 断言生产者转录条数
**恒等于** `ar.member_record_stats(...)["records_valid"]` —— 用消费者自己的计数当参照。

**二、变异存活先分清「守卫无牙」还是「变异没打中」**。本轮 M5「同键挑赢家」在 22 条守卫下
**rc=0 全绿存活**。逐行追因:我把去重写在 `transcribe` 的** per-input 局部列表**上,
而跨载体冲突是在 `build` 里 `members.extend(rows)` **之后**才算的 ——
**变异根本没打到跨载体那条路径**。同时暴露真缺口:没有任何守卫覆盖
「**同一载体内**重载同名、修饰符不同」。

**两边都补,不含糊**:
- 补守卫 `test_overload_conflict_inside_one_carrier_is_not_resolved`(载体内重载冲突 → conflicting_keys=1、消费者判 unknown);
- 把 M5 拆成 **M5a 载体内去重** / **M5b 跨载体去重**(改 `build` 的 extend)。

**最终 7/7 全杀,基线 0 红,23 条守卫**:M1 全当 public(5 条红)、M2 丢 private(7 条红)、
M3 缺字段补空串(4 条红)、M4 拆空容器闸门(2 条红)、M5a(1 条红)、M5b(3 条红)、M6 丢空串行(2 条红)。
harness 先跑**无变异基线校准**(22→23 全绿)才认后面的数;每个变异都核**锚点恰好命中 1 次**,
命不中记为 harness 失败而不是「等价变异」(坑 25:分母不许被自己截断)。

**三、文件级 + subprocess**。变异改的是 `/tmp/w18/mut/<M>/pilot/engine/` 副本,
连测试文件一起拷进沙箱(测试用 `parents[1]` 定位 engine,所以沙箱自洽),
再 `pytest` 跑副本 —— 同进程 monkeypatch 对 CLI 恒无效(坑 2 / 坑 62)。

### 做法 44:本仓库的「出处引用」有五种形态 —— 校验前先分类,否则必误报(主控本会话连踩五次)

我在同一个复核任务里把校验器写错五次,**每次都是先怀疑被测系统就会误报**。
根因不是粗心,是**把不同种类的引用当同一类处理**。本仓库实际存在的形态:

| 形态 | 例子 | 正确解析方式 |
|---|---|---|
| **basename:行号** | `AddedItemRequest.java:7`、`V1__init_schema.sql:5` | 在**产物 `inputs` 声明的路径**里按 basename 找,再核行号 |
| **文件:JSON pointer** | `openapi.json:#/components/schemas/X/properties/y` | 按 pointer 逐段解(含 `~0`/`~1` 反转义),**不是行号** |
| **产物#键** | `fn_findings.json#FN-PRICE-CLIENT-CONTROLLED` | 两跳:先读那个产物,再按键找条目 |
| **需求/用例编号** | `APL-15`、`UPL-04`、`RFP-05` | **不是文件路径**,是验收用例 ID,别当断链 |
| **符号级锚点** | `com.jiuyan.banquet.service.OrderService#apply` | FQCN#method,按符号查源码,**没有行号不等于没出处** |

**五次误报的具体形态**(全部实测):
1. 把 basename 当仓库根相对路径 → 320/341 指控「文件不存在」
2. 把 JSON pointer 当行号 → 又一批假指控
3. 搜索根只覆盖被测仓库,而快照在**审计仓库** `pilot/openapi.json` → 60 条假失败
4. 把需求编号 `APL-15` 当 `file#key` → 假报「出处链断 35 条」
5. 只看 `location.file` 一个字段 → 假报「74 条无位置」,实际它们有**符号级锚点 + case_id + invariant + 实测值**

**规约**:
- **先枚举形态,再分类计数**;不同形态走不同解析器。
- **用产物自己声明的 `inputs` 当搜索根**,别自己猜。
- **「没有行号」≠「没有出处」**。符号级锚点是合法形态。
- 报「断链/缺证据」之前,**先打开一条真实条目看它的完整结构**。
- 与坑 25(先自证参考实现)、做法 39(先分类再计数)同源;本条给的是**本仓库的具体形态清单**,
  可直接照着写校验器,不必再踩一遍。

### 坑 81:「不覆盖输入」守卫只锁字面路径写法 —— 去规范化变异全绿存活,且真把输入产物覆盖了(V16 实测)

**被验对象**:`member_declaration.py` 的 `--out` 防覆盖闸门 + 守卫 `test_out_must_not_clobber_input`。
实现是**对的**:`if args.out.resolve() in {path.resolve() for _, path in inputs}`(canonical 等价类)。
守卫喂的却是**同一种写法**:`--input A=decl.json --out decl.json`(两边都是同一个相对字符串)。

**V16 新锚点 V4**(精确复刻缺陷态,只去规范化,不动别的):
```python
if str(args.out) in {str(path) for _, path in inputs}:
```
→ **23/23 全绿存活,零指名红守卫**。

**危害探针(证明不是等价变异)**:同一文件、两种写法 —— 输入给绝对路径、out 给相对路径:
| 引擎 | rc | 输入 sha256 前 16 位(前→后) | 输入被覆盖 |
|---|---|---|---|
| 原版 | 2 | `6cc4a36de9a8f479` → `6cc4a36de9a8f479` | 否 |
| V4 变异版 | **0** | `6cc4a36de9a8f479` → `63cd546b607933c8` | **是** |

原版还能挡住**符号链接别名**覆盖(`--input A=alias.json --out real.json`,alias→real):rc=2、零覆盖。
守卫对这两种形态**一个都没测**。真实用法里输入就是仓库产物 `pilot/fn_declaration.json`,
覆盖它等于毁掉上游事实 —— 这不是纸面风险。

**根因**:守卫锁的是「字符串相等」,不变式要的是「**canonical 路径等价类**」。
两者在测试喂的那种写法下重合,所以断言恒真。**与坑 78 同族,但形态是新的**:
坑 78 是「reference basename vs inputs basename」,这里是「同一文件的多种写法」。

**规约**:
- 凡是 `resolve()` / `normpath` / `realpath` 参与的不变式,守卫必须喂**至少两种写法指向同一文件**
  (绝对 vs 相对、`./x` vs `x`、符号链接 vs 真身),否则去规范化变异必然存活。
- 报「N/N 变异被杀」时,必须并排报**锚点覆盖了哪些等价类**;只覆盖一种写法的闸门不算被守住。

### 坑 82:产物的人读口径声明(notes)零守卫 —— 篡改成假话仍全绿,机器判定一字不变(V16 实测)

`member_declaration.build()` 的 `notes` 是下游**人读的唯一口径声明**,其中一条明写:
「缺 modifiers 字段的行不在此列(已跳过并计入 skipped_reasons)」——这正是坑 80 的诚实边界。

**V16 新锚点 V5**:只把这条 note 改成假话「缺 modifiers 字段的行**已补空串**并计入 empty_modifiers」。
→ **23/23 全绿存活**。危害探针实测:变异版产物 `skipped_reasons={"modifiers_missing":1}`、
`records_emitted=2`(机器行为**完全正确**),但 notes 对着人**撒谎说补了空串**。

**根因**:23 条守卫全部断言机器字段(members / coverage / counts / verdict),
**没有任何一条读 `notes`**。诚实性散文是产物的一部分,却是检测力的盲区。
与坑 2(同义反复守卫)同族:声称守住的东西,断言里不存在。

**规约**:
- 产物里**承载判定口径的散文**(「什么算合格」「什么被跳过」「不得当成等价口径」)必须有守卫,
  最低限度断言关键句存在且与机器字段一致(如 `skipped_reasons` 非空时不得出现「已补空串」字样)。
- 变异矩阵要把「只改 prose、不改逻辑」当成**独立锚点**打一次;它存活就说明口径声明无牙。
- 不许把「机器判定对」当成「产物诚实」——人读的那一半同样是交付物。

### 坑 83:「复用消费者判据」只复用了判定函数,字段名词汇表仍活在两处(V16 实测)

做法 43 声称生产者「不另立封闭集」。V16 逐条核对:**判定函数确实复用了**——
`transcribe` 的容器选择/记录抽取/行合格判定分别直调 `arch_rules.select_source / extract_records / member_row_ok`,
`skip_reason` 只做归因不参与判定(把闸门换成 `skip_reason` 的 V7 变异,13 组输入输出**逐字节全等**,
证明当前严格互补、`not_member_shaped` 分支不可达)。

**但字段名词汇表是重复载体**:
- `arch_rules.py:300` → `validate_record_source(..., ("class", "method", "modifiers"), ...)`
- `member_declaration.py:27` → `FIELDS = ("class", "method", "modifiers")`

**没有任何守卫断言这两个元组相等**(grep 全仓:两处字面量,零交叉引用)。
今天不咬人,因为 `member_row_ok` 只查这三个字段;**消费者一旦给 member_source 加第四个字段
并让 `member_row_ok` 要求它,生产者的 FIELDS 不跟随 → 静默漂移**(坑 11 的原形)。

**同族残留(V16 一并实测)**:
- `build()` / `transcribe()` **库层没有空容器闸门**,只有 `main()` 有。实测 `build()` 喂空容器
  返回 `members=[]` 不抛错;库调用者可以直接写出空产物。(消费者 `arch_rules.audit()` 与 `main()`
  两处闸门均实测 rc=1 / ValueError,未被削弱。)
- 生产者**吃不下第三载体** `file_declaration.json`:`files` 容器每行是「类 + methods 列表」,
  不是成员行。实测 rc=1「63 条记录但全不合格(method_missing: 63)」。
  所以「三载体并集增益 0」是**分析结论**(V16 独立 flatten 后实测:465 键、只多 2 个合成 `$values`、
  零边使用、端到端仍 347/610 measured=306),**不是交付的生产者的能力**。报增益时必须说清是谁算的。

**规约**:
- 宣称「复用消费者判据」要**分清三层**:判定函数 / 字段名词汇表 / 闸门站点。
  复用了第一层不等于另两层没有重复载体;逐层给 file:line。
- 同一封闭集的字面量出现在两个文件,就要有一条**断言两者相等**的守卫,否则按坑 11 处理。
- 库层 API 与 CLI 的闸门要分别测;只在 CLI 设闸,等于把不变式寄托在「没人 import 它」上。

### 做法 45:验证者的两个口径纪律 —— 跨沙箱比对先归一化,移动靶全量计数报三个数(V16)

**一、跨沙箱等价比对必须先归一化沙箱相关字段(自踩,坑 25 本会话实例)**
V16 判 V7 变异是否等价时,把两个沙箱的产物 JSON **整体**比对,结果 11 组里 4 组报「不等」,
差点把等价变异误报成真洞。根因:产物 `sources[].path` 记的是**各自沙箱的绝对路径**
(`/tmp/v16/harm/V7_orig_normal` vs `.../V7_mut_normal`),sha256 同理。
剔掉 `path` / `sha256` 后重跑:**13/13 逐字节全等** → 当前等价变异。
**规约**:比对前先声明「哪些字段是沙箱相关的」并归一化;否则测量工具的差异会记到被测对象账上。
判等价还必须写明**依赖哪条可达性前提**(坑 61):V7 的前提是 `skip_reason` 与 `member_row_ok` 严格互补,
前提消失即升级为真洞,而当前**没有结构性守卫锁定闸门身份**。

**二、移动靶下的全量计数必须报「收集 / 实跑 / 排除」三个数 + 排除清单**
V16 实测同一仓库三个时刻:
| 时刻 | 全仓收集 | 实跑(排除活服务) | 结果 |
|---|---|---|---|
| 16:04(W18 报) | 1110 | — | 1087 + 23 |
| 16:12(V16 基线) | 1118 | **1104** | 1104 passed + 5 subtests,0 failed |
| 16:28(V16 重跑) | **1166** | **1152** | 1152 passed + 5 subtests,0 failed |

排除清单(硬边界:禁跑探针/写库):`pilot/test_probes.py`(8 条,向活 DB 写订单)、
`demos/shop/test_d6_fixture.py`(6 条,需两个 demo 容器)。**1166 − 14 = 1152**,账对得上。
+48 的增量**逐文件归因**到并发 agent 在验证期间新建的 `test_price_expectation_oracle_gate.py`(16:19:34)、
`test_parity_crosscheck.py`(16:25:06),**不是 W18 交付的回归**(坑 5)。
同时刻重冻结:被验文件与全部测量输入 sha256 **一字未变**,所以差值测量本身不是移动靶。

**规约**:
- 只报「N passed」在并发仓库里**不可复核**;必须报收集数、实跑数、排除清单(逐文件 + 条数 + 排除理由),
  让读者自己能对上账。
- 增量必须**逐文件归因到 mtime/作者**,不许笼统说「全绿」或笼统背锅。
- 差值测量与全量计数要**分开冻结**:前者要求输入 sha 不变,后者允许靶在动但必须报三个数。

---

## 二十五、W15 第二轮补充(2026-11-15,价格根因簇:一条未经确认的预期撑起 5 条台账缺陷)

### 坑 84:闸门按「层」装,业务预期按「根因簇」传播 —— 只修被点名的那一条会留下 4 条同源假阳性(W15 实测)

**现象**:任务点名 `FN-PRICE-CLIENT-CONTROLLED`(CRITICAL / 0.97 / verified)。实测它不是孤条,
而是根因 `price.final_price.upper_bound_unvalidated` 的**簇头**,同根因共 **5 条**跨 3 层:
函数层 own 1 + 函数层 cited 1(语义地图)+ 集成层 cited 2 + 函数层 cited 1(numeric)。
服务层对**同一条预期**早已正确压住(`business_findings.json` 的 `unconfirmed_suspects`
里有 `BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE`),函数层两扇门与集成层一扇门完全绕过它。

**实测证据**:该预期在全项目**唯一的 oracle 声明**是 `invariants.json` 的
`BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE`:`source=inferred` / `confirmation=unconfirmed` /
`reference=「无文档依据（现有文档只规定金额 >= 0）」`。而 `fn_groups.json` 的分组、
`semantic_map.json` 的职责、`integration_diff.py` 的 `add()` 三处都不消费任何 oracle。
预期甚至被写进了**用例本身**:`fn_cases.json` 的 APL-15 `check={state_le, finalPriceCent, 46800}`、
`invariant=「落库的成交价不得高于套餐总价」` —— FAIL 判定是拿发明出来的界比出来的。

**只修簇头会制造新硬违规**:`aggregate_findings.py:116` 有「无 own 归属:所有条目都只是引用别的层」。
压掉 own 而留下 4 条 cited → 根因孤儿 → 硬违规 +1。**撤销必须按簇,不能按条。**

**修法与实测差值**(真跑纯判定层刷新真实产物,见做法 46):
台账 **95→90**、defect **78→73**(CRITICAL 6→4 / HIGH 17→15 / MEDIUM 44→43 / LOW 11 不变)、
根因 **70→69**(撤 2 增 1)、collapsed 25→21、**硬违规 1→1 不变**;
positive 15 / observation 2 不变;按 `(layer,id,title)` 比对:消失 5 / 新增 0 /
共有条目内容变化 **2**(均为有意的连带修复)。三张疑点清单留痕:
class refuted 3→4、fn unconfirmed 0→2、integration unconfirmed 0→2,观测与证据原样保留。

**防复发守卫(已机械化)**:`pilot/tests/test_price_expectation_oracle_gate.py` **46 条**,
含 `test_no_defect_still_cites_the_retracted_root_cause`(遍历 finding_meta 全表,
凡仍挂被撤销根因的 defect,其开单门必须已声明非 confirmed 的 oracle)与
`test_all_three_layers_agree_on_the_same_expectation`(同一预期跨层确认状态必须一致)。
通用闸门收敛到单一载体 `engine/oracle_gate.py`,并用
`test_semantics_agree_with_the_class_layer_gate`(6 组参数化)锁它与 `class_rules.oracle_gate` 逐例同判(防坑 11)。

**通用纪律**:裁定一条缺陷前,先按 `root_cause_key` 枚举**整簇**并查清每扇门;
撤销时同时检查「根因是否还有 own」。**按条修会留下同源假阳性,或制造归属硬违规。**

### 坑 85:「规范违背不成立」≠「风险不存在」—— refuted 与 unconfirmed 不能照抄上一条的裁定(W15)

**现象**:上一条(address)判 `refuted`,本条容易顺手也判 refuted。**错。** 两者证据形态不同:

| | address(W15 第一轮) | 成交价上界(本轮) |
|---|---|---|
| 文档怎么说 | **明确否定**该预期:「address 可为空;为空时保存为空字符串」(API.md:307) | **确认实现符合规范**:业务校验对照.md:20 在「已确认一致的规则」表里记 `resolveFinalPrice`「一致」;但**从未要求上界**,:60-65「当前未定义」清单也没列它 |
| 确认状态 | `refuted` | `unconfirmed` |
| 进哪个清单 | 只保留观测,不再等业务确认 | **待确认疑点(规范层风险)**,等业务确认 |

**价格篡改风险是真的**:客户端可把任意非负数字变成订单最终价,集成链实测匿名下单 1 分成功。
硬约束 5 禁止它以 CRITICAL defect 进台账,但**不禁止、也不应该把它吞掉** ——
业务校验对照.md:6 自己写着「代码疑似违背业务预期的问题单独列出,不吞掉」。

**极性翻转(D4,最危险的一种)**:原 `fn_groups.json` 的 related 把**反驳它的**验收用例
`test_create_order_use_final_price`(finalPriceCent=5000 / planTotalCent=8888 → 断言保存 5000)
写成「既有测试把保留客户端成交价写成了断言——**缺陷被固化成契约**」。
即把 confirmed 的反证当成缺陷的佐证。守卫 `test_polarity_inversion_removed` 锁死该表述不得复活。

**逐载体证据(九处,全部机器枚举)**:API.md:337-338 / AGENTS.md:422-427(含 :419-421「不要做老板确认功能」)
/ AGENTS.md:503 验收项 / 业务校验对照.md:15、:20、:60-65 / OrderService.java:149-157(逐字实现)
/ CreateOrderRequest.java:24 `@Min(0)` 无上界 / V1__init_schema.sql `chk_orders_final_price_cent CHECK (>=0)`
/ openapi.json `finalPriceCent={minimum:0}` 无 maximum / tests/auto-api/tests/test_orders.py:14-16。

**通用纪律**:判 refuted 还是 unconfirmed,要看文档是**明确否定该预期**还是**只确认了现状、对该预期沉默**。
前者不再等确认,后者必须进待确认疑点并保留风险评级。**风险评级不是缺陷严重度**,
本轮保留 CRITICAL 作为风险评级(服务层对同一预期评 HIGH,跨层评级口径不一致已如实报给编排者)。

### 坑 86:撤销一条业务预期根因,会把同根因的**技术可自证**缺陷一起孤儿化(W15)

**现象**:簇里第 5 条 `FN-NUMERIC-BIGINT-PRECISION-LOSS`(LOW,cited)是
「int64 分值超出 JS `Number.MAX_SAFE_INTEGER`,前端显示与库里不一致」——
**IEEE754 平台事实,不依赖任何业务预期**,却挂在被撤销的业务预期根因下。
整簇压掉会连它一起删(假阴性);只压簇头会让它变成无 own 的孤儿。

**可操作分类判据(本轮用它把 5 条分开,不是靠感觉)**:
> 问:这条结论的「错误」判据,能否只从**语言/平台/通用安全规范 + 观测**推出?
> 能 → 技术可自证,不需要业务 oracle(NPE、IEEE754 精度、TOCTOU 撞唯一键、内容不嗅探、无鉴权)。
> 不能,必须引用**本项目的业务规则**(价格须 ≤ 套餐价、地址须非空白、定金须 ≤ 成交价)→ 业务预期,必须有 confirmed oracle。

按此判据:簇里 4 条属业务预期(撤),1 条属技术可自证(留)。
**这也说明「关键词扫出 45 条无 oracle 的 defect」不可当待裁定量** —— 扫描分不开这两类(做法 39 同族)。

**修法**:`finding_meta.py` 把它重挂 `numeric.amount_exceeds_js_safe_integer` 并改 `relation=own`。
实测:新根因 severity LOW / finding_count 1 / layers_own=[function],硬违规数不变。
守卫 `test_technical_finding_survives_and_owns_its_root_cause` +
`test_new_root_cause_has_exactly_one_owner` + 函数层「无 oracle 的分组照常开单」阳性对照。

**连带诚实性缺口(自己收口)**:它的 detail 来自探针 `numeric_props.py:118`,原文仍写
「与 price.final_price.upper_bound_unvalidated 同一根因,这里只量化后果,**不重复开单**」——
重挂后这句变成假话。修法是**删掉探针里复述归属的散文**,让 `finding_meta.py` 做归属的单一载体
(坑 11:同一事实两个载体必然漂移),而不是把文案改成新的重复。守卫 + 变异 M9 锁死。

### 坑 87:两个「看起来改了」的空变异 —— `{} or {...}` 与改变量名都不改变行为(W15 自踩)

**实测**:第一版 M8 把 `ORACLES: dict[str, dict] = {` 替换成 `= {} or {` ——
`{} or X` 求值仍是 `X`(非空 dict 为真),**映射一字未变**;
第一版 M7 把 `(art / "fn_rule_suspects.json").write_text(` 前面加 `_disabled = ` ——
**文件照样写**。两个变异都报「未咬住」,若直接下结论就会误报「守卫无牙」。

**根因**:与做法 31「锚点未命中必须显式报跳过」同族,但更隐蔽 ——
**锚点命中了、文本也变了(`assert mutated != pristine` 通过),行为却没变**。
文本差异 ≠ 行为差异。

**修法**:M8 改成把两个 id 键换成不存在的键(映射真的失效);
M7 改成把输出文件名换成 `fn_rule_suspects.disabled`(目标文件真的不落盘)。
两个都 KILLED。**并且**:M7 暴露守卫本身用 `FileNotFoundError` 崩溃而非断言失败,
`_caught` 只捕 AssertionError → 补 `assert path.is_file(), "疑点清单必须落盘"` 把崩溃变成指名断言。

**通用纪律**:每个变异必须附**判别性用例**证明行为真的变了(做法 31);
`assert text != pristine` 只是必要条件,不是充分条件。
守卫要用**显式断言**表达不变式,不要依赖下游崩溃 —— 崩溃不等于守卫 firing。

### 做法 46:修生成器 ≠ 刷新发布物 —— 判定层是纯函数,台账能在不跑探针的前提下真刷新(W15)

**背景**:第一轮只在沙箱重算、没刷新真实产物,编排者实测公开台账仍带着已证伪的假阳性。
**同事读的是台账,不是中间裁定文件** —— 已知假阳性仍在发布,比 oracle 产物未刷新更严重。

**完整重生成链分类(逐脚本核 IO,不凭描述)**:
- **探测层(有副作用,禁跑)**:fn_static / fn_dynamic / semantic_probe / file_static / file_dynamic /
  contract_static / contract_probe / spring_ctx_probe / property_binding_probe /
  class_contract / class_static / class_dynamic / integration_static / integration_dynamic /
  integration_reconcile / integration_chain / business_probe / cleanup.sh / scheduler /
  build_agent_graph / agent_coverage / probe_residue_check
- **判定层(纯读 JSON + 写产物,可跑)**:`engine/fn_diff.py`、`engine/file_diff.py`、
  `contract_diff.py`、`class_diff.py`、`integration_diff.py`、
  `engine/normalize_findings.py`、`engine/aggregate_findings.py`、`engine/feature_coverage.py`

**本轮实跑**(按 run_pipeline.sh 顺序,只跑受改动影响的层):
`engine/fn_diff.py` → `class_diff.py` → `integration_diff.py` → `normalize`(rc=2,既有 UNMAPPED 契约缺口,非阻断)
→ `aggregate`(rc=0) → `feature_coverage`(rc=0)。**零探针、零写库、零 maven、零容器。**
顺序有依赖:`integration_diff` 读 `fn_rule_suspects.json`(函数层先跑,run_pipeline.sh:57 早于 :82)。

**聚合层连带诚实性核对(编排者点名要查,11 项全过)**:
`sum(finding_count)==台账总数`(90==90)、`finding_uids` 无重复、**悬空引用 0**、
未被认领的 finding 0、`finding_count==len(finding_uids)` 逐条、
根因 severity == 全组最严重度(0 不符)、`layers_own` 为空 0、
summary 三字段与实测一致、`by_kind`/`by_layer` 之和 == total、
覆盖度矩阵里被撤销的 id/根因出现 **0** 次、`audit_report.md` 残留 **0** 处且头条已变
「发现 90 条 → 根因 69 条(归并 21 条)」。

**卡点如实报(不跑禁止操作、不手改产物)**:`units.json`(6 处)与 `agent_coverage.json`(2 处)
仍引用被撤销的 uid/根因,刷新它们需要 `engine/build_agent_graph.py` —— **W19 正在改该文件,禁碰**。
`change_impact.json`(1 处)同理依赖单元索引。`numeric_findings.json` 的 detail 旧文案需重跑
`numeric_props.py`(自称探针,禁跑),**只影响文案,不影响计数/严重度/根因归属**(那些取自 finding_meta,已正确)。
`baselines/pre_generic_class_findings.json` 是历史等价性快照,全仓无消费者,不改。

**通用纪律**:交付「源头修好了」之前必须问**发布物刷新了没有**。
能纯静态刷新就刷新并报实测差值;不能就逐文件报卡点与归属,
**宁可报 UNKNOWN,也不跑禁止操作或手改 JSON 充数**(手改会绕过自己建的闸门,且下次重生成即被覆盖)。

### 做法 47:聚合层复核判据必须取自引擎源码 —— 否则复核器自己制造假指控(W15 自踩)

**实测**:我第一版聚合核对判「根因 severity 应等于 **own 条目**的最高严重度」,
报出 1 条不符(`order.item_quantity_unvalidated` 根因 HIGH / own 最高 MEDIUM)。
回源码核:`aggregate_findings.py:131` 是 `_worst([r["severity"] for r in group])` ——
**取全组(含 cited)最严重度**。按引擎真实语义重核 → **0 条不符**。
那 1 条是**我的判据错**,不是引擎缺陷;且改前改后完全相同,与本轮改动无关。

**根因**:坑 25 的又一次实例(复核器 bug 方向单向,只会制造「被验方有罪」的假阳性)。
这次的具体形态是**聚合口径猜错**:severity 取 own 还是取全组,是引擎的声明,不是我的直觉。

**通用纪律**:写任何「产物应当满足 X」的复核判据前,先 grep 引擎里生成该字段的表达式,
按**它的口径**写判据;报「N 条不符」前逐条回源码核。
另:本轮全量出现 1 红 `test_path_overrides.py::test_relation_net_default_path_equals_explicit_binding_path`,
归因三条证据 —— ① 该文件 mtime 16:28:13(W19 新建,晚于我上次全绿);
② 它只跑 `relation_net.py`,而该脚本只读 class_declaration/fn_declaration/invariants/openapi/relation_net,
**我重生成的产物一个都不在其中**;③ 单独跑 16 passed、与 relation_net 测试同跑 31 passed,只在全量里红。
判为 W19 在飞状态,按做法 7 诊断不越权修。

### 坑 89:交叉验证只比对返回元组的一个字段 —— status 全对,counts/conflict 契约可以随便漂(V15 实测)

复验主控新增的 `pilot/tests/test_parity_crosscheck.py`(GF(2) 并查集奇偶 vs 2^n 暴力染色,400 图固定种子)。
**守卫本身成立:非空洞、确定性、无误红、参考实现正确。但它的比对 scope 只覆盖了函数契约的 1/4。**

**1. 参考实现自证(坑 25,双向校准 + 第三方独立实现)**
- 自己另写**第三个实现**(BFS 二染色,既非 DSU 也非枚举),三方对账:
  13 条手算真值 **13/13 一致**;400 随机图 **0 分歧**;
  **穷举 n=2..4 全部 4164 张图**(每条边 absent/consistent/inconsistent/unknown 四态)**0 分歧**;
  再放开自环穷举 n=1..3 共 4164 张,**0 分歧**。
- **已知假校准**:把 `_brute` 故意改成「先判 unknown 再判可满足性」,在
  「有 unknown 边 + 已知边本身矛盾」上立刻与引擎分歧(unknown vs inconsistent)→ 证明交叉验证对参考实现 bug 有判别力。
- 主控问的那处 `if not known or unknown > 0: pass`(:28-31)是**纯 no-op 死代码,不是语义错误**:
  下面的可满足性循环无条件先跑,顺序本来就对。语料里这类图有 **36 张**,真被覆盖。
  但注释描述的意图由别处实现,**留着会误导下一个读者**,建议删。

**2. 语料覆盖(实测计数,不是「看起来覆盖了」)**
奇偶环 43、偶环 20、**全已知且可满足的环 7**、不连通 178、孤立细胞 169、全 unknown 38、
零边 68、unknown+已知矛盾 36;三态 91/43/266。n=2..6 全present。
**自环 0** —— `itertools.combinations` 结构上产不出自环;但**生产调用方 `frame_check.py:581` 也用
combinations**,所以自环在生产同样不可达。**这不是真缺口,别当缺陷报**(做法 38 同源)。
DSU 内部:`union` 时 `pb!=0` 有 56 次、`pa!=0` 61 次、同根非平凡奇偶校验 57 次 → 加权合并真被 exercised。

**3. 变异矩阵 12 锚点:10 KILLED / 2 SURVIVED,两个存活性质完全不同**
主控自报 U1–U4 我逐个复现 **4/4 KILLED**,红名逐个命中、0 collateral、空对照 rc=0、恢复复绿。
V15 自选 8 个:M5 丢 pb、M6 find 用 `|` 代 `^`、M7 `or`→`and`、M8 同根即判一致、
M10 标签极性翻转、M12 unknown 掩盖矛盾 → **6/6 KILLED**。两个存活:
- **M11 `par` 初值 0→1:等价变异,已给结构证明**。不只靠「穷举到 n=4 没差异」——
  插桩统计 **read-before-write:7634 次 par 读、10772 次 union 写、写前读 = 0**。
  `par[x]` 只在 `parent[x]!=x` 时被读,而那必然发生在 union 写过 `par[rb]` 之后 → **初值是死存储**。
- **M9 `break`→`continue`:不是等价变异,是真 scope 洞**。穷举 4164 图:
  **status 差 0,但 counts 差 456 例、conflict 边身份差 128 例**。
  最小可区分图:`c0..c3`,边 `{c0c2:同, c0c3:同, c1c2:同, c1c3:异, c2c3:同}` →
  现实现 `known=4`,continue 版 `known=5`,status 都是 inconsistent。
  守卫第 61 行 `got, _conflict, _known, _unknown = ...` **把后三个字段直接丢了**。
  全仓 grep:`edges_known`/`edges_unknown` 只有生产者 `frame_check.py:591`,**0 个消费者、0 条守卫**;
  `conflict` 在 `test_frame_check.py:347/677` 也只断言 `is not None`,从不钉是哪条边。
  → **矛盾时 counts 是「截断到冲突边」的部分计数,这个语义没有任何地方钉住,也没在报告里说明**。

**4. 非空洞断言双向有效 + 反向假阳性为零**
- 语料强制全 consistent → 红(`{consistent:343, inconsistent:0, unknown:57}`);
  强制全 unknown → 红(`{0,0,400}`)。**两个方向都咬得住**。
- 替换成**三种正确但不同的实现**:①带路径压缩的加权 DSU ②去掉 break(完整计数)
  ③BFS 二染色内核 —— **4 tests 全绿**。**守卫没过拟合到当前实现**,无误红。
- 确定性:两次独立跑语料+结果指纹 sha256 **完全相同**。

**教训**:
1. **交叉验证要声明「比对的是返回契约的哪几个字段」。** 只比 status 时,
   「400 图零分歧」这句话的覆盖范围就只有 status;counts/conflict 可以任意漂而头条看不出来。
   与坑 66(覆盖≠检测力)同族,这是**字段级**的版本。
2. **存活变异必须分到「等价」与「真洞」两类,且等价要给结构证明**(插桩 read-before-write),
   不能只靠「有限穷举没找到反例」——后者是上界线索,不是结论(坑 25 粗测纪律)。
3. **验参考实现要引入第三个独立算法 + 已知假校准**,只跑真样本的复核器没有判别力。

### 坑 88:产物世代错配 —— 不报错,而是产出「看起来合理的错数」(主控实测,W19 先发现线索)

**实测**:同一目录下两批产物相差 13 小时、规模差三倍以上:
```
pilot/relation_net.json      Nov 15 01:49   162 节点 / 151 边
pilot/units.json             Nov 15 14:39   536 单元
pilot/edges.json             Nov 15 14:39   886 边
pilot/fn_declaration.json    Nov 15 14:39   464 条
```

我把 162 节点的 net 与 536 单元的 units **一起**喂给 frame_check,引擎**照常 exit 0**,
产出 `cells_total 546` 但 `cells_footprint_known 130`。
我据此对外报了「**76% 细胞足迹未知,这是真实覆盖天花板**」。

**这个结论是错的**:大部分「未知」是因为 net 根本不覆盖那些细胞,**是产物错配的伪影,不是能力边界**。
同理受污染的还有 `pairs_total`、`unknown_pairs_over_total`、候选数。

**为什么这类洞最危险**:它**不报错**。引擎拿到不匹配的输入仍然算得出数,
数字看起来完全合理(有分子有分母有比率),于是被当成事实引用。
比 crash 危险得多 —— crash 会被发现,合理的错数会被传播。

**规约**:
1. **报任何覆盖率/分母之前,先核输入产物是否同一世代**(mtime、规模量级、节点集覆盖率、输入指纹)。
2. **引擎应当拒绝或显式告警世代错配**,宁可 INCOMPLETE/UNKNOWN,不可出看似合理的错数。
   同族先例:两个引擎输入目录分叉 → 静默 2 单元 / 0 边且 exit 0。
3. **发现自己引用过的数被污染,立刻公开纠正**,不要悄悄换个数字继续讲。
4. 与坑 5(在飞失败先归因)、做法 39(先分类再计数)同源;本条补的是
   **「输入本身不一致」这个因子** —— 它既不是代码缺陷也不是环境抖动,而是**产物管理缺陷**。

### 坑 90:「防覆盖」守卫锁的是路径字符串等价类,不是**文件身份** —— 硬链接别名 rc=0 毁掉上游证据(W18 实测,V16 判 PARTIAL 后自查挖出)

我的 `--out` 防覆盖写成 `args.out.resolve() in {p.resolve() for p in inputs}`,
声称是「**不得覆盖任何输入产物**」。V16 报「绝对 vs 相对、符号链接两种形态零守卫」——
补守卫时顺着同一不变式往下推一格,**挖出 V16 没报的第三种形态:硬链接**。

**实测危害**(规范形状输入,确保走到写入分支):
```
inode same: True
rc= 0   sha 786c3cf98aef -> eaa05af55d2e   数据丢失= True
```
`resolve()` 只规范化**路径字符串**(相对→绝对、跟随符号链接);硬链接是**同 inode 不同路径**,
两个字符串永远不等,于是闸门放行、`write_text` 直接毁掉上游声明产物。
修法是按文件身份比:`os.path.samefile`(out 已存在时),不存在时退回规范化路径比。
修后三种形态全部 rc=2、sha 逐字未动,且**合法输入不误红**(同目录不同文件 / 指向非输入的符号链接 /
out 尚不存在,三种都 rc=0)——真实产物副本上复验同样通过。

**我自己的探针先踩了坑 25**:第一版硬链接用例没给 `--rules`,输入是 `functions` 容器而默认形状要 `members`,
于是 rc=1 是**形状拒绝**、根本没走到写入,sha 当然没变。差点把「已挡住」当成结论发布。
**测覆盖/写入类守卫,必须先确认执行真的到达了那个分支**(用规范形状输入或补 --rules),
否则 rc≠0 是别的因子给的,不是被测守卫给的。

**规约**:
- 守卫粒度必须匹配**声称的不变式**,不是匹配最容易想到的那种输入形态。
  「不得覆盖输入」的等价类是**文件身份**(inode),不是路径字符串;只锁字符串就是坑 78 的原形。
- 顺带把 `--rules` 也纳入保护:它也是输入产物,只保护 `--input` 会让声称宽于守卫。
- 与坑 78(变异要精确复刻缺陷态)、做法 37(报指名红名)同源;本条补的是**等价类选错**。

### 做法 48:等价变异要用「差分空间 + 反向漂移变异」双向坐实;口径声明与词汇表同样需要守卫(W18)

V16 另选 8 锚点、4 个存活。逐个定性,**不许停在「存活但无害」**:

**① V6(去掉空修饰符簿记的 `.strip()`)= 真洞,不是等价变异。**
判别用例:`modifiers="   "` / `"\t"` / `"\n"` / `" \t "`。消费者 `visibility_level` 对纯空白
`split()` 得空 token 列表 → 返回 `package_private`,与空串**完全同义**;
去掉 `.strip()` 后 `"   "` 为真值 → 簿记漏列,而 notes 声称「empty_modifiers 列出这些键供复核」就成了假话。
实测 4 个 SUBFAIL 全红。**判别用例必须落在两个实现语义分歧的那个输入上**,不是随便找个输入。

**② V7(闸门换成自己的 `skip_reason`)= 当前等价,但用双向证据坐实。**
- 正向:枚举 **219 个行形态**(class/method/modifiers 各取 合法/空串/None/数字/list/dict 的笛卡尔积
  + 三种缺键 + 非对象 + 键名不符 + 额外键),`member_row_ok(row) ⟺ skip_reason(row)=="not_member_shaped"`
  **零分歧**。分母写进断言(`assertEqual(len(rows), 219)`),防自己截断(坑 25)。
- 反向:等价是绑定当前实现的临时结论(坑 61),所以把差分做成**常驻守卫**;
  再造一个**真分歧变异** V7b(`skip_reason` 去掉 `or not value`,空 class 被它放行)——
  实测 3 条指名红:`test_attribution_gate_agrees_with_consumer_on_every_shape`、
  `test_every_rejection_has_a_specific_reason`、`test_accepts_exactly_what_consumer_accepts`。
  **差分守卫能杀真分歧,才证明它不是同义反复**(坑 2)。V7a 存活是正确结果,不是漏网。

**③ 口径声明(notes)此前零守卫 —— 篡改 23/23 全绿。**
补法分两层,避免只锁字符串的同义反复:
- 声明层:**语义锚点放在测试里**(外生于被验对象),逐条 note 钉关键语义 + 钉条数。
  4 个篡改变异(「不得当成等价口径」→「可当成」、「不挑赢家」→「挑赢家」、删一条 note、
  「package_private」→「public」)各由 `test_notes_state_every_honesty_claim` 指名抓住。
- 行为层:每条声明配一个**可证伪的行为断言** —— sha256 真能核、空串/纯空白真被消费者读成
  package_private、候选列表 caveat 两侧都实测(喂本文件被拒 / 喂原产物被接受)、
  冲突键真的 `load_members` 成 None。**声明与行为分别有守卫,篡改任一侧都红。**

**④ 词汇表去重(坑 11):判定函数复用了,字段名词汇表却仍是第二份字面量。**
改成从消费者派生:`FIELDS = tuple(f for f in arch_rules.DEFAULT_MEMBER_SOURCE if f != "records")`,
**arch_rules.py 一个字节没动**(sha 前后同为 `fc0f01c4…`,尊重 V16 冻结)。
三条守卫分别钉:派生关系 + 源码里不许退回字面量、消费者闸门**恰好**要求这些键(逐键抽掉必须变红)、
消费者 `validate_config` 产出的 member_source 键集 == records+FIELDS。
第三条用**消费者侧漂移变异**验证(在 /tmp 副本把 `arch_rules.py:300` 的字面量去掉 modifiers)——
实测**只**红 `test_consumer_config_validation_uses_the_same_vocabulary` 一条,单因子干净。

**本轮总账**:守卫 23 → **41 + 4 subtests**;变异 **14/14 锚点应用成功、13/13 缺陷态全杀、
1 个等价变异如预期存活**;无变异基线校准 0 红。其中 V4b(只删 samefile 分支)只红硬链接那一条、
V4d(过宽禁一切符号链接 out)只红反向合法用例一条 —— **两个方向都证明守卫粒度刚好,不宽不窄**。

### 坑 91:撤掉假阳性时,别让底下的**真实风险**一起消失(主控实测)

**实测**:一条 CRITICAL「成交价由客户端说了算」被正确裁定为**假阳性**——
客户端定价是文档化设计(API.md:338、AGENTS.md:426),那条「不得高于套餐价」的预期
在全项目唯一的声明是 `inferred / unconfirmed / 「无文档依据」`。撤销正确。

**但**:
```
pilot/fn_rule_suspects.json   unconfirmed_count = 2  ← 完整 oracle 出处 + 说明,记录得很好
pilot/audit_report.md         grep「待确认|unconfirmed|疑点」→ 0 命中
```

**价格篡改这个真实的规范层风险,从「CRITICAL 缺陷」变成了「同事完全看不见」。**
对使用者而言,只存在于中间文件、报告不提的风险 **等于不存在**。

**这是「修假阳性」的反向失败模式**:
- 正向失败:把不是缺陷的东西报成缺陷(假阳性,损害信任)
- **反向失败:撤掉假阳性时,把「不知道、需业务方确认」也一起藏了**(漏报,同样损害信任)

**规约**:
1. **撤销 ≠ 删除**。三态各有归宿:confirmed 进台账、unconfirmed 进「待确认疑点」、
   refuted 只留观测。**三者都必须在最终报告里可见**,不能只活在中间产物里。
2. **头条要并排报三个口径**(确认缺陷 N / 待确认疑点 M / 已证伪 K),**不得混算成一个总数**。
   否则读者会把「90 条」当成全部结论。
3. 守卫要**双向**:少报一桶必须红,把 unconfirmed 当缺陷计入分子也必须红,
   某桶为空时不得误红(坑 78)。
4. 与坑 66(覆盖≠检测力)同族:**不是数字错了,是该被看到的东西没被展示。**
   审计工具的失败不只有「报错数」,还有「不报该报的」。

### 坑 92:「单跑绿、全量红」有三个原因,先查「刚引入的错误断言」(主控自踩 + 纠正他人归因)

**实测经过**:我给 `test_parity_crosscheck.py` 加 counts 断言,假设 `unknown` == 全部 unknown 标签数。
它把**未变异的引擎**判红 17 例(空对照 rc=1)。取證後找到真因:
**引擎在矛盾处 `break`,后面的 unknown 边根本没被计数**,所以 `unknown=0` 是真实行为,我的假设错了。
改成「重放同一迭代顺序、只统计 break 前的前缀」后:4 passed,M9 被杀,M11 正确存活(等价变异)。

**同时**:另一个 agent 在并发跑全量时看到这条红(单跑绿),
把它归因为「另一个 agent 在飞的顺序依赖」。**归因错了** —— 时间窗正好落在我写错到修好的两分钟里。

**规约**:「单跑绿、全量红」至少有三个原因,**按此顺序排查**:
1. **刚引入的错误断言**(最可能,尤其并发时有人正在改测试)—— 先看空变异对照是否也红。
   **空对照红 = 守卫自己错了,不是被测系统错了。** 这是最快的判别器。
2. 顺序依赖 / 共享状态(夹具污染、模块级缓存、cwd 漂移)
3. 并发编辑(移动靶)

**不要跳过第 1 步直接归因到 2/3** —— 那会把自己的错说成别人的,或把别人的真缺陷说成环境噪声。
两个方向的误归因都损害信任。与坑 5(在飞失败先归因)、坑 25(先自证参考实现)同源。

### 做法 49:变异 harness 必须沙箱化「工具链」,不只是文件(主控转述 W19 事故)

**事故**:W19 把「复用已有字节码」变异回「无条件编译」,该变异让测试**真的执行了**
`ensure_compiled()` → javac + rmtree,作用在**共享字节码目录**上(sources.txt 时间戳可证)。
项目硬约束禁跑 javac/maven/docker。

**为什么 /tmp 文件副本拦不住**:副本隔离的是**被读的源码文件**,
但编译命令读的是**共享目录**、写的是**共享产物**,并且会调用**真实外部工具**。
文件级隔离与工具链隔离是两件事。

**补法**:harness 里加 **PATH 挡板**(把 javac / mvn / docker 指向失败桩),
使任何变异都不可能真的调起工具链。W19 已就位。

**事故处理本身是对的,值得照抄**:立即停止 → 取證(时间戳)→ 评估损害
(重抽的两份声明与事故前逐字段语义相同 ⇒ 零语义损害)→ 加挡板 → **自报而非隐瞒**。

**推广**:凡是「复用已有产物」的优化入口,都要想到它的变异态会**真的去生产那个产物**。
设计变异矩阵时先问:这个变异会不会触发外部副作用?会就先加挡板再跑。

### 做法 50:先分解分母、再给各类**权重**,最后才归因 —— 只分类不加权仍会下错全局结论(主控同一个数错四次)

**实测经过**:同一个「细胞足迹未知 416/546」的数,我连续四次修正定性:
1. 「76% 未知是真实覆盖天花板」
2. 「是产物世代错配的伪影」(发现 net 与 units 相差 13 小时)
3. 「主要是粒度错配」(看到 units 里有 `BanquetOrder` 这种裸类名,net 是 FQCN#method)
4. **「粒度 17% / 陈旧 83%」** ← 只有这次是对的

**前三次错在哪**:每次都**拿部分证据下全局结论**。第 3 次尤其典型 ——
我**已经分类了**(认出裸类名 vs FQCN#method 两种形态),但**没量各类占多少**,
看到几个样例就说「主要是粒度」。

**按层拆开后的真值**(只读,不跑引擎):
```
class        7 单元   0 在 net    0%
file        63 单元   0 在 net    0%
function   464 单元 119 在 net   25.6%   ← 345 个真缺口
integration  1 / service 1        0%
```
416 = **72 非 function 层(粒度,合理)** + **345 function 层真缺口(83%)**。
粒度只解释 17%。**第 3 次结论把主因说反了**,而且差点让下游 agent 按错方向行动(已及时撤回)。

**规约**:
1. **先分解分母**(按层/按类型/按来源),**再给每一类计数**,最后才归因。
2. **分类 ≠ 加权**。认出两种形态只是第一步;**必须量出各占多少**,否则「主要是 X」是猜的。
3. **样例只能提示假设,不能支撑比例结论。** 看到几个裸类名不足以说「主要是粒度」。
4. 结论被新证据推翻时,**立刻撤回并通知已收到旧结论的下游**,不要等自己想清楚全套再说。
5. 与做法 39(先分类再计数)同源;本条补的是**加权**这一步,以及**撤回要及时**。

### 坑 93:两个不同的「默认产物目录」—— env 覆盖让「只读分析」静默写进审计仓库(W19 实测,自己踩过)

**先纠正一个形状断言**:交接说 relation_net「没有任何路径覆盖入口」。实测**证伪**:
`findings_schema.target_path()`(findings_schema.py:47)读 `LAYERED_AUDIT_TARGET`,
零改动就能把整条图链驱动到 /tmp(实测 162 节点 / 198 边、536 单元 / 886 边)。
真阻塞不是「没入口」,是①入口要求手写 target.json 四个必填段,②`fn_static.main()` /
`file_static.main()` **无条件** `ensure_compiled()`(javac + rmtree)+ mvn + docker cp。
与坑 79 同族:「没有 X」是形状断言,先 grep 一遍别的载体再宣布。

**真缺陷在默认值分叉**,两个引擎的「产物目录」根本不是同一个:

| 读取点 | 无 `paths.artifacts_dir` 时默认 |
|---|---|
| `project.artifact_dir()`(project.py:92)→ class_static / fn_static / file_static | **`HERE` = 脚本所在目录(pilot/)** |
| `findings_schema.target_path().parent` → relation_net / build_agent_graph | **target.json 所在目录** |

默认绑定下两者重合,所以从来没人发现。一旦 `LAYERED_AUDIT_TARGET=/tmp/x/target.json`
而没写 `paths.artifacts_dir`:引擎写 /tmp/x,**抽取器写 pilot/**。
我实测就这样误写了 `pilot/class_declaration.json`(硬边界明令禁写)。

**损害评估与还原(照抄这套)**:
1. `git status` **看不见**它 —— `.gitignore:7` 有 `pilot/*_declaration.json`,是本地产物。
   **git 干净 ≠ 没被写**;判断有没有污染要比 mtime 与内容,不能只看 git。
2. 我在跑之前留了改前副本(/tmp),逐字段比对:**只有 `generated_at` 变,620 行声明数据逐字节相同**。
3. 用改前副本还原内容 + `touch -t` 还原 mtime,`cmp` 验逐字节相同。

**规约**:
- 探针 / 验证 / 新入口一律**显式 `--out` 到临时目录**,不依赖任何默认产物目录。
- 跑任何会写产物的脚本前,**先留改前副本**;gitignore 的产物没有 git 兜底。
- 加路径覆盖时,覆盖分支必须**绕开**会读绑定的函数(`artifacts_dir()` 会 `load_target()`,
  缺 target.json 直接抛)—— 否则「给了覆盖却仍要求绑定」,覆盖等于没加。

### 坑 94:PATH 挡板拦不住 `rmtree` —— 工具链沙箱化必须连 work_dir 一起(W19 二次事故,补做法 49)

做法 49 记了我第一次事故并说「PATH 挡板已就位」。**实测:挡板不够,我又毁了一次共享目录。**

**机理**:M11 把「复用字节码」变异回「无条件编译」→ subprocess 测试真的调 `ensure_compiled()`:
```python
shutil.rmtree(CLASSES, ignore_errors=True)   # ← 先删,挡板管不到
CLASSES.mkdir(...)
run(["javac", ...])                          # ← 挡板在这里才生效(exit 99)
```
javac 被拦住了,**删除已经执行**。`/tmp/jiuyan_fn/classes` 从 64 个 class 变成 **0 个**,
下游整条链静默产出「类 0 个 / 函数 0 个」且 **exit 0**(这本身又是坑 93 同族的假绿)。

**为什么第一版哨兵没报**:我只查 `sources.txt` 的 mtime。它确实变了(第一次事故),
但第二次事故的关键损害是**目录被清空**,mtime 查不出来。

**补法(已实测有效,25 条变异跑完共享目录 64 class 不变)**:
1. PATH 挡板:javac / mvn / docker → exit 99 的桩;
2. **沙箱 target.json 的 `paths.work_dir` 指向沙箱内**,并在跑 pytest 时注入
   `LAYERED_AUDIT_TARGET` —— 这样 `CLASSES` 落沙箱,rmtree 只删沙箱自己的东西;
3. 哨兵查**两个**量:sources.txt mtime **与共享目录 class 数**。

**恢复的诚实边界(不许说「零语义损害」)**:共享目录已被删空,无法原样恢复。
我从 `jiuyan/backend/target/classes`(maven 产物,0 missing / 0 stale)复制回去,
但它与 W6 的 javac 产物**不逐字相同**:463 条函数记录里 **29 条有差**
(`bytecode_size` / `branch_count` / `method_reference_calls` 等,含坑 61 记过的 SeedAssetInitializer)。
「重抽产物与事故前逐字段相同」只对**第一次**事故成立(那次真编译成功了,同源同 classpath)。
**需要 W6 那份字节码的人必须重新编译,我无权做。**

### 坑 95:`lru_cache` 钉住绑定解析 —— 守卫混用「进程内路径」与「subprocess 环境」必然假红(W19 自踩)

我的守卫 `test_relation_net_default_path_equals_explicit_binding_path` **单跑绿、全量红**。
按坑 92 的顺序排查,命中的是**第 2 类(模块级缓存)**,不是并发:

- `findings_schema.target_path()` / `load_target()` / `contract_path()` 全带 `@lru_cache(maxsize=1)`;
- `test_audit_entry.py:458` 用 `monkeypatch.setenv("LAYERED_AUDIT_TARGET", …NOPE.json)`;
  若缓存在那个窗口里被首次填充,**整个进程后续都钉在那个路径上**,env 还原也不解锁;
- 我的守卫一侧用 `rn.project_dir()`(**进程内、吃缓存**),另一侧跑 subprocess(**吃真实 env**)
  → 两侧读不同目录 → 产物字节不同 → 假红。实测差在 `counts.nodes`。

**修法**:守卫两侧路径**都从同一个 env 字典推**,全合成夹具自足,不调任何吃缓存的解析函数。
修后单跑与全量都绿(31 条)。**规约**:凡「进程内解析 + subprocess 执行」混用的等价性守卫,
都要先问解析结果是否被缓存;等价性比较的两侧必须同源取路径。

### 坑 96:「76% 细胞足迹未知」不是世代错配伪影 —— 同世代重生逐数相同,按层分解才归因(W19 实测,补坑 88)

坑 88 推测「76% 足迹未知主要是产物错配的伪影」。**我按它的要求同世代重生后实测:推论不成立。**

**做法**:三个抽取器全部从 jiuyan 源码/字节码重抽 → build_agent_graph → relation_net,
全链一次生成,并用新加的**内容指纹**校验同世代(`--check-generation` 返回「世代一致」)。

**结果与旧数逐数相同**:
```
cells_total 546 / cells_footprint_known 130 / unknown 416   (130/546 = 23.81%)
pairs_total 148785 / pairs_decidable 8385 / pairs_unknown 107952
candidates 9 / delta0 {consistent:0, unknown:9} / cycle_obstructions 0
frame_check.input_warnings = []
```

**按层分解分母(这才是归因)**:
- 130 个已知足迹细胞 **恰好等于** net 的方法节点数 25 HTTP_ENTRY + 77 SVC_OP + 28 REPO_OP;
  其中 119 个对得上 units、11 个是 graph_only。**全部是 function 层**。
- 未知的 416 = function 345 + **file 63 + class 7 + service 1 + integration 1**(减 1 个重复键)。
  即 **file/class/service/integration 四层一个都没有足迹** —— net 压根不建模它们。

**结论**:76% 是**建模 scope 边界**(关系网只画 controller/service/repository 的可执行方法),
不是世代错配。要涨这个数,得让 net 覆盖文件层/类层,或让 frame_check 承认这些层不在足迹模型域内
(后者是口径修正,前者是能力扩展)——**两条都不是「重生一次产物」能解决的**。

**规约**:把「疑似伪影」写成结论前,必须①同世代重生一次看数变不变,②按层/按类分解分母。
只做①会把结构边界误当伪影撤回;只做②会漏掉真错配。与做法 50(先分解再加权)同源。

### 做法 51:stdout 文案也是判定面 —— 未测量不许打印正面结论(W19)

三态铁律(硬约束 3)通常被理解成「返回值 / 产物字段」。**人读的那行字同样在判定面上。**

实测:`fn_static` 加了「复用已有字节码」入口后 `deployed_ok=False`(没取部署 jar、没比对),
而旧代码是 `if drift: … else: print("源码与部署产物 javap 输出逐字节一致")` ——
`drift` 为空是因为**根本没比**,于是打印了一句**正面结论**。
这与「字段名拼错得到一张通过的成绩单」同族,只是载体换成 stdout。

**修法**:`if not deployed_ok:` 先判,打印「部署产物未比对…漂移未知,不是无漂移」;
`elif drift:` 报漂移;`else:` 才允许说逐字节一致。`file_static` 的 test classpath 同样处理
(取不到就打印「未知…不是空 classpath」)。

**守卫**:变异 M9 **精确复刻旧缺陷态**(把三分支改回两分支),被指名守卫
`test_unmeasured_drift_is_not_reported_as_identical` 抓住(断言「逐字节一致」不在 stdout、
「未比对/未知」必须在)。反向:不带复用参数时编译路径照旧走,由
`test_compile_mode_still_compiles` / `test_file_static_compile_mode_still_compiles` 守住,不许过度跳过。

---

## 二十六、W15 第三轮补充(2026-11-15,反向失败模式:撤掉假阳性后真风险从报告里消失)

### 坑 97:留痕文件不是发布面 —— 闸门压下来的真风险,同事完全看不见(W15 实测,编排者查出)

**现象**:前两轮把 13 条未经确认/被否定的结论压出缺陷台账,留痕在各层
`*_rule_suspects.json` 与 `business_findings.json`,note 写得很清楚。但编排者实测
`pilot/audit_report.md` 对「待确认 / unconfirmed / 疑点 / 已证伪 / refuted」**0 命中**。

**后果**:价格篡改这个**真实的规范层风险**,从「CRITICAL 缺陷」变成了「同事完全看不见」。
撤掉假阳性是对的,但底下的真风险跟着一起消失了。北极星是「直接暴露代码有没有问题」,
**只在中间文件里、报告不提的风险,对使用者等于不存在**。

**根因**:我把「留痕」当成了「可见」。留痕在中间 JSON,**发布面是 audit_report.md**。
与坑 66(覆盖 ≠ 检测力)同族:不是数字错了,是**该被看到的东西没被展示**。
也是坑 84 的镜像 —— 那轮是「同源假阳性没撤干净」,这轮是「撤干净了但风险没露出来」。

**修法(引擎零项目字面量)**:`target.json` 新增 `suspect_sources` 声明
(layer / file / unconfirmed_key / refuted_key,四个来源:function、class、service、integration);
`aggregate_findings.py` 新增 `collect_suspects()` 收集归一 + `calibers()` 三口径纯函数
+ `_suspect_section()` 渲染两节 + 头条并排报。产物 `root_causes.json` 新增 `suspects` 块
(机器可读,不只散文)。

**实测差值**:报告 **190 → 764 行**;新增「八、待确认疑点」**8 条**、
「九、已证伪(只留观测)」**5 条**;关键词命中 **0 → 31**;
头条三口径「确认缺陷 73 / 待确认疑点 8 / 已证伪只留观测 5」,并显式声明后两者**不在台账 90 条内**;
台账数字一字未变(90 = defect 73 + positive 15 + observation 2),硬违规仍 1 条(同一条既有越界)。
全量 **1188 → 1227 passed + 9 subtests, 0 failed**(隔离法:去掉我的新文件 1193,我 +34)。

**防复发守卫(已机械化)**:`pilot/tests/test_suspect_visibility.py` **34 条 + 9 个变异全 KILLED**。
**通用纪律**:撤掉一条结论时,必须同时回答「**它现在在哪里能被使用者看到**」。
留痕 ≠ 可见;中间产物的 note 写得再好,发布面不提就等于没写。

### 坑 98:疑点有四种「未测量」形态与三种记录形态 —— 只锁一种就会静默报 0(W15)

**三种上游记录形态**(必须都归一,认不出的计数外露,坑 17 同族):
- A held 形 `{rule_id, oracle, finding:{id,severity,title,evidence}}` —— fn / integration / class 闸门
- B 桶形 `{rule, table, api_http, api_note, oracle}` —— class `rule_oracle_buckets`,**上游本来就没有 id**
- C 结论形 `{id, severity, title, detail, evidence, oracle}` —— service `business_findings`

**四种「未测量」形态,全部 INCOMPLETE + 告警,不得静默当 0**(承坑 46「缺失的所有物理形态」):
① 项目绑定未声明 `suspect_sources`;② 声明了但文件不存在;③ 文件在但桶键缺失(形态漂移);
④ 记录形态认不出。另加第五种 integrity 检查:**oracle 状态与所在桶不符** → `state_mismatches`。
实测当前产物:channel_status=OK、warnings 0、missing 0、unrecognized 0、mismatch 0。

**空桶是合法输入,不得误红**:两桶都空 → 测得 0、OK、零告警,章节仍在并显式写「本桶 0 条」。
守卫 `test_empty_buckets_are_legal_and_not_incomplete` / `test_cli_empty_buckets_are_legal`,
反向变异 M5(空桶误判 INCOMPLETE)必须红。**双向守卫的两个方向在同一输入上可同时满足**(坑 59)。

**渲染层的三个诚实性细节**:
① B 形无 id → 用预期原文当标识,**不编造 id**(报告里三条 class 桶形记录原先都显示
「(无 id,见预期原文)」,无法区分);
② `detail` 与 `observation` 内容重复 → 删 detail,保留结构化证据(全文在 root_causes.json);
③ **refuted 条目的 severity 不得标「风险评级」** —— 预期已被否定,没有风险,
改标「原结论严重度(已撤销)」;只有 unconfirmed 才标「风险评级(不是缺陷严重度)」。

### 做法 52:三口径并排报 + 分子计算提成纯函数,双向变异才咬得住(W15)

**三口径必须显式互斥,不得混算**:
```
台账 90 条 = 确认缺陷 73 + 正面结论 15 + 观测 2      （三个 kind）
另有 待确认疑点 8 条 / 已证伪只留观测 5 条           （不在台账 90 条内）
```
`unconfirmed` 是「**不知道是否成立,需业务方确认**」——**既不是缺陷,也不是已排除**。
计入缺陷分子 = 编造预期(正向失败);写成「已排除」= 吞掉真风险(反向失败)。**两个方向都要有守卫。**

**关键实现纪律**:把分子计算从 `main()` 里提成纯函数 `calibers(records, suspects)`,
否则「疑点不得进缺陷分子」这条只能靠 subprocess 变异咬,同进程 monkeypatch 够不着(坑 2/做法 36)。
提成纯函数后 M3(把 unconfirmed 加进 defect)同进程直接 KILLED。

**双向变异矩阵(9/9 KILLED,0 存活;基线先绿)**:
| 变异 | 方向 | 载体 | 指名守卫 |
|---|---|---|---|
| M1 去掉待确认疑点章节 | 风险消失 | 同进程 | `_guard_both_sections` |
| M2 去掉已证伪章节 | 留痕消失 | 同进程 | 同上 |
| M3 疑点计入缺陷分子 | 编造预期 | 同进程 | `_guard_defect_numerator` |
| M4 unconfirmed 写成「已排除」 | 吞掉风险 | 同进程 | `_guard_not_excluded` |
| M5 合法空桶误判 INCOMPLETE | 误红合法输入 | 同进程 | `_guard_empty_bucket_ok` |
| M6 文件缺失静默当 0 | 未测量洗成 0 | 同进程 | `_guard_missing_file_incomplete` |
| M7 删第八节 | 风险消失 | **文件级 + subprocess** | `_assert_cli_visible` |
| M8 删第九节 | 留痕消失 | **文件级 + subprocess** | 同上 |
| M9 疑点计入分子 | 编造预期 | **文件级 + subprocess** | 同上 |

**沙箱可移植性**:`all_findings.json` / `layers.yaml` / `target.json` 都是 tracked,
suspects 文件用合成夹具 → CLI 守卫**不依赖任何 gitignore 产物**,fresh clone 可跑。
**通用纪律**:凡「压下去 / 撤掉 / 降级」一个结论,交付必须同时包含
①它去了哪个清单 ②那个清单在**发布面**可见 ③两个方向的变异都咬得住。

### 坑 99:未经验证的**撤回**,只是另一个未经验证的断言 —— 我把一个正确的数撤错了两次(主控自踩,W19 推回)

**经过**:frame_check 报「细胞足迹已知 130/546(76% 未知)」。我对这个数连续改了四次定性:
1. 「76% 是真实覆盖天花板」
2. 撤回 →「是产物世代错配的伪影」(发现 net 与 units 的 mtime 差 13 小时)
3. 再撤回 →「主要是粒度错配」(看到 units 里有裸类名、net 是 FQCN#method)
4. 再撤回 →「粒度 17% / 陈旧 83%,345 个 function 层是真缺口」(按层拆了分母)

**W19 用实测推回**:它建了**独立参考实现的世代指纹**,在**验证一致的同世代**上重跑全链,
得到 **130/546、148785、8385 —— 与我宣称「被污染」的数完全相同**。
并给出**结构性证明**:130 个已知足迹细胞 **恰好等于 net 的方法节点总数**
(25 HTTP_ENTRY + 77 SVC_OP + 28 REPO_OP),**全部 function 层**;
file 63 / class 7 / service 1 / integration 1 **零足迹,是设计如此** —— net 就是方法层可执行图。

**结论**:那个数**从未被污染**。我的第 2、3、4 次定性**全错**,
**最初的第 1 次反而最接近正确**(只是原因该说「结构性范围」而不是「能力天花板」)。

**为什么这比「报错数」更值得记**:
- 我以为「撤回」是谦逊、安全的动作 —— **错了**。撤回同样是一个断言,同样需要证据。
- 我每次撤回都**通知了下游 agent**,它们差点按错方向行动(一次已发出、及时追回)。
  **错误的撤回会污染别人的工作方向,危害与错误的断言相同。**
- 三次撤回的共同病根:**拿局部证据(mtime 差、几个样例、按层计数)当全局结论**,
  而**没有做那个能一举判定的实验**(同世代重跑 + 指纹校验)。

**规约**:
1. **撤回前先做能判定真伪的实验**,别用「发现了另一个可能原因」代替「证明了原结论错」。
   本例中那个实验很便宜:重跑一次 + 比对指纹。
2. **区分「存在其他可能解释」与「原解释被证伪」**。前者只够降低置信度,不够撤回。
3. 已经发出的错误结论(含错误撤回),**立刻追回并说明**,不要等自己想清全套。
4. **让下游用实测推回你** —— 这次是 W19 的独立指纹实现救了这个数。
   若我只听「同意」,这个错会留在最终报告里。
5. 与做法 50(先分解分母再加权)同源,但本条更狠:**加权也做了(第 4 次),仍然错**,
   因为**分解本身选错了维度**(按 layer 分解,而真正的维度是「是否在可执行图范围内」)。
   **分解维度也要用证据选,不是想当然。**

### 做法 53:把「建模域」写进产物 —— 域外细胞是 not_applicable,不是 unknown(W19,补坑 96/99 的机械化补法)

坑 99 定了性(那个数从未被污染,是结构性范围),但**定性不能防复发**。这里给可机械化的补法与决定性证据。

**决定性证据:把 345 个「function 层却不在 net」的细胞按包拆开**(同世代产物,只读):
```
dto 203 | entity 104 | config 16 | exception 15 | mapper 6 | banquet 1     ← 不在 net
service 77 | controller 25 | repository 17 = 119                          ← 在 net
```
回源码核实 `relation_net.py:183-188`:建点**按类名后缀**
`*Controller→HTTP_ENTRY / *Service→SVC_OP / *Repository→REPO_OP`,其余**不建点**。
所以 345 不是陈旧、不是「范围不足待补」,是**声明过的建模域之外** ——
重生成一万次也不会变(实测同世代重生后仍是 119/464)。

**补法:生产者把域写进产物**(零项目字面量,只声明框架惯例后缀):
```python
net["modeling_domain"] = {
    "cell_key_form": "FQCN#method",
    "node_types": [...], "method_node_types": ["HTTP_ENTRY","SVC_OP","REPO_OP"],
    "class_suffixes": ["Controller","Service","Repository"],
    "scope_note": "域外单元的足迹是 not_applicable(域外),不是 unknown(域内未测)"}
```
消费者据此把域外细胞标 **not_applicable** 并从「未知足迹」分子里剔除,
或按层分开报覆盖率。**三态在这里有个常被忽略的第四面**:
`unknown` = 域内没测出来(待查,是真缺口);`not_applicable` = 压根不在域内(不是缺口)。
把域外算成 unknown 会**虚增待查队列**,同时**掩盖真正的域内缺口**(本例 119/464 里
域内该覆盖却没覆盖的部分),两个方向都误导。

**守卫必须非空泛 + 双向**(坑 2 / 坑 78):
- 先断言**真有方法节点**(`assert method_nodes`),否则「每个节点都落在声明后缀里」恒真;
- 正向:三个后缀**各自真建出了点**(不是写着好看);
- 反向:非后缀类(`SynDto`)**绝不建点**;
- 变异 M26「声明里删掉 Repository」与 M27「建点逻辑 Service→Svc 但声明不改」
  **两个方向各被指名守卫抓住** —— 声明漂移与行为漂移都逃不掉。

**边界(诚实报)**:`frame_check` / `audit_entry` 是冻结/他人所有的文件,
**消费侧接线需要所有者批准**;本轮只做到「生产者声明域 + 守卫锁住声明与行为一致」,
以及**按层分开报覆盖率**(function 119/464=25.65%,class 0/7,file 0/63,service 0/1,integration 0/1)。
**没有**把汇总的 22.2% 当成能力结论发布。

### 做法 53:做法 44 的引用形态清单要再加一维 —— **容器形状**(主控第六次同族误报)

做法 44 列了五种**引用形态**(basename:行号 / JSON pointer / 产物#键 / 需求编号 / 符号锚点)。
本轮又踩一次,病根不在引用形态,而在**容器形状**:

```python
nodes = net.get("nodes")
nk = {x.get("key") ... for x in nodes} if isinstance(nodes, list) else {}   # ← 错
```
`nodes` 实际是 **dict(以 unit_key 为键)**,不是 list-of-records。
`else {}` 让交集算成 **0**,我据此差点报「units 与 net 完全不相交」。
正确写法是 `set(nodes)`(取它的键),那样得到 **119**。

**规约**:计数前先 `print(type(x).__name__, len(x))` **同时看类型和长度**;
`list[dict]` 与 `dict[key→dict]` 的遍历语义完全不同,`for x in d` 给的是**键**不是记录。
**别写 `else {}` / `else []` 这种静默兜底** —— 它把「形状不认识」变成「空集」,
于是「不认识」被报成「零」。宁可抛异常。

与坑 25(先自证参考实现)、做法 39(先分类再计数)、做法 44(引用形态)同源。
本会话我在这一族上错了六次,**每次都是静默兜底或维度选错把「不知道」变成了「零」**。

### 坑 100:子串锚点锁不住**极性** —— notes 加了守卫之后,把「不」改成「也」仍 41/41 全绿(V16 第二轮实测)

坑 82 报「notes 零守卫」,W18 补了 `test_notes_state_every_honesty_claim`:
语义锚点外生于被验对象(放在测试的 `NOTE_CLAIMS` 里)+ 钉条数 + 每条配可证伪行为断言。
**方向对,粒度不够**:锚点是 `assertIn(anchor, note)` 的**子串存在性**,不锁否定词。

**V16 新锚点(保留全部子串锚点,只翻极性)**:
| 变异 | 改法 | 结果 |
|---|---|---|
| N1 | `缺 modifiers 字段的行**不**在此列` → `**也**在此列` | **SURVIVED,41 passed + 4 subtests,零红** |
| N2 | `本产物的判定力**低于**完整输入` → `**不低于**完整输入` | **SURVIVED,同上** |

两个变异的 `NOTE_CLAIMS` 锚点**全部仍满足**、条数不变(实测 `anchors_missing=[]`、`count_ok=True`)。

**危害探针(声明与机器字段互相矛盾,可核)**:
- N1:产物 note 说缺字段行「也在此列」,而机器 `sources[0].empty_modifiers == []`、
  `skipped_reasons == {"modifiers_missing": 1}` —— **声明直接对着人撒谎**。
- N2:note 说判定力「不低于完整输入」,而机器 `conflicting_keys == 1`、
  `ar.load_members(...)[key] is None`(消费者判不出来)—— 判定力**确实**下降了。

**根因**:子串锚点把「语义」降维成「词出现过」。否定词、比较方向、量词(全部/部分)都不在锚点里。
与坑 2(同义反复守卫)同族:**声称守住语义,断言只守词面**。

**规约**:
- 口径声明的守卫必须钉**极性**:把否定词/比较词本身写进锚点(`"不在此列"`、`"低于完整输入"`),
  而不是只钉名词(`"empty_modifiers"`、`"conflicting_keys"`)。
- 更强的做法:声明与机器字段**交叉断言** —— `skipped_reasons` 非空时 note 不得声称缺字段行在复核清单里;
  `conflicting_keys>0` 时 note 不得声称判定力不下降。让 prose 与 machine 互为对照。
- 变异矩阵必须包含「**保留全部锚点的极性翻转**」这一类;只测「删锚点/换名词」会给出虚假安全感。

### 坑 101:粗变异被**相邻不变式的守卫**顺手杀掉 ≠ 该不变式有守卫(V16 第二轮实测,坑 78 的新形态)

W18 把 `--rules` 纳入防覆盖保护集:`protected = [path for _, path in inputs] + [args.rules]`。
V16 造粗变异 **C1**:`clobbers` 里 `for path in inputs:` → `for path in inputs[:1]:`
→ **KILLED,但指名红只有 `test_out_clobbering_rules_config_is_refused` 一条**。
杀它的是 **rules 保护**那条守卫(protected 被截断后 rules 落到第 2 位),
**不是多载体守卫** —— 41 条里**没有任何一条**测「out == 第二个 `--input`」。

**精确锚点 C1b**(只摘掉第二个及以后的 `--input`,保留 rules 与第一个输入):
```python
protected = [path for _, path in inputs][:1] + ([args.rules] if args.rules else [])
```
→ **SURVIVED,41 passed + 4 subtests,零红**。

**危害探针(数据丢失实证)**:两个输入,`--out` = 第二个输入:
| 引擎 | rc | 第二输入 sha(前→后) | 被毁 |
|---|---|---|---|
| 原版 | 2 | `e02227d76e42` → `e02227d76e42` | 否 |
| C1b | **0** | `e02227d76e42` → `e6b4ca5258a7` | **是** |

多载体并集正是这个生产者的**主用法**,而它恰好是零守卫的那一格。

**根因**:一个变异同时破坏多个不变式时,**先红的那条守卫会掩盖其余不变式的无牙**。
坑 78 说的是「粗变异被别的断言杀掉」,本条补的是:**被杀掉之后必须核「红的是不是我声称守的那条」**;
红名对不上,就要把变异收窄到单因子再打一次。

**规约**:
- 变异被杀后,**比对指名红名与该变异意图打击的不变式**;不一致就收窄锚点重打。
- 集合类保护(`protected` / 白名单 / 候选列表)必须有**逐元素**守卫:
  第 1 个、第 2 个、最后一个、以及非 `--input` 成员(rules)各一条。
- 报「N/N 全杀」时并排报**每个变异红的是哪条**;只报计数会把这种掩盖当成安全。

### 做法 54:防覆盖不变式的**文件身份等价类清单** + 等价变异要自己扩维度(V16 第二轮)

**一、「原版本来就挡得住」必须用验证者自己冻结的原版副本对跑,不许采信声称。**
V16 用第一轮冻结的 `/tmp/v16/mut/baseline`(sha `f2c64539…`,独立于 W18)与新版对跑 17 个场景,
全部用**规范 members 形状输入 + 不传 --rules**,确保执行真的到达写入分支(坑 90 的自踩教训):

| 形态 | 原版 | 新版 | 判定 |
|---|---|---|---|
| 绝对输入 + 相对 out(同文件) | rc=2 挡住 | rc=2 | W18「本来就挡得住」**属实** |
| out 是输入的符号链接别名 | rc=2 挡住 | rc=2 | 同上**属实** |
| **out 是输入的硬链接别名** | **rc=0 毁证据** | rc=2 | W18 自查挖出的真洞,**已修** |
| out 同 inode + 路径含 `..` | rc=0 毁证据 | rc=2 | 已修 |
| **out 大小写变体(case-insensitive FS)** | **rc=0 毁证据** | rc=2 | **V16 新发现的第四种形态**,已修 |
| **out 覆盖 `--rules`** | **rc=0 毁配置** | rc=2 | 原版不保护 rules,已修 |
| out 覆盖第二个输入 | rc=2 | rc=2 | 行为对,但**零守卫**(坑 101) |
| out 不存在 + 父目录符号链接别名 | rc=0 | rc=0 | **正确放行**:写入创建新 inode,不可能毁输入 |
| out 经符号链接父目录解析到输入 | rc=2 | rc=2 | 正确拒绝 |
| out 父目录是**目录硬链接** | — | — | **APFS 不可达**:`os.link(dir)` → `Operation not permitted`,如实报 N/A |
| 合法:同目录不同文件 / 符号链接指向非输入 / 尚不存在 / 悬空符号链接 / 非输入文件的硬链接 / 同 basename 不同目录 | rc=0 | rc=0 | **零误红** |

`--rules` 的硬链接 / 符号链接 / `..` 三种别名形态另测:全 rc=2、配置 sha 逐字未动。
**结论:新版在所有可达身份等价类上都挡住,且不误红合法输入。**

**清单(可直接照着写守卫)**:字符串规范化(相对/绝对/`..`)、符号链接、**硬链接**、
**大小写变体(case-insensitive FS)**、Unicode 规范化、**非 `--input` 的输入(rules)**、
**多输入的第 2..N 个**、out 不存在(不可达,应放行)、目录硬链接(多数 FS 不可达,报 N/A 不判红)。

**二、等价变异的判别空间要自己扩维度,别只用被验方给的那个。**
W18 声称 `member_row_ok ⟺ skip_reason` 在 **219** 个行形态上零分歧。V16 独立复算:
`6×5×7=210 + 3 缺键 + 6 非对象/额外键 = 219` ✓,分母断言 `assertEqual(len(rows), 219)` 确实在测试里 ✓。
V16 自己扩到 **23408** 个形态:值类型 18 种(含 `True/False/0/3.5/()/set()/b"bytes"`/纯空白)
× 3 字段笛卡尔积 × **4 种 spec**(upstream / net / 自定义键名 / **三字段同键的退化 spec**)
+ 键存在性 8 子集 + 9 种非对象 + 额外键/键名不符 → **分歧 0**。
`C2`(删 `resolve()` 相等分支只留 `samefile`)在 17 个身份场景上**行为零差** → 等价变异,
前提写明:out 存在时 samefile 覆盖全部身份等价类,out 不存在时写入创建新 inode。

**三、我自己的探针先错了三次(坑 25,全部自证后修正,未记到被测方账上)**:
① 把**非输入**的目标文件也列入「被毁」监视 → 合法写入被误判成 clobber(2 例);
② 把**不可达形态**(目录硬链接)判成 BAD 而不是 N/A;
③ 跨沙箱比对产物时把沙箱绝对路径也算进去(第一轮已踩,本轮复用归一化)。
**规约**:先声明「监视哪些文件」= 输入产物集合,不含 out 的合法目标;不可达形态单列 N/A。

### 坑 102:极性翻转能穿过「关键词子串 + 条数」守卫 —— 锚点必须带左上下文,把否定词/方向词本身当断言对象(W18 实测,V16 两轮查出)

我给产物 `notes`(口径声明)补了守卫:逐条钉关键词子串 + 钉条数。V16 第二轮实测**两个极性篡改变异双双 SURVIVED 41+4**:

| 变异 | 原文 → 篡改 | 为什么穿过 |
|---|---|---|
| N1 | 「缺 modifiers 字段的行**不**在此列」→「**也**在此列」 | 锚点是 `空串/package_private/empty_modifiers/skipped_reasons`,**一个都没少** |
| N2 | 「判定力**低于**完整输入」→「**不低于**完整输入」 | 锚点是 `conflicting_keys/sources_without_members/不得当成等价口径`,全在;条数也没变 |

**根因**:翻转后的字符串**仍然包含**原关键词 ——「不低于」含「低于」,「也在此列」不含「不在此列」但我的锚点里压根没有那个「不」字。
**只钉关键词 = 钉住了话题,没钉住立场。** 而口径声明的价值恰恰在立场(否定/方向),不在话题。

**修法**:短语带**左上下文**,让翻转必然破坏子串:
`本产物的判定力低于完整输入`(翻转成「判定力不低于完整输入」就不再包含它)、
`缺 modifiers 字段的行不在此列`、`是转录，不是新测量`、`修饰符冲突不挑赢家`、`喂本文件会被 select_source 拒掉`。

**并且给量具本身加自证守卫**(坑 25 双向校准):
`test_polarity_phrases_do_not_match_their_own_flips` 断言**每个极性短语都不被它的翻转形态包含**。
若我当初只钉「低于」,这条自证会立刻红 —— 它防的是「锚点选错导致守卫是摆设」,不是防被测系统。

实测:N1–N5 五种极性翻转(不在此列/低于/不是新测量/不挑赢家/会被拒掉)**全部 KILLED**,
指名红名都是 `test_note_polarity_is_pinned_not_just_keywords`。
**代价要说清**:这种守卫对**合法改写也严格**(措辞变了就要同步改守卫),这是有意的 —— 口径声明是契约,改它必须过复核。

### 做法 55:变异被杀 ≠ 目标守卫有牙 —— 必须核「红的是不是我想让的那条」,并造只靠该因子才能杀的守卫(W18)

V16 的关键观察,我自己复现了:**C1**(`clobbers` 只遍历 `inputs[:1]`)被杀,
但**唯一红名是 `test_out_clobbering_rules_config_is_refused`** —— 因为 `protected` 里 rules 排在输入之后,
砍掉遍历就同时暴露了 rules,**旁边那条守卫帮忙杀了它**。这是坑 78 的虚假安全感:
「变异被杀」让我以为多输入保护有守卫,其实没有。

**判别性变异 C1b**:`protected` 只取**第一个** `--input` 但**保留 rules** → rules 守卫仍绿,
多输入无人守 → **SURVIVED 41+4**。危害实证(不是推理):
```
C1b 变异版: rc=0  第二个输入 sha d6cf24f42053 -> d07787d6739a  数据丢失=True
我的原版  : rc=2  第二个输入 sha d6cf24f42053 -> d6cf24f42053  数据丢失=False
```
**代码本来就是对的,缺的是守卫** —— 这两件事必须分开报。

**补法**:守卫要**只靠该因子才能杀**。`test_out_clobbering_any_later_input_is_refused`
造 **3 个输入**,用 subTest **逐个**把 out 指向第 0/1/2 个,各断言 rc=2 + sha 不变。
C1b 下第 0 个仍绿、第 1/2 个红 → 指名红名**只有这一条**,rules 那条全程绿。

**harness 也要升级**:每个变异记 `target_guard`,跑完断言**它亲自在红名集合里**,
不在就打印「被旁边守卫代杀」。本轮 **9/9 缺陷态全杀,且 9/9 目标守卫亲自红**。

**双向不能只测「杀缺陷态」**:另造两个**守卫过宽**变异 ——
C3 按 basename 禁(误红「同 basename 不同目录」)、C4 按 `st_nlink>1` 禁(误红「硬链接指向非输入文件」),
各由**对应的反向合法用例**指名抓住。这证明 6 种合法形态的守卫不是装饰:
同目录不同文件 / 符号链接指向非输入 / 尚不存在 / **悬空符号链接** / **非输入文件的硬链接** / **同 basename 不同目录**。

**等价变异不去杀**:C2(删 `resolve()` 相等分支只留 `samefile`)17 场景行为零差,
SURVIVED 是正确结果(V16 已定性,我复现一致)。**把等价变异当洞去修,会制造过度设计。**

**工具链与空对照**(做法 49 / 坑 92):harness 加 PATH 挡板(javac/java/mvn/docker/psql/gradle → exit 127 失败桩),
本轮触发 0 次(本生产者不调工具链,挡板是防御性的);每次先跑**空变异对照**(47+7 全绿)才认后面的数。

### 做法 56:受限刷新发布物的六步协议 —— 并发在飞时,差值必须可归因(W19,补做法 46 的操作面)

做法 46 定了「修生成器 ≠ 刷新发布物」。本轮刷新三个图产物时,把**并发安全**与**可归因**做成六步:

1. **先备份再写**,并 `cmp` 验备份逐字节相同。共享产物没有 git 兜底
   (`pilot/*_declaration.json` 被 gitignore;图产物虽被跟踪,但工作树本就有他人在飞改动,
   `git checkout` 会连带毁掉别人的工作)——**备份是唯一安全回退路径**。
2. **刷新前先量全量基线**,记下失败名与**错误签名**。本轮基线 1 failed / 1249 passed,
   红的是 `class_contract_diff.py:349 AttributeError: 'function' object has no attribute 'warrant_split'`(W15 在飞)。
3. **只写自己拥有的产物**,写完**核别人的产物 mtime 原样**:
   声明产物(W18 正在用 fn_declaration 做 members 测量)、findings/suspects/台账(W15 正在逐条裁定)。
   本轮实测三份声明 mtime 未变(14:39:05 / 00:55:26 / 00:54:40)。
4. **新字段要核「非空泛」,不只核「存在」**:`modeling_domain` 在产物里 ≠ 它说得对。
   要拿真实节点回验:130 个方法节点、后缀分布 Controller 25 / Service 77 / Repository 28、
   **每个节点都落在声明后缀内**。否则就是坑 2 的同义反复守卫换了个载体。
5. **退出码直接取,不过管道**:`cmd | tail; echo $?` 拿到的是 `tail` 的 rc,不是 `cmd` 的。
   本轮 `--check-generation` 用 `cmd > file 2>&1; echo $?` 取到**真实 rc=0**(net↔units 与 net↔edges 都一致)。
6. **刷新后重测,比对失败名 + 签名 + 计数三项全等**才算零退化。
   本轮改前改后都是 1 failed / 1249 passed / 12 subtests、同一测试名、同一 AttributeError 签名
   ⇒ 退化 0。**只比「红了几条」不够**,数目相同的两个不同的红也是回归。

**归因隔离的便宜做法**:读那条红测试**它读哪些文件**。本轮它只读
`class_declaration / class_contract / class_observation / class_scenarios` + `class_contract_diff`,
**不读我刷新的三个产物** ⇒ 结构上不可能被我影响,不必等重测就能先排除自己。
与坑 92(先查空对照)、坑 5(先归因再报)同源,本条补的是**并发在飞时的操作顺序**。

---

## 二十七、W15 第四轮补充(2026-11-15,两扇无闸门开单门 + 45 条逐条裁定:A/B 判据来源二分)

### 坑 103:关键词扫描分不开「技术可自证」与「业务预期」—— 判据必须落在 warrant 声明上(W15)

**现象**:编排者用关键词扫 78 条 defect 得「断言业务预期但无 oracle」**45 条**,自己判定该数不可信 ——
绝大多数是可由代码自证的安全缺陷(无鉴权、上传不嗅探、XFF 伪造、CORS 过宽),**不需要业务 oracle**。
按那个数去装闸门会把 A 类一起压掉 = **漏报,比假阳性更危险**。

**可操作判据(三问,按序;本轮用它逐条裁定 45 条)**:
1. **Q1 域替换**:把被测系统换成任意 CRUD 应用,这条断言是否仍是缺陷?
2. **Q2 权威出处**:这条「应该」能否指向通用权威(CWE / 语言规范 / 平台语义 / HTTP 规范),
   而**不必引用本项目的业务量**?
3. **Q3 混合裁决**:若两者都像,问「**撤销该业务规则后断言是否仍成立**」——
   仍成立归 technical,不成立归 business。

**机器可执行形式**:每个开单条目必须声明 `warrant`:
`{kind: technical, authority, domain_swap: holds, basis, evidence}` 或
`{kind: business, domain_swap: fails, basis, evidence, oracle}`。
闸门按 kind 分流:technical 直接放行(**绝不压**)、business 走 `oracle_gate.split`、
**未声明一律保守压住并标「未裁定」**(不静默开单也不静默丢弃)。
守卫逐条核:technical 的 authority/basis/evidence 非空且 domain_swap=holds;
business 的 domain_swap=fails 且 oracle 确认状态合法;`add()` 的每个 id 都在 WARRANTS 里(门普查)。

**实测裁定分布(45 条 = file 18 + service 23 + class 差分C/D 4)**:
**A 27 条 / B 7 条 / 非缺陷 11 条**。B 7 条 = refuted 2 + unconfirmed 4 + confirmed 1。
台账 **90→86**、defect **73→69**、根因 **69→65**、待确认疑点 **8→11**、已证伪 **5→6**;
**CRITICAL 4→4、HIGH 15→15 一个没动**(A 类零漏报),只 LOW 11→9、MEDIUM 43→41;
共有条目内容变化 **0**、硬违规 1→1。

**两条被证伪的 B(与 address/price 同族,新发现)**:
- `FILE-VALIDATION-STATUS-SPLIT`:断言「400 与 422 是同一类参数非法,状态码漂移」。
  **AGENTS.md:153-156 明文把 400 定义为参数错误、422 定义为业务校验失败(两个不同类别)**;
  业务校验对照.md:14 记「一致」,:24 更明确记「套餐商品不可重复 … Service **422** … 一致」。
  原结论的前提被文档直接否定 → refuted。
- `SVC-PAGINATION-COUNT-DISCARDED`:断言「count(*) 是纯开销,前端从不读 total」。
  **AGENTS.md:128-140 的分页契约明文要求返回 total/totalPages** —— 算 total 是履约不是浪费。
  「别让后端算一个没人要的数」这条建议与明文契约相反 → refuted。
  (底层 Seq Scan 成本是真的,但已由 `index.idx_products_name_unusable` 认领,不重复开单。)

**B 类有 confirmed oracle 照样开单(阳性对照,证明闸门不过度压制)**:
`FILE-ERROR-BODY-SHAPE-INCONSISTENT` —— AGENTS.md:100「所有接口必须返回统一 JSON」是明文契约,
499 空 body 违背它 → confirmed → **保留为 defect**。

**通用纪律**:装「业务预期闸门」前必须先有**可操作的 A/B 判据并逐条声明**,
否则闸门会按「有没有 oracle」一刀切,把技术缺陷一起压掉。**判据本身要能被守卫核**(authority 非空、
domain_swap 与 kind 一致),不能只是一段描述。

### 坑 104:business 缺 oracle 时退化成「无声明即客观事实」的放行分支(W15 自踩,守卫抓住)

**现象**:`warrant_split` 第一版对 `kind=business` 直接 `split(warrant.get("oracle"), …)`。
而 `split` 的既有口径是「**未声明 oracle → 放行**」(为的是不误伤语言级客观事实)。
于是 `{kind: "business"}`(声明了是业务预期、但没给 oracle)**被放行开单** —— 恰好是本闸门要防的那件事。

**实测**:`test_business_without_oracle_is_held` 红,`kept == [F]` 而应为 `[]`。
真实配置里每条 business 都带 oracle,所以**产物差值为 0、已武装未触发**;
但任何后来者补一条 business 声明而忘了 oracle,就会静默开单。

**修法**:business 分支缺 oracle 时**显式构造** `{source: unspecified, confirmation: unconfirmed,
note: "声明为业务预期但未给 oracle 来源 —— 按未确认处理"}`,不再退回 split 的缺省口径。

**根因**:复用一个函数时,把它的**缺省语义**一起继承了,而新调用点的缺省语义应当相反。
`split(None)` 的「放行」对老门是对的(无声明=客观事实),对「已声明是业务预期」是错的。
**通用纪律**:复用带缺省分支的函数前,先问「我的调用点在缺省输入下应当是什么行为」;
若与既有缺省相反,必须在新入口显式构造输入,不能依赖下游缺省。

### 坑 105:同名遮蔽 + 沙箱依赖闭包过期 —— 引擎模块被项目侧同名函数遮住(W15)

**现象一(遮蔽)**:`class_contract_diff.py` 第一轮写了 `from class_rules import oracle_gate`
(那是个**函数**),第四轮要用引擎模块 `engine/oracle_gate.py` 的 `warrant_split`,
直接写 `oracle_gate.warrant_split(...)` → **`AttributeError: 'function' object has no attribute 'warrant_split'`**,
`class_diff.py` rc=1。修法:引擎模块另起别名 `import oracle_gate as warrant_gate`
(不能改函数名 —— 已有守卫按 `ccd.oracle_gate` monkeypatch)。

**现象二(沙箱闭包过期)**:修好遮蔽后全量仍 **5 红**,全在我自己第一轮的
`test_nullability_oracle_gate.py`。原因:它的文件级沙箱只拷 5 个项目侧模块,
而 `class_contract_diff` 现在新增了引擎依赖 → 子进程 `ModuleNotFoundError: No module named 'oracle_gate'`。
修法:沙箱清单加 `SANDBOX_ENGINE_MODULES = ("engine/oracle_gate.py",)` 并按相对层级拷入。

**根因**:① 项目侧函数与引擎模块同名,import 语义决定谁遮蔽谁;
② **沙箱的依赖闭包是手工维护的清单,源码新增依赖时它不会自动跟着变**(坑 11 多载体同族)。
**通用纪律**:给模块新增跨目录依赖时,grep 所有**手工维护的拷贝清单**(沙箱/打包/部署)一并补;
同名符号跨层复用时先确认绑定的是模块还是函数。自己的红必须自己闭环,不得报成集成回归。

### 做法 57:门普查要按「生产者」而不是按「层」—— 41 条来自 8 个生产者,不是 2 扇门(W15)

**实测**:任务说「file 18 + service 23 = 41 条未裁定」。按 `all_findings.json` 的 `source.file` 逐条回溯:
file 18 条**全部**来自 `file_findings.json`(单一门 `engine/file_diff.py`);
service 23 条来自 **8 个生产者** —— `findings.json` 10(contract_diff,我的门)、
`business_findings.json` 1(已有闸门)、`concurrency_findings.json` 3、`config_findings.json` 4、
`orm_findings.json` 2、`perf_findings.json` 1、`property_binding_findings.json` 1、`transaction_findings.json` 1。
**「两扇门」只覆盖 28/41**,其余 13 条属 6 个探针生产者。

**后果与处置**:7 条 B 类里我只能闸住 5 条;`SVC-PAGINATION-COUNT-DISCARDED`(perf_load.py)
与 `SVC-POST-NOT-IDEMPOTENT`(concurrency_props.py)的生产者**既是探针又不在我的所有权**,
禁跑 ⇒ 如实裁定 + 给精确诊断交编排者,**不越权改也不手改产物**。
(与上一轮 `numeric_props.py` 同一处置口径。)

**风险评级跨层不一致的建议(不擅自统一)**:同一预期 E1 在 function/integration 疑点评 CRITICAL、
service 评 HIGH。**建议统一到 CRITICAL,但理由是证据而不是「取最高」这条规则**:
集成链已实证匿名端到端可利用(1 分订单落库、DB/API/前端三方一致接受),且系统零鉴权
(`NO_AUTHZ` CRITICAL 已在台账)。**架构上正确的修法是「风险评级是预期的属性,不是观测层的属性」**——
单一声明落在 `invariants.json` 的 `BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE`,其余层引用它(坑 11 单一载体)。
**本轮没做**:改 invariants.json 需重跑 `business_probe.py` 才能刷新 `business_findings.json`,
探针禁跑 ⇒ 会造成配置与产物不一致。交编排者调度。

**通用纪律**:报「某层 N 条未裁定」时必须按 `source.file` 回溯到**生产者**,
再按所有权与可跑性分类:能闸的闸、不能闸的给诊断。**按层估算会把工作量与覆盖率同时算错**。

### 坑 106:自动派生绑定把「模块根」当「仓库根」—— 静默降级只塌一个下游指标(W19 实测)

给零配置入口加自动派生绑定时,我把 `paths.backend` 设成**仓库根**。多模块仓库里
含 `src/main/java` 的是**模块根**(`repo/backend`),不是 `repo`。后果:

`file_static` 用 `BACKEND/src/main/java/...` 反查源文件并算相对路径 → 路径不存在 →
**63 个文件的 `file` 字段全成 `null`**,而脚本 **rc=0**、产物照常写出、
`files: 63` 看着完全正常。下游 `build_agent_graph` 靠 `file` 反查类→文件,
于是**同类自调用边从 61 塌到 2**。

**为什么这类洞难发现**:表面指标全对(单元 536、文件 63、函数 464、rc=0),
只有**一个下游派生指标**塌了。不拿已知良好基线逐指标比,根本看不出来。
我是因为自己先手工跑过链(61)才发现自动版是 2。

**规约**:
1. 自动派生配置后,**必须核派生结果的语义有效性**,不只核脚本退出码。
   本例的判据很便宜:`file` 字段非空率必须 63/63,不是 0/63。
2. 派生「根目录」类配置时,先问**消费者拿它拼什么路径**。
   `backend` 的消费者拼 `backend/src/main/java`,所以它必须是模块根。
3. 守卫要直接锁派生值:`test_derived_backend_is_module_root_not_repo_root`
   断言 `paths.backend == repo/"backend"`;变异 M33 精确复刻缺陷态(改回 `backend = repo`),
   **只红这一条指名守卫**。
4. 与坑 93(两个默认产物目录)同族:**路径语义错配不报错,只产出看似合理的错数**。

### 做法 58:覆盖率口径重分类的三条红线 —— 守恒 / 检测力不变 / 新缺口先问干净输入(W19)

把「416 个域外细胞被算成未知足迹」改成 `not_applicable` 时,三条红线缺一不可:

**一、守恒**(承坑 67):细胞三口径之和 == `cells_total`;
pair 四桶(可判 / 域内 unknown / 空足迹可判不相交 / 域外 not_applicable)之和 == `pairs_total`。
实测 546 = 130 + 0 + 416;148785 = 8385 + 0 + 0 + 140400。
**不守恒就等于把一部分对静默丢了**,比原来的错数更坏。

**二、检测力一字不变**(承坑 44/66):重分类**只改覆盖率口径**,
候选集、候选 id 序列、环障碍数、`pairs_decidable`、`candidates_delta0` 必须与改前完全相同。
守卫 `test_reclassification_does_not_change_detection_power` 直接比这五项。
**没有这条,「修正口径」与「洗绿」在产物上无法区分。**

**三、新增缺口条件前,先问「一个完全干净的输入会不会触发它」**(承坑 78 反向):
我一开始加了「net 未声明建模域 → 记缺口」,结果**误红两条既有的干净图守卫**
(5/5 可判、零 unknown、零候选,本该 GREEN)。未声明域时引擎已保守把域外全算 unknown
并发 input_warning,既有缺口条件已覆盖 —— 我那条是**重复计数**。撤掉后 178 条既有测试复绿。

**消费侧配套**:`unknown_pairs_over_total` 归零**绝不能**被读成绿灯。
入口必须把域外细胞数单列成缺口(实测 416 → 支柱仍 INCOMPLETE、退出码 2),
并把三口径写进头条:「域内已测 130/130、域内 unknown 0、域外 not_applicable 416」。
变异 M32 拆掉这条缺口 → 指名守卫
`test_out_of_domain_keeps_incomplete_even_when_unknown_ratio_zero` 红。

### 做法 59:证明「口径改动不改检测力」要用单因子隔离,不是拿两次跑批比(W19 自踩)

我要证明域归类只改覆盖率口径、不改检测力,第一次拿两个产物文件比,结果:
- 两边**都是改后**跑的(同一 `--out` 目录被后一次覆盖)→ **自己比自己,恒等,零信息**(坑 2 同族);
- 换成真·改前文件后,又出现 `cycle_status`/firing 桶**不一致** —— 差点误报「检测力被改了」。
  追因:改前那次是 audit_entry 跑的(**带 --oracles**),我改后那次是手敲 CLI(**漏了 --oracles**)。
  **是输入差,不是代码差。**

**正确做法(已实测)**:固定 net/units/oracles 全部输入,**只删掉 `modeling_domain` 这一个字段**
生成对照副本,两次跑同一引擎。结果九项检测力指标全同
(`pairs_decidable` 8385、`cycle_obstructions` 0、`candidates_delta0` {0,9}、
`candidates_evidence_strength` {9,0}、`cycle_status` {consistent 3, unknown 11}、
`buckets_firing_capable` 1、`buckets_co_singleton_only` 2、`overlapping_pairs` 55、`cycle_buckets` 14),
候选 id 序列逐字相同;**只有口径变**(416 从 `cells_in_domain_unknown` 移到 `cells_out_of_domain`,
`pairs_not_applicable` 0 → 140400)。

**规约**:
1. 比对前先核**两份产物是不是同一份代码 + 同一组输入**跑的。核法:看新字段在不在、
   看 `inputs`/命令行记录、看 mtime 与覆盖关系。**同目录同名文件极易被后一次跑批覆盖。**
2. 声称「只改 X 不改 Y」,就要造**只差 X** 的对照,而不是找两次历史跑批。
3. 出现意外差异先查输入差(漏参数、不同产物世代),再查代码差 —— 与坑 88(世代错配)、
   做法 39(先核 scope 与键形态)同源。

### 坑 107:扩域 ≠ 扩检测力 —— 344 个域外细胞写足迹全零,真杠杆在足迹契约不在建模域(W20 实测 + 沙箱实证)

**任务假设**:relation_net 只给三种后缀建点,345 个 function 细胞在域外;
「盲扩会抬覆盖不抬检测力,但 entity 回调 / mapper / config 小子集可能有价值」。
**实测:主假设成立,小子集期望被推翻 —— 没有任何子集买到检测力,故不扩域(证据分支交付)。**

**按包加权表(做法 50,唯一键 344 / 记录 345,`ApiResponse#ok` 在 units 重复 1 次)**:

| 包 | 细胞 | 声明可见写足迹 | 写足迹上判据附着 | 纯访问器(get/set/is+record) | 有调用无写 | 其他 |
|---|---|---|---|---|---|---|
| dto | 202 | 0 | 0 | 200 | 0 | 2(静态工厂) |
| entity | 104 | 0 | 0 | 100 | 0 | 4(含 2 回调) |
| config | 16 | 0 | 0 | 2 | 4 | 10(文件 I/O / ThreadLocal) |
| exception | 15 | 0 | 0 | 3 | 9 | 3 |
| mapper | 6 | 0 | 0 | 0 | 6 | 0 |
| banquet | 1 | 0 | 0 | 0 | 0 | 1(main) |
| 合计 | 344 | **0** | **0** | 305 | 19 | 20 |

**两个修正**:
1. **mapper 也没有写足迹**:6 个全是 entity→DTO 方向(`toResponse`),只调实体 getter。
   「转换逻辑可能有价值」在现行足迹契约下不成立(getter 读不计入足迹)。
2. **entity 回调(@PrePersist/@PreUpdate)确实写字段,但写是 putfield 直赋,
   声明产物里不可见**(`internal_calls=[]`、`side_effects=[]`、`is_pure=True`,
   回源码 BaseEntity.java:19-27 核实)。即使模拟抽取器补上字段写(S9 反事实),
   只 +1 条生命周期噪音 lead(onCreate×onUpdate 共写 updatedAt)、firing +0 ——
   域内没有任何细胞触碰 createdAt/updatedAt,该字段判据全是 DDL not_null 余单例域。

**沙箱(/tmp/w20,真 frame_check + 生产口径 plumbing=REPO_OP / business=L0 / oracles 341)**:
7 个子集(callback/mapper/config/dto/entity/exception/all)全部
**candidates +0 / firing +0 / obstructions +0 / cycle_buckets +0**;
覆盖侧 pairs_decidable 8385→112101(13.4×)、measured 130→474 ——  textbook 坑 64/66:
known-empty 细胞抬可判对数,倒排索引一个桶都不长。capability_map 同跑零增益
(oracles_implemented 226/341、边 271/133 逐条不变);arch_rules 由坑 79 并集论证
(新节点 ⊆ fn_declaration 键,并集不变)。

**真杠杆定位(语义反事实,不是扩域,未实现)**:S8 = getter 读计入足迹 **且** getter/setter
归一到同一字段资源键(`normalize_resource` 只归一 setter,`getStatus` 与 `setStatus`
落不同桶)→ leads +33、firing 桶 +3、约束桶 +13。**瓶颈是足迹契约,不是建模域**;
且 S8 同时改域内细胞足迹,属跨引擎契约变更,须独立验证(V17 在飞时不动)。

**副作用(扩域连结构中性都不是)**:entity setter 建点后,`dst in nodes` 使原 DERIVES 边
改走 CALLS 分支 —— DERIVES 25→0、CALLS 110→387,capability_map 的 DERIVES 证据链丢失
(判定数不变,证据出处变)。dto 子集无此副作用(不截 entity setter 调用)。

**harness 双向(坑 78 精神,证据分支无新代码可变异,改验 harness 自身)**:
空对照 = 原型无扩展与发布产物节点/边逐条一致;阳性对照 S10 = 给 dto 访问器注入 1 个
假写汇 → candidates 9→10(证明「零」非空洞);PATH 挡板 javac/mvn/docker→exit 99 +
共享目录哨兵(classes 数 + sources.txt mtime)前后不变。重放:`sh /tmp/w20/replay.sh`。

**规约**:
1. 扩域前先量「能买到多少检测力」:逐包分解写足迹 / 判据附着 / 纯访问器(做法 50),
   再在沙箱用真消费者跑子集对比,**覆盖增量与检测力增量并排报**(坑 64/66 硬要求)。
2. 写足迹不可见时扩域 = 制造 known-empty 细胞:pairs_decidable 虚胖,索引零桶。
   「模型变大 ≠ 调节能力变强」(Conant-Ashby)在这里有了实测数字。
3. 声明产物看不见的写(putfield / 字段直赋)是**抽取器缺口**,不是建模域缺口 ——
   先分清缺口在哪一层,再决定改谁;补抽取器之前扩域是空转。
4. 数字钉快照:本轮基线 firing 1 / co 4(约束桶 5),与坑 66 时期 2/6 不同,
   因判据语料已变(243→341);报数必须带所验产物,不得照抄旧坑数字(坑 4/58/67)。

### 坑 108:闸门守卫全绿 ≠ 裁定已执行 —— 普查分母小于裁定 population,B 类假阳性从未接闸的生产者照常被发布(V18 实测)

**任务**:证伪 W15「分类判据 + 45 条裁定(27A+7B+11非缺陷)+ 闸门」。判据本身与闸门机制我都 independently 复核通过(见做法 60),**但「45 条裁定已执行 + 产物已刷新」被证伪**。

**决定性证据(三条,互相印证)**:
1. **台账里躺着 3 条 B 类缺陷,warrant=None,生产者从未接闸**:
   `SVC-PAGINATION-COUNT-DISCARDED`(service/MEDIUM,来自 `perf_load.py:326`)、
   `INT-LIST-COUNT-END-TO-END-WASTE`(integration/MEDIUM,同源)、
   `SVC-POST-NOT-IDEMPOTENT`(service/MEDIUM,来自 `concurrency_props.py:200`)。
   `perf_load.py` / `concurrency_props.py` **不 import oracle_gate**,直接写 `*_findings.json` → normalize 原样进台账。
2. **W15 自己的 docstring 声称裁定过它们**:`test_warrant_gate.py:24-31` 明列
   SVC-PAGINATION = **B refuted**(「分页契约明文要求 total,『纯开销』被否定」)、SVC-POST = **B unconfirmed**。
   回原文核成立:`AGENTS.md:139` 分页契约明文含 `total:100`/`totalPages:5` → 「total 是纯开销应丢弃」发明了比文档更严的业务规则,**正是本项目要消除的高置信假阳性形态**。POST 幂等 likewise:`RFC7231` POST 本就非幂等,「必须幂等」是业务预期无文档来源。**裁定对,但没执行 —— 三条仍以 defect 发布。**
3. **走私变异存活(坑 78 精神,精确复刻缺陷态)**:往沙箱台账 append 一条 `{kind:defect, warrant:{kind:business, confirmation:refuted}}` 的 `SMUGGLED-BIZ-DEFECT`(模拟未接闸生产者),W15 全套 **47 守卫仍 rc=0 全绿**。根因:`test_business_without_confirmed_oracle_is_not_in_the_ledger` 遍历的是 `_all_warrants()`(file12+contract9+class3=**24 键**),**不是台账缺陷集(69)**;凡不在 24 键里的生产者,其 B 类缺陷对守卫完全不可见。

**普查计数(自己枚举,不照抄)**:warrant_split 只接 3 个生产者(file_diff/contract_diff/class_contract_diff);split 接 fn_diff/integration_diff;其余 perf/concurrency/config/orm/numeric/mutation/feature/property_binding **零闸门**。service 层 23 条确来自 8 个生产者(contract_diff 仅 10),file 18 是 config 声明门数(12defect+6positive)非台账数(16)——「41=18+23」混了「门数」与「台账条数」两种单位。

**docstring 自相矛盾**:标题「B 类 7 条」下列了 **8 个名字**;且该集合与机器普查的 7B 不一致(docstring 含 2 条未执行的 SVC、漏掉 contract 的 ENVELOPE_BREAK confirmed)。机器 `_all_warrants` 的 7B 是对的、已执行;docstring 的「7B」是另一套未对齐的population。

**「CRITICAL/HIGH 一个没动」字面不成立**:按稳定 id 比 HEAD(95)→工作树(86),移除 9 条,其中 **4 条是 CH**(FN-PRICE-CLIENT-CONTROLLED/FN-SEMANTIC-PRICE-UPPER-BOUND/INT-THREE-PRICES-NO-DETAIL-TOTAL/PRICE_CLIENT_CONTROLLED_XL,2C+2H)。**但这 4 条全是已证伪的价格假阳性,移除正确**;真 CH 技术缺陷(NO_AUTHZ/FN-UPLOAD-NO-CONTENT-SNIFF/INT-ANON-WRITE-REWRITES-SNAPSHOT/INT-UPLOAD-ANONYMOUS-READBACK 等)一条没丢,13/13 firing A 类全保留。当前 86/69(4C/15H/41M/9L)与声称一致。措辞应改「真缺陷的 CH 一个没动,移除的 4 条 CH 全是假阳性」。

**规约**:
1. **「裁定」与「执行」是两件事**:docstring/报告里写了 refuted/unconfirmed,不等于产物里压住了。核裁定必须**回台账查该 id 的 kind**,不能只读裁定文字。
2. **守卫的分母必须等于声称的 population**:声称裁定 45 条,守卫只查 24 键,差集就是盲区。普查守卫要遍历**台账全部缺陷**(或全部开单生产者),不是只查已接闸的那几个。
3. **接闸要按生产者枚举,不是按「门」**:一个生产者不调 warrant_split,它的所有缺陷都绕过闸门。新增生产者必须同步接闸 + 进普查,否则走私变异恒存活。
4. 与坑 78(变异精度/双向)、坑 101(相邻守卫顺手杀≠有守卫)、做法 46(修生成器≠刷新发布物)同源;本条补的是**守卫 scope 与裁定 population 的对齐**。

### 做法 60:证伪「分类判据」要独立跑三问 + 用存活走私变异测守卫 scope,而不是只跑它自己的变异(V18)

**独立三问(12 条,逐条回原文/源码,不照抄裁定)**:对 file/contract/class 的 A/B 声明自己跑 Q1域替换/Q2权威出处/Q3混合,**12 条分类全部与 W15 一致**——没找到「业务归 A 绕过闸门」或「技术归 B 被误压」。重点核 `FILE-UPLOAD-LIMIT-SPLIT-ACROSS-THREE-FILES`:authority=CWE-1078/DRY,basis 明说判据是「同一常量不得多处复制」而非「上限该是多少」,**5MB 是证据不是判据**(换成任何重复常量都触发)→ **不是伪 A**,区分成立。`CLASS-ERROR-MESSAGE-MISMATCH`(文案匹配真因)、`FILE-XFF`/`FILE-CORS`(CWE-290/942)同理通用。B 类三条回原文:`AGENTS.md:153-156`(400≠422)→FILE-VALIDATION-STATUS-SPLIT refuted 成立;`:100`(统一JSON)→FILE-ERROR-BODY-SHAPE confirmed 成立;`:139`(total 必需)→SVC-PAGINATION refuted 成立。**判据可操作、A 类 authority 真通用、B 裁定回原文成立 —— 这部分 PASS。**

**双向变异(6 新锚点,沙箱 /tmp/v18/sb + PATH 挡板,空对照 rc=0)**:
M1 technical 路由进 unconfirmed oracle(A 误压)→ 24 守卫红;M2 business 恒放行 → 12 红;
M3 未声明恒放行 → 6 红;M4 翻 domain_swap=holds→fails → **恰好 1 条**指名守卫红(坑 101:锁对了不变式);
M5 等价重构 `.get("kind")`→`.get("kind",None)` → **存活全绿**(合法不误红);M6 contract_diff 加未声明 add() id → 普查守卫红。
**闸门机制本身双向健全 —— 在它接线的 3 个生产者范围内。**

**关键补充:测守卫 scope 用「存活走私变异」**:M7 往台账塞一条未接闸生产者的 B-refuted 缺陷 → 47 守卫全绿(坑 108)。**只跑被验方自己的变异矩阵会漏掉 scope 盲区**;独立验证者必须构造「缺陷态存在于守卫分母之外」的变异,看它是否存活。存活即证明覆盖洞,比再杀十个粗变异更有价值。

**全量基线归因(坑 92)**:`pytest pilot/tests demos/shop` = 1313 passed + 12 subtests + 6 failed/6 errors。6 红**全部**是 `demos/shop/test_d6_fixture.py`,traceback 明写 `RuntimeError: psql 失败: V18 PATH BLOCKER: docker called (forbidden)` —— 是我的 docker 挡板拦了 demo 库探针(硬边界禁 docker/写库),**非代码缺陷**;1313+6=1319 与声称基线总数吻合,warrant/oracle/audit_entry/frame_check 零红。共享字节码哨兵 64 class 前后不变、git status 78 不变、8 个 W15 关键文件 sha256 逐字节不变(只读纪律成立)。

**规约**:证伪分类判据 = ① 独立跑三问找归错(本轮无)+ ② 核 authority 是否偷引业务量(本轮无伪 A)+ ③ B 裁定回原文(成立)+ ④ **回台账核裁定是否真执行**(本轮证伪)+ ⑤ 存活走私变异测守卫 scope(本轮证伪)。①②③ 通过不等于 ④⑤ 通过;**判据对 ≠ 裁定落地**。

### 坑 109:openapi 判据 target 用 `schema`、frame_check 挂载只读 `entity` —— 跨实体过挂被 :520 掩盖,挂载口径零守卫(V17 独立实测)

**现象**(用户提出候选,V17 带证据确认,非照单全收):`oracles_harvested.json` 里 openapi 60 条
target 键是 `{schema, field}`,annotation 66 条是 `{entity, field}` —— **值同键不同**。
消费者口径不一致:`capability_map.py:983/1054/1092/1112` 与 `frame_check.py:520` 都读
`entity or schema` ✓;但 **`frame_check.py:223` 只读 `t.get("entity")`** ✗,于是 openapi 判据
在 :223 匹配不上,落到 **:232 裸字段名兜底**(`not entity and not table and field 匹配`,
且 :232 **不排除 schema**)⇒ 按字段名跨实体过挂(V10 修过的假障碍那一类被词汇漂移重新打开)。

**最小反例**(实跑 `_matching_oracles`,非推理):两条 openapi 判据 `A.name`(域{x})与
`B.name`(域{y}),ident=`("field","A","name")`:
- schema 键 → 返回 `['OA','OB']`(**OB 过挂**);entity 键 → 返回 `['OA']`(正确)。**行为不同 ⇒ 真缺陷,非等价变异(坑 61)。**

**真实语料量化**(jiuyan full3,自己数):候选资源上 **8 条 openapi 判据过挂到 3 个异实体**
(BanquetOrder / BanquetPackage / Product),如 `api:CreateOrderRequest.status.enum` 挂到
`BanquetOrder.status`。**但检测力后果 = 0**:schema→entity 改名后九项指标全同、候选 id 全同、
环全同;候选唯一差异是 `resources.oracles` 挂载列表。原因三道闸:① `_cell_section:520` 按
`entity or schema == 资源实体` 再过滤,约束集不被污染(去掉它则裸交集=空集=假障碍,实测);
② δ⁰ 用 findings 不用 oracles;③ 过挂的 8 条全是 **L2** 非 L0,`business_oracles` 空 ⇒ 候选过滤不变。

**守卫缺口(判别性变异坐实)**:把 :223 改 `entity or schema` + :232 加 `not schema`(修法方向),
跑 `test_frame_check + domain + closed_primitives` **315 passed / 0 红** ⇒ **没有任何守卫锁挂载口径**。
既有 `test_schema_oracle_resource_level_guard` 只锁 :520 裁决路(`constraint_section_cells==0`),
**不锁 :223/:232 挂载路**。对照:删 :520 再过滤 → 该守卫 + `test_resource_guard_not_cell_footprint`
双红(裁决保护**有**守卫);挂载过挂本身**无**守卫。

**根因二层**:除键名漂移外,net 资源 owner 是 **FQCN**(`com.jiuyan.banquet.dto.AddedItemRequest`)、
openapi schema 是**简名**(`AddedItemRequest`),:223/:520 直接比简名≠FQCN ⇒ openapi 字段判据
**从来正确挂不上** DTO 资源,只能借 :232 裸字段泄漏。修 :223 键名之外还需简名/FQCN 归一,否则
openapi L2 对 frame_check 裁决实际是装饰性的。

**规约**:① 同一 target 语义在多消费者必须**同口径**(entity or schema 一处漏读就跨实体过挂);
② 裸字段兜底(:232)必须排除**带实体/schema/table 绑定**的判据,只留真正全局 L0 PRD;
③ 「裁决被下游再过滤保护」≠「挂载正确」—— 挂载计数膨胀与潜在假障碍要**单独有守卫**,
不能因为九项指标不变就判无缺陷(坑 64/66:覆盖/挂载 ≠ 检测力,但挂载错仍是错);
④ 守卫缺口要用**判别性变异**(修缺陷态看有无守卫红)坐实,0 红即缺口,不是「修了也没事」。
与坑 41/60(跨实体按资源实体)、坑 78(变异精度+反向)、做法 55(红的是不是目标守卫)同源。
**最小修法建议(未改文件)**::223 读 `entity or schema`、:232 加 `and not t.get("schema")`、
另做简名/FQCN 归一;消费侧接线需所有者批准。

### 坑 110:推导锚表「计数涨、检测力涨、业务口径零」—— 与人工锚**人口不相交**,不许报成「接近人工版」(W21 实测)

**任务前提**:入口声称 openapi 与业务不变式「不可约、无生产者」。复核结论:**B 可闭,A 只能半闭**。

**A 的实测(jiuyan,最少参数,沙箱冻结快照)**:接上 `oracle_bridge.to_invariants` 后
锚 **0 → 118**,其中 **62 条落到真实表且 covered_fields 非空**、56 条无容器、**0 条假表**。
看着是大增益。但把推导锚与人工锚**逐条对**:

| 口径 | 人工 `pilot/invariants.json` | 推导锚表 |
|---|---|---|
| 条数 | 6 | 118 |
| id 交集 | — | **0(完全不相交)** |
| assert 类型 | `field_lte_field` 2 / `field_eq_field` 2 / `jsonb_subset_of` 2 | `field_not_null` 71 / `field_gte_const` 23 / `field_not_blank` 18 / `field_gt_const` 4 / `field_in_set` 2 |
| 跨字段业务口径 | **6/6** | **0/118** |
| confirmation | confirmed **1** / unconfirmed 4 / refuted 1 | confirmed 118/118 |
| level | L0 业务规则 | L1 技术约束 111 / L0 7 |

**根因**:采集器能确认的是**作者写下的单字段技术约束**(NOT NULL / >=0 / NOT BLANK);
人工不变式是**跨字段业务口径**(定金 ≤ 成交价、快照 ⊆ 套餐)。`jsonb_subset_of` 根本不在桥的封闭集里。
两者是**不同人口**,不是同一人口的近似。报「锚 0→118,接近人工版」就是编造。

**规约**:报推导增益必须并排报四个数 —— **总数 / 可执行数(真表+字段覆盖) / 与人工版的 id 交集 /
断言类型分布**。交集为 0 时结论只能写「**补充了另一类判据,不可替代人工业务声明**」。
这是坑 66(覆盖≠检测力)的新形态:不只是「桶能不能 firing」,还有「**量的是不是同一个人口**」。
顺带一条反向事实:人工锚表 6 条里 **5 条不是 confirmed**,而 `relation_net` 建锚**不看 confirmation** ——
「锚 6」从来不等于「6 条已确认业务规则」,拿它当推导版的对标基线本身就要打折。

### 坑 111:把 DTO 容器名当 JPA 实体写进锚表 → 消费者 snake_case 兜底造出**不存在的表**(W21 实测)

**缺陷态(我先犯了一次,被自己的守卫抓住)**:采集判据的 `target.entity` 有 66 条,值全是
**DTO 类名**(`CreateOrderRequest` / `AddedItemRequest`…),不是 JPA 实体。
`relation_net.anchor_tables` 匹配不到 `ent_map` 时走 `resolve_table` 的 **snake_case 兜底**,
于是 `CreateOrderRequest` → 表 `create_order_request` —— **DDL 里没有这张表**。

实测三种写法的锚质量(同一份 216 条 mapped,冻结快照):

| entity 怎么写 | 无容器 | 表名不存在 | 命中真表 | 字段可覆盖 |
|---|---|---|---|---|
| `to_invariants` 原样(**不写 entity**) | 216 | 0 | 0 | 0 |
| 直接抄 `target.entity`(DTO 名) | 160 | **56** | 0 | 0 |
| 只在唯一落到真实 JPA 实体时才写 | 78 | **0** | **82** | **82** |

**第二行比第一行更坏**:第一行是诚实的「锚不到表」,第二行是**伪造绑定** —— 产物里有个表名,
下游按它建桶、算覆盖,而那张表不存在。与坑 80(补空串把 unknown 造成确定判定)同族:
**兜底路径会把「不知道」变成「一个看起来确定的错答案」**。

**修法(已落沙箱)**:`entity` 只在 ① `target.entity` 本身就在 `class_declaration.jpa` 里,
或 ② `target.table` 反查**唯一**实体时才写;否则**不写 entity**(让消费者返回空表列表),
并把 `dto_container_not_entity` / `no_container` 计数写进产物。多实体映到同一表 → **不猜**。
守卫 `test_dto_container_must_not_fabricate_a_table` 直接断言 `anchor_tables(...) == []`,
变异 M8「照抄 DTO 名」被它指名抓住。

**规约**:往消费者的**兜底字段**里写值前,先读它的兜底分支。兜底不是无害默认,
它常常是「造一个形状合法但事实不存在的值」。生产者的诚实边界是**留空 + 计数**,不是填满。

### 坑 112:「全量 1319 passed / 0 failed」有**环境前提**;无 demo 容器时 6 failed + 6 errors,且会真调 docker psql DELETE(W21 实测)

**实测**:同一份 pristine 副本,`pytest pilot/tests` = **1303 passed + 12 subtests / 0 红**;
加上 `demos/shop` 才凑成 1319 collected,但结果是 **6 failed + 1313 passed + 6 errors**
(红名全在 `demos/shop/test_d6_fixture.py` 等,需要 8090/8091 两个 demo 容器在跑)。

**更硬的一条**:这些测试的清理路径会真的执行
`docker exec jiuyan-postgres psql … DELETE FROM shop_quota_occupancy …`。
本轮 **PATH 挡板命中 12 次**,全部是这类调用 —— 挡板把它们挡成 rc=97,所以那 6 个 error。
**没有挡板就会真删 demo 库**(违反硬约束 2 的零残留前提,且本轮禁跑写库测试)。

**规约**:① 报基线数字必须连**命令范围与环境前提**一起报(`pilot/tests` 与 `+demos/shop` 是两个数);
② 接手别人给的「N passed 0 failed」先在自己环境复现一次,复现不出先查前提再查代码(坑 67 同族);
③ 凡是跑全量 suite,harness 一律加 PATH 挡板(javac/mvn/docker/psql/gradle),
**demos 侧测试不是纯函数**(做法 49 的第二个实例:上次是 javac,这次是 docker psql DELETE)。

### 做法 61:静态推导契约的六条纪律 —— 独立参照验证 / 类级注解归属 / 窄用途 / 因子隔离(W21)

本轮把「openapi 无生产者」闭掉了(**绑定 0 → 25**,人工版 19),四条可照抄的纪律:

**一、推导必须有独立参照,不能自证**。仓库里已有真实 `pilot/openapi.json`(25 个操作)。
把注解推导出的 `(verb, path)` 集合与它**逐条对**:`missing=[] extra=[]`,25/25 全等,
`ambiguous_classes=[] path_collisions=[]`。**先证明推导 faithful,再谈增益** ——
否则「推出来 25 个」可能只是推出来 25 个错路径。

**二、javap 的类级注解被挂在方法记录上,合成 path 前必须先判归属**。实测:
`CategoryController` 3 个方法,只有 `delete` 带 `RestController`/`RequestMapping`/`Tag`
(解析器把类级 RuntimeVisibleAnnotations flush 给最后一个方法)。
把 `RequestMapping` 当方法级 → 拼出 `/api/categories/api/categories/{id}`。
`fn_declaration` **不区分类级/方法级**,这是真实的信息丢失。本轮的转录约定:
**无 `method=` 的 RequestMapping 即类级基路径**;同类出现 ≥2 个不同 raw、或 raw 带 `method=`
→ 记 `ambiguous_classes` 并**放弃该类**,不猜。变异 M6(丢类级基路径)/M7(重复前缀)各被 5/6 条指名守卫抓住。

**三、窄用途,不越界**。合成契约**只写 `up/openapi.json` 供 `relation_net.bind_entries`**,
**不喂 `oracle_harvest`**:DTO 约束已由 `annotation` 判据采过(66 条),再采一遍是重复计数。
所以 L2 接口契约缺口**仍然如实报**,oracle 支柱分子分母一字不变(118/177)。
产物里不含 `components`,守卫 `test_synthesized_contract_carries_no_schema_and_is_not_harvested`
+ 变异 M12(顺手合成 schema)锁住这条边界。

**四、增益必须因子隔离,交互项要单独归因**。四个 run 同一命令只改一个输入:

| run | 绑定 | 锚 | 候选链 | 模式 |
|---|---|---|---|---|
| 最少参数(修前) | 0 | 0 | **0** | — |
| 只供人工 openapi + 推导锚(A) | 19 | 178 | 33 | T1 24 / T3 9 |
| 只供人工 invariants + 合成契约(B) | 25 | 6 | 9 | T1 9 |
| 人工两文件 | 19 | 6 | 9 | T1 9 |
| 全推导(A+B) | **25** | **118** | **45** | T1 24 / **T2 12** / T3 9 |

**T2 的 12 条是合取增益**:它要求入口**已绑定 endpoint**(`emergence_candidates:354` 过滤
`v.get("endpoint")`)**且**该表有锚。A 缺 categories/package-types 的绑定,B 缺这两张表的锚 ——
**单独任一个都拿不到 T2**。12 条的 endpoint 正是人工版未绑定的那 4 个
(`DELETE/POST /api/categories{,/{id}}`、`/api/package-types{,/{id}}`)。

**+6 绑定的归因(不是推导更准,是消费者词形匹配弱)**:`relation_net._controller_keys`
朴素单数化 `categories` → **`categorie`**(≠ `category`),`package-types` → `package-type`
(≠ 驼峰 `packagetype`),中文 `@Tag`(「分类」「套餐分类」)也给不出类名。
合成契约的 tags 带**声明类简名**(字节码事实,不是猜)→ 这 6 条绑上。
** faithful 变体(只带 @Tag)实测 19/6,与人工版逐条相同** —— 证明推导本身无偏差。

**五、四支柱分子/分母零变化,必须明说**。绑定/锚的收益落在 net 产物与候选链生成器,
**不在四支柱任何一个分子上**:oracle 118/177、capability 139/177、arch 347/610、
emergence 8385/148785 修前修后**逐字相同**,`frame_check.json` **逐字节相同**,
退出码仍是 2。把「绑定 0→25」说成「审计能力提升」是越界结论。

**六、并发所有权下的交付形态**:V17 正在飞(`/tmp/v17_verify_*/runs` 秒级在写),
所以本轮**只改沙箱副本**,交付 patch + 新守卫文件,不动仓库 `audit_entry.py`。
依赖的 `relation_net.py` 被 W20 同时改着 —— 交付前把**当前版**拷出来逐函数比对:
`bind_entries` / `_controller_keys` / `anchor_tables` / `load_invariants` / `assert_fields` /
`entity_tables` **六个函数逐字相同**,并用当前版重跑绑定仍是 25/19。
**规约**:跨所有权引用别人的在飞文件,必须核「我依赖的那几个函数」有没有变,
而不是核整个文件 sha(整文件必然在变,核了等于没核)。

### 做法 60:给在飞 agent 发纠正**是排队不是插队** —— 靠检查点防错方向,别靠纠正速度(主控实测)

**实测机制**:向 running 会话发消息,返回值里 `queuedTurnCount` 递增 ⇒ **消息在队列里等它当前 turn 结束**。
一个 turn 可能跑十几分钟,**纠正就晚十几分钟到**。工具只说「its normal steering or queueing policy applies」。
**没有注入/steer 机制**。唯一的中断是 `stop_chat_session`,它**同时取消队列输入**,是硬中止、丢在飞工作。

**本轮真实代价**:主控两次给 W19 发纠正(「主要是粒度错配」「345 是陈旧缺口」),都是排队的,
**而且两次都是主控自己错了**。若 W19 当时按错话行动就白干十几分钟。

**规约**:
1. **该硬中止的**:agent 正朝错方向**产出成果**,或可能违反硬约束(改被测仓库、跑 javac、洗三态)。
   丢十分钟工作比落一个错改动便宜 ⇒ `stop_chat_session` + 带纠正重新派。
2. **可以排队的**:补充、细化、追加验证项 —— 晚到不影响正确性。
3. **更根本的是结构性预防:派发时就要求「先测量、后实现」,设检查点。**
   让错方向被**agent 自己的证据**挡住,而不是靠编排者迟到的纠正。
   本轮两次生效:W20 先量检测力 ⇒ 测出「7 个扩展子集全零增量」,推翻了编排者的扩域假设;
   W22 先测可行性 ⇒ 测出「全局 0 对」,推翻了编排者的 δ⁰ 假设,还顺带挖出 positive 混入违反指纹的真 bug。
4. **派发模板必带一句**:「下面的假设要你独立检验,不同意带证据推回;先测量再实现」。
   编排者的假设**不是指令**,是待检验命题。
5. 另:**`callbackMode` 默认 `"none"`**,不设就只能轮询。派发一律带 `supervise`,
   完成/需授权时自动回调。**一轮里可以连续多次等待**,不要一轮只等 60 秒就停下来等人催。

### 坑 113:足迹契约杠杆实测 —— getter 读可见买到 +3 真 iv 桶,但线索 72% 是良性数据流;合并必须字节码验证,名字推导有三类反例(W20)

**承接坑 107**:扩域零增益,真杠杆在足迹契约(getter 读不可见 + getter/setter 资源身份分裂)。
本轮先答三问再实现,opt-in 落地于 relation_net(帧检查零改动)。

**问题1(合并凭什么证据)**:javap 抽 63 类 getfield/putfield(/tmp 副本,只读),
被调用的 51 种实体 getter **全部可验证**(含继承链)。名字推导在本语料碰巧全对,
但有三类反例证明它不可靠:① 7 个 record 访问器 `isActive()` 读 **isActive** 字段,
isX→x 推导会错并成 active;② `TraceContext.setTraceId` 是 ThreadLocal.set,**无 putfield**;
③ `getTraceId` 无 getfield。规约:**只按字节码目标发射,不可验证保分裂**;
别名 getter(getAliased 读 other)按真实目标发 setOther。守卫 M1/M2 专杀名字推导。

**问题2(+18 线索噪声占比,单因子=基线域只开读通道)**:写-写 **0**;
已解释依赖 9/18(5 CALLS 可达 + 4 lambda 边缺失——javap MethodHandle 缺口,
「CALLS 不可达」会高估独立性);同类聚合操作 4/18;**真独立跨类共触 5/18,全部带判据锚**。
即 ~72% 良性/低可操作,28% 是 frame rule 的真目标群——但全是 lead(delta0 unknown),
结构上不开单(审计入口只认环障碍 RED)。**线索不是这个杠杆的主价值**。

**问题3(+3 firing 桶)**:BanquetPackage.setItems(iv [1,∞))、BanquetPackage.setPriceCent(iv [0,∞))、
Product.setPriceCent(iv [0,∞))——**真区间,非余单例**,来自 doc 裸字段判据
(api 判据 target 是 DTO 实体,被跨实体守卫正确挡掉,见坑 109 挂载口径)。
对称挂载下检的是「资源全局约束可满足」,与基线桶同信息量(坑 66 限定同样适用);
obstructions 0→0。**主价值是实体字段约束进入约束集通道(+3 firing-capable / +1 co)**。

**实现(只改 relation_net.py + 新测试,frame_check 零改动)**:
`--getter-reads field_access.json` opt-in;读发射成 **setter 等价键**入 source_fields
(倒排索引按原始串建桶,必须与 sinks 同键;读/写由 provenance 区分,不靠键形——
「field:」形会落平行桶,零重叠,试过就知道)。缺输入/形状不合格 rc=1 不写产物;
开启时 modeling_domain 声明 footprint_channels + field_access_input 哈希;
**generation_stamp 不动**(动了会与 units 世代错配)。关闭时产物**逐字节==发布产物**(实测)。

**单因子隔离(固定输入只开关)**:measured/pairs_decidable 不变(无新细胞);
overlapping 55→110、candidates 9→27、firing 1→4、co 4→5、obstructions 0→0;
既有 9 候选裁决级字段(frame_violation/strength/delta0/survives)**零变化**,
但 2 个候选 overlap_written  enriched(delete 的 getName 读变可见)——
**证据富化不是裁决变化,必须显式报,不许藏**。capability_map 对通道**完全中性**(逐字段实测)。

**双向变异(7/7 KILLED,7/7 指名命中)**:M1 名字推导→aliased/computed 守卫红;
M2 计算型兜底→computed 红;M3 读入 sinks→读-读不变式红;M4 缺输入放行→拒绝守卫红;
M5 声明漂移→同步守卫红;M6 作用域泄漏→非实体守卫红;M7 原始 getter 键(身份分裂)→写-读候选红。
空对照 rc=0;合法输入(正确合并/继承/默认关)14 守卫全绿=不误红(坑 78 反向)。

**规约**:
1. 资源身份合并要金标准证据(字节码 getfield/putfield),名字推导哪怕「本语料全对」也不用——
   反例存在于别的类(record/ThreadLocal),守卫必须造得出来。
2. 读通道扩容的诚实报法:线索按「已解释依赖/同类操作/独立共触」分解(调用图缺口要认),
   桶按域类型分 firing/co——**线索多≠检测力强,桶多≠障碍多**(坑 64/66 用在契约变更上)。
3. 单因子隔离要查**既有候选的全部字段**,不止「在不在」:证据富化与裁决变化是两件事,
   前者可接受但必须显式报,后者必须零。
4. 新资源键形先验「倒排索引按什么建桶」:归一化身份相同≠同桶,原始串才是桶键。

---

## 二十八、W15 第五轮补充(2026-11-15,V18 查出三处交付矛盾:裁定未执行 / 覆盖面夸大 / uid 污染)

### 坑 117:裁定只写在交付散文里、产物没执行 —— 比假阳性更危险(W15 实测,V18 查出)

**现象**:上一轮我把 `SVC-PAGINATION-COUNT-DISCARDED` 裁定为 B refuted、
`SVC-POST-NOT-IDEMPOTENT` 裁定为 B unconfirmed,写进了交付表格与测试模块 docstring,
理由是「生产者是探针、禁跑、不在我的所有权」。**但产物里它们仍是 MEDIUM defect、
`warrant: None`、`service_rule_suspects.json` 空(0/0)** —— 裁定根本没落盘。

**为什么比假阳性更危险**:假阳性至少还能被人复核出来;「声称已裁定但未执行」
**让人以为闸门盖住了**,读者不会再去查那两条。这是坑 97(留痕≠可见)的升级形态:
那次是产物里有、报告里没有;这次是**报告里有、产物里没有**。

**根因**:我把「不能跑探针」误推成「不能执行裁定」。裁定的载体是**声明**,
执行点可以不在生产者 —— 台账有唯一漏斗 `normalize_findings.py`。

**修法(已落地)**:新增项目侧 `warrant_registry.json`(键=finding id),
由 `engine/normalize_findings.py` 在入台账处消费:business 非 confirmed 拦下并落
`ledger_rule_suspects.json`,technical 放行,**未登记的不受影响**(不误压)但进「未裁定」计数。
实测:两条真的出了台账,裁定真的落盘(refuted 指 AGENTS.md:139-140、unconfirmed 指「无文档依据」)。
顺带发现并裁定了同源的第三条 `INT-LIST-COUNT-END-TO-END-WASTE`(与 SVC-PAGINATION 是同一条预期,
上一轮按 id 去重时漏了它)。**本轮台账 86→83、defect 69→66、已证伪 6→8、待确认 11→12。**

**防复发守卫(本轮最重要的新守卫)**:`test_every_adjudicated_business_claim_is_executed_in_the_artifacts`
—— 遍历登记册每条非 confirmed 的 business 裁定,断言 ①id 不在台账 ②id 在某个 suspects 通道
且确认状态一致。登记册与产物是两个独立文件,**只写登记册不跑漏斗 → 本条红**。
配套反向守卫 `test_every_adjudicated_technical_claim_stays_in_the_ledger`(A 类被误压必须红)。
文件级变异 M7(拆掉漏斗闸门)→ `_assert_cli_executes_verdicts` 红。

**通用纪律**:「我裁定了 X」这句话的验收标准是**产物里能查到 X 的裁定**,不是交付表格里有一行。
凡受阻于禁跑/所有权,先问「执行点能不能移到唯一漏斗」,再决定是否如实标「未裁定」——
**不得只在文档里写**。

### 坑 118:闸门覆盖面按「门」声称、按「层」统计 —— 15 个生产者只接了 3 个(W15,V18 查出)

**现象**:上一轮报「45 条逐条裁定」,但 `warrant_split` 只接了 3 个生产者、覆盖 24 个 id;
台账实际有 **15 个生产者、86 条**。裁定表与机器覆盖面不是一回事,读者会以为 45 条都过了闸门。

**修法**:覆盖清单入产物 + 入报告,已接与未接**并排报**:
`target.json` 声明 `producer_gates`(产物文件 → 生产脚本 + 闸门模式 warrant/oracle/none),
`normalize_findings.py` 生成 `warrant_coverage`(逐生产者:生产条数/defect/登记册覆盖/漏斗拦下/**未裁定**),
报告第十节渲染同一张表,头条加一行「A/B 二分 2 / 仅 oracle 4 / **未接 9**;未接名下未裁定 defect 3 条」。
**未声明的生产者显式记 unknown 并告警,不静默当成已接。**

**实测**:15 个生产者 = warrant 2 + oracle 4 + none 9;登记册 12 条;漏斗拦下 3;
**未裁定 3 条并逐条列名**:`FN-NUMERIC-MONEY-INPUT-SILENT-MUTATION`、
`JAVA-SERVICE-EXPOSES-JPA-ENTITY`、`FN-MUTATION-SURVIVOR-WEAK-ASSERTION`。

**守卫抓到的两个真 bug**:① `unadjudicated` 只统计 `gate=="none"` 漏了 `"unknown"`,
与 `ungated_producers`(含两者)口径不一致 → 合成夹具下未裁定数恒 0;
② 登记册里 orm 生产者名写成 `orm_probe.py`,实际是 `orm_query_probe.py` →
`test_registry_only_covers_ungated_producers` 红。**两处都是「两个口径/两个载体不一致」(坑 11 同族)。**

**防复发守卫**:`test_coverage_lists_every_producer_in_the_ledger`、
`test_unadjudicated_count_is_recomputable`(必须能逐行重算,不得是凭空写的数)、
`test_ungated_producers_are_all_listed`、`test_report_shows_ungated_producers_side_by_side`、
`test_unadjudicated_ids_are_identifiable`(未裁定必须可查到具体 id,不得只给总数)。
变异 M5(清单藏掉未接生产者)/M6(未裁定数洗成 0)/M8(文件级藏未接行)全部 KILLED。

**通用纪律**:报「已裁定 N 条」时必须同时报**机器覆盖面**:几个生产者、几种闸门模式、
未接几个、未裁定几条、未裁定具体是谁。**裁定表是人工产物,覆盖清单是机器产物,两者都要给。**

### 坑 119:「某严重度一个没动」用 uid 比会被序号漂移污染 —— 必须用稳定 ID(W15,V18 查出)

**现象**:上一轮报「CRITICAL/HIGH 一个没动」。V18 按稳定 ID 比,指出累计实际移除 9 个 ID
(含 2 CRITICAL + 2 HIGH)。**我的措辞在本轮范围内为真、在累计范围内为假**,但没写清范围 ⇒ 夸大。

**根因**:`uid = layer:id#N` 的 `#N` 是**层内序号**,移除一条会让兄弟序号整体平移(坑 74)。
用 uid 做差集会把「1 条移除」报成「多条消失 + 多条新增」,严重度统计随之失真。

**修法与实测(稳定 ID = `(layer, id)`)**:
- **累计(我五轮之前 95 条 → 现在 83 条)**:移除 **12 个稳定 ID**、新增 **0**、
  共有 ID 严重度变更 **0**。defect severity:CRITICAL **6→4**、HIGH **17→15**、
  MEDIUM 44→38、LOW 11→9。移除的 2 个 CRITICAL 是 `FN-PRICE-CLIENT-CONTROLLED` /
  `PRICE_CLIENT_CONTROLLED_XL`,2 个 HIGH 是 `FN-SEMANTIC-PRICE-UPPER-BOUND` /
  `INT-THREE-PRICES-NO-DETAIL-TOTAL` —— 全是已证伪的价格假阳性。
- **本轮(漏斗闸门)**:移除 3 个稳定 ID、新增 0、严重度变更 0。

**通用纪律**:报「未动 / 零回归 / 只删了 N 条」这类声称,**一律用稳定 ID 比,并写明比对范围**
(本轮 vs 累计)。uid 只能用于定位,不能用于差集与计数。

### 做法 63:禁跑探针时,裁定的执行点移到唯一漏斗 —— 登记册 + 覆盖清单(W15)

**处境**:9 个生产者是探针(perf_load / concurrency_props / config_probe / orm_query_probe /
property_binding_probe / numeric_props / mutation_test / java_feature_scan / transaction_probe),
禁跑 ⇒ 无法在源头重生成。上一轮据此把裁定停在散文里,被 V18 判为矛盾。

**做法**:① 裁定声明落 `warrant_registry.json`(项目侧,键=finding id,每条带
kind/authority 或 oracle/domain_swap/basis/evidence/**producer**);
② 执行点在 `normalize_findings.py` —— 五层产物入台账的**唯一漏斗**,纯函数、无需探针;
③ 拦下的落 `ledger_rule_suspects.json` 并接进 `suspect_sources` → 报告 §8/§9 自动可见;
④ 覆盖清单 `warrant_coverage` 入 `all_findings.json` + 报告 §10,已接/未接/未裁定并排;
⑤ 登记册**只覆盖未接闸门的生产者**(`test_registry_only_covers_ungated_producers` 锁死),
避免与门级闸门形成双载体(坑 11)。

**为什么这不是「手改产物充数」**:改的是**声明 + 漏斗机制**,产物由重生成得出;
下次探针重跑后,生产者仍会产出那条 finding,但漏斗照样拦下 —— 比门级闸门更稳。

**双向变异 8/8 KILLED(指名红名)**:
M1 漏斗恒放行 → `_guard_funnel_executes_business_verdict`;
M2 漏斗忽略登记册 → 同上;
**M3 漏斗把 technical 也压 → `_guard_funnel_keeps_technical`(A 类漏报,反向)**;
M4 confirmed 业务契约被压 → `_guard_funnel_keeps_confirmed_business`;
M5 覆盖清单藏未接生产者 → `_guard_coverage_lists_ungated`;
M6 未裁定数洗成 0 → `_guard_unadjudicated_recomputable`;
M7 文件级拆漏斗闸门 → `_assert_cli_executes_verdicts`;
M8 文件级藏未接行 → 同上。基线先绿,合成夹具不依赖 gitignore 产物。

**通用纪律**:受阻于「不能跑生产者」时,先找**唯一漏斗**能不能承载裁定;
能就把执行点移过去并加「声称⇒产物」守卫,不能才如实标未裁定。
**「未裁定」是一个必须显式计数、显式列名、显式进报告的状态,不是沉默。**

### 坑 114:`{"source": 标签, **info}` 会**静默覆盖** provenance —— 两个语义撞一个键(W21 落地时被自己的守卫抓住)

**缺陷态(我写的,落地前被新守卫判红)**:推导信息字典里带 `"source": "oracle_bridge.to_invariants"`
(意思是「用哪个函数转录的」),外层又写 `info["invariant_anchors"] = {"source": "derived_from_confirmed_oracles", **di}`
(意思是「这份产物哪来的」)。`**di` 在后 → **外层 provenance 标签被内层转录器名覆盖**,
产物里 `source` 变成了 `oracle_bridge.to_invariants`,而 `derived_from_confirmed_oracles` 这个来源分类**消失**。
同一处 `{"source": "unavailable", **di}` 也被覆盖 —— 「推不出」被报成「转录器名」。

**为什么危险**:这不是崩溃,是**出处标签被换了语义**。下游按 `source` 分「人工供给 / 推导 / 不可用」时,
推导与不可用两态会塌成同一个值,正是硬约束 3 要防的「不知道变成一个看起来确定的答案」。
与坑 11(同一封闭集活在多个载体)、做法 53(容器形状)同族:**键名是契约,不是装饰**。

**修法**:内层改名 `transcriber`(它本来就是转录器名),`source` 只留给 provenance。
守卫 `test_derived_anchors_never_claimed_as_human_business_declaration` 端到端断言
`report["upstream"]["invariant_anchors"]["source"] == "derived_from_confirmed_oracles"`,
变异 M21(把 `transcriber` 改回 `source`)被它**指名抓住**。

**规约**:① 用 `{**a, **b}` 合并前先算 `set(a) & set(b)`,非空就要问「这两个同名字段是一个语义吗」;
② **provenance / 分类标签不要与内容元数据共用键名**;③ 这种覆盖单测很难看见
(两个字典各自都合法),必须有一条**端到端断言最终标签值**的守卫。

### 做法 62:并发下报差值的口径 —— 同源双树 + **红名集合相等**,不是通过数相等(W21)

**实测处境**:编排者给的基线是「1303 pilot + 16 demo = 1319 passed + 12 subtests」。
本轮开工时实测已经漂了:同一命令收集到 **1411 条**,pristine 树就有 **10 failed + 6 errors**
(W22 在改 frame_check、W20 在改 relation_net、W15 在改聚合链,三家的新测试同时在飞)。
**拿一个漂移的通过数当基线,再报自己的差值,等于把别人的红算进自己的账**(坑 92 的反向误归因)。

**可照抄的做法**:
1. **一次 rsync 分叉出 pre/post 两棵树**,post = pre + 我的补丁 + 我的新守卫。
   `diff -rq pre post` 必须**只列出我拥有的文件** —— 这是「因子已隔离」的机械证明,
   之后无论别人怎么改仓库,这两棵树的差值都只归我。
2. **比红名集合,不比通过数**。本轮 pre/post 各 16 条红,`diff` 红名清单**逐条相同**,
   通过数 1395 → 1428(+33 恰为我的新守卫数)。**集合相等 + 增量等于新增守卫数**才是零回归。
3. **既有红按文件所有者归因,并给证据**:6 failed + 6 errors 全在 `demos/shop/test_d6_fixture.py`,
   报错原文 `RuntimeError: psql 失败: BLOCKED toolchain call: docker` ⇒ **环境**(本轮禁 docker/写库,
   PATH 挡板挡下 24 次);另 4 条在 `test_warrant_registry_funnel.py`(W15)与
   `test_satisfaction_channel.py`(W22)⇒ **他们在飞的工作**。不替他们判根因,只报归属与证据。
4. **依赖别人在飞的文件时,比「我依赖的那几个函数」而不是整文件 sha**。
   W20 的 relation_net 整文件 sha 本轮变了三次,但 `bind_entries` / `_controller_keys` /
   `anchor_tables` / `load_invariants` / `assert_fields` / `entity_tables` / `resolve_table` /
   `_covered_columns` **八个函数逐字相同**,并用当前版重跑绑定仍 25/19 ⇒ 我的归因不受影响。
5. **对照树与真仓库的引擎世代要核一次**:本轮 `diff -rq pre/pilot/engine 真仓库` 只多出
   `normalize_findings.py`(W15 刚改),而它**不被四支柱任何引擎引用**(grep 实证)⇒ 对照仍成立。

**落地后的并排口径(涨与不变一起报)**:绑定 0→25、锚 0→118(62 命中真表 / 0 假表 / 56 无容器)、
候选链 0→45(T1 24 / T2 12 / T3 9);**四支柱 state+分子+分母逐字不变**
(118/177、139/177、347/610、8385/148785),`frame_check.json` **逐字节相同**,
关系网 162 节点里**只有 25 个的 `endpoint` 字段变了**,198 条边逐字相同,退出码仍 2。

### 坑 120:ground_truth=verified ≠ 违反 verified —— positive 根因混进违反指纹,两细胞因共享表扬被判「对违反的看法一致」(W22 实测)

**现象**(已发布代码的真 bug,与 δ⁰ 扩展无关):`frame_check._section_value` 把**所有** verified finding
的非空 root_cause_key 当违反级截面值。测量快照(86 findings)里 83 条 verified 含 **15 条 kind=positive
(不变式被遵守)、1 条 observation**。后果:两细胞共享一个**正面**根因 → 假 consistent ——
那不是「对违反的看法一致」,是「对表扬的看法一致」。三态纪律只锁了 gt 轴(verified/unverified),
没锁极性轴(defect/positive/observation)。

**影响量化(先分解再加权,做法50)**:当前语料 **0**。分解(W22 首版两处数错,V20 复验后重算对齐):
15 条 positive = **12 未归因 + 3 归因**(首版误写 11+4 —— 把含 observation 的 4 从 15 里减,算术错);
observation 1 条单独归因(PackageController#updateStatus)。归因的 3 positive
(UploadController#uploadProductImage、OrderController#create/update)对足迹资源**零提及**
(claim 是 CJK,rck 是下划线单 token,_finding_mentions 精确 token 匹配不命中)→ 全桶成对标签 diff 0,
candidates_delta0 {0,9} / cycle_status {8,0,6} / obstructions 0 修前修后逐项不变。
**语料漂移警示**:首版测量后 W15 并发改 all_findings.json(86→83 findings、verified 83→80、
defect 67→64、归因 27→26),首版「83 verified/67 defect」对冻结快照正确但对当前语料过期 ——
分解数必须钉语料快照哈希(坑126)。0 对结论在漂移下不变(提及 0/连接 0/单细胞桶,重验)。
**机制错但语料未触发,仍须修 + 守卫** —— 下一个提到字段名的 positive finding 就会引爆。

**修法(已落地)**:违反集只收 kind 非 positive/observation(缺 kind 按遗留违反,兼容现有 111 条契约);
positive 不丢弃 —— `_satisfaction_section_value` 单独取值,产物 `findings_positive` 可见;
observation 两通道都不进。默认 δ⁰ 不消费满足集(消费是 opt-in 通道的事,坑121)。

**守卫双向(坑78)**:11 条极性守卫(test_section_value_polarity.py)。变异 M1 整删 kind 过滤 →
positive/observation 守卫双红;M2 只漏 positive → positive 守卫红、observation 守卫绿;
M3 只漏 observation → 反之 —— 每条守卫咬自己的因子。M4 把 positive 洗成 consistent → 红;
M5 丢弃 positive → 可见性守卫红。反向:只有 positive 的合法输入不得判违反/障碍,守卫锁死。

**规约**:
1. verified 证**观测可信**,kind 证**极性**;判定取值必须两轴同时锁,只锁一轴就是半个三态。
2. 修极性 bug 先量影响再修:归因 × 资源提及 × 共现三层分解,0 就报 0,不许拿机制严重性冒充影响面。
3. 正面证据不是垃圾,是另一条通道的输入 —— 修「混入」的正确动作是分通道,不是删除。

### 坑 121:同资源 ≠ 同性质 —— 满足级连接键必须是 (资源×判据);顶点导出标签是 coboundary,parity 永检不出障碍(W22 实测 + 结构证明)

**设计推回(带证据)**:编排者设计「A 在同资源有 verified 违反、B 有 proven guard ⇒ δ⁰=inconsistent」
在**资源级**连接下不可靠。真实语料里的近失反例:`OrderService#apply` 同时有 verified 缺陷
FN-APPLY-AMOUNT-INVARIANTS(定金能否大于套餐总价 —— **跨字段序关系**)与 proven guard
deposit>=0 / planTotal>=0(**一元非负**)。两者同时为真,零矛盾。若将来某 finding 提及 depositCent
token 且归因到同桶另一细胞,资源级规则会误判 inconsistent。guard 证据本身是 oracle 相对的
(capability_map `_enforces` 界蕴含)—— 满足自带性质匹配,违反侧必须连到**同一判据**才构成矛盾。
机械化:M9 变异(资源级连接)被 test_untied_violation_stays_unknown /
test_different_oracle_same_resource_stays_unknown 指名红。

**可行性(如实报 0)**:全局 (违反细胞×guard细胞×资源) **0 对**。分解:8 条 guard 边 / 4 个 ident,
所在桶**全是单细胞桶**(只有 guard 细胞自己触碰该资源,δ⁰ 是成对判定,第二细胞不存在);
0 条 verified finding 提及 guard 字段;0 条 finding 有 oracle 连接。**机制成立但语料不足**,
瓶颈在上游(足迹重叠 + finding→oracle 连接),不在取值域。不造数据。

**环级条件命题(首版「coboundary 定理」被 V20 两轮证伪:全称版→双条件版;终版四前提)**:
**在四个前提下**满足级通道产不出 cycle_obstructions:**P1 单向** oa≠∅ **⇒** va≠∅
(`_tied_oracle_ids` 谓词 = `_section_value` 谓词 **+ 判据连接**,tied 违反 ⊆ 违反指纹;
**反向 va⇒oa 不成立** —— 首版继承 V20 第一轮「谓词逐条相同」的双条件是错的,V20 已自行撤回,坑127);
**P2** firing 要求满足侧无违反指纹(`not vb`/`not va`);**P3** 无外部 judgments;
**P4** unknown 边被 `_parity_check` 排除(`label==unknown → continue`,不参与 union;藏在 parity 不在 δ⁰,
代码侧已有 6 条既有守卫,首版陈述漏列)。此时 1-边只跨 V/S 切割、0-边只在 V 内,c(v)=1[v∈S] 全局 2-着色
⇒ 已知边 coboundary ⇒ parity 恒一致。**四前提逐个破坏可达(已机械化,tests/test_satisfaction_premises.py,
入库可重跑)**:P1 破(去资源提及谓词)→1、P2 破(A1)→1、P3 破(judgments)→0→1、P4 破(unknown 参与 union)→1,
同输入未破坏对照全 0。P3 生产不可达(CLI 无 --judgments、audit_entry 不传),定理带此限定。
**P1 双条件与通道正确性互斥**:要 va⇒oa 成立就得拆判据连接键 = R9 推回的资源级缺陷(同资源不同性质
误判矛盾)—— 已写进产物 note,防后人朝「补全双条件」方向修。
**首版错误链**:依据写成「顶点导出⇒coboundary」(假命题,三互异指纹三角形反例)→ 继承 V20 错双条件
(未独立重推,坑128)→ 守卫用 2-细胞桶无环可测(A1 下绿,空守卫)→ 混合夹具名过度承诺
(三边全 unknown,测的是 P2 挡边不是已知边 coboundary)。**已修**:真 3-细胞环
test_p2_not_vb_blocks_all_edges_on_3cycle(A1 指名红,名实相符)+ 纯细胞阳性对照(3 已知边真跑 parity,
V20 独立重算 rel(1,0,1) XOR=0)+ P3 边界反例 + 四前提可达性守卫;notes 同步终版。
**保留的实测结论**:当前语料 cycle_obstructions 0、判定力落点在候选级 δ⁰;parity 的 consistent
不得读成「已验证一致」—— 它可能只是 coboundary 恒等式。

**词汇表分歧(记录不修,bridge 属 W21)**:capability_map._assert_bounds 接受 field_gte_const+field,
oracle_bridge 拒收(只认 left)—— 同一 assert 形状两引擎不同命运。真实 DDL 用 field_cmp 未触发;
与坑83/109 同族(词汇表活在两处)。

**opt-in 纪律(已落地,V20 复验后补全三锁,做法66)**:`--satisfaction` 默认关;缺 capability →
guard 源 INCOMPLETE + 告警,不静默当成「无 guard」;产物自述分解(guard 事实数 / oracle 连接 finding 数 /
触发数),0 触发可归因;头条不得写「已生效」。当前语料实测:guard 事实 8/8、连接 0/0、触发 0/0。
**三锁(V20 查出首版缺二)**:① 默认值守卫(不传参的 run() + 无旗标的真 CLI,A4/A8 变异指名红);
② 逐字节守卫(供 capability 不开开关 ≡ 完全不供,统计计算与发布同门,A-cap 变异指名红);
③ 自述面与判定面同门(disabled 时 stdout 报「未测量」,不得发布 8/8 自相矛盾)。
**价值陈述(首版漏写,已补进产物 notes)**:通道唯一产出是候选 δ⁰=inconsistent,但
audit_entry.pillar_emergence 的 RED **只消费 cycle_obstructions,从不读候选 delta0 inconsistent**,
且 audit_entry 不传 --findings/--capability/--satisfaction ⇒ **双重未接线,对已发布裁定零影响**。
候选级 inconsistent 是待接线的机器证明矛盾,不是已生效裁定。

**规约**:
1. 补取值域前先问「连接键是不是性质粒度」—— 资源级共现只是线索,判据级才是矛盾。
2. 声称新通道有检测力,先测共现对数与连接数;0 就写「机制成立≠语料充足」进产物自述。
3. 「顶点导出」不蕴含 coboundary;环级无矛盾的充分条件是 1-边构成二分切割(带 P1/P2/P3 前提,坑125)。
   环级定理守卫必须有 ≥3 细胞的真环,且变异掉定理依赖的实现细节验证守卫有牙;全称陈述带可达性限定。

### 做法 64:裁定载体=声明、执行点=唯一漏斗 —— 探针禁跑也能真执行裁定;验漏斗先打漏报方向(V18 复验 W15 重修,PASS)

**背景**:V18 一轮(坑 108)证伪 W15「裁定写在 docstring 但产物未执行」。W15 重修的核心洞察:**把「不能跑探针」误推成「不能执行裁定」是错的 —— 裁定的载体是声明(warrant_registry.json),执行点可以不在生产者,而在台账的唯一入台账漏斗(engine/normalize_findings.py)**。探针(perf_load/concurrency_props/orm/config/property_binding…)禁跑、装不了源头闸门,但它们的发现都过 normalize 漏斗,登记册在那里被消费:business 非 confirmed 拦下并落 ledger_rule_suspects.json,technical 放行,**未登记的不受影响**(只进「未裁定」计数)。

**复验六项全过(独立造输入,不信它自己的测试)**:
1. **漏报方向(最危险)实测**:往真 `nf.apply_warrant_registry` 灌合成记录 —— technical(自带/登记册)、未登记、confirmed business、positive/observation **全部保留**;business 非 confirmed 被拦(阳性对照);未登记计入 unadjudicated+unknown。**零误压**。
2. **登记册↔产物一致性**:独立遍历 12 条(9 technical 全在台账;3 business 非 confirmed 全不在台账且在 suspects 通道状态一致)。**自造缺陷态验守卫有牙**:把 SVC-PAGINATION 塞回台账+移出 suspects(模拟「只写登记册不跑漏斗」)→ `test_every_adjudicated_business_claim_is_executed_in_the_artifacts` 红;删一条 technical → `test_every_adjudicated_technical_claim_stays_in_the_ledger` 红;恢复复绿。
3. **第三条同源假阳性**:INT-LIST-COUNT-END-TO-END-WASTE 与 SVC-PAGINATION 同预期(「前端不读 total 故后端算它零收益」),同被 AGENTS.md:139 分页契约(含 total/totalPages)否定 → refuted 成立。**普查无第四条同源**:LIST_TRUNCATED_AT_100(HIGH 仍在台账)是**另一条**预期(前端固定 page=1/pageSize=100 不可翻页),登记册 note 已显式声明「本轮未裁定」,非 total-waste 同族。
4. **覆盖清单自数**:15 生产者 warrant 2 / oracle 4 / none 9 / unknown 0,与产物逐字一致;9 个 none 名下 defect 逐行重算 unadjudicated=3(JAVA-SERVICE-EXPOSES-JPA-ENTITY / FN-MUTATION-SURVIVOR-WEAK-ASSERTION / FN-NUMERIC-MONEY-INPUT-SILENT-MUTATION),与产物相等;unknown 确被计入(合成验证)。
5. **累计 12 稳定 ID 移除**:按 (layer,id) 比 HEAD(82 unique)→工作树(70 unique),移除 12 / 新增 0 / 共有 ID 严重度变更 0。12 条全有证伪依据(逐条追到 suspects 通道+audit_report 裁定;FN-SEMANTIC-PRICE-UPPER-BOUND 挂在 rule_id OB-RESOLVE-PRICE-UPPER-BOUND 下,finding.id 匹配)。移除的 4 条 CH 全是价格假阳性,真 CH 技术缺陷一条没丢(当前 4C/15H/38M/9L=66)。
6. **新锚点变异 5 个(含 2 漏报方向,双向)**:R2-M1 把 technical 路由进 unconfirmed(A 误压)→7 红;R2-M2 删 `or not w`(未登记误压)→恰 1 红 test_funnel_unregistered_passes…;R2-M3 business 恒放行→6 红;R2-M4 等价重构 `.get(id,None)`→存活全绿;R2-M5 unadjudicated 洗 0→6 红。空对照 30 绿。

**基线归因(坑 92)**:`pytest pilot/tests demos/shop` = 1435 passed + 12 subtests + 6 failed/6 errors。6 红全是 demos/shop/test_d6_fixture.py,`RuntimeError: psql 失败: V18 PATH BLOCKER: docker called` —— 我的 docker 挡板拦了 demo 库(硬边界禁),非缺陷。比声称 1425 多 16 条**全绿**(并发 W20/W21/W22 落地新测试,移动靶),非回归。字节码 64 class、git status 86、6 个关键文件 sha256 前后不变。

**残留(如实报,非 W15 本轮失败)**:4 个 oracle-only 生产者(class/fn/integration/business)只有 split 闸门、无 A/B 二分,清单已诚实标 gate=oracle;其下 LIST_TRUNCATED_AT_100(HIGH)等可能是未裁定业务预期,属已披露的下一轮工作,不是隐藏洞。3 条 unadjudicated 是设计内的待裁定队列,未被静默澄清。

**规约**:
1. **裁定与执行解耦**:声明放登记册(项目侧),执行放唯一漏斗(归一化处)。探针禁跑 ≠ 裁定不能执行 —— 只要所有发现都过一个漏斗,就能在那里执行声明。这解了坑 108「守卫分母 < 裁定 population」的死结。
2. **验漏斗先打漏报方向**:technical/未登记/confirmed 三类必须保留,任一被压即 FAIL。漏报比假阳性危险,变异矩阵必须含「误压 A 类」「误压未登记」两个反向锚点。
3. **守卫牙齿要自造缺陷态验**:把「裁定写了但产物没执行」精确复刻(塞回台账+移出 suspects),看指名守卫是否红;只跑被验方自己的变异不够。
4. **覆盖清单三口径并排**(已接 warrant / 仅 oracle / 未接 none+unknown),unadjudicated 必须可逐行重算且列名,unknown 不得静默当成已接。与坑 108、做法 46/50/56 同源。

### 坑 122:javap 抽取器方法签名正则把 lambda 名截成尾数字 → 同类多 lambda 键碰撞覆盖(V19 独立实测)

**现象**:W20 的 extract_field_access.py 用 `METHOD_SIG = ^  [^ ].*\b([\w$<>]+)\(...` 抽方法名。
贪婪 `.*` 回溯到**最后一个 `\b`**,而 `$` 不是 `\w`,故 `lambda$create$0` 被截成 `0`、
`lambda$orderSpec$6158dec4$1` 被截成 `1`。OrderService 的 lambda$buildPackageSnapshot$1 与
lambda$orderSpec$…$1 **同尾数字 1 → 键碰撞,后者覆盖前者**(V19 用自己的非贪婪正则重抽 63 类逐条比对坐实)。
另:record 超类漏抽(ApiResponse super='' 实为 java.lang.Record);FIELD_REF `Field ([\w$]+):`
遇 owner 前缀(`// Field owner.name:`)会漏(本语料 0 命中,潜在脆弱)。

**作用域(关键,不许夸大)**:这些缺陷**只落在 service/config/exception 的 lambda 与 record**,
**实体类零分歧**(V19 独立重抽:entity 类 mapping diff=0,51 个被调实体 getter reads 逐条 51/51 全等,
6 个 BaseEntity 继承 getter 沿 FQCN super 链解析正确)。交付的 relation_net 读通道**只解析实体 getter**
(实体无 lambda),故 W20 的 7 项声称不受影响——这是「抽取器潜在缺陷」不是「通道缺陷」,两者分开报。

**修法**:方法名正则用非贪婪 `.*?` + `[\w$<>]+` 贪婪吃全名(含 `$`);record 超类单独识别;
FIELD_REF 取 `:` 前最后一段(兼容 owner 前缀)。

**通用纪律**:javap 文本抽取的「方法名/字段名」正则必须对 **lambda 合成名(`lambda$enc$N`/`lambda$enc$hash$N`)、
record、内部类、owner 前缀字段引用**逐类自测;报「N 个方法全抽取」必须并排报「键唯一性」(碰撞数),
否则覆盖数掩盖信息丢失(坑 3/64 同族:计数涨≠信息全)。验证者要用**独立正则重抽逐条比对**,不能只信抽取器自报。

### V19 独立验证结论:W20 足迹契约 opt-in 通道 7 项声称全部 PASS(独立重算,非照抄)

**口径**:冻结 HEAD 9b6f3b3,relation_net sha 7d2b6c7f(验后不变);自建 /tmp/v19 沙箱 + PATH 挡板(javac/mvn/docker/jar 禁,javap 只读允许);
当前 pilot 基线 **1425 passed + 12 subtests**(声称 1303 已陈旧,在飞 agent +122;变异用隔离副本 16 守卫,不受在飞影响)。
**用交付 build(field_access=…) 路径 + V19 自抽字节码映射**(非 W20 post-hoc 原型)独立重算:
① 51/51 实体 getter 可验证、6 继承正确、7 isActive record 读自身名、TraceContext 零字段访问(仅 static ThreadLocal)——全确认;
② +3 firing 桶独立枚举全是真 iv(setItems/setPriceCent×2),第 4 新桶 setBanquetType=nonnull 正确归 co-singleton 不计 firing;
③ solo +18 候选 0 写-写、14 有锚;补 18 条 lambda 边后 4 条 method×own-lambda 翻 dep,**5 条跨类独立全部仍独立**(BFS + 直接调用图双重核:productSnapshot 只调实体 getter、applyBase 只调 setter+repo 读,均不到 create);
④ 单因子:9 既有候选裁决级字段(frame_violation/strength/delta0/survives_filter)**0 变化**,仅 2 处 overlap_written 证据富化;
⑤ capability_map 自跑 off/on **0 非 reproduce 差异**;⑥ normalize_resource:54 既有 setter 分支认得 setter 等价键,通道无需改 frame_check;
⑦ 默认关两次 CLI 逐字节相同、taint 与发布产物全节点一致、无通道键。
**V19 新变异 3 锚点全 KILLED 指名命中**(M1 强名合并不可验证→test_computed_getter_keeps_split 等 3 红;
M2 ThreadLocal static 当字段读→test_threadlocal_static_accessor_not_emitted 恰 1 红;M3 默认开→test_default_off_is_byte_identical 恰 1 红);
反向合法不可验证保分裂 3 守卫绿(坑 78 双向);空对照 16 绿;哨兵 repo+字节码不变。
**残留**:坑 122 抽取器 lambda 键碰撞(作用域外,建议修);「+3 firing」是 firing-CAPABLE(域类型可不相交)非「已检出 3 违规」(3 桶当前 consistent),面向非专家须并排报当前 status(坑 66)。

### 坑 123:变异矩阵不入库,就不是别人能重跑的证据(V17 查出,主控记)

**实测**:W21 声称「21/21 变异全杀」,但它的 harness 在 `/tmp/w21`,**没有入库**。
V17 复验时**无法独立重放那个矩阵**,只能改为「验 33 条已入库守卫 + 自己另造 4 个锚点」。
结论仍然成立(它 4 个新锚点全杀),但**「21/21」这个数字本身不可复核**。

**为什么这是问题**:本项目的硬约束是「每个增量必须经独立 Agent 证伪」。
证伪的载体是**可重跑的守卫**,不是交付方报告里的数字。
harness 留在 /tmp ⇒ 会话结束即消失 ⇒ 那个矩阵退化成**一次性实验**(同坑:GF(2) 的 400 图曾是一次性的)。

**规约**:
1. **变异矩阵要么入库成常驻守卫,要么在报告里明确标「一次性实验,不可重放」**。
   不得把一次性矩阵的数字写成好像是持续保护。
2. 交付时**至少把锚点清单与期望红名入库**(哪怕 harness 本身是临时的),
   让复验者能照着重建。锚点是知识,harness 只是工具。
3. 复验者遇到不可重放的矩阵,**不要照抄数字**,自己造锚点 —— V17 这次做对了。
4. 与「GF(2) 400 图从一次性实验变成常驻守卫」同源:**测量要变成守卫才算数**。

### 坑 124:javap 签名解析的两个名字陷阱 —— 贪婪正则截断 lambda 同尾数字碰撞,\b 锚不住 $ 前导名(W20,V19 残留1 修)

**缺陷(坑122 的根因)**:`METHOD_SIG = r'^  [^ ].*\b([\w$<>]+)\(...'` 有两处错:
① 贪婪 `.*` 回溯到最后一个 `$`,`\b` 在 `$→数字` 边界成立 ⇒ `lambda$buildPackageSnapshot$1`
与 `lambda$orderSpec$6158dec4$1` **都截成 "1"**,dict 静默覆盖(OrderService 5 个 lambda 只剩 4 键);
② 只改非贪婪还不够:`$deserializeLambda$` 的前导 `$` 在 space→$ 处**无 \b 边界**,仍丢成 `deserializeLambda$`。
**修法**:非贪婪前缀 + **去掉 \b** + 贪婪名字段:`r'^  [^ ].*?([\w$<>]+)\((.*)\)...'`
(名字=紧邻 `(` 前的字符段,不需要边界锚)。

**守卫(双向)**:selftest 九个签名(两个同尾数字 lambda/常规/构造器/record/泛型/$ 名)
+ **独立参照交叉校验**(取 `(` 前最后空白 token,不用正则名字段),全 63 类逐行断言一致。
变异 E1 贪婪回退→红「'1' != lambda$orderSpec$6158dec4$1」;E2 非贪婪保留 \b→红「丢失前导 $」。2/2 指名 KILLED。

**修后核实体映射(任务硬要求)**:51 条被调 JPA getter 映射**逐字不变**;通道 58 资源、
+18 候选、+3 firing、obstructions 0 全部复现。实体包唯一差异在 **OrderStatus(非 JPA 枚举)**:
修前 `$values()` 截成 `values` 覆盖真 values(同族碰撞),修后各归其位——是修复不是回归。
已知局限(通道作用域外,显式记不修):`static {}` 不解析为方法,其 Code 归前一方法(枚举 $values)。

### 做法 65:firing-capable 措辞必须带「不是已检出违规 + 当前桶状态」双限定,且限定本身要有守卫(W20,V19 残留2)

坑66 纪律落到**生产者侧**:relation_net 开启读通道时,modeling_domain 的 footprint_channels
带 `detection_power_note`(firing-capable=域可不相交,**不是已检出违规**;新增桶**当前全是 consistent**,
obstructions 不变),CLI 并排打印同句。守卫锚定**否定词与现状短语本身**(坑102:子串/计数锁不住极性):
M8 删限定→红;M9 把「当前全是 consistent」翻成 inconsistent→红;M10 删 CLI 限定→红。
实测 4 个新约束桶(3 iv + 1 nonnull)状态全 consistent——限定与事实相符,不是装饰。
**规约**:凡报「可 firing/可检出」的产物自述与 CLI,必须并排报当前 status;
限定措辞进守卫,丢限定或翻极性必须红。面向非专家的头条不许只写「+3 firing」。
### 坑 125:「顶点导出 ⇒ coboundary」不是定理 —— 真依据是 V/S 二分切割;且 2-细胞桶的「环定理守卫」无环可测(V20 独立实测)

**背景**:W22 交付满足级 δ⁰ 通道,声称证了结构性定理「违反/满足标签是顶点导出的余边界,
GF(2) 奇偶恒一致 ⇒ 通道结构上产不出 cycle_obstructions」,并「把定理机械化成守卫」。

**V20 独立推导 + 探针,结论分三层(不许混为一谈)**:

1. **定理本身在 CLI 可达路径上成立,但依据不是「顶点导出」**。真依据:
   `_violated_oracle_ids` 与 `_section_value` 的 finding 谓词**逐条相同**(verified + 非空 rck +
   提及资源 + kind 过滤),故 `oa≠∅ ⟺ va≠∅`。于是 `inconsistent` 的 `oa and not vb` 要求
   **一侧 V(违反指纹非空)、另一侧 S(指纹空)** ⇒ **1-边只跨 V/S 切割**;`consistent` 要求两侧非空
   ⇒ **0-边只在 V 内**。取 c(v)=1[v∈S] 即全局 2-着色 ⇒ coboundary ⇒ 无奇环。
   实测:4000 次随机顶点状态组合、657 条 inconsistent 边,**全部**跨 V/S;全部 consistent 边在 V 内。
   3-细胞真环探针:2 条 inconsistent + 1 条 unknown → obstructions 0。

2. **「顶点导出的二值标签恒为 coboundary」是假命题**(LESSONS 坑121 规约3 与
   `frame_check.py:1063` 既有 note 都这样过度一般化)。反例:边标签 =「两侧违反指纹不同」,
   **完全由顶点状态导出**,三个互异指纹的三角形 → 三边全 1 →
   `_parity_check` 返回 `('inconsistent', ('B#m','C#m'), 3, 0)`。
   **顶点导出 ≠ 余边界**;充分条件是 1-边构成二分切割,不是「由顶点导出」。

3. **「结构上产不出 cycle_obstructions」在 API 层被证伪**。`run(judgments=…)` 混合覆盖时:
   judgments 给 A-B=consistent、B-C=consistent,满足级通道给 A-C=inconsistent →
   环 status **inconsistent**、obstructions **0→1**(同输入 satisfaction=False 时为 0)。
   **作用域限定(如实报)**:CLI 无 `--judgments` 旗标、audit_entry 也不传,故**生产不可达**;
   但定理陈述没带这个限定。另:定理成立**依赖 `not vb` 这个实现细节**——去掉它(A1 变异)
   三细胞各带违反+guard → 三边全 inconsistent → obstructions 1。它不是结构必然。

**守卫无牙(坑78/101、做法55 的教科书实例)**:指名守卫
`test_satisfaction_labels_are_coboundary_no_cycle_obstruction` 用的是 **2-细胞桶**——
**2 个顶点根本没有环**,`status != "inconsistent"` 恒真,与定理无关。
A1 变异(去 `not vb`,真能造出环障碍)下该守卫**保持绿**,唯一红的是
`test_different_rck_both_violating_stays_unknown_even_with_guard`(δ⁰ 语义守卫,不是定理守卫)。
**「定理已机械化」不成立**:要锁定理必须用 ≥3 细胞桶 + 断言 1-边跨 V/S + 断言全已知边环 parity=0。

**局限声明的位置不对称**:局限**写进了 LESSONS 坑121**(显著),但**没写进产物自述的满足级 notes**
(`frame_check.py:1050-1055` 只讲机制/连接键/0 触发分解,一句不提环级结构局限);
唯一提 coboundary 的是既有通用 note 1063,读者不会把它与新通道关联。

**规约**:
1. 声称「结构定理」必须写出**充分条件**,并把条件本身当断言对象。此处条件是二分切割,
   不是「顶点导出」;依据写错,定理就算结论对也是**未证明的断言**(坑99 同族)。
2. **环级定理的守卫必须有环**。2-细胞桶测不出任何环性质,`status != inconsistent` 是恒真式。
   报「定理已机械化」前先问:我的夹具里有几个顶点?
3. 定理依赖某个实现细节(`not vb`)时,必须**变异掉那个细节**看定理是否塌;塌了就说明
   它是**当前实现的性质**,不是通道的结构性质,措辞要降级。
4. 全称陈述(「结构上产不出」)必须带**可达性限定**(CLI 无 judgments 入口 ⇒ 生产不可达),
   否则 API 层反例即证伪。与坑107「扩域≠扩检测力」同源:作用域不许省略。

### 做法 66:opt-in 通道要锁**三件事**,只锁「关掉时的行为」等于没锁默认值(V20 实测 W22)

**实测两个存活变异(全绿零红)**:
- A4:`run(..., satisfaction: bool = False)` → `True` ⇒ **197 passed / 0 red**。
- A8:CLI `ap.add_argument("--satisfaction", action="store_true")` → `store_false` ⇒
  **241 passed / 0 red**,且变异后**不带旗标**跑 CLI 真的启用通道
  (`满足级通道: ok（guard源 INCOMPLETE）`)。

**为什么守卫没咬住**:`test_default_off_never_inconsistent` **显式传 `satisfaction=False`**,
它锁的是「关掉时的行为」,**不是「默认是关的」**。全仓 grep `byte_identical|逐字节|store_true`
在该通道守卫里**零命中**。对照:W20 的同类通道有 `test_default_off_is_byte_identical`,
V19 的「默认开」变异被它恰 1 红杀掉 —— **同一项目里姊妹通道的纪律比它强**。

**第三件漏的事:默认关时产物并非逐字节相同**。供 `--capability` 但**不开** `--satisfaction`:
sha `441c7816…` vs `f66d28d9…`,差异恰 3 键
`satisfaction_guard_stats.{facts_used 0→8, guard_edges_seen 0→8, excluded_not_guard 0→210}`。
根因:`run:859` 无条件算 `_guard_facts`,`run:1040` 无条件发布,而 `run:1037` 的
`resource_level_satisfaction_judgments` **有** satisfaction 门 —— **同一产物里两个口径,一个 gated 一个没 gated**。
后果是可复现的自相矛盾 stdout:`满足级通道: disabled（guard源 disabled） guard事实 8/8 …`
(做法51:stdout 文案也是判定面;非专家会把 8/8 读成「通道测到了 8 条 guard」)。
**判定面确实逐字节相同**(counts/candidates/cycles 全等,真实语料 off vs on 也是),漏的是自述面。

**规约 —— opt-in 通道必须同时锁三件,缺一即 PARTIAL**:
1. **默认值**:变异 `run()` 签名默认与 CLI `store_true`,两者都必须指名红。
   显式传 False 的测试**不算**默认值守卫。
2. **逐字节**:默认关的产物 vs 基线逐字节相同,**且要覆盖「供了证据输入但没开开关」这一格**
   ——只测「什么都不供」会漏掉 ungated 的统计键。
3. **自述面与判定面同门**:凡 gated 通道的统计量,发布点必须与判定点用**同一个门**;
   否则 disabled 通道会发布非零事实数,头条自相矛盾。

### V20 独立验证结论:W22 三态修正 + 满足级通道 —— PARTIAL(机制真,数字与定理陈述有错,opt-in 三缺二)

**口径**:冻结 HEAD 9b6f3b3;`frame_check.py` sha `cef9aa20…` **验前验后不变**;
pilot 根 139 文件 **0 漂移**;git status **86 不变**。自建 /tmp/v20 沙箱 + PATH 挡板
(javac/java/mvn/docker/psql/gradle 禁;javap 只读允许)。基线 **1425 passed + 12 subtests, rc=0**,
验后**同数同红名集合(空)**⇒ 零回归。空变异对照 197 绿(坑92 第 1 步已走)。
W22 自报 1424,差 +1 归因在飞 W20/W21/V19(V19 同轮也记 1425),非回归。

**PASS 的部分(独立重算,非照抄)**:
- **极性修正机制真**:M1 整删 kind 过滤 → 8 红(两类极性守卫双红);M2 只漏 positive → 7 红且
  observation 守卫**绿**;M3 只漏 observation → 恰 2 红。**每条守卫咬自己的因子**,坑78 双向成立。
- **影响 0 比它声称的更强**:把 `_section_value` 换回旧行为跑真实语料,
  **整份 report 逐字节相同**(不只头条计数);546 细胞 × 115 资源逐格比对,截面值差异 **0**;
  逐候选 delta0 差异 0、逐环 status/basis 差异 0。
- **候选级 inconsistent→unknown(M4)被 7 条指名守卫杀**(含 opt_in_fires / symmetric_direction /
  tie_via_evidence_refs / tied_positive_finding)⇒ **隐藏真障碍的方向已锁**。
- **反向零假阳性**:3-细胞全 positive 全 tied 全 guard、observation-only、未verified positive、
  真实 16 条 positive/observation 子集 —— 开关两态下 inconsistent 恒 0、obstructions 恒 0,
  且 `findings_positive` 可见。
- **零触发可归因,分解独立复现**:guard 事实 8/8、tied 违反 0/正面 0、触发 0/0;
  4 个 (细胞,ident) guard 事实**全在单细胞桶**(depositCent/planTotalCent→OrderService#apply、
  priceCent→PackageService#applyBase、priceCent→ProductService#apply),δ⁰ 是成对判定故第二细胞不存在;
  **83 条 finding 无一有 `oracle_id` 字段**;(违反×guard×资源) 共现对独立枚举 = **0**。
- **126 绿精确复现**(frame_check 101 + domain 10 + parity 4 + polarity 11)。
- **词汇不匹配真实存在**:最小复现 `{type:field_gte_const, field:amount, value:0}` →
  bridge 拒(`带求值器不消费的键 ['field']`)、`capability_map._assert_bounds` 收(`('numeric',[('>=',0)])`)、
  `frame_check._oracle_constraint` → None;换 `left` 三者全收。**真实语料影响 0**
  (341 条判据全用采集器词汇,封闭原语 **0** 条),它「field_cmp 未触发」的判断对。
  **归属判断对**:bridge 在本轮硬边界里就是「V17 在验 W21」,mtime 08:17 早于 W22 的 18:22,不该它修。

**FAIL / 须撤回或补的部分**:
1. **分解数字错两处(做法50 同族,结论不变但数要撤)**:
   ①「83 条 verified」→ 实为 **83 条总 finding、80 条 verified**(64 defect + 15 positive + 1 observation),
   另 2 unverified + 1 mutant_survivor。②「15 条 positive 里 11 条未归因、归因 4 条」→
   实为 **12 条 positive 未归因 + 3 条 positive 已归因 + 1 条 observation 已归因**;
   11+4=15 内部不自洽,因为那 4 条里的 observation 不属于 15 条 positive。
   零资源提及 **0/16** 确认无误。
2. **`_satisfaction_section_value` 是死代码**:生产路径**零调用**,只被自己 2 条单元测试引用。
   A3(极性翻转)与 M5(丢弃)产生**完全相同的红名集合** —— 未接线函数的指纹。
   「positive 不丢弃 —— `_satisfaction_section_value` 单独取值」**夸大**:产物可见性来自
   `find_candidates:468-470` 的内联过滤,真通道用的是 `_satisfied_oracle_ids`(判据 id 键),另一套机制。
3. **定理依据错 + 守卫无牙 + 全称陈述被 API 反例证伪** → 见坑125。
4. **opt-in 三缺二**(默认值无守卫、供 capability 时非逐字节、自述面 ungated)→ 见做法66。
5. **通道价值没说清(不是夸大,是漏了关键事实)**:通道唯一产出是候选 δ⁰=inconsistent,而
   `audit_entry.pillar_emergence` 的 RED **只认 `cycle_obstructions`**,delta0 只读 unknown/consistent,
   **从不读 inconsistent**;且 audit_entry **不传 `--findings`/`--capability`/`--satisfaction`**。
   ⇒ 本通道**对已发布裁定零影响,双重未接线**。它写了「判定力全在候选级 δ⁰」,
   但没写「候选级 δ⁰ 不被裁定消费」。另 `audit_entry.py:351-352` 的
   「候选 δ⁰ 只有 consistent/unknown 两态,给不出 inconsistent」现已**一般性陈旧**
   (仅对该调用路径仍真),属 W21 文件的跨文件接缝残留,不是 W22 该修。
   **它没有声称增强涌现检测力**(这个方向无夸大),但「价值是什么」的诚实答案是
   「当前为零,瓶颈在上游足迹重叠 + finding→oracle 连接 + 裁定消费」,这句没写。
6. **9 处坑号交叉引用错**:`frame_check.py:243,333,371,1050,1052` +
   `test_section_value_polarity.py:1` + `test_satisfaction_channel.py:1,141,329` 全写 **坑110/111**,
   实际条目是 **坑120/121**(坑110/111 是 W21 的推导锚表坑)。顺引用会读到错的教训。
7. **13/13 变异矩阵不可复核(坑123)**:harness 未入库,且两份守卫文件里**无任何锚点清单**
   (grep M1/M9 零命中)。V20 **未照抄该数字**,自建 12 锚点(M0 空对照 + M1-M5 + A1-A7 + A8 CLI),
   其中 **A4/A8 存活**,其余指名红。

**残留(如实报,非 W22 本轮失败)**:两引擎词汇分歧面比它记录的大 ——
341 条判据双引擎普查:**6 条 capability_map 收而 bridge 拒**(全是 `field_cmp`+`unit:'MB'`,
bridge 拒绝单位换算、capability_map 把界当无量纲数值),另 **15 条 bridge 收而 capability_map 不收**。
6 条单位案是 bridge 的**设计性拒收**,但 capability_map 照收构成真语义分歧。
属 capability_map/oracle_bridge 所有者,不在 W22 权限内。
并发漂移:`relation_net.py`(7d2b6c7f→eaf9842e)与其 getter 守卫被 W20/V19 改动,**非 V20 残留**;
LESSONS.md 本轮从 4991 行漂到 5096 行(其他验证者在追加)。
### 坑 126:分解数不钉语料快照哈希,并发漂移让它静默过期 —— 且「15−4=11」把 observation 减进了 positive(W22,V20 复验查出)

**两处错(结论 0 对不变,数错)**:W22 首版分解报「15 positive = 11 未归因 + 4 归因」与
「83 verified = 67 defect + 15 positive + 1 observation」。V20 独立重算对不上:
1. **算术错**:归因的 4 条含 1 条 observation;positive 归因实为 3,未归因 = 15−3 = **12**。
   把跨类别的计数(positive+observation=4)从单一类别(15)里减 —— 分解的桶必须先互斥再相减。
2. **语料漂移**:首版测量(86 findings/83 verified/27 归因)后,W15 并发改 all_findings.json
   → 83 findings/80 verified/64 defect/26 归因。首版数对冻结快照正确,对当前语料过期。
   报告没钉语料哈希,读者无法判断「数错」还是「语料变了」—— 两者归因完全不同(坑99:
   撤回/修正前先做能判定的实验;这里实验 = 重算 + 比对冻结产物记录的 findings_total)。

**判定实验(已做)**:冻结基线产物 out/baseline.json 记录 findings_total=86/attributed=27,
当前重算 83/26 ⇒ 漂移属实,非首版算术错(算术错独立存在,两处各自归因)。
0 对结论漂移不变:guard 字段提及 0、oracle 连接 0(全局 83 条)、4 个 guard 桶仍单细胞,重验。

**规约**:
1. 报分解数必须钉**语料文件哈希**(不是 findings_total 这类计数 —— 计数本身会漂移,钉它等于没钉),
   快照清单**入库**(pilot/baselines/w22_satisfaction_snapshot.json,坑123:只在 /tmp 不算别人能重跑的证据),
   并声明测量时刻;并发在飞时,交付前对当前语料**重算一遍**,差值分「我算错」与「语料漂移」两桶归因。
   (W22 首版钉错对象:钉了计数且只在 /tmp,V20 第二轮查出,已改钉 6 输入+引擎+测试的 sha256 入库。)
2. 分解桶先互斥再相减:positive/observation/defect 是三个桶,拿 positive+observation 的和
   从 positive 里减是范畴错。分类≠加权(做法50)之前还有一步:**分类必须互斥**。
3. 与坑88(世代错配)、做法59(单因子隔离先核同输入)同源:数字过期不是错,不声明快照才是错。
### 坑 127:验证者自己给的「谓词逐条相同」是错的 —— P1 双条件被交付方逐字继承进产物(V20 第二轮自踩并撤回)

**我上一轮的错**:V20 第一轮报告写「`_violated_oracle_ids` 与 `_section_value` 的 finding 谓词
**逐条相同**,故 `oa≠∅ ⟺ va≠∅`」。**漏了一条**:`_tied_oracle_ids` 末尾还有
`out.update(t for t in ties+evidence_refs if t in oracle_ids)` —— **必须存在判据连接**。
`_section_value` 没有这一条。所以谓词**不相同**,双条件**假**。

**最小反例(实测 6 例中 3 例破)**:verified defect + 提及资源 + 非空 rck,但
① 无 `oracle_id`/`evidence_refs`;② tie 指向不在 `oracle_ids` 的 id;③ `evidence_refs` 只含
`X.java:10` 这类非判据字符串 —— 三种都 `va≠∅` 而 `oa=∅`。

**后果**:W22 第二轮**逐字继承**了我的错,写进 LESSONS 坑121 重述与
`frame_check.py` 产物 note(「P1 违反集与截面值谓词相同(oa≠∅⟺va≠∅)」)。
**验证者的错误比交付方的错误传播得更远** —— 它会被当成已独立核过的结论抄进产物自述。

**定理结论不受影响,但依据要改**:证明只需要**单向** `oa≠∅ ⇒ va≠∅`
(1-边要求 `oa` 非空 ⇒ 该侧 ∈ V;`not vb` ⇒ 另一侧 ∈ S)。20000 次随机顶点状态、
3430 条 inconsistent + 487 条 consistent,**零违例**。
**而且双条件与本通道自身的正确性互斥**:要让 `va≠∅ ⇒ oa≠∅` 成立,就得**去掉判据连接要求**
—— 那正是坑121 推回的资源级连接缺陷(R6 变异,4 条守卫指名红)。
所以 P1 写成双条件不是笔误,是**与设计要求矛盾**。

**规约**:
1. **验证者给出的「前提/依据」也要自己被变异核**。我核了它的守卫、它的数、它的定理结论,
   却把**我自己推的中间引理**当成已证事实交出去 —— 引理没被任何守卫锁,于是错了没人发现。
   凡写「谓词逐条相同」「X ⟺ Y」,必须**逐条列出两边的条件并造反例**,不能凭印象。
2. **双条件比单向蕴含强得多,代价也大得多**。证明只用 ⇒ 就别写 ⟺;写强了不仅是错,
   还可能与系统别的正确性要求冲突(此例:⟺ 要求拆掉判据连接键)。
3. 发现自己的错**立刻撤回并指名传播范围**(此处:LESSONS 坑121 + frame_check 产物 note),
   不能只在自己报告里改口 —— 坑99 的「撤回要及时」对验证者同样适用。

### 做法 67:条件命题的前提清单要**逐个破坏验可达**,并主动找**未列出的第 N 个前提**(V20 第二轮)

W22 把全称定理重述为 P1+P2+P3 条件命题,并声称「任一前提破坏可达」。
**逐个破坏实测(不是只验它举的两个例子)**:

| 前提 | 破坏方式 | 结果 |
|---|---|---|
| P2 | 去 `not vb`/`not va`(A1) | known=3 status=inconsistent **obstructions 1** |
| P1 | 违反侧收 positive/observation(N2) | known=3 **obstructions 1**(正确实现同输入 known=0/obstructions 0) |
| P3 | 注入 judgments 两条 0-边 | **obstructions 0→1** |
| **P4(未列)** | `_parity_check` 把 unknown 当 rel=1 而非排除(N1) | known=3 **obstructions 1**(正确实现 0) |

**P4 是定理漏列的前提**:「unknown 边必须被 parity 排除,不得当成任何确定关系」。
三个 S 类细胞(全 positive、无 guard)三边全 unknown,一旦当成 1 就是奇环。
代码侧**已被 6 条既有 parity 守卫保护**(N1 下 `test_unknown_edge_never_becomes_consistent`、
`test_no_known_edges_is_unknown_not_consistent`、`test_parity_matches_brute_force_on_random_graphs` 等红),
所以是**陈述不完整,不是代码有洞** —— 两者要分开报。

**另一条纪律:守卫名不许承诺它没测的性质**。
`test_channel_coboundary_on_real_3cycle_mixed_cells` 拓扑上是真 3-细胞环,但
**三条边全 unknown ⇒ `edges_known == 0`**,parity 无事可做。它测的是 **P2 挡边**,
不是 coboundary。A1 下它确实指名红(我独立跑过,2 红含它),**有牙**;
但名字里的 "coboundary" 由**另一个**守卫(`…_pure_cells` 阳性对照)承担。
阳性对照我独立核过是**真的**:三边标签 (A-B=1, A-C=0, B-C=1)、XOR=0、
`_parity_check` 直喂返回 `('consistent', None, 3, 0)` —— known=3/unknown=0,parity 确实跑了。

**规约**:
1. 条件命题交付后,验证者要**逐个前提造破坏变异 + 量 obstructions**,不能只复核交付方举的例子。
2. 主动问「还有第几个前提?」——此例 P4 藏在 `_parity_check` 而不在 `_delta0`,
   **跨函数的隐含前提最容易漏**。
3. **有环 ≠ 环上有已知边**。报「真环守卫」必须并排报 `edges_known`;
   `edges_known==0` 的环守卫测的是挡边逻辑,不是 parity 性质。名字与所测性质要对齐。
4. 阳性对照必须**独立重算边标签 + 直喂 parity**,确认它不是空转(坑78 反向的第三形态:
   对照看起来绿,其实根本没跑到被测机制)。

### V20 第二轮复验结论:W22 重修 —— PARTIAL(P2/P3/P4/P5 PASS,P1 依据仍错;另发现 1 个新洞)

**口径**:HEAD 9b6f3b3;`frame_check.py` sha `0ef51a39…`(验前验后**不变**);
pilot 根 139 文件 **0 漂移**、engine/tests 97 文件 **0 漂移**、git status **86 不变**。
/tmp/v20r2 沙箱 + PATH 挡板。**基线 1432 passed + 12 subtests rc=0,验后同数同红名集合(空)**。
**差值全归因**:1425→1432 = +7 = W22 satisfaction 守卫 34→39(**+5**)+ W20 并发
`test_relation_net_getter_reads` 14→16(**+2**),**零未解释差值**。
空变异对照 204 绿。V20 只跑 `pilot/tests` 口径,**未跑 demos/shop**(硬边界禁 docker/psql);
编排者提到的 1443/1427 不是 V20 报过的数。

**PASS(独立重跑,非照抄)**:
- **P2 真环守卫**:A1 下 **2 红,含指名目标** `test_channel_coboundary_on_real_3cycle_mixed_cells`
  (不是旁边代杀)。阳性对照真跑 parity(见做法67)。`test_external_judgments_break_precondition_p3`
  存在且**有牙**:N4(parity 恒返回 consistent)下它与另 4 条 parity 守卫红。
- **P3 opt-in 三锁全部成立**:`_guard_facts(capability if satisfaction else None)` 计算与发布同门;
  **四组合真 CLI 逐字节**:①不供 ②只供 capability ③只供 findings ④三参全供(均不开开关)
  → **②==① `74784f86…`、④==③ `414afa97…`**;④与⑤(全供+开开关)**counts 全等**;
  ③≠① 属正常(findings 本就被默认违反通道消费,不是 opt-in 输入)。
  disabled 时 `satisfaction_guard_stats` 七键**全 0**、`resource_level_satisfaction_judgments=0`。
  stdout 改为 **`guard事实未测量(通道未启用)`**,8/8 自相矛盾**消除**。
  **默认值双守卫各自由目标守卫指名红**:A4(`run()` 默认翻 True)→ 2 红含
  `test_run_default_satisfaction_is_off`;A8(CLI `store_true`→`store_false`)→ **恰 1 红
  `test_cli_default_off_and_on`**(单因子,无代杀)。Acap(同门退回 ungated)→ 2 红含
  `test_capability_without_flag_is_byte_identical`。
- **P4 价值陈述真入产物**:notes 里有完整「双重未接线」段(RED 只消费 cycle_obstructions、
  不读候选 δ⁰=inconsistent、不传三参、候选级 inconsistent 是待接线的机器证明矛盾不是已生效裁定);
  CLI 尾句亦有「候选级 inconsistent 尚未被 audit_entry 裁定消费」。**不只 LESSONS**。
- **P5 分解数正确**:独立重算 **12 未归因 + 3 归因 = 15**、observation 0+1、
  total 83 / verified 80 / defect 64 / attributed 26、资源提及 **0/16**、115 桶 546 细胞 —— 逐项相等。
  漂移判定实验属实:`/tmp/w22_delta0/out/baseline.json` 记 `findings_total=86 / attributed=27`,
  当前 83/26 ⇒ 漂移与算术错**两桶各自归因**,坑126 的「分类必须互斥再相减」是对的补法。
- **第一轮回归锚点全杀**:R1 极性整删 8 红、R4 候选 inconsistent→unknown **9 红**、
  R6 资源级连接 4 红、R9 guard 收 unknown 恰 1 红。

**FAIL / 须补**:
1. **P1 仍是假命题(且是我上轮的错,见坑127)**:产物 note 与 LESSONS 坑121 都写
   「谓词逐条相同(oa≠∅⟺va≠∅)」。实为**单向** `oa≠∅ ⇒ va≠∅`;6 例中 3 例破双条件。
   定理结论不变(20000 次随机零违例),但**依据必须改**,否则产物自述里挂着一条假引理。
2. **前提清单缺 P4**(unknown 边必须被 parity 排除),破坏它 obstructions 0→1。
   代码已被 6 条既有守卫保护 ⇒ **陈述不完整,非代码洞**。
3. **新洞 N3(存活,204 全绿零红)**:`_satisfaction_value` 的 **(细胞, ident) 键里「细胞」这一维无守卫**。
   变异成「只按 ident 匹配、忽略细胞」⇒ guard 证据跨细胞泄漏。
   **真缺陷非等价变异(实测)**:guard 只属 C#m、B#m 无 guard,A 有 tied 违反 →
   正确 `A|B=unknown, A|C=inconsistent`;N3 下 **`A|B=inconsistent`(假阳性)**。
   这是**满足级通道的归属维**,与坑121 锁的「判据维」是两条不同的键维,只锁了一条。
   最小补法:造「guard 属第三细胞」夹具,断言无 guard 的那一侧不得被判 inconsistent。
4. **守卫名过度承诺**:`…_3cycle_mixed_cells` 的 `edges_known==0`,测的是 P2 挡边不是 coboundary
   (有牙,但名字与所测性质不符,见做法67 规约3)。

**残留(第一轮已报、本轮未动,不在 P1-P5 范围内)**:
- **9 处坑号引用仍错**:`frame_check.py:243,333,371,1051,1053` +
  `test_section_value_polarity.py:1` + `test_satisfaction_channel.py:1,141` 写 **坑110/111**,
  实际是 **坑120/121**。(新增的 :1068/:326 引用坑121/122 是对的。)
- **`_satisfaction_section_value` 仍是死代码**:生产路径零调用,只被自己 3 处单元测试引用。
- **坑123 未闭 + 新形态**:W22 的 `mutation_harness.py`/`mutation_harness_v2.py`/`out/baseline.json`
  仍只在 `/tmp/w22_delta0`,未入库 ⇒ 「M0 绿、A1/A4/A8/A-cap 全杀」与「R 系列回归全杀」
  **第三方不可重放**(V20 未照抄,自建 13 锚点)。
  **更硬的一条**:`test_satisfaction_channel.py:424` 把 **`/tmp/w22_delta0/bin` 硬编码进入库测试的 PATH**。
  该目录现在存在(W22 沙箱未清),所以本机能跑;但它是**私有沙箱路径入库**,
  换机器或 /tmp 清理后**静默退化为空挡板**(做法49:挡板必须真在)。
  本例 frame_check 是纯函数、根本不需要挡板 ⇒ 属 cargo-cult。
  同族:W21 的 `test_audit_entry_derivation.py:3` 也引用 `/tmp/w21`。
- `audit_entry.py:351-352`「候选 δ⁰ 给不出 inconsistent」仍一般性陈旧(W21 文件)。
- 两引擎词汇分歧面 6+15 条(capability_map/oracle_bridge 所有者)。
### 坑 128:从验证者继承的前提也要独立重推 —— 复合键每一维都要有自己的守卫(W22,V20 第二轮)

**三件事,同一病根:把「别人说过/测过」当成「已证」**:
1. **继承错前提**:V20 第一轮给「谓词逐条相同 ⇒ oa≠∅⟺va≠∅」,我逐字继承进坑121 与产物 note。
   V20 第二轮自己撤回(漏看 `_tied_oracle_ids` 末尾的判据连接条件,坑127)。**验证者的结论也是
   待验证断言**(坑99 同族:未验证的撤回是另一个未验证断言;这里未验证的继承同样)。
   正确前提:**单向** oa≠∅⇒va≠∅(tied⊆违反指纹),定理结论不变;且双条件与通道正确性互斥
   (补全 va⇒oa = 拆判据连接键 = 资源级缺陷)。已独立重推 `_tied_oracle_ids` 全部条件后改写。
2. **复合键无守卫的维度 = 未锁的不变式**(N3,本轮唯一真代码缺陷):`_satisfaction_value` 按
   (细胞, ident) 取 guard,细胞维无守卫 ⇒ 「guard 忽略细胞键」变异存活 204 全绿。
   补 test_guard_evidence_does_not_leak_across_cells(guard 只属 C,B 不得继承 ⇒ A|B=unknown,
   A|C=inconsistent),N3 变异**恰 1 红指名、无代杀**。判据连接键的教训(资源×判据)同样适用:
   **键有几维,守卫就要几维**,缺哪维哪维就能泄漏。
3. **测试名过度承诺**:mixed 夹具拓扑是真 3-细胞环,但三边全 unknown(edges_known==0),
   parity 无事可做 —— 它测的是 **P2 挡边**,不是「环上 coboundary」。**有环 ≠ 环上有已知边**。
   改名 test_p2_not_vb_blocks_all_edges_on_3cycle,已知边 coboundary 由 pure 对照承担
   (3 已知边 rel(1,0,1) XOR=0,V20 独立重算确认)。名字承诺了什么,夹具就必须真测什么。

**规约**:
1. 继承任何外部前提(含验证者给的)前,对着源码把谓词/条件**逐条重推一遍**;重推不了就标「未独立验证」。
2. 复合键的守卫按维枚举:每一维造一个「该维失效」的变异,必须各有指名红。
3. 条件命题的前提清单逐个破坏验可达(做法67),并主动找未列出的第 N 个前提(P4 藏在 parity 不在 δ⁰);
   可达性探针入库(test_satisfaction_premises.py,自变异 tmp 副本,四前提破→1 对照→0)。
### 坑 129:精确匹配 loosened 成 substring 的两处存活锚点 —— guard 字段维与判据连接维各有一条假阳性通道(V20 第三轮实测)

W22 第三轮把坑121 的「判据维」与第二轮的「细胞维」都锁了,但**同一连接键上还有两条维没锁**:

**V3(达判定面,真缺陷)**:`_guard_facts` 的字段匹配
`(_snake(fld) != afld and fld != afld)` loosened 成 `afld not in _snake(fld)` ⇒ **210 全绿零红**。
可达夹具(我第一版夹具打不到,重造才打到 —— 见下):桶在 **guard 自己的字段** `e.X.setAmountCent` 上,
判据 O1 的 `target.field=amountCent` 但 `assert.field=amount`(**另一性质**),guard 在 amountCent。
正确实现:`excluded_field_mismatch=1`、`facts_used=0`、`A|B=unknown`;
V3:`facts_used=1`、**`A|B=inconsistent`、fired=1** ⇒ **假阳性到候选级判定面**。
这正是坑121「同资源≠同性质」在 **guard 侧**的复发:细胞维锁了、判据 id 维锁了,
**guard 字段 ↔ assert 字段的精确相等**没锁。

**V4(达自述面)**:`_finding_ties` 的 `oid in ties or oid in evidence_refs` loosened 成 substring
⇒ **210 全绿零红**。`evidence_refs=["O10"]` 被当成 O1 的连接 ⇒
`satisfaction_tied_violation_findings` **0→1**(假)。δ⁰ 不受影响,因为 `_tied_oracle_ids`
自己做精确 `t in oracle_ids` —— **同一语义活在两处,一处精确一处可被 loosened**,
与坑83/109/121 的「词汇表活在两处」同族。

**我自己踩的坑(纪律)**:V3 第一版夹具我把结论**预先写死在 print 里**(「⇒ 假 inconsistent」),
但数据显示两侧都 `unknown` —— 因为 guard fact 的 ident 键是 `("field",X,"amountCent")`,
与桶 ident `("field",X,"amount")` 不匹配,`_satisfaction_value` 取不到 ⇒ 变异被**键结构中和**。
**预写结论差点让我报一个未坐实的缺陷**(坑99 同族:断言要先有实验)。
正确动作:撤回那行结论 → 问「要让它到达判定面,桶必须落在哪个 ident 上」→ 重造夹具 → 坐实。
**变异存活 ≠ 等价变异,也 ≠ 真缺陷;必须造出到达被声称后果的夹具才算证明。**

**规约**:
1. 连接键的每一维都要有自己的守卫:**(细胞 × 资源 × 判据 id × guard字段↔assert字段)**。
   锁了三维不等于锁了键;substring loosened 是每一维独立的攻击面。
2. 同一语义在两处实现(此处 `_finding_ties` 与 `_tied_oracle_ids`)时,**两处都要变异**,
   只变一处会漏掉自述面的假计数。
3. 变异存活后**先问「后果可达吗」**:被键结构/上游过滤中和的变异,要重造夹具打到声称的后果,
   否则只能报「计数面缺陷」不能报「判定面缺陷」。**分级要跟着证据走,不跟着预期走。**

### V20 第三轮复验结论:W22 四项重修 —— 四项全 PASS,另发现 2 个存活锚点(V3 达判定面)

**口径**:HEAD 9b6f3b3;`frame_check.py` sha `2986562a…`(验前验后不变);
pilot 根 **0 漂移**、engine/tests/baselines **0 漂移**、git status **88 不变**。
**基线 1438 passed + 12 subtests rc=0,验后同数同红名集合(空)**;差值 1432→1438 = **+6 新守卫**
(premises 5 + N3 1),并发 +0,与声称一致。空变异对照 **210 绿**。只跑 pilot 口径,未跑 demos。

**四项逐条 PASS(独立重跑,非照抄)**:
1. **单向前提充分**:自己重推 + **30000 次随机穷举**(5150 inconsistent / 693 consistent,
   `oa⊄va` 违例 **0**、切割违例 **0**)。证明只需 `oa≠∅ ⇒ va≠∅`(1-边:oa 非空⇒该侧∈V,
   `not vb`⇒另一侧∈S;0-边:两侧非空⇒同在 V)⇒ `c(v)=1[v∈S]` 全局 2-着色。
   **tied ⊆ 违反指纹**包含关系成立(谓词 = 截面谓词 + 判据连接)。
   **无第五前提**:`_pair_label_from_findings` 传整细胞 findings、ident 由桶资源唯一决定
   ⇒ V/S = f(cell, ident) 是真顶点性质。**作用域条件**(只覆盖二值退回桶,constraint_set 桶
   的障碍与本通道无关)是隐含限定,不是前提。互斥声明(补全双条件=拆判据连接键=R9 缺陷)已入 note。
2. **四前提守卫真可重跑(坑123 闭)**:`pilot/tests/test_satisfaction_premises.py` **5/5 从我自己的
   仓库副本跑通**,用 pytest `tmp_path`、引擎路径由 `__file__` 相对解析、**零 /tmp 残留依赖**、
   锚点 `assert src.count(old)==1` 保唯一。P1破→1 / P2破→1 / P3破→0→1 / P4破→1,对照均 0 —— 我实跑确认。
3. **N3 双向**:N3 变异 ⇒ **恰 1 红且是目标** `test_guard_evidence_does_not_leak_across_cells`,**无代杀**。
   反向四例(guard 属第三细胞 / 字段不同 / 判据不同 / kind=unknown)全部 `A|B=unknown`、obstructions 0。
4. **快照真钉哈希且入库**:`pilot/baselines/w22_satisfaction_snapshot.json` **10/10 sha256 逐条对得上**,
   全为 64 位 hex(不是计数),所列文件全存在。改名 `test_p2_not_vb_blocks_all_edges_on_3cycle` **名实相符**
   (该夹具 edges_known=0,只测 P2 挡边);coboundary 由 pure 对照承担(第二轮已独立核:三边 (1,0,1)、
   XOR=0、`_parity_check` 直喂 `('consistent',None,3,0)`,parity 真跑)。
5. **真实语料逐字节**:default vs capability-no-flag **sha 同为 `4bbae6ba…`**;开开关后 9 候选全 unknown、
   obstructions 0、触发 0/0。回归锚点 A1(5 红含改名守卫)/A4(2 红含目标)/A8(**恰 1 红**)/Acap(2 红含目标)全杀。

**新锚点矩阵(A/N/R 系列之外)**:V1(unknown 洗成 consistent,P4 反方向)**6 红**已锁;
**V3(guard 字段 substring)存活 210 绿 —— 判定面假阳性,见坑129**;
**V4(判据连接 substring)存活 210 绿 —— 自述计数假 0→1**。
两者均为**本轮之前既存代码**的未锁维,非本轮回归;通道默认关且双重未接线 ⇒ **当前生产影响 0**,但守卫缺口真实。

**残留(三轮累计,未动)**:
- **9 处坑号仍错**:`frame_check.py:243,333,371,1051,1053` + `test_section_value_polarity.py:1`
  + `test_satisfaction_channel.py:1,141` 写 **坑110/111**,实际 **坑120/121**。三轮未修。
- **`/tmp/w22_delta0/bin` 硬编码仍在** `test_satisfaction_channel.py:438`(入库测试引用私有沙箱 PATH;
  本机目录尚存所以能跑,换机/清理后静默退化为空挡板,做法49)。premises 新文件已做对,旧文件未跟上。
- **`_satisfaction_section_value` 仍是死代码**(生产零调用)。
- **note 措辞自相矛盾**:定理 note 已改「四个前提」,但价值 note 仍写「环级检测力受上述**三**前提限定」。
- 快照 `suite_baseline_pilot_only` 记 1432(第二轮),已标注轮次故属诚实但未更新。
- `audit_entry.py:351-352` 陈旧注释(W21 文件);两引擎词汇分歧 6+15 条(capability_map/oracle_bridge 所有者)。
### 坑 130:复合键的字段维与「同一语义两处实现」—— 计数面与判定面会一精一粗漂移;变异被键结构中和≠守卫有牙(W22,V20 第三轮)

**两个既存真缺陷(V20 第三轮查出,收尾轮修)**:
1. **V3 字段维没锁**(坑121「同资源≠同性质」在 guard 侧复发):`_guard_facts` 的 guard 字段↔assert.field
   精确相等无 substring 守卫。细胞维(N3)、判据 id 维锁了,**字段维漏了**。夹具要点(V20 教训):
   桶必须落在 guard 自己的字段(setAmountCent)上,ident 键匹配才真到达判定面 —— 键不匹配会中和变异。
   修:test_guard_field_substring_of_assert_field_is_mismatch(amountCent vs amount,断言
   excluded_field_mismatch=1 + unknown);V3 变异(精确→substring)恰 1 红指名。
2. **V4 同一语义活在两处**:`_finding_ties`(计数/证据面)与 `_tied_oracle_ids`(判定面)各自实现连接匹配。
   精确→substring 变异下 evidence_refs=["O10"] 被当 O1 连接,**计数 0→1 假而判定不动** —— 一精一粗。
   修:**合并为单一实现 `_finding_tie_ids`**,两面共用;V4b 变异(substring 且把 O10 归因到 O1)恰 1 红。
   **复合键有几维守卫就要几维;同一语义只许一个载体**(坑83 词汇表同族,这里是匹配语义)。

**我自己踩的等价变异陷阱(分级跟证据走)**:首版 V4 变异把收集过滤改成 substring 但**仍收集原串 t**
⇒ 产出 {"O10"},下游 `oa & sb`({O10}∩{O1}=∅)把它重新过滤 ⇒ **零红存活**。这不是守卫漏了,
是变异被键结构中和(等价变异) —— 与 V20 自述的第一版 V3 夹具陷阱同形。撤回,重造**精确复刻缺陷态**
的 V4b(substring 匹配且映射到被匹配的 oracle id)才坐实。**变异存活先分三级:真缺陷/等价变异/夹具未到达,
跟着证据走,不跟着预期走**(坑78/61 同族)。

**四项清理(已做,证据)**:错坑号 8 处 110/111→120/121(grep 归零);硬编码 /tmp PATH 挡板 →
tmp_path 自造失败桩(换机不退化,做法49);价值 note「三前提」→「四前提」;死代码
`_satisfaction_section_value` **接线**到产物可见面 satisfaction_section(与违反集对称,可见≠裁决,
默认关照样发布,接线守卫锁)。快照清单 10 个 sha256 刷新入库。

**规约**:
1. 复合键(细胞×资源×判据×字段)每一维造「该维失效」变异,各有指名红才算锁全。
2. 同一语义(匹配/归一/谓词)只许一个实现;计数面与判定面共用,否则变异下两面漂移,假计数比假裁决更隐蔽。
3. 变异零红先验夹具是否到达判定面(键匹配?桶存在?谓词触发?),再判等价/真缺陷;结论印在数据后面,不预写。
### V20 最终门结论:W22 满足级通道收尾轮 —— **PASS**(提交基线前最后一道门,四处全过,零残留阻断)

**口径**:HEAD 9b6f3b3;`frame_check.py` sha `49c5f6ff…`(验前验后不变);pilot 根 **0 漂移**、
engine/tests/baselines **0 漂移**、git status **88 不变**;引擎纯净度 **rc=0**(34 文件零项目耦合)。
**基线 1441 passed + 12 subtests rc=0,验后同数、红名集合皆空**;差值 1438→1441 = **+3 新守卫**
(V3 / V4b / satisfaction_section 接线各 1),并发 +0。空变异对照 **286 绿**。只跑 pilot 口径,未跑 demos。

**四处核验(全部独立重跑)**:
1. **V4 合并真消除重复载体**:`_finding_tie_ids`(:321-327)是**唯一实现**,
   `_finding_ties`(:331)与 `_tied_oracle_ids`(:351)**都委托它** ⇒ 计数面与判定面共用一个载体。
   全文件 grep 无第二处 tie 逻辑(:202 的 evidence_refs 属资源提及,不是连接)。
2. **V3/V4b 自造变异各恰 1 红指名,无代杀**:
   V3(guard 字段 substring)→ 恰 1 红 `test_guard_field_substring_of_assert_field_is_mismatch`;
   V4b(substring **且映射 O10→O1**)→ 恰 1 红 `test_evidence_refs_substring_is_not_oracle_tie`。
   **V4b 真映射已直验**:`evidence_refs=["O10"]`、oracle_ids={O1} → 正确 `[]`、V4b `['O1']`
   ⇒ 输出判据 id 而非原串,精确复刻缺陷态(不是被键结构中和的等价变异)。
   **V4a(收集原串)独立复跑 = 存活 286 绿** ⇒ **W22 自撤「首版是等价变异」属实**,不是托词。
   N3 → 恰 1 红 `test_guard_evidence_does_not_leak_across_cells`。
3. **死代码接线后「可见≠裁决」成立**:`_satisfaction_section_value` 现由 `frame_check.py:476-477`
   消费,产出 `satisfaction_section`(与违反集对称)。**全仓消费者普查:只有生产者 + 守卫;
   `audit_entry.py` 对 `satisfaction` 零命中** ⇒ 未静默变成裁决输入,三态红线守住。
   **新锚点 W1(把可见面越权 union 进 `_delta0` 的 va/vb)→ 6 红**(含
   `test_positive_shared_rck_is_not_consistent`、`test_positive_only_input_no_violation_no_obstruction`)
   ⇒ 这条越权路径**有守卫**,不是只靠「目前没人接」。
4. **四项清理 grep 自核**:①错坑号 110/111 在四份文件里**归零**;
   ②硬编码 `/tmp/w22*` PATH **归零**(改 tmp_path 自造失败桩);③价值 note 已改「**四**前提」(:1084),
   与定理 note(:1073)一致;④死代码已接线(见 3)。
   **它报 8 处、我上轮报 9 处 —— 它的数对,我的 9 是陈旧计数**(含一条第二轮已被重写的行),此处更正我自己的数。
5. **快照刷新真对得上**:`pilot/baselines/w22_satisfaction_snapshot.json` **10/10 sha256 逐条匹配当前文件**,
   全 64 位 hex,`measured_at=19:41:57`,`suite_baseline_pilot_only` 已更新为 1441 且保留逐轮溯源。

**真实数据**:default vs capability-no-flag **逐字节相同**(sha 同为 `201abf5a…`);
开关两态 **counts 逐项全等、candidates 逐字节相同、cycles 逐字节相同**;
9 候选全 unknown、obstructions 0、fired 0、guard 事实 8/8;
`satisfaction_section` 在默认关产物里**存在但全空**(语料无提及资源的 positive),符合「可见≠裁决」。
回归锚点 A1(5 红含 P2/P4 前提守卫)/A4(2 红含目标)/A8(**恰 1 红**)/Acap(2 红含目标)全杀。

**残留(非阻断,不属 W22 权限或已披露)**:
- `test_audit_entry_derivation.py:3` 仍引用 `/tmp/w21`(**W21 文件**,docstring 而非 PATH,越界不修)。
- `audit_entry.py:351-352`「候选 δ⁰ 给不出 inconsistent」一般性陈旧(**W21 文件**;仅对该调用路径仍真)。
- 两引擎词汇分歧 6+15 条(`capability_map`/`oracle_bridge` 所有者,第三轮已报)。
- 通道**双重未接线**依旧(RED 只消费 cycle_obstructions、不传三参)⇒ 对已发布裁定影响 0,
  产物 note 与 CLI 尾句均已诚实声明;这是**已披露的待接线工作**,不是隐藏洞。

**四轮累计纪律记账**:V20 自己在第一轮给出假引理(P1 双条件)、第三轮预写结论差点误报 V3,
两次都自撤并指名传播范围(坑127/129)。**验证者的错误必须和交付方的错误一样被撤回** ——
本轮 W22 独立重推确认了我的撤回,并把它自己的等价变异也撤了,双向自撤成立。
### 坑 131:见证环重构必须用生成森林路径 —— 冲突后任意路径奇偶不定,BFS 最短路会凑出偶环假见证(W23 自踩两连,自证守卫抓住)

**经过**:高阶通道(神经复形 H¹)判红后要给出见证环。第一版用「已知边子图 BFS 最短路 + 冲突边」:
BFS 没排除冲突边自身 ⇒ 最短路就是该边 ⇒ **2-顶点退化假环**,scope 误判 within_bucket。
修了排除后仍错:**冲突发生后组件内标签已矛盾,任意 a→b 路径的奇偶不定**,最短路可能与冲突边
凑成**偶环**(parity=0)——不是障碍见证。300 trial 种子自证在 trial 1 就抓住(见证环奇偶≠1)。

**正解**:并查集合并成功的边构成**生成森林**;冲突边不在森林中,森林上 a→b 唯一树路径的
奇偶 = pa⊕pb(归纳可证:森林路径奇偶与 find() 累积奇偶恒等),加冲突边恒为奇(冲突条件
pa⊕pb≠rel 保证)。见证 = 树路径 + 冲突边,≥3 顶点、全已知边、闭环、奇环,四条定义性质构造上成立。

**为什么第一版守卫没抓住**:自证只断言「判红必有 obstructions」,不断言见证本身的定义性质。
与坑 78/101 同族:**声称产出 X,就要直接断言 X 的定义**,不是断言「X 非空」。补强后(环≥3 顶点、
顶点不重、边数=顶点数、全已知边、闭环、奇偶=1)两个 bug 都指名红。

**规约**:
1. 矛盾系统里提取见证,必须走**产生矛盾的那棵森林/证明树**,不能走任意图路径 —— 矛盾之后
   「路径奇偶」不再是良定义量。
2. 见证类产物的守卫要逐条断言见证的定义性质(闭环/奇偶/边合法性),「非空」只是存在性,不是有效性。
3. 变异矩阵里「等价变异」要如实标:MC 首版(unknown>0 分支改 consistent)在「有 unknown 边必无
   conflict」的结构下不可达,存活不是守卫无牙;换成真可达的 MC2(unknown 边当 consistent 参与
   union)即被 selfcheck/M8 指名杀。判等价先证不可达(坑61 同族)。

### 坑 132:dim H¹ 是拓扑容量不是检测力 —— 语料 H¹=0 是结构事实(W23 实测;原标题后半「约束通道结构为零」已被 V21 证伪,见下方更正与坑133/134/135)

**【V21 证伪,W23 更正(2026-11-15)】本条原版有三处错论断,均已由可重跑反例证伪并修复:**
1. **「约束通道高阶检测力结构为零」对产物字段为假**:CE1(三不同 ident 资源各挂 in[1,2]+in[3,4],
   截面空集,三角桶)旧 3 桶全判红,原版新通道仍计 incremental=1。正确表述:**信息增量为零,
   不是字段为零** —— 修法是边级冗余对账(见证边两端点含于某旧 inconsistent 桶 ⇒ 已覆盖;
   全边覆盖 ⇒ 冗余),CE1 入库为守卫。
2. **结构理由找错**:约束通道不产新不一致对的真依据是**旧通道已枚举桶内全部成对交**
   (cycle_consistency 成对循环),任何约束导出的不一致对所在桶必已判红 —— **与截面是否细胞无关
   无关**。「同 ident 截面恒等」只覆盖一格,原守卫只断言 s1==s2 全程不调通道(已改名,
   通道级论断由 CE1 守卫锁定)。
3. **「真盲区只有跨资源环」低报了自己**:CE2(r1=[A,B,C] 全 consistent + r2=[A,B] inconsistent)
   旧 0 障碍,新见证 ⊆ r1(scope=within_bucket)但信息增量=1,且 dim H¹=0。真盲区是
   **跨桶标签聚合**,与「有无单桶包含全环」无关;scope 只是拓扑分类,不是冗余判据。
另两处口径修正:**见证增量≠信息增量**(原版按顶点集包含判冗余,漏「每边已被旧桶判红」,
检测力高估);**裁定来自全局 1-骨架 GF(2) 奇偶,余链秩只产容量统计量、不参与裁定**
(V21 实测 h1=999 单因子变异裁定面逐项不变;CE2 即 h1=0 有障碍),status 语义是「已知边无奇环」
不是「无障碍」,产物 notes 与 CLI 已加限定并入库守卫(删限定指名红)。详见坑133/134/135、做法68。
以下原版文字保留作错误链记录,**读时以更正为准**。

**实测(快照钉哈希,入库守卫 test_higher_order.py::test_m10 可重跑)**:
jiuyan 语料(relation_net b128afaf / units 9a8b5176 / oracles 82fa1e91)重叠图 52 顶点/55 边/
24 组件,环空间维数 27。神经复形(资源桶覆盖的 Čech nerve)V41/E55/T44,rank δ⁰=28、rank δ¹=27,
**dim H¹=0**。独立交叉验证:27 个基本环全部可表为同桶三角形边界的 GF(2) 组合
(rank(三角形+基本环)=27=rank(三角形))——重叠图是「团树」(桶经共享顶点树状连接),
**一切环都在单桶内,跨资源环拓扑上不存在**。这是结构事实,不是「没测」。
已知边 8(全来自约束桶、全 consistent)/unknown 47;喂 83 条 findings 后已知边仍 8
(finding↔资源断连实证,与任务诊断一致)⇒ 通道 status=unknown,障碍 0。

**三个数必须并排报(坑64/66 的上同调版;更正后口径)**:
① dim H¹=拓扑容量(未被同桶三角形填充的独立环数),**不参与裁定**(裁定=全局 GF(2) 奇偶);
② 已知边子图环空间维数=当前标签下可检验的环容量(语料=2,且两环都在单桶内);
③ obstructions_incremental=**信息增量**(边级对账后含未覆盖边的见证数;语料=0)。
只报①会被读成「查了 N 个高阶障碍」,只报③会被读成「机制没用」。原版①写「可容纳的独立跨资源
障碍数」、③写「实际增量检出」均不准:①暗示 H¹ 类检测(实际不参与裁定),③把见证增量当信息增量。

**诊断复核的一处推回(带证据;后半已被 V21 证伪,见本条顶部更正)**:任务引 note「需三重以上交集才显现的高阶障碍算不了」过宽 ——
**桶内 Helly 失败(成对交非空、全桶交空)旧通道已检**(cycle_consistency 全桶交集,
test_m7 monkeypatch 细胞相关截面 {1,2}/{2,3}/{1,3} 实证旧通道判红)。~~真盲区只有**跨资源环**
(边来自不同桶、无单桶包含全环)。且**细胞无关截面(_cell_section 不读 fp)下跨桶 Helly 结构上
不可能**:截面只依赖资源 ident,同 ident 的不同拼写桶截面恒等。约束通道的高阶增量检测力在当前数据模型下**结构为零**;真增量在
标签通道(judgments/findings)的跨资源奇环。DTO 投影使截面细胞相关后,约束通道才可能有高阶增量。~~
**更正**:真盲区=跨桶标签聚合(CE2 见证可在单桶内);约束通道信息增量为零的真依据=旧通道已枚举
桶内全部成对交(与细胞无关性无关);「结构为零」对字段为假(CE1),边级对账后信息增量才为 0。

**双向变异(入库可重跑;V21 证伪后扩充)**:M1 真跨桶聚合障碍(三 2-细胞桶三角形、judgments 全 inconsistent)→
新通道 inconsistent/信息增量1,**旧通道 0 障碍全 consistent**(检测力真增量);
M2 合法偶环(1,1,0 与 0,0,0)→ consistent 零障碍(反向不误红);M3 桶内奇环 → 旧已判红,
新见证全边覆盖、信息增量=0;M4 一条 unknown 边 → unknown(三态红线)。
**CE1**(纯约束跨资源,旧 3 桶全红 → 信息增量=0)/ **CE2**(跨桶聚合桶内见证,h1=0 仍有障碍,
信息增量=1)/ **V3**(4-细胞桶见证 ⊊ 旧红桶,包含反转可达)入库为守卫。
9 个精确变异全指名杀(MA 默认开→M5/M6;MB2 见证退化→selfcheck/M1/CE1/CE2/V3;MC2 unknown 洗一致→
selfcheck/M8;MD 增量多计→M3/CE1/V3;ME 偶环误报→M2 等 9 红;MF 秩公式丢 rank δ¹→手算夹具/selfcheck/CE2;
**V3 包含反转→V3 守卫指名红**;V5/V6 删 status/裁定限定→口径守卫各恰 1 红)。

**离散 Morse 归约不用的依据(定理前提核过)**:CGN(Found Comput Math, DOI 10.1007/s10208-015-9266-8,
Curry–Ghrist–Nanda,2015 接收/2016 刊出)的 Morse 归约保持上同调的前提是**配对限制映射为同构** +
匹配无环。常数 GF(2) 系数下前提平凡成立,但本语料复形极小(E55/T44,bitset 消元微秒级),零规模收益;
**约束集「层」的限制是求交、不是线性映射,CGN 定理对约束通道根本不适用** —— 不得引该文献为
约束通道的高阶计算背书。文献复核:DOI/作者/期刊属实,Abramsky 语境性 H¹ 与 Conghaile k-CSP
方向属实但本项目未消费(不引为证据)。

**纪律**:opt-in 三锁照做法 66(默认关字节一致/真 CLI 无旗标≡API 默认/关闭时 stdout 报未测量
不发布 dim H¹);单因子隔离实测(开启后 cycles/obstructions/candidates/原 counts 逐项不变);
audit_entry 未传旗标 ⇒ 双重未接线,对已发布裁定零影响,产物 notes 诚实声明。
基线 1441+12 subtests → 1454+12(+13 守卫)→ V21 证伪重修后 **1458+12**(+17 守卫,零回归)。
任务文引「consistent 3/unknown 11」是旧快照,实测 5/9(坑68:报数带快照)。
字节一致基线按做法 68 升级为**改动前引擎冻结产物**(pilot/tests/fixtures/frame_check_pre_higher_order.json,
M10 逐字节比对),不再是同代码显式传 False。

### 坑 133:「结构为零」既没有守卫、也被纯约束反例证伪 —— 连结构理由都找错了(V21 独立实测 W23)

**被验断言**(坑132):「约束通道的高阶增量检测力在当前数据模型下**结构为零**」,
理由「细胞无关截面 ⇒ 同 ident 异拼写桶截面恒等 ⇒ 跨桶 Helly 不可能」。
指名守卫 `test_cell_independent_sections_no_cross_bucket_helly`。

**实测反例 CE1(不 monkeypatch、纯约束、细胞无关)**:三个**不同 ident** 资源
`field:X.f`/`field:Y.g`/`field:Z.h`,各挂两条互斥判据(in[1,2] 与 in[3,4])⇒ 各桶截面 = `("set", ∅)`;
细胞 A/B/C 摆成三角桶 r1=[A,B] r2=[B,C] r3=[A,C]。逐桶核实**同桶两细胞截面恒等**(细胞无关成立)。
结果:旧通道 3 桶全 inconsistent;**新通道 status=inconsistent、见证 [B,A,C]、scope=cross_resource、
redundant=False、obstructions_incremental=1**。「结构为零」对**产物字段**直接为假。

**守卫核实**:名为「no_cross_bucket_helly」的守卫**只断言 `s1 == s2 == ("set",{1,2})`,
全程不调用高阶通道**;全文件 `obstructions_incremental == 0` 的三处断言分别是 M2 合法偶环、
M3 桶内奇环、M10 语料,**没有一处是「纯约束 + 跨资源」格**。
⇒ 坑78 同族:**声称 X 就要直接断言 X**;这里声称的是最重要的论断,却只锁了一个相邻性质。

**结构理由也错**:约束通道不产生**新的不一致对**,真正依据是
`cycle_consistency:763-767` **已枚举桶内全部成对交**(`for a,b in combinations(have,2)`),
任何约束导出的 inconsistent 边都已被旧桶判红 —— **与截面是否细胞无关无关**。
「同 ident 截面恒等」只覆盖同 ident 一格,推不出一般跨桶结论(不同 ident 的截面本就不同)。
**正确表述**:「约束通道不产生旧通道未知的**不一致对**;但 `obstructions_incremental`
仍会因跨资源见证环而非零」——是**信息增量**为零,不是**字段**为零。措辞差别很大(坑99 同族)。

### 坑 134:见证增量 ≠ 信息增量;scope 分类漏了「跨桶标签聚合」这一真盲区(V21 独立实测 W23)

**两个反例,都不需要 monkeypatch**:

| 反例 | 构造 | 旧通道 | 新通道 | 被推翻的断言 |
|---|---|---|---|---|
| CE1 | 三不同 ident 各含不可满足判据集,三角桶 | **3 桶全判红**(3 条不一致对全已知) | 增量 **1** | 「增量=新检测力」 |
| CE2 | r1=[A,B,C] judgments 全 consistent;r2=[A,B] judgment inconsistent | **2 桶全绿、0 障碍** | 见证 {A,B,C}、**scope=within_bucket**、containing=['r1']、redundant=**False**、增量 **1** | 「真盲区只有『无单桶包含全环』的跨资源环」 |

**CE1 的病根**:`redundant = any(vset <= obs for obs in old_inconsistent_buckets)` 只按**顶点集包含**对账。
见证 {A,B,C} 不含于任何 2-细胞旧桶 ⇒ 判为增量,**尽管它的每条边都已被旧桶 individually 判红**。
这是坑64/66「覆盖≠检测力」的上同调新形态:**见证增量 ≠ 信息增量**。
修法:冗余对账要再判「见证的每条边是否都落在某个旧 inconsistent 桶内」,或把增量口径显式改名为
「旧通道未给出的**环级**见证数」并同报「其中多少条边是旧通道已知的」。

**CE2 的病根**:新通道标签是**跨桶聚合**(`_higher_order_labels`,与 `find_candidates:500-505` 的 δ⁰ 同语义,
这点 W23 说对了),而旧 `cycle_consistency` 是**按桶**算奇偶、从不聚合。
于是「单桶顶点集内的环」也能因**别的桶把某条边翻成 inconsistent** 而变奇环。
⇒ 真盲区是**跨桶标签聚合后的奇环**,与「有无单桶包含全环」**无关**;
`scope=within_bucket` **不蕴含** `redundant=True`,M3 的「桶内奇环⇒旧已判红」不是定理。
CE2 的增量是**真**增量(旧全绿、独立暴力着色不可满足),即 W23 **低报**了自己通道的检测力,
同时 `scope` 字段把它误标成桶内 —— 分类面与检测力面不一致。

**附带(前瞻,当前不可达)**:细胞相关截面下若桶内**部分**细胞有截面(`full=False`),
`_higher_order_labels` 把**整桶所有对**丢给 judgments/findings,**完全丢弃已有的成对约束证据**。
CE3 实测(sec A={1},B={2},C=None):旧判红 1、新 **unknown**(保守,未洗绿,可接受),
但 DTO 投影落地后这是一条**证据丢失通道**,应与 M9 一起升级。

### 坑 135:opt-in 通道的 status 不带口径限定 ⇒ 同一产物自相矛盾头条(V21 独立实测 W23)

**实测(纯约束、单桶 2 细胞、截面空集、不 monkeypatch)**:`field:X.f`=[A,B] 挂 in[1,2] 与 in[3,4]。
同一份产物里:旧 `cycle_obstructions=1`、桶 inconsistent;新 `higher_order.status=**consistent**`、障碍 0。
真 CLI stdout 两行并排:
```
cycles: buckets 1 status {'consistent': 0, 'inconsistent': 1, ...} obstructions 1 ...
高阶通道: ok status=consistent  nerve V2/E1/T0 ... 障碍 0（增量 0）—— 覆盖≠检测力：dim H¹ 是拓扑容量不是已检出数
```
**病根**:新通道 `status` 的语义是「**已知边子图无奇环**」,**不是「无障碍」**——
单条 inconsistent 边(2-细胞桶)无环可判,union 成功 ⇒ consistent。
现有 notes 只限定了 `dim H¹`(容量≠检出),**没限定 status**。非专家会把 `status=consistent` 读成
「高阶通道查过了,没矛盾」,而同一产物旧通道正判红 1 条(做法51:stdout 文案也是判定面)。
**对照姊妹通道**:满足级 stdout 明写「0 触发可能是语料不足…**不是「无矛盾」**」——
同一项目里满足级通道的限定纪律比它强(与做法66 指出的同族不对称)。
**修法**:status 旁并排打印口径(「无奇环 ≠ 无障碍;成对/桶级障碍见 cycles」),
或把字段改名 `cycle_parity_status`,并加守卫断言「旧 inconsistent>0 时新通道 stdout 不得出现裸 consistent」。

### 做法 68:逐字节基线要用**改动前引擎**,不是同代码显式传 False;等价变异要自证互斥(V21)

**W23 的 M5 锁的是 `run()` vs `run(higher_order=False)` —— 同一份代码的两个调用**,
这能锁「旗标门控」,**锁不住「新增代码本身改了旧产物」**(坑127 同族:验证者/交付方自己给的参照也要独立)。
**V21 的补法(便宜且强)**:`git show HEAD:pilot/engine/frame_check.py` 取改动前引擎,
连同**同一份依赖目录**复制到 /tmp,两个模块并行 import,对**6 个输入格**逐字节比:
裸 / +oracles / +plumbing+business / +findings / **+capability 但不开 satisfaction** / +capability+satisfaction。
实测 6/6 全等(sha 94f34649 / 6aaf75ea / bf41ab39 / 02740a0b / bf41ab39 / 315f6d0);
真 CLI 无旗标 pre vs post 产物 sha **70a79300 相同**,stdout **只增 1 行 disabled、零删除、不含 `dim H¹=`**。
⇒ 做法66 三锁在**真基线**下成立(这是比交付方自证更强的证据,应照抄)。

**等价变异要自证,别把守卫缺口和等价变异混报**:V21 首版 V2「把 `if inconsistent` 与
`elif all consistent` 换序」**存活**,一度以为是守卫缺口;**自证后撤回**——
两条件在 `labs` 同时含 consistent 与 inconsistent 时,换序后仍落回 `elif inconsistent` ⇒ **语义恒等**。
换成真极性变异(`if "inconsistent" in labs` → `if "consistent" in labs`)即被 **6 条指名守卫**杀。
**规约**:报「存活」前必须构造一个**合法输入**使变异态与正确态**行为可区分**;
构造不出就标等价并写明依据(坑61),构造得出就是真缺口(本轮 V3 即如此)。

### V21 独立验证结论:W23 高阶障碍通道 —— **PARTIAL**(机制真/数字真/三锁真;两个论断被证伪 + 1 个可达变异存活)

**快照**:HEAD `5061d5e`,工作树 `frame_check.py` sha `38c80fa8…`(319 文件全量快照实验前后**逐字节零差异**,
`git status` 与接手时相同,零残留)。基线**独立复现**:`pilot/tests` 排除新文件 = **1441 passed + 12 subtests**,
含新文件 = **1454 + 12**,恰 **+13**、零回归;13/13 新守卫单跑全绿。PATH 挡板(javac/java/mvn/docker/psql/gradle/curl/wget)全程就位。

**PASS(独立重算,非照抄)**
1. **H¹=0 是结构事实**:不用它的余链秩,改用**生成树基本环基 + 三角形边界秩**(三条消元路径:
   集合单遍、0/1 矩阵全主元、基本环并秩互校)⇒ 环空间 27、rank(三角形)=27、rank(并)=27 ⇒ **dim H¹=0**。
   重叠图 **52/55/24**、神经 **V41/E55/T44**、rank δ⁰=28 δ¹=27、已知边 **8 全 consistent 全来自约束桶**、
   unknown **47**、已知边环空间 **2**、喂 **83** 条 findings 后已知边仍 **8** —— 逐数复现。三枚语料钉值哈希相符。
2. **生成树奇偶重推成立**:归纳不变式「`find(x)` 返回的奇偶 = 森林上 x→root 路径奇偶」逐 union 验证;
   冲突边必非树边(每对只处理一次)⇒ 树路径 ≥2 边 ⇒ 见证 ≥3 顶点;环奇偶 = (pa⊕pb)⊕rel = 1(冲突条件保证)。
   **独立实现**(迭代并查集 + 显式路径压缩,与被测方递归无压缩不同代码路径)逐见证对账;
   **300 随机图**:每个引擎见证都落在**暴力枚举的全部简单环**集合内且为奇环(139 例判红逐环对账),
   三态与 **2^n 暴力着色**逐态一致。
3. **缺陷态真能咬住目标断言**(坑78/做法55):V5a(BFS 不排除冲突边)红在 **`len(set(cyc))>=3` 本身**
   (trial 1,假环 `['c1#m','c2#m']`);V5b(BFS 排除冲突边)红在 **`parity==1` 本身**。
   V21 另独立搜到**具体偶环反例图**(4 桶 6 顶点,冲突边 (c3,c4):BFS 最短路见证 [c3,c1,c4] 奇偶 **0**,
   生成森林见证 [c3,c1,c0,c4] 奇偶 **1**,引擎实际输出全为奇环)。
4. **M1 检测力真增量**:旧 3 桶各只 1 条边 ⇒ **结构上无环可判**(不是漏判),新全局奇环判红,
   独立 2³ 暴力着色不可满足。**桶内 Helly 旧通道已检**:`cycle_consistency:770-776` 全桶交集 +
   `test_m7` 实跑绿 + V21 独立 CE3/CE4 复核 ⇒ 诊断推回的**前半成立**。
5. **不碰旧面**:16 个旧函数体(含 `_parity_check`/`cycle_consistency`/`_cell_section`/`_delta0`/
   `_pair_label_from_findings`/`find_candidates`/满足级)**逐字节相同**;新增 4 个模块级函数 + 1 嵌套;
   `run` 14 行 / `main` 9 行差异;全仓除 `frame_check.py` 外 **零 `higher_order` 命中**,
   `audit_entry:331-344` argv 不含旗标 ⇒ **双重未接线**成立。
6. **opt-in 三锁 + 单因子隔离**:见做法68(真基线 6/6 逐字节);语料开启后
   `cycles`/`candidates`/`cycle_obstructions`/原 `counts` **逐项不变**,新增恰 3 键。
7. **文献限定属实**:DOI **10.1007/s10208-015-9266-8**、作者 **Justin Curry / Robert Ghrist / Vidit Nanda**、
   期刊 **Found Comput Math**、收 2013-12-31 / 修 2015-04-03 / 接 2015-04-20 / 2016 刊出 —— 独立核实。
   「配对限制映射须为同构 + 匹配无环」是 CGN Morse 归约的前提;约束集层的限制是**求交( meet )不是线性映射**
   ⇒ 定理对约束通道不适用,论证成立;常数 GF(2) 系数下前提平凡成立但复形极小(E55/T44)零收益 ⇒ 不用 Morse 归约的依据成立。

**FAIL / 需修(四条,全部带可重跑反例)**
- **F1 「结构为零」被证伪且无守卫**(坑133):CE1 纯约束 + 细胞无关截面 ⇒ `incremental=1`。
  指名守卫只断言两截面相等,从不调用高阶通道。**这是它最重要的论断,却是唯一没被锁住的论断。**
- **F2 「真盲区只有跨资源环(无单桶包含全环)」被证伪**(坑134):CE2 单桶 r1 包含全环、旧全绿、
  新 `scope=within_bucket` 且 `incremental=1`。真盲区是**跨桶标签聚合**;M3 的蕴含不是定理。
- **F3 见证增量 ≠ 信息增量**(坑134):CE1 旧已判红全部 3 桶,新仍计增量 1 ⇒ 检测力被**高估**;
  冗余对账只按顶点集包含,漏「每条边都已被旧桶判红」。
- **F4 status 缺口径限定 ⇒ 自相矛盾头条**(坑135):旧 `obstructions 1` / 新 `status=consistent` 同产物并排,
  纯约束可达。姊妹满足级通道有「不是无矛盾」限定,本通道没有。

**变异矩阵(V21 自选 12 个缺陷锚点 + 2 个等价,全部在 /tmp 副本,入库清单如下 —— 坑123)**
沙箱 `/tmp/v21/mut/pilot`(与仓库 sha 逐字节相同),靶区 8 个守卫文件 **192 passed** 为空对照。

| 锚点 | 缺陷态 | 结果 | 指名红名 |
|---|---|---|---|
| V1 | 三角形填任意三顶点(不限同桶) | KILLED 5 红 | h1_hand_computed / selfcheck / m5 / m6 / m10 |
| V2 | 聚合极性:任一 consistent 即判一致 | KILLED 6 红 | m1 / m2 / m3 / m4 / m9 / selfcheck |
| **V3** | **冗余对账包含方向反转 `vset<=obs`→`obs<=vset`** | **SURVIVED 192 全绿** | **真缺口(已构造合法输入证明可达:旧判红桶{A,B} ⊊ 见证{A,B,C})** |
| V4 | scope 松成「任一顶点在桶内」 | KILLED 1 红 | m1 |
| V5a | 见证退回 BFS 不排除冲突边(坑131 缺陷态1) | KILLED 2 红 | selfcheck(**退化环断言本身**)/ m1 |
| V5b | 见证退回 BFS 排除冲突边(坑131 缺陷态2) | KILLED 1 红 | selfcheck(**奇偶断言本身**) |
| V6 | CLI 旗标接了但 run() 硬写 False | KILLED 1 红 | m6 |
| V7 | run() 签名默认 False→True | KILLED 2 红 | m5 / m6(m10 不红:它未断言 off 无 higher_order 键,期望由 V21 修正) |
| V8 | CLI store_true→store_false | KILLED 1 红 | m6 |
| V9 | 关闭时仍计算(只是不发布) | SURVIVED | **等价**:函数体零 I/O、输入深比较未变、两次调用结果相同 ⇒ 纯函数,只多耗时 |
| V10 | 三态去掉 `known==0` 分支 | KILLED 1 红 | m8 |
| V11 | rank δ⁰ 用顶点数代替秩 | KILLED 3 红 | selfcheck / h1_hand_computed / m10 |
| V12 | 增量把 redundant 也计入 | KILLED 1 红 | m3 |
| V13 | 神经复形丢 2-细胞桶(`>=2`→`>=3`) | KILLED 6 红 | m1 / m2 / m4 / m9 / h1_hand_computed / m10 |
| V14 | 见证边集漏冲突边(不闭环) | KILLED 1 红 | selfcheck |
| V2-EQ | (自撤)换序 if/elif | SURVIVED | **等价**:两条件互斥,已证(做法68) |

**11/12 缺陷锚点指名杀;V3 存活且已证可达 ⇒ 守卫缺口,不是等价变异。**
另:**`h1=999` 单因子变异下语料裁定面逐项不变(唯一差异键 `cochain`)**,CE2 更是 `dim H¹=0` 却报 1 障碍
⇒ **裁定实际由全局 1-骨架 GF(2) 奇偶得出**(与旧 `_parity_check` 同算法,作用域从桶内扩到全局),
神经复形/余链秩**只产出统计量、不参与裁定**。W23 已诚实声明「dim H¹ 是容量不是检测力」,
但通道名「神经复形 H¹ 通道」仍会让读者以为检出的是 H¹ 类 —— **措辞需限定,机制本身无误**。

**V21 自踩并自撤(四处,均未污染结论)**:① 首版独立 H¹ 公式写成 `环空间 − 秩增量` 得 27,
错(三角形边界本就在环空间内,秩增量恒 0),应为 `环空间 − rank(三角形)` = 0;
② 暴力环集成员判定未对引擎见证做同规范化 ⇒ trial 1 假红,是我的参照错不是引擎错;
③ V2 首版是等价变异,误疑守卫缺口,自证互斥后撤回并换真极性变异;
④ harness cwd 写成 `mut/pilot` 导致空对照 rc=4 —— 按坑92 第1步先判「自己的工具错」,未归因被测方。

**判定**:**PARTIAL**。机制、数字、三锁、单因子隔离、文献限定、不碰旧面**全部独立复现成立**;
但**两个核心论断(「结构为零」「真盲区只有跨资源环」)被可重跑反例证伪**,
**增量口径高估检测力**,**status 缺限定造成自相矛盾头条**,且**最重要的论断无守卫 + 1 个可达变异存活**。
四条修法都很小(改措辞 + 补冗余对账的边级判据 + 补 3 条守卫),不需要动机制。

### 坑 136:边级冗余对账的修法与验证 —— 见证增量≠信息增量,「结构论断」必须有通道级守卫(W23 重修,V21 证伪后)

**【V21 第二轮证伪,W23 再更正(2026-11-15)】本条修法 1 的「{x,y} ⊆ 旧红桶」覆盖判据双向都错,
已改为逐对判定粒度,见坑139。**CE5(单不一致边+一致边)证高估:一致边不携带矛盾,不该参与
冗余判定;CE6(旧桶因别的原因判红)证低估:桶判红有三条路径(成对交 break 在首对 / 全桶交空
Helly / judgments 奇环),都不证明桶内任意一对被判红,「两端点⊆旧红桶」≠「该边已被判红」。
notes「边级对账后约束通道信息增量必为 0」原版仍假(CE1 只是全不一致边特例),已改为
「不一致边全部约束导出的见证信息增量为 0」。混合覆盖夹具 CE7 入库,W1(all→any)/W3b(去旧红
条件)/W4(去逐对标签条件)全指名杀。以下原版修法 1 保留作错误链记录,**读时以坑139 为准**。

**修法(六条,全部入库可重跑)**:
1. **边级冗余对账**:~~见证边 (x,y) 被旧通道覆盖 ⇔ {x,y} ⊆ 某旧 inconsistent 桶;**全边覆盖** ⇒
   redundant_with_skeleton=true。~~**(第二轮更正:覆盖=某含该对的桶给该对 inconsistent 标签
   且该桶旧已判红;只不一致边参与,一致边空真覆盖;见坑139)** 逐边留痕 edges_covered_by_skeleton。
   顶点集包含判据废弃(CE1 不充分:三边各被 2-细胞旧红桶覆盖,顶点集却不含于任何单桶;
   CE2 不必要:见证 ⊆ 单桶但标签来自另一桶,旧通道全绿)。
2. **CE1/CE2/V3 入库为守卫**(test_higher_order.py):CE1 纯约束跨资源 → 信息增量=0;
   CE2 跨桶聚合桶内见证 → 信息增量=1 且 h1=0;V3 4-细胞桶见证 ⊊ 旧红桶 → 包含反转可达,
   反转突变被 V3 守卫指名红(M3 同红)。
3. **status 口径限定**:notes 与 CLI stdout 明写「语义=已知边无奇环,不是无障碍」;
   构造并立面守卫(2-细胞约束桶截面空集 ⇒ cycles obstructions=1 与 高阶 status=consistent
   同产物并立)。删限定变异 V5 → 口径守卫恰 1 红。
4. **裁定来源澄清**:notes/CLI/docstring 明写「裁定=全局 1-骨架 GF(2) 奇偶(跨桶标签聚合);
   神经复形/余链秩只产容量统计量,不参与裁定;dim H¹=0 仍可有障碍(CE2)」。删限定变异 V6 → 恰 1 红。
   通道名保留 --higher-order(改名收益不抵改动面),用限定消歧。
5. **诊断表述更正**:真盲区=跨桶标签聚合(不是「跨资源环」);约束通道**信息增量**为零
   (不是「字段为零」),真依据=旧通道已枚举桶内全部成对交(与细胞无关性无关)。
   原守卫 test_cell_independent_sections_no_cross_bucket_helly 名过其实(只断言 s1==s2,
   全程不调通道)→ 改名 test_same_ident_sections_equal_across_spellings,通道级论断归 CE1。
6. **字节基线升级(做法68)**:M10 关闭态产物与**改动前引擎冻结产物**
   (pilot/tests/fixtures/frame_check_pre_higher_order.json,126KB,钉值语料上生成)逐字节比对;
   原 M5 同代码显式 False 只保留作接线守卫。

**修后实测**:语料(relation_net b128afaf/units 9a8b5176/oracles 82fa1e91)裁定面不变:
status=unknown、dim H¹=0、障碍 0、**信息增量 0**;旧 14 桶 5/9/0 逐项不变;关闭态 ≡ 改动前基线
逐字节(cmp)。9 变异全指名杀(含 V3/V5/V6),空对照恢复绿。基线 1454+12 → **1458+12**
(+4 守卫:CE1/CE2/V3/口径;零回归)。纯净度守卫 rc=0。

**规约**:
1. 「结构上不可能/为零」是全称论断,**必须有通道级守卫**(真调通道、断言产物字段),
   截面性质守卫(s1==s2)锁不住通道论断 —— 论断与守卫的粒度要对齐(坑78 同族)。
2. 增量/冗余对账要按**证据单元**(边)而不是**聚合单元**(顶点集):旧裁决的粒度是桶,
   桶判红覆盖的是它的全部成对边,不是它的顶点集的任意子集结构。
3. 统计量与裁定量必须在产物里分清:容量(H¹)≠裁定(奇偶)≠增量(边级对账),
   通道名暗示的检测机制要与实际裁定机制一致,不一致就明写限定(坑64/66 的上同调版)。
4. 验证者的反例(CE1/CE2)入库为守卫才是证伪的完成态 —— 散文证伪会随会话消失(坑123)。

### 坑 137:边级冗余对账仍双向错 —— 「全边覆盖」把无矛盾的一致边也算进去,「端点在同桶」不等于该边被判红(V21 第二轮实测 W23 重修)

**重修后的判据**:`covered = [any({x,y} <= obs for obs in old_inconsistent_buckets) for x,y in cyc_edges]; redundant = all(covered)`。
CE1(三边全不一致)修后增量 0 ✓、CE2 增量 1 ✓、V3 方向反转被指名杀 ✓、口径限定三删各恰 1 红 ✓、
冻结基线经独立用改动前引擎(sha `49c5f6ff`,无 `higher_order_consistency`)重生成 **126414 字节逐字节相同**(sha `70a79300…`)✓。
**但新判据两个方向都错,各有一个不 monkeypatch 的反例**:

| 反例 | 构造(纯真实数据形态) | covered | 实测增量 | 应为 | 错向 |
|---|---|---|---|---|---|
| **CE5** | 纯约束、**细胞无关截面逐桶核实**;X.f=[A,B] 截面空集(旧判红),Y.g=[B,C] 与 Z.h=[A,C] 截面 {1,2}(旧一致)⇒ 奇环只 1 条不一致边 | `[T,F,F]` | **1** | **0** | **高估** |
| **CE6** | r1=[A,B,C,D] 自身 judgments 使 A-B-D 成奇环 ⇒ 旧判红 r1(conflict=**B-D**,r1 内 (A,B)=**consistent**);r2/r3/r4 各 2 细胞边 inconsistent ⇒ 旧在这三桶**全绿** | `[T,T,T]` | **0** | **1** | **低估(吞真增量)** |

**CE5 病根**:`all(covered)` 要求**一致边**也被覆盖。一致边不携带矛盾,不该参与冗余判定。
唯一的不一致对 (A,B) 旧已判红 ⇒ **零信息增量**,却因两条一致边未被覆盖而报 1。
⇒ notes 那句「边级对账后约束通道的**信息增量必为 0**」**仍为假**(CE1 只是「全边不一致」的特例)。
**正确判据**:只对见证的**不一致边**要求覆盖(`all(c for c,e in zip(covered,edges) if label[e]=="inconsistent")`)。

**CE6 病根(任务点名要核的等价性)**:**「两端点 ⊆ 旧 inconsistent 桶」≠「该边已被旧通道判红」**。
旧桶判红有三条互不相同的路径:① 成对交不相交(`cycle_consistency:764-767` **break 在第一个冲突对**,其余对根本没评估)
② 全桶交空(Helly,**没有任何一对**不相交)③ judgments 奇环(**只有环上部分边**不一致)。
三条都只证明「**桶**被旗标」,不证明「**这条边**被判不一致」。CE6 里 (A,B) 在 r1 自身判定中是 consistent,
旧通道从未判它红,新判据却算它「已覆盖」,于是把来自 r2/r3/r4 的**真跨桶增量**整个吞掉。
**修法**:覆盖要按**旧通道对该对的逐对判定**(约束桶=`_is_empty(_intersect(sec_a,sec_b))`;judgments 桶=该桶标签),
不是按桶细胞集包含。`_higher_order_labels` 的 `per_pair` 已经逐桶算过标签,留一份「旧红桶内逐对不一致集」即可,零额外成本。
**通用纪律**:「已被上游覆盖」这种断言,必须落到**上游实际产出的那个判定粒度**(这里是「对」,不是「桶」);
粒度粗一级就会同时产生高估与低估。与坑134「见证增量≠信息增量」同族,但本轮是**修完仍错**,且错向相反。

### 坑 138:守卫夹具的「覆盖形态」要普查 —— 全覆盖/零覆盖各一条,缺「部分覆盖」就同时放过两个可达变异(V21 第二轮)

**实测**:196 守卫空对照全绿下,两个**新锚点存活**,且都已用合法输入证明**可达**(不是等价变异):

| 锚点 | 缺陷态 | CE5 上正确态 → 变异态 | 结果 |
|---|---|---|---|
| W1 | `redundant = all(covered)` → `any(covered)` | 增量 **1 → 0**、redundant False→True | **SURVIVED** |
| W3 | 边级覆盖 `{x,y} <= obs` → `{x,y} & obs` | covered `[T,F,F]`→`[T,T,T]`、增量 **1 → 0** | **SURVIVED** |

**单一病根**:入库夹具的覆盖形态只有两种 —— CE1/M3/V3 **全覆盖**(`[T,T,T]`)、CE2/M1 **零覆盖**(`[F,F,F]`)。
`all` 与 `any` 在这两种形态上**恒等**,`<=` 与 `&` 在「边两端点同属一个 2-细胞红桶」时也**恒等**。
⇒ **缺一条「部分覆盖」夹具**,两个变异同时穿过。补 CE5/CE7 形态(`[T,F,F]` / `[F,T,F,F]`)即双双指名杀。
**规约**:布尔聚合判据(`all`/`any`)与集合判据(`<=`/`&`/`==`)的守卫夹具,必须**按判据的等价类普查形态**
(全真/全假/**混合**),只造两端极值等于没锁聚合词。与坑78(变异精度)、做法54(等价类清单)同源,
本条补的是**「混合态」这一格最容易漏,而它恰是区分 all/any 的唯一格**。

### V21 第二轮复验结论:W23 六条重修 —— **PARTIAL**(四条全过,边级对账仍双向错 + 2 个可达变异存活)

**快照**:HEAD `f6b4cf8`(父 `5061d5e`),工作树 `frame_check.py` sha `98fcc967…`;
改动前引擎在 `5061d5e` 与 `f6b4cf8` **同为 `49c5f6ff`**(基线无歧义)。
322 文件全量快照实验前后**零差异**,`git status` 与接手时相同。PATH 挡板全程就位。
**基线独立复现 1458 passed + 12 subtests**(与声称一致);新守卫文件单跑 **17 passed**(13+4)。

**PASS(独立重核,非照抄)**
1. **冻结基线来源为真**(本轮重点):用改动前引擎(无 `higher_order_consistency`)在钉值语料上独立重生成,
   **126414 字节、sha `70a793004b734b82…` 与入库夹具逐字节相同**;夹具不含 `higher_order` 键。
   ⇒ **不是同代码关旗标的同义反复**,做法68 升级成立。
2. **单因子隔离**:关闭态序列化 == 冻结基线(逐字节)**且** == 改动前引擎当场重跑(全键深比);
   开启后变化的顶层键**恰为** `counts` 与 `higher_order`,`counts` **原有键零变化**、新增恰 3 键;
   `cycles`/`candidates`/`cycle_obstructions` 逐项相同。语料裁定面不变:14 桶 **5/9/0**、
   nerve **V41/E55/T44**、δ⁰=28 δ¹=27 **H¹=0**、known **8**/unknown **47**、障碍 0、增量 0。
3. **口径限定守卫双向成立**:W4 删 notes「不是「无障碍」」→ **恰 1 红**;W5「不参与裁定」→「参与裁定」(notes+CLI 全改)→ **恰 1 红**;
   W6 删 CLI「语义=已知边无奇环,不是无障碍」→ **恰 1 红**;三次红的都是 `test_status_semantics_and_adjudication_source_qualifiers` 本身。
4. **V3 缺口已闭**:W8 包含方向反转 → **KILLED**,指名红 `test_m3` + `test_v3_proper_superset_bucket_redundancy_direction`。
5. **重修本身有守卫**:W2 边级退回顶点集 → **KILLED**,指名红 `test_ce1`;W10 冗余判据退回 scope → **KILLED**,指名红 `test_ce1`+`test_ce2`
   ⇒ 「scope 只是拓扑分类不是冗余判据」双向锁住。W9 撤销跨桶聚合 → KILLED(指名红 `test_m9`)。
6. **部分覆盖不判冗余(`all()` 机制本身)正确**:CE7 4-环 covered=`[F,T,F,F]` → redundant=False ✓。
7. **不碰旧面**:14 个旧函数体(含 `_parity_check`/`cycle_consistency`/`_cell_section`/`_delta0`/满足级)**逐字节相同**;
   新增 4 模块级函数+1 嵌套、零删除;`check_engine_purity` **rc=0**。
8. **数字复现**:CE1 修后增量 **0**、CE2 **1**、M1 **1**、语料 **0** —— 与声称一致。

**FAIL(三条,全部带可重跑反例)**
- **F1 notes「约束通道信息增量必为 0」仍为假**(坑137 CE5):纯约束 + 细胞无关截面,奇环只 1 条不一致边且旧已判红 ⇒ 实测增量 **1**。
  `all(covered)` 把**一致边**也算进覆盖要求 ⇒ **高估**。修法:只要求**不一致边**被覆盖。
- **F2 「端点 ⊆ 旧红桶」≠「该边被判红」**(坑137 CE6):旧桶因**别的对**(conflict=B-D)判红,见证三边的不一致标签全来自旧**全绿**的 r2/r3/r4,
  却被判全覆盖 ⇒ 增量 **0**,**吞掉真跨桶增量**(低估)。修法:覆盖落到**逐对判定**粒度,`per_pair` 已有该信息,零额外成本。
- **F3 两个可达变异存活**(坑138):W1(`all`→`any`)、W3(`<=`→`&`)在 196 守卫下全绿,已用 CE5 证明行为可区分(增量 1→0)。
  单一病根:**夹具只有全覆盖与零覆盖,缺部分覆盖**。补一条混合态夹具即双双指名杀。

**变异矩阵(V21 第二轮自选 10 锚点,空对照 196 绿;坑123 清单入库)**
沙箱 `/tmp/v21r2/mut/pilot`(sha 与仓库逐字节相同),靶区 8 守卫文件。

| 锚点 | 缺陷态 | 结果 | 指名红名 |
|---|---|---|---|
| W1 | `all(covered)`→`any(covered)` | **SURVIVED(已证可达)** | — 守卫缺口 |
| W2 | 边级对账退回顶点集(撤销重修) | KILLED 1 | ce1 |
| W3 | `{x,y} <= obs`→`{x,y} & obs` | **SURVIVED(已证可达)** | — 守卫缺口 |
| W4 | notes 删「不是「无障碍」」 | KILLED **恰1** | status_semantics_qualifiers |
| W5 | 「不参与裁定」→「参与裁定」 | KILLED **恰1** | status_semantics_qualifiers |
| W6 | CLI 删 status 口径限定 | KILLED **恰1** | status_semantics_qualifiers |
| W7 | 删 `edges_covered_by_skeleton` 留痕 | KILLED 3 | ce1 / m3 / v3 |
| W8 | V3 包含方向反转(上轮存活) | KILLED 2 | m3 / v3 |
| W9 | 跨桶聚合撤销(末桶胜) | KILLED 1 | m9 |
| W10 | 冗余判据退回 scope | KILLED 2 | ce1 / ce2 |

**8/10 指名杀;W1/W3 存活且已证可达 ⇒ 真缺口,非等价变异。**
**判定:PARTIAL**。六条声称中 **2/3/4/5/6 与做法68 升级全部独立复现成立**(冻结基线来源尤其扎实);
**第 1 条(边级对账)的核心正确性不成立** —— 它把上轮的「高估」修成了「部分高估 + 部分低估」,
且 notes 里那句新的结构论断(「信息增量必为 0」)**第二次被纯约束反例证伪**。
三条修法都很小(覆盖只算不一致边 + 覆盖落到逐对粒度 + 补一条部分覆盖夹具),**不需要动裁定机制**。
**给下一轮的提醒**:本轮是「结构论断连续两轮被同一类反例证伪」——
凡写「必为 0」「结构上不可能」,先造**只含一条不一致边**与**旧桶因别的原因判红**这两个极端夹具,再下笔。

### 坑 139:冗余对账要落到逐对判定粒度 —— 桶判红≠桶内每对被判红;一致边不携带矛盾;「必为0」先造两个极端夹具(W23 第三轮,V21 第二轮证伪后)

**病根(双向都错,上轮的「高估」被修成「部分高估+部分低估」)**:
坑136 的覆盖判据「见证边两端点 ⊆ 某旧 inconsistent 桶」把**桶级旗标**当成了**逐对判定**。
旧桶判红有三条路径,**都不证明桶内任意一对被判红**:
① 成对交不相交 —— cycle_consistency **break 在第一个冲突对**,其余对根本没评估;
② 全桶交空(Helly)—— **没有任何一对**不相交;
③ judgments 奇环 —— **只有环上部分边**不一致,其余对 consistent。

**两个反例(独立重放成立,入库为守卫)**:
- **CE5 高估**:纯约束,X.f=[A,B] 截面空(旧红),Y.g=[B,C]/Z.h=[A,C] 截面{1,2}(旧一致)⇒
  奇环只 1 条不一致边 (A,B),旧已判红 ⇒ 信息增量应 0;原版 all(covered) 要求**一致边也被覆盖**,
  covered=[T,F,F] ⇒ 误判增量 1。**一致边不携带矛盾,不该参与冗余判定**。
- **CE6 低估**:r1=[A,B,C,D] 因 A-B-D 奇环判红(conflict=B-D,r1 内 (A,B)=consistent);
  r2/r3/r4 各 2 细胞边 inconsistent 旧全绿。见证三角的不一致边来自绿桶,旧通道从未判红这些对 ⇒
  信息增量应≥1;原版「两端点⊆r1」⇒ covered=[T,T,T] ⇒ 吞真增量判 0。
  (实测该夹具有两个奇环见证 B-A-C 与 C-A-D,后者 A-C 边亦来自绿桶 ⇒ 增量=2。)

**修法(零额外成本)**:_higher_order_labels 的 per_pair 本就逐桶算标签 ⇒ 返回 (聚合, 逐桶) 二元组。
覆盖判据:**边 (x,y) 覆盖 ⇔ 某含该对的桶给该对 inconsistent 标签 且 该桶旧已判红**;
**只不一致边参与**(一致边空真覆盖);全部不一致边覆盖 ⇒ redundant。
约束导出的逐对 inconsistent ⇒ 所在桶必旧红(成对枚举必命中冲突)⇒ 不一致边全约束导出的见证
增量必 0(CE1 全不一致边 / CE5 单不一致边两形态守卫);混合见证逐边对账。

**守卫缺口的单一病根(元教训)**:入库夹具只有**全覆盖**(CE1/M3/V3)与**零覆盖**(CE2/M1)两极,
`all`/`any` 与 `<=`/`&` 在两极上**恒等** ⇒ W1(all→any)/W3(包含→相交)存活且可达。
**补一条混合态夹具 CE7**(4-环 covered=[T,F,F,T]:1 不一致边被旧红约束桶覆盖 + 2 不一致边来自
绿桶 + 1 一致边空真)⇒ W1 指名红;W3b(去旧红条件)/W4(去逐对标签条件)被 CE6 指名杀。

**V21 元教训(写进纪律)**:**「结构论断连续两轮被同一类反例证伪」** —— 凡写「必为 0」「结构上
不可能」,**先造两个极端夹具再下笔**:① 只含一条不一致边(其余一致) ② 旧桶因别的原因判红
(见证边与判红原因不相干)。两极夹具 + 混合态夹具,三态齐备才许下全称论断。
与坑78(变异精度)、坑125(定理守卫要有环)、做法67(前提逐个破坏验可达)同族:
**论断的边界形态就是守卫的夹具形态**。
**补(V21 坑140 病根)**:判据里**每个合取条件要各自踩遍三态** —— 条件C(桶旧状态)缺 unknown 格、
条件B 量词(any)缺「一对跨两桶一红一绿」格、条件B 标签缺红桶 unknown 格 ⇒ X2/X3/X4 三个可达
变异同时穿过。已补三条夹具,各自指名红(恰 1 红,零代杀);X1(一致边空真)已证等价不动。

**修后实测(重报信息增量)**:语料 0(障碍 0)/ M1=1 / CE1=0 / CE2=1 / CE5=0 / CE6=2。
11 变异全指名杀(W1→CE7;W3b→CE6 等 5 红;W4→CE6 恰 1 红;MA/MB2/MC2/MD/ME/MF/V5/V6 复跑仍杀),
空对照恢复绿。基线 1458+12 → **1461+12**(+3 守卫 CE5/CE6/CE7,零回归)。
单因子隔离重验:关闭态 ≡ 改动前冻结基线逐字节;语料裁定面 status=unknown/h1=0/障碍0/增量0 不变。

### 坑 140:新判据的**每个守卫条件**都要有自己的混合态夹具 —— 补了 all/any 一格,又漏了三格(V21 第三轮实测 W23)

**背景**:第二轮 W1(`all`→`any`)与 W3(`<=`→`&`)存活,病根是夹具只有全覆盖/零覆盖。
第三轮补了 CE7 混合态,两者双双指名杀 ✓。**但新判据引入了三个新条件,每个又各缺一条混合态夹具**:

```
covered = [labels[e] != "inconsistent" or                       # 条件A 一致边空真
           any(per_bucket[r].get(e) == "inconsistent"            # 条件B 逐对标签
               and old_status[r] == "inconsistent"               # 条件C 该桶旧已判红
               for r in pair_res[e])]
```

| 锚点 | 缺陷态 | 探针夹具(合法输入,已证行为可区分) | 正确→变异 | 结果 |
|---|---|---|---|---|
| **X2** | 条件C `== "inconsistent"` → `!= "consistent"`(**unknown 桶也算红**) | r1=[A,B,C] 只给 (A,B)=inc、其余对无 finding ⇒ 旧 **unknown**;X.f=[B,C]/Y.g=[A,C] 截面空 ⇒ 旧红。见证 {A,B,C} 的 (A,B) 只来自 r1 | 增量 **1→0** | **SURVIVED** |
| **X3** | 条件B 的 `any` → `all`(要求该对**所在全部桶**都红且不一致) | (A,B) 同属 X.f(红+inc) 与 r2(绿+inc);见证 {A,B,D} 另两边全来自红桶 | 增量 **0→1** | **SURVIVED** |
| **X4** | 条件B `== "inconsistent"` → `!= "consistent"`(**红桶的 unknown 标签也算覆盖**) | r1=[A,B,C,D] 因 B-D 奇环判红,但 (A,C) 无 judgment ⇒ 逐对 **unknown**;(A,C) 的 inc 来自绿桶 r4 | 增量 **1→0** | **SURVIVED** |
| X1 | 条件A `!= "inconsistent"` → `== "consistent"` | — | 无差异 | **等价**(见证边恒已知:树路径边只在已知边 union 成功时入森林,冲突边来自已知边;1500 随机图 7486 条见证边 unknown=0) |

**单一病根**:入库夹具里 **旧桶状态只有 inconsistent/consistent,没有 unknown**;
**每对只属一个桶,没有「一对跨两桶且一红一绿」**;**红桶的逐对标签只有 inc/con,没有 unknown**。
三个条件各自的「第三态」都没被夹具踩到 ⇒ 三个可达变异同时穿过 199 守卫。
**规约(补坑138)**:判据里每多一个**合取条件**,就要为它造一条**该条件为假而其余为真**的夹具;
三态量(consistent/inconsistent/**unknown**)参与判据时,**unknown 那一格必须单独有夹具**,
否则「== X」与「!= 非X」这两种写法在只有两态的夹具上恒等。与坑129(精确匹配 loosened 成 substring)同族。

### V21 第三轮确认结论:W23 三处重修 —— **PARTIAL(机制与全部论断 PASS;仅剩 3 个可达守卫缺口,各补一条夹具即闭)**

**快照**:HEAD `f6b4cf8`,工作树 `frame_check.py` sha `215a603c…`;322 文件全量快照实验前后**零差异**,
`git status` 与接手时相同。PATH 挡板全程就位。**基线独立复现 1461 passed + 12 subtests**;守卫文件单跑 **20 passed**(17+3)。

**PASS(逐条独立重核)**
1. **CE5 = 0** ✓:纯约束、细胞无关,奇环只 1 条不一致边且旧已判红 ⇒ covered `[T,T,T]`(一致边空真)、redundant=True、增量 **0**。
2. **CE6 = 2 独立核实为真**(它把期望从 1 改成 2 是**对的**):用它的 lab1((A,D)=consistent、(B,D)=inconsistent),
   独立暴力枚举已知边子图得 **4 个奇环**;独立并查集(迭代+路径压缩)得**冲突边 2 条** = (B,C),(C,D);
   引擎见证 **2 个**且都落在奇环集内。见证①[B,A,C] 三条不一致边分别来自 **r2/r3/r4(旧全绿)** ⇒ covered `[F,F,F]`;
   见证②[C,A,D] 唯一不一致边 (A,C) 来自 **绿桶 r4**(r1 给的是 consistent)⇒ covered `[F,T,T]`。两个都是真增量 ⇒ **2**。
   (注:见证数 = 冲突边数 ≤ 环秩,不是奇环总数;另 2 个奇环是这 2 个的 GF(2) 组合,报基不报全,合理。)
3. **第四反例 CE8(V21 新造,选择性测试)PASS**:X.f=[A,B]∅ / Y.g=[B,D]∅ / Z.h=[A,D]∅(旧全红)+ r2=[B,C] / r3=[A,C] judgments inc(旧绿)
   ⇒ 见证 {A,B,D} 三条不一致边全来自旧红桶 ⇒ covered `[T,T,T]`、redundant=True;
   见证 {A,B,C} 含绿桶不一致边 ⇒ covered `[T,F,F]`、redundant=False;**总增量 = 1**。
   **增量只算含未覆盖不一致边的见证,选择性正确**。
4. **notes 新表述不再过宽**:「不一致边全部约束导出的见证,信息增量为 0」——
   **5500 个随机配置(3000 纯约束 + 2500 约束×judgments 混合)、13888 个命中见证,零反例**;
   另有结构证明:约束标签只来自 `full` 桶(`_higher_order_labels:872`),而 `full` ⇒ `have==cells` ⇒
   旧通道必进成对枚举(`cycle_consistency:763-767`),任一对不相交即判红 ⇒ 该桶必旧红 ⇒ 条件B+C 同时成立。
   **前瞻限定**:该蕴含依赖「约束标签只在 full 桶产生」这一实现事实;DTO 投影若放开部分截面约束标签,须重推。
5. **CE7 混合态夹具真使三个变异指名红**(从仓库跑):W1(`all`→`any`)→ **3 红含 ce7**;
   W3b(去条件C)→ **5 红**(ce2/ce6/ce7/m1/m9);W4(去条件B)→ **恰 1 红 = ce6**。与声称逐字一致。
   X5(整体退回「两端点⊆旧红桶」)→ 3 红(ce5/ce6/ce7)⇒ 本轮重修本身有守卫。
6. **单因子隔离 + 冻结基线**:关闭态序列化 == 入库夹具(逐字节,sha `70a79300…`)**且** == 改动前引擎
   (`49c5f6ff`,无 `higher_order_consistency`)当场重跑(全键深比);开启后变化顶层键**恰为** `counts`+`higher_order`,
   `counts` **原有键零变化**、新增恰 3 键;`cycles`/`candidates`/`cycle_obstructions` 逐项相同。
   语料裁定面不变:14 桶 **5/9/0**、V41/E55/T44、δ⁰28 δ¹27 **H¹=0**、known 8/unknown 47、障碍 0、增量 0。
7. **裁定机器未动**(与第二轮引擎 `98fcc967` 去注释逐行比):并查集/生成森林/见证重构/神经复形/余链秩/三态/已知边环空间
   **全部相同**,唯一差异恰为两处预期改动(`old_inconsistent_buckets`→`old_status`、`labels`→`labels, per_bucket_labels`);
   CLI 口径限定行逐字相同;冻结基线夹具 sha 未变;14 个旧函数体 vs 改动前引擎逐字节相同;`check_engine_purity` **rc=0**。
8. **信息增量重报复现**:语料 **0** / M1 **1** / CE1 **0** / CE2 **1** / CE5 **0** / CE6 **2** —— 与声称一致。

**FAIL(仅守卫覆盖,无机制错误、无假论断)**
- **F1 三个可达变异存活**(坑140):X2(unknown 桶算红)、X3(逐对覆盖 any→all)、X4(红桶 unknown 标签算覆盖),
  199 守卫全绿,均已用合法夹具证明行为可区分(增量 1→0 / 0→1 / 1→0)。**三条夹具已在坑140 表中给出,可直接入库。**
- X1 为**等价变异**(已证:见证边恒已知),不计缺口。

**变异矩阵(V21 第三轮 9 锚点,空对照 199 绿;坑123 清单入库)**
沙箱 `/tmp/v21r3/mut/pilot`(sha 与仓库逐字节相同),靶区 8 守卫文件。

| 锚点 | 缺陷态 | 结果 | 指名红名 |
|---|---|---|---|
| W1 | `all(covered)`→`any` | KILLED 3 | ce2 / ce6 / **ce7** |
| W3b | 去「该桶旧已判红」 | KILLED **5** | ce2 / ce6 / ce7 / m1 / m9 |
| W4 | 去「逐对标签 inconsistent」 | KILLED **恰1** | **ce6** |
| X1 | 一致边空真写法改写 | SURVIVED | **等价**(见证边恒已知,7486 边 unknown=0) |
| **X2** | 旧红 `==inc`→`!=con` | **SURVIVED(已证可达)** | — 缺口 |
| **X3** | 逐对覆盖 `any`→`all` | **SURVIVED(已证可达)** | — 缺口 |
| **X4** | 逐对标签 `==inc`→`!=con` | **SURVIVED(已证可达)** | — 缺口 |
| X5 | 退回「两端点⊆旧红桶」 | KILLED 3 | ce5 / ce6 / ce7 |

**判定:PARTIAL —— 但性质与前两轮根本不同**:本轮**没有任何假论断、没有任何错误输出**;
三处重修全部正确,第四反例与 5500 配置随机搜索都攻不动它。剩余项**纯为守卫覆盖**:
新判据三个合取条件各缺一条「该条件为假、其余为真」的夹具(坑140 已给出可直接入库的三条)。
**补完这三条夹具即可判 PASS,不需要动任何机制代码。**
**三轮累计纪律记账**:V21 自己在第一轮错写独立 H¹ 公式、错做环规范化、把等价变异误疑为缺口、harness cwd 错致空对照红;
第二轮错用自家 CE6 夹具核对方的 CE6(标签不同);第三轮 X2 夹具漏接 C→r1、把新函数列进「旧函数体」清单、
区段锚点用了对方已改的变量名 —— **七次全部自撤并修正,未污染任何结论**(坑92 第1步:空对照红先判自己的工具错)。

### V21 最终门结论:W23 高阶通道第四轮(补三条夹具)—— **PASS**(提交前最后一道门,零残留阻断)

**快照**:工作树 `frame_check.py` sha `35912c78…`;322 文件全量快照实验前后**零差异**,`git status` 与接手时相同。
**基线独立复现 1464 passed + 12 subtests**;守卫文件单跑 **23 passed**(20+3),三条新夹具从仓库单跑 **3 passed**。

1. **机制代码零改动(逐行核验)**:与第三轮已验引擎 `215a603c…` 做 `diff -u`,**唯一差异是 notes 里一句前瞻限定字符串**,
   零代码行改动 ⇒ 未超出本轮授权。裁定机器/生成森林/余链秩/三态/CLI 口径行全部沿用第三轮已验状态。
2. **三条夹具各恰 1 红、零代杀**(空对照 202 绿,靶区 8 守卫文件):
   X2(`==inc`→`!=con`)→ **仅** `test_x2_unknown_old_bucket_not_counted_as_red`;
   X3(`any`→`all`)→ **仅** `test_x3_pair_in_red_and_green_bucket_any_not_all`;
   X4(逐对标签 `==inc`→`!=con`)→ **仅** `test_x4_unknown_per_pair_label_in_red_bucket_not_coverage`。
   坑140 三个可达缺口**全部闭合**,且每条夹具只杀自己的目标变异(无坑101 的相邻不变式代杀)。
3. **前瞻限定守卫**:V7 删 notes 那句 → **恰 1 红** = `test_status_semantics_and_adjudication_source_qualifiers`。
4. **单因子隔离**:关闭态序列化 == 入库冻结基线(逐字节)**且** == 改动前引擎(`49c5f6ff`)当场重跑(全键深比);
   开启后变化顶层键**恰为** `counts`+`higher_order`,`counts` 原有键**零变化**、新增恰 3 键;
   `cycles`/`candidates`/`cycle_obstructions` 逐项相同。语料裁定面不变:**5/9/0**、H¹=**0**、known **8**/unknown **47**、增量 **0**。
5. `check_engine_purity` **rc=0**。

**四轮累计**:V21 共提出 4 个反例(CE1/CE2/CE5/CE6)+ 1 个选择性反例(CE8)+ 22 个自选变异锚点,
查出 2 个假论断、1 个双向错的冗余判据、6 个可达守卫缺口;W23 每轮先独立重放反例再动手,全部修正到位。
**本轮无新发现,零残留,判定 PASS。**
