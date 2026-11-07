# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.5 ms | 8.29 ms | 10.14 ms | 0.031 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 11.02 ms | 11.57 ms | 9.79 ms | 0.647 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 17.56 ms | 20.59 ms | 8.03 ms | 3.214 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.17 ms | 37.15 ms | 11.51 ms | 15.058 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.5ms→36.17ms（4.8×）：确认随数据量线性增长，但 36.17ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 41.6%，占「不带 keyword」的 47.8%
- 每行成本 0.000723 ms/行，按此折算约 1383190 行才触及 1 秒预算
- 清理：删除探针商品 50000 行