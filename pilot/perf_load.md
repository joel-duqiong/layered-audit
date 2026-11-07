# 端到端延迟曲线（service.service_latency）

关键词 `ai_probe_perf`，每级 12 轮取 p50。

| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |
|---|---|---|---|---|---|---|---|

> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），不是接口耗时；两者之比即分页 total 的开销占比。
| 30 | 0 | 7.62 ms | 9.32 ms | 6.55 ms | 0.037 ms | Seq Scan | Seq Scan |
| 2030 | 2000 | 9.05 ms | 9.89 ms | 5.74 ms | 0.618 ms | Seq Scan | Seq Scan |
| 10030 | 10000 | 19.04 ms | 20.34 ms | 7.58 ms | 3.208 ms | Seq Scan | Seq Scan |
| 50030 | 50000 | 36.91 ms | 38.55 ms | 11.15 ms | 10.502 ms | Seq Scan | Seq Scan |

**判定：PARTIALLY_CONFIRMED** —— 数据量 30→50030 行（1668×）时端到端延迟 7.62ms→36.91ms（4.8×）：确认随数据量线性增长，但 36.91ms 远未到 1000ms 预算 —— 机制成立、后果未兑现

- count(*) 查询占「带 keyword 列表接口」延迟的 28.5%，占「不带 keyword」的 42.4%
- 每行成本 0.000738 ms/行，按此折算约 1355459 行才触及 1 秒预算
- 清理：删除探针商品 50000 行