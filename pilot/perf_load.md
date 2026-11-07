# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 10 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 5.72 ms | 6.22 ms | 6.31 ms | 0.018 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 9.18 ms | 10.12 ms | 8.55 ms | 0.526 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 17.12 ms | 18.22 ms | 7.11 ms | 2.951 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 35.74 ms | 36.95 ms | 11.52 ms | 14.484 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 5.72ms→35.74ms（6.2×）：确认随数据量线性增长，但 35.74ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 40.5%，占「不带 keyword」的 45.5%
- 每行成本 0.000714 ms/行，按此折算约 1399832 行才触及 1 秒预算
- 清理：删除探针商品 50000 行