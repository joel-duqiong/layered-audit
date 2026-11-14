# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.11 ms | 8.01 ms | 6.84 ms | 0.018 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 10.13 ms | 11.73 ms | 8.62 ms | 0.43 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 16.82 ms | 17.81 ms | 8.73 ms | 2.08 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 37.59 ms | 38.47 ms | 11.37 ms | 17.934 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.11ms→37.59ms（5.3×）：确认随数据量线性增长，但 37.59ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 47.7%，占「不带 keyword」的 45.7%
- 每行成本 0.000751 ms/行，按此折算约 1330939 行才触及 1 秒预算
- 清理：删除探针商品 50000 行