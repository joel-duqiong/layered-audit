# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 6.51 ms | 7.02 ms | 8.94 ms | 0.023 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 8.24 ms | 9.14 ms | 7.48 ms | 0.594 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 16.99 ms | 17.82 ms | 8.7 ms | 3.63 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.07 ms | 37.23 ms | 11.1 ms | 15.114 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 6.51ms→36.07ms（5.5×）：确认随数据量线性增长，但 36.07ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 41.9%，占「不带 keyword」的 46.2%
- 每行成本 0.000721 ms/行，按此折算约 1387025 行才触及 1 秒预算
- 清理：删除探针商品 50000 行