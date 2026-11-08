# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.15 ms | 7.83 ms | 8.26 ms | 0.038 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 10.51 ms | 12.33 ms | 8.27 ms | 0.475 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 19.6 ms | 21.12 ms | 10.57 ms | 3.205 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 38.23 ms | 41.29 ms | 10.89 ms | 14.807 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.15ms→38.23ms（5.3×）：确认随数据量线性增长，但 38.23ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 38.7%，占「不带 keyword」的 47.7%
- 每行成本 0.000764 ms/行，按此折算约 1308658 行才触及 1 秒预算
- 清理：删除探针商品 50000 行