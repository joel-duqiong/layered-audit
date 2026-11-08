# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 6.57 ms | 7.3 ms | 5.23 ms | 0.011 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 9.39 ms | 10.22 ms | 6.65 ms | 0.427 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 19.85 ms | 23.51 ms | 8.28 ms | 2.916 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.01 ms | 37.83 ms | 11.01 ms | 13.279 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 6.57ms→36.01ms（5.5×）：确认随数据量线性增长，但 36.01ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 36.9%，占「不带 keyword」的 42.7%
- 每行成本 0.000720 ms/行，按此折算约 1389336 行才触及 1 秒预算
- 清理：删除探针商品 50000 行