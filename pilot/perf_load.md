# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.77 ms | 8.67 ms | 8.22 ms | 0.022 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 11.27 ms | 11.84 ms | 7.92 ms | 0.5 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 18.51 ms | 20.06 ms | 8.3 ms | 2.23 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.6 ms | 37.87 ms | 12.08 ms | 10.623 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.77ms→36.6ms（4.7×）：确认随数据量线性增长，但 36.6ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 29.0%，占「不带 keyword」的 41.6%
- 每行成本 0.000732 ms/行，按此折算约 1366939 行才触及 1 秒预算
- 清理：删除探针商品 50000 行