# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 1.02 ms | 1.44 ms | 0.97 ms | 0.017 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 3.97 ms | 4.49 ms | 1.54 ms | 0.43 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 11.02 ms | 12.22 ms | 2.74 ms | 2.023 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 29.97 ms | 30.73 ms | 5.01 ms | 10.824 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 1.02ms→29.97ms（29.4×）：确认随数据量线性增长，但 29.97ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 36.1%，占「不带 keyword」的 97.3%
- 每行成本 0.000599 ms/行，按此折算约 1669336 行才触及 1 秒预算
- 清理：删除探针商品 50000 行