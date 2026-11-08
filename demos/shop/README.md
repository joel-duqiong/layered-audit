# demo-shop —— 审计系统的演示目标

**这个项目存在的唯一理由：验证审计系统在「jiuyan 没有的 Java/Spring 特性」上照样能用。**
上一轮把「跨仓库 / 涌现 / 语义地图 / 3 个零覆盖类别」标成「不实现」，
理由是「jiuyan 里没有这些面」—— 那是偷懒。**通用性的定义就是能处理有这些特性的项目。**

## 它刻意包含什么

| 特性 | jiuyan | demo-shop | 用来验证什么 |
|---|---|---|---|
| 复杂继承层次（3 层） | 单层 | `BaseEntity → AuditableEntity → ShopProduct/ShopCustomer/ShopOrder` | `class.inheritance_design` |
| 条件装配（@Profile） | 无 | `MemberPricingStrategy @Profile("vip")` | 换 profile 装配结果就变 → 只有起两个实例对比才发现 |
| 缓存 | 无 | `@Cacheable("products")` | `aop_aspect` / 缓存里放可变对象 |
| 定时任务 | 无 | `@Scheduled(cron=...)` | `aop_aspect` |
| 自定义切面 | 只有 @Transactional | 继承层次 + 覆写 | 里氏替换违反 |
| 同一接口多实现 | 无 | `PricingStrategy` × 2（@Profile 互斥） | `dependency_injection` |
| 高复杂度函数 | 简单 | `ReportService.aggregate()` 三层嵌套 + N+1 形状 | `function.complexity` |
| 对外暴露内部可变状态 | 无 | `ProductService.snapshot()` 返回内部实体列表 | `class.encapsulation_leak` |
| 跨仓库调用 | 无 | `FxClient` → HTTP → `python-svc`（**另一个仓库，另一种语言**） | 跨仓库 |
| 无 OpenAPI 快照 | 有 | 无（不引 springdoc） | 服务层在缺 openapi 时能不能退化 |
| 不用 @Table 的实体 | 有 @Table | 无（靠默认命名策略推表名） | 类层能不能推出表名 |

## 埋的缺陷（都是**可被机械判定**的）

| 编号 | 缺陷 | 性质 | 怎么才能发现 |
|---|---|---|---|
| D1 | `ShopOrder.touch()` 覆写父类时把审计人清空 | **里氏替换违反** | 类层：子类覆写改变了父类承诺的后置条件 |
| D2 | `MemberPricingStrategy` 用 `@Profile`（部署环境维度）表达 VIP（客户等级维度） | **条件装配用错维度** | 静态看不出；起 default / vip 两个实例对比行为才暴露 |
| D3 | `ProductService.snapshot()` 返回内部可变实体 | **封装泄漏** | 类层：public 方法返回内部可变类型 |
| D4 | `ReportService.aggregate()` 三层嵌套 + 循环内查库 | **高复杂度 / 查询放大** | 函数层：分支数 / 复杂度 |
| D5 | `@Cacheable` 缓存里放可变对象 | **缓存污染** | 服务层：缓存命中后对象被改 |
| **D6** | **额度按报价查、账按实付记** | **真涌现** | 见下 |

### D6：真正的涌现（个体都对，合起来错）

```
QuotaService.withinLimit(customer, quoted)   单独看：额度检查，完全正确
LedgerService.record(order, settled)        单独看：记账，完全正确
        ↓ 合起来
OrderService.create:
    额度按【报价 900000】检查   ← QuotaService
    结算 = 报价 × 0.9 = 810000  ← PricingStrategy（VIP 九折）
    账按【实付 810000】记       ← LedgerService
    ⇒ 额度被占住 900000，账上只有 810000，差 90000 无对应账目
```

**任何单点检查（额度够不够、账记对没记对）都发现不了它。**
这正是这套系统存在的理由 —— 单点全过 ≠ 系统是对的。

## 怎么跑

```bash
# 1. 建库（在既有 postgres 容器里另开一个库，零残留）
docker exec jiuyan-postgres psql -U jiuyan -d postgres -c "CREATE DATABASE demo_shop;"

# 2. 编译（复用 jiuyan 的 maven 离线依赖，所以能离线构建）
cd backend && mvn -o -q -DskipTests package

# 3. 起跨仓库的另一半（Python，stdlib，宿主机跑）
python3 python-svc/server.py &

# 4. 起 demo 后端（复用 jiuyan 的镜像 + 我们自己的 jar；不加 OTel agent）
docker run -d --name demo-shop-backend --network jiuyan-acceptance_default \
  -p 127.0.0.1:8090:8090 --entrypoint java \
  -e SERVER_PORT=8090 -e SPRING_DATASOURCE_URL=jdbc:postgresql://postgres:5432/demo_shop \
  -e SPRING_DATASOURCE_USERNAME=jiuyan -e SPRING_DATASOURCE_PASSWORD=jiuyan-acceptance-local \
  -e SPRING_PROFILES_ACTIVE=default -e APP_FX_BASE_URL=http://host.docker.internal:8098 \
  -v "$PWD/backend/target/demo-shop-backend-0.0.1-SNAPSHOT.jar:/app/app.jar:ro" \
  jiuyan-backend -jar /app/app.jar

# 5. 审计它（换目标 = 换 LAYERED_AUDIT_TARGET，脚本一行不改）
LAYERED_AUDIT_TARGET=$PWD/target.json python3 ../../pilot/fn_static.py

# 6. 或者一把梭：demo-shop 有自己的完整流水线
#    类层 -> 业务不变式（服务层）-> 关系网 -> 候选链 -> 真跑裁决
#    -> 归一化 + 聚合 -> 涌现 FDR 门禁 -> 残留守卫
bash run_pipeline.sh
#  exit 0 = 全绿；涌现门禁此时是 GREEN（FDR_inj 1.000 / Attr_acc 1.000）
```

> demo 的健康检查探的是 8080，而应用跑在 8090 —— 所以
> `docker ps` 里这两个容器显示 unhealthy。**不影响运行**（实测都返 200），
> 是探针端口写错了。

## 关键：换项目不改编脚本

```bash
LAYERED_AUDIT_TARGET=.../demos/shop/target.json python3 ../../pilot/fn_static.py
#  → 类 27 个 / 函数 77 个（纯函数 71 个）

LAYERED_AUDIT_TARGET=.../demos/shop/target.json   python3 ../../pilot/fn_dynamic.py --cases-file .../demos/shop/fn_cases.json
#  → 用例 6 条：通过 6，失败 0
```

所有路径来自 `target.json` 的 `paths` 段；`pilot/project.py` 是唯一读它的地方。

## 这个 demo 现在跑通了什么

| 层 | 状态 | 关键结论 |
|---|---|---|
| 类层 | ✅ | 15 条发现（含里氏替换违反、条件装配用错维度、封装泄漏、实体缺值相等） |
| 服务层 | ✅ | 2 条业务不变式判定 —— **口径分裂**（service own）+ **VIP 拿不到会员价**（class cite） |
| 集成层 | ✅ | 涌现裁决 `EMERGENT_CONJUNCTIVE`，核心集 INJ-TIER-VIP + INJ-PROFILE-VIP |
| 涌现 FDR 门禁 | ✅ **GREEN** | FDR_inj 1.000 / Attr_acc 1.000（分母 2 / 检出 2 / 归属正确 2） |

**涌现门禁为什么先是红的**：demo 最初只有类层和集成层，
`INJ-PROFILE-VIP` 的根因没有任何一层能开单（只被引用）——
门禁把它**指名道姓地报了出来**。补上服务层的差分证据后转绿。
阳性对照：抽掉服务层那两条 finding，门禁立刻变回红（FDR 0.500）——
**这个绿是挣来的，不是把阈值调松换来的**。

**函数层 / 文件层的差分判定层还是 jiuyan 专属**（`fn_diff.py` / `file_diff.py`
里还有项目字面量），所以 demo 目前开不出这两层的单 —— 已知缺口，见
`HANDOFF.md` §11-B15。

**函数层载体已经通用化**：`pilot/fn_harness/GenericHarness.java` 里零项目字面量 ——
目标类、依赖打桩、样本、用例、类型别名全部来自用例文件（`fn_cases.json`）。
换项目只换这个文件。

harness 会做的事（都是通用的）：
- 反射选目标类的构造器，**递归构造依赖图**（接口 → 动态代理并查打桩表；具体类 → 递归构造其构造器参数）
- 按用例里的类型别名解码参数（record 用 Jackson 还原，实体按字段名找 setter）
- 记录返回/异常/落盘状态/每个打桩方法的调用次数

用例文件长这样：

```json
{
  "types":    {"order": "com.x.Order"},
  "fixtures": {"11": {"$entity": "com.x.Product", "fields": {"id": 11}}},
  "stubs":    {"com.x.YRepo": {"findById": {"$byKey": {"11": {"$fixture": "11"}}},
                               "save": {"$echoArg0": true}}},
  "ctor":     {"com.x.ZService": {"0": "<UPLOAD_ROOT>"}},
  "cases":    [{"id": "X-01", "class": "...", "method": "...", "args": [...],
                "check": {"type": "returns_eq", "value": 1}}]
}
```

**验证过等价性**：同一批 jiuyan 用例，原版 harness 与通用 harness 的判定 **0 条不同**（64 通过 / 28 失败）。
