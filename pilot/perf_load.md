# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.38 ms | 8.28 ms | 7.27 ms | 0.025 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 10.32 ms | 10.73 ms | 9.27 ms | 0.475 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 16.93 ms | 17.88 ms | 9.19 ms | 2.325 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.78 ms | 38.84 ms | 11.48 ms | 12.316 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.38ms→36.78ms（5.0×）：确认随数据量线性增长，但 36.78ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 33.5%，占「不带 keyword」的 46.0%
- 每行成本 0.000735 ms/行，按此折算约 1360250 行才触及 1 秒预算
- 清理：删除探针商品 50000 行