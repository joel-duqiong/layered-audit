# 分层审计 · 根因与覆盖度报告

生成物：/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/root_causes.json　输入：/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/all_findings.json

- 发现 23 条 → 根因 17 条（归并 6 条）
- 硬违规 0 条；契约缺口（无法归类）2 条

## 一、修复顺序（按根因）

| # | 严重度 | 归属层 | 根因 | 标题 | 发现数 | 置信 | ground_truth | 可跑复现 |
|---|--------|--------|------|------|--------|------|---------------|----------|
| 1 | HIGH | class | `pricing.member_tier_bound_to_deployment_profile` | 同一请求在两个部署实例上结果不同：settledAmountCent 分叉 | 4 | 0.94 | verified | `python3 demos/shop/business_probe.py` |
| 2 | HIGH | file | `cross_repo.fx_fallback_returns_wrong_scale` | FxClient 汇率取不到时兜底成 1（=1%），把结算金额砍到 1/100 | 1 | 0.9 | verified | `—` |
| 3 | HIGH | class | `error.business_error_as_500` | 业务规则失败被当成服务器错误抛出去：customerName 不存在（服务层抛异常） -> HTTP 500 | 1 | 0.9 | verified | `—` |
| 4 | HIGH | class | `inheritance.touch_postcondition_changed` | com.demo.shop.entity.ShopOrder 覆写 com.demo.shop.entity.AuditableEntity.touch 后置条件变了：updatedBy 不再相同 | 1 | 0.95 | verified | `—` |
| 5 | HIGH | file | `order.quota_checked_on_quoted_amount_ledger_on_settled` | OrderService 同一文件里两处对同一个订单金额用了不同口径：额度按报价卡、账按结算记 | 1 | 0.93 | verified | `—` |
| 6 | HIGH | integration | `order.quota_ledger_amount_divergence` | 额度占用额（报价）与账本记录额（实付）在会员链路上口径分裂 | 1 | 0.95 | verified | `python3 demos/shop/emergence_probe.py --chain EMERG-QUOTA…` |
| 7 | HIGH | class | `rule.missing_in_db_dto_service` | 3 条业务不变量在 DB / DTO / 服务层三层全都不存在 | 1 | 0.85 | verified | `—` |
| 8 | HIGH | class | `service.product_service_returns_internal_entities` | com.demo.shop.service.ProductService.snapshot 把内部实体集合直接交出去 | 1 | 0.92 | verified | `—` |
| 9 | HIGH | class | `unannotated.class.JPA_COLUMN_MISSING_IN_DDL` | ShopOrder.currency 映射的列 currency 在 DDL 里不存在 | 1 | 0.4 | verified | `—` |
| 10 | MEDIUM | class | `entity.no_value_equality` | ShopOrder 没有值相等：放进集合或当键，语义会变 | 4 | 0.8 | verified | `—` |
| 11 | MEDIUM | class | `cache.holds_mutable_container` | com.demo.shop.service.ProductService.listCached 缓存里放的是可变容器 | 1 | 0.88 | verified | `—` |
| 12 | MEDIUM | class | `dto.primitive_hides_missing_required_field` | shop_order.quoted_amount_cent 声明 NOT NULL，DTO 用原始类型 long —— 缺省静默变成 0 | 1 | 0.9 | verified | `—` |
| 13 | MEDIUM | class | `rule.check_without_dto_counterpart` | 3 条 DB CHECK 在 DTO 层找不到同义约束 | 1 | 0.75 | verified | `—` |
| 14 | MEDIUM | class | `shop_order.no_data_lifecycle` | shop_order 表没有软删/归档，永久增长（当前 56 kB） | 1 | 0.8 | verified | `—` |
| 15 | MEDIUM | class | `unannotated.class.CLASS-NULLABILITY-THREE-WAY-MISMATCH` | shop_order.currency 三方可空性声明不一致 | 1 | 0.4 | verified | `—` |
| 16 | INFO | class | `check.dto_aligned` | 1/4 条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合 | 1 | 0.95 | verified | `—` |
| 17 | INFO | class | `nullability.three_way_aligned` | 可空性三方一致 1/3 列（同向设防 1 · DTO 更严 0 · 都可空 0） | 1 | 0.95 | verified | `—` |

## 二、硬违规（违反 attribution.rule / out_of_scope）

无。

## 三、归并提示（同层同根因多开）

| 根因 | 归属层 | 说明 |
|------|--------|------|
| `entity.no_value_equality` | class | class 层同一根因开了 4 条 own，可归并为 1 条根因 |

## 四、跨根因关联

| 根因 | 关联到 |
|------|--------|
| `order.quota_ledger_amount_divergence` | `order.quota_checked_on_quoted_amount_ledger_on_settled` |
| `order.quota_ledger_amount_divergence` | `pricing.member_tier_bound_to_deployment_profile` |

## 五、覆盖度矩阵（层 × 契约声明的 category）

**function**

| category | 命中 | 状态 |
|----------|------|------|
| boundary | 0 | 零覆盖 —— 没人管 |
| complexity | 0 | 零覆盖 —— 没人管 |
| exception_path | 0 | 零覆盖 —— 没人管 |
| numeric | 0 | 零覆盖 —— 没人管 |

**file**

| category | 命中 | 状态 |
|----------|------|------|
| state_consistency | 0 | 零覆盖 —— 没人管 |
| error_handling_pattern | 1 | ok |
| cohesion | 0 | 零覆盖 —— 没人管 |
| contract_alignment | 1 | ok |

**class**

| category | 命中 | 状态 |
|----------|------|------|
| encapsulation_leak | 2 | ok |
| invariant | 9 | ok |
| contract_violation | 3 | ok |
| inheritance_design | 1 | ok |

**service**

| category | 命中 | 状态 |
|----------|------|------|
| interface_contract | 2 | ok |
| resilience | 0 | 零覆盖 —— 没人管 |
| authz | 0 | 零覆盖 —— 没人管 |
| data_consistency | 1 | ok |
| resource_leak | 0 | 零覆盖 —— 没人管 |
| service_latency | 0 | 零覆盖 —— 没人管 |

**integration**

| category | 命中 | 状态 |
|----------|------|------|
| end_to_end_business | 1 | ok |
| cross_service_performance | 0 | 零覆盖 —— 没人管 |
| cross_service_attack_chain | 0 | 零覆盖 —— 没人管 |
| cross_layer_correlation | 0 | 零覆盖 —— 没人管 |
| emergent_masking | 0 | 零覆盖 —— 没人管 |

## 六、契约缺口（category 装不下）

合计 2 条，按层：{'class': 2}

| uid | 建议类别 |
|-----|----------|
| `class:JPA_COLUMN_MISSING_IN_DDL#0` | 见 all_findings.json 的 suggested_category |
| `class:CLASS-NULLABILITY-THREE-WAY-MISMATCH#13` | 见 all_findings.json 的 suggested_category |

## 七、同一文件承载多个根因（文件层内聚信号）

| 文件 | 根因数 | 根因 |
|------|--------|------|
| `/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/backend/src/main/java/com/demo/shop/service/OrderService.java` | 3 | `error.business_error_as_500`, `order.quota_checked_on_quoted_amount_ledger_on_settled`, `order.quota_ledger_amount_divergence` |
| `/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/backend/src/main/java/com/demo/shop/entity/ShopOrder.java` | 3 | `entity.no_value_equality`, `inheritance.touch_postcondition_changed`, `shop_order.no_data_lifecycle` |
| `/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/backend/src/main/java/com/demo/shop/service/ProductService.java` | 2 | `cache.holds_mutable_container`, `service.product_service_returns_internal_entities` |
