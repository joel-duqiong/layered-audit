# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 10 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 6.83 ms | 7.61 ms | 7.18 ms | 0.024 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 10.39 ms | 11.19 ms | 8.49 ms | 0.607 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 20.72 ms | 21.84 ms | 9.04 ms | 2.713 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.08 ms | 36.92 ms | 13.64 ms | 13.143 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 6.83ms→36.08ms（5.3×）：确认随数据量线性增长，但 36.08ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 36.4%，占「不带 keyword」的 34.7%
- 每行成本 0.000721 ms/行，按此折算约 1386640 行才触及 1 秒预算
- 清理：删除探针商品 50000 行