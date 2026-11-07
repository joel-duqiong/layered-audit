# 分层审计 · 根因与覆盖度报告

生成物：/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/root_causes.json　输入：/Users/june/.cc-weixin/workspace/layered-audit/demos/shop/all_findings.json

- 发现 1 条 → 根因 1 条（归并 0 条）
- 硬违规 0 条；契约缺口（无法归类）0 条

## 一、修复顺序（按根因）

| # | 严重度 | 归属层 | 根因 | 标题 | 发现数 | 置信 | ground_truth | 可跑复现 |
|---|--------|--------|------|------|--------|------|---------------|----------|
| 1 | HIGH | integration | `order.quota_ledger_amount_divergence` | 额度占用额（报价）与账本记录额（实付）在会员链路上口径分裂 | 1 | 0.95 | verified | `python3 demos/shop/emergence_probe.py --chain EMERG-QUOTA…` |

## 二、硬违规（违反 attribution.rule / out_of_scope）

无。

## 三、归并提示（同层同根因多开）

| 根因 | 归属层 | 说明 |
|------|--------|------|

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
| error_handling_pattern | 0 | 零覆盖 —— 没人管 |
| cohesion | 0 | 零覆盖 —— 没人管 |

**class**

| category | 命中 | 状态 |
|----------|------|------|
| encapsulation_leak | 0 | 零覆盖 —— 没人管 |
| invariant | 0 | 零覆盖 —— 没人管 |
| contract_violation | 0 | 零覆盖 —— 没人管 |
| inheritance_design | 0 | 零覆盖 —— 没人管 |

**service**

| category | 命中 | 状态 |
|----------|------|------|
| interface_contract | 0 | 零覆盖 —— 没人管 |
| resilience | 0 | 零覆盖 —— 没人管 |
| authz | 0 | 零覆盖 —— 没人管 |
| data_consistency | 0 | 零覆盖 —— 没人管 |
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

合计 0 条，按层：{}

| uid | 建议类别 |
|-----|----------|

## 七、同一文件承载多个根因（文件层内聚信号）

| 文件 | 根因数 | 根因 |
|------|--------|------|
