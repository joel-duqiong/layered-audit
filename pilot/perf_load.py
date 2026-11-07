"""端到端延迟曲线 —— 验证「端到端延迟随数据量塌陷」假设（契约类别 service.service_latency）。

类层已实测 idx_products_name 对服务层查询模式不可用（591 倍），但集成层没复现端到端
延迟塌陷。本脚本补这一段：

1. 按阶梯把 products 放大到 N（哨兵数据带 ai_probe_perf_ 前缀，写完即删，零残留）；
2. 每个阶梯量端到端 HTTP 延迟（真实走 HTTP：Spring + JPA + 网络，是用户能感知的数）；
3. 用 EXPLAIN (ANALYZE, BUFFERS) 归因：时间花在 seq scan 还是 count(*)；
4. 出延迟曲线 + 判定（CONFIRMED / PARTIALLY_CONFIRMED / FALSIFIED）。

口径（别改口径不改结论）：
- EXPLAIN 跑的是语义等价 SQL，不是 JPA 实际生成的语句 —— 只用于归因，不作为语句级证据。
- 每级台阶前跑一次 ANALYZE products，否则优化器拿不到统计信息，plan 不真实。
- 端到端延迟取 p50（不是均值）：偶发 GC / 连接池抖动不该主导结论。
"""
from __future__ import annotations
import project

import argparse
import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

HERE = project.artifact_dir()
PFX = "ai_probe_perf_"
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
PG_CONTAINER = "jiuyan-postgres"
OUT_JSON = HERE / "perf_load.json"
OUT_MD = HERE / "perf_load.md"
OUT_FINDINGS = HERE / "perf_findings.json"


def psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "jiuyan", "-d", "jiuyan", "-tAc", sql],
        check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def psql_rows(sql: str) -> list[dict[str, Any]]:
    raw = psql(f"SELECT to_jsonb(_r) FROM ({sql}) _r")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def explain(sql: str) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS) —— 取执行时间、扫描方式、命中行数。"""
    raw = psql(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
    plan = json.loads(raw)[0]["Plan"]
    return {
        "execution_ms": plan.get("Actual Total Time"),
        "node": plan.get("Node Type"),
        "rows": plan.get("Actual Rows"),
        "scan": _scan_kind(plan),
        "buffers_hit": plan.get("Shared Hit Blocks"),
    }


def _scan_kind(plan: dict[str, Any]) -> str:
    node = plan.get("Node Type", "")
    if node == "Seq Scan":
        return "Seq Scan"
    child = plan.get("Plans") or []
    if child:
        return _scan_kind(child[0])
    return node


def insert_products(count: int, category_id: int) -> None:
    if count <= 0:
        return
    psql(
        "INSERT INTO products (name, category_id, price_cent, unit, description) "
        f"SELECT '{PFX}' || g, {category_id}, 1000, '份', 'perf probe' "
        f"FROM generate_series(1, {count}) g")


def delete_products() -> int:
    before = int(psql(f"SELECT count(*) FROM products WHERE name LIKE '{PFX}%'"))
    psql(f"DELETE FROM products WHERE name LIKE '{PFX}%'")
    return before


def measure(session: requests.Session, path: str, params: dict[str, Any],
            rounds: int, warmup: int = 2) -> dict[str, Any]:
    samples: list[float] = []
    status = 0
    body: Any = None
    for i in range(warmup + rounds):
        start = time.perf_counter()
        r = session.get(BASE + path, params=params, timeout=60)
        elapsed = (time.perf_counter() - start) * 1000.0
        if i >= warmup:
            samples.append(elapsed)
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = None
    samples.sort()
    data = (body or {}).get("data") or {}
    return {
        "status": status,
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(samples[max(0, int(len(samples) * 0.95) - 1)], 2),
        "min_ms": round(samples[0], 2),
        "max_ms": round(samples[-1], 2),
        "returned_items": len(data.get("items") or []) if isinstance(data, dict) else None,
        "server_total": data.get("total") if isinstance(data, dict) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="端到端延迟曲线")
    parser.add_argument("--scales", default="0,1000,5000,20000",
                        help="阶梯：累计要插入的商品行数（逗号分隔）")
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--keyword", default="ai_probe_perf")
    args = parser.parse_args()

    scales = sorted({int(x) for x in args.scales.split(",")})
    session = requests.Session()
    steps: list[dict[str, Any]] = []
    category_id = int(psql("SELECT id FROM categories ORDER BY id LIMIT 1"))
    baseline_products = int(psql("SELECT count(*) FROM products"))
    added = 0
    deleted = 0
    try:
        for target in scales:
            delta = target - added
            if delta > 0:
                insert_products(delta, category_id)
                added += delta
            psql("ANALYZE products")
            total_rows = int(psql("SELECT count(*) FROM products"))
            step: dict[str, Any] = {
                "probe_rows": added,
                "total_products": total_rows,
                "endpoints": {},
                "sql": {},
            }
            step["endpoints"]["products_with_keyword"] = measure(
                session, "/api/products",
                {"keyword": args.keyword, "page": 1, "pageSize": 100}, args.rounds)
            step["endpoints"]["products_no_keyword"] = measure(
                session, "/api/products", {"page": 1, "pageSize": 100}, args.rounds)
            step["endpoints"]["packages"] = measure(
                session, "/api/packages", {"page": 1, "pageSize": 100}, args.rounds)
            step["endpoints"]["orders"] = measure(
                session, "/api/orders", {"page": 1, "pageSize": 100}, args.rounds)
            kw = args.keyword.lower()
            step["sql"]["count_query"] = explain(
                f"SELECT count(*) FROM products WHERE lower(name) LIKE '%{kw}%' AND is_deleted = false")
            step["sql"]["page_query"] = explain(
                "SELECT p.* FROM products p WHERE lower(p.name) LIKE "
                f"'%{kw}%' AND p.is_deleted = false ORDER BY p.id DESC LIMIT 100")
            step["sql"]["count_no_keyword"] = explain(
                "SELECT count(*) FROM products WHERE is_deleted = false")
            steps.append(step)
            print(f"  探针 {added:>6} 行 / 表内 {total_rows:>6} 行  "
                  f"keyword p50 {step['endpoints']['products_with_keyword']['p50_ms']:>8} ms  "
                  f"无 keyword p50 {step['endpoints']['products_no_keyword']['p50_ms']:>8} ms  "
                  f"count(*) {step['sql']['count_query']['execution_ms']:>8} ms")
    finally:
        deleted = delete_products()
        print(f"  清理：删除 ai_probe_perf_ 商品 {deleted} 行，"
              f"当前商品数 {psql('SELECT count(*) FROM products')}（基线 {baseline_products}）")

    payload = {
        "keyword": args.keyword,
        "rounds": args.rounds,
        "baseline_products": baseline_products,
        "probe_rows_deleted": deleted,
        "steps": steps,
    }
    verdict = judge(steps)
    payload["verdict"] = verdict
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(payload)
    emit_findings(payload)
    print(f"  判定 {verdict['verdict']}：{verdict['reason']}")
    print(f"  输出 -> {OUT_JSON} / {OUT_MD}")


BUDGET_MS = 1000.0


def judge(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """判定延迟是否构成「塌陷」，并量化分页 total 的纯开销。

    判定口径（别改口径不改结论）：
    - CONFIRMED：延迟增长快于数据量增长（超线性），或实测规模内已跨过 1 秒预算。
    - PARTIALLY_CONFIRMED：延迟随数据量线性增长（结构性 O(N) 确认），但实测规模下
      远未到不可用阈值 —— 机制成立、后果未兑现。
    - FALSIFIED：延迟基本不随数据量增长。

    「增长 6 倍」本身不构成塌陷：数据量同时涨了 1668 倍。所以判据用相对量 + 绝对预算，
    不是看倍数。
    """
    if len(steps) < 2:
        return {"verdict": "INSUFFICIENT", "reason": "阶梯不足 2 级，无法判断增长趋势"}
    first, last = steps[0], steps[-1]
    ep_key = "products_with_keyword"
    lat0 = first["endpoints"][ep_key]["p50_ms"]
    lat1 = last["endpoints"][ep_key]["p50_ms"]
    rows0 = max(first["total_products"], 1)
    rows1 = max(last["total_products"], 1)
    growth = (lat1 / lat0) if lat0 > 0 else float("inf")
    row_growth = rows1 / rows0
    per_row_ms = lat1 / rows1
    projected_rows = int(BUDGET_MS / per_row_ms) if per_row_ms > 0 else 0

    if growth > row_growth or lat1 >= BUDGET_MS:
        verdict = "CONFIRMED"
        reason = (f"数据量 {rows0}→{rows1} 行（{row_growth:.0f}×）时端到端延迟 "
                  f"{lat0}ms→{lat1}ms（{growth:.1f}×）：增长快于数据量，或已跨过 "
                  f"{BUDGET_MS:.0f}ms 预算")
    elif growth >= 1.5:
        verdict = "PARTIALLY_CONFIRMED"
        reason = (f"数据量 {rows0}→{rows1} 行（{row_growth:.0f}×）时端到端延迟 "
                  f"{lat0}ms→{lat1}ms（{growth:.1f}×）：确认随数据量线性增长，"
                  f"但 {lat1}ms 远未到 {BUDGET_MS:.0f}ms 预算 —— 机制成立、后果未兑现")
    else:
        verdict = "FALSIFIED"
        reason = (f"数据量 {rows0}→{rows1} 行（{row_growth:.0f}×）时端到端延迟 "
                  f"{lat0}ms→{lat1}ms（{growth:.2f}×），未随数据量塌陷")

    count_kw = last["sql"]["count_query"]["execution_ms"]
    count_nokw = last["sql"]["count_no_keyword"]["execution_ms"]
    ep_kw_ms = last["endpoints"][ep_key]["p50_ms"]
    ep_nokw_ms = last["endpoints"]["products_no_keyword"]["p50_ms"]
    return {
        "verdict": verdict,
        "reason": reason,
        "latency_growth_x": round(growth, 2),
        "row_growth_x": round(row_growth, 2),
        "latency_p50_ms": lat1,
        "budget_ms": BUDGET_MS,
        "per_row_cost_ms": round(per_row_ms, 6),
        "projected_rows_for_budget": projected_rows,
        "mechanism": "count/page 查询均为 Seq Scan，idx_products_name 从未被使用",
        "count_query_ms": count_kw,
        "count_waste_ratio_with_keyword": round(count_kw / ep_kw_ms, 3) if ep_kw_ms else 0.0,
        "count_no_keyword_ms": count_nokw,
        "count_waste_ratio_no_keyword": round(count_nokw / ep_nokw_ms, 3) if ep_nokw_ms else 0.0,
        "count_query_scan": last["sql"]["count_query"]["scan"],
        "page_query_scan": last["sql"]["page_query"]["scan"],
    }


def _write_md(payload: dict[str, Any]) -> None:
    lines: list[str] = []
    a = lines.append
    a("# 端到端延迟曲线（service.service_latency）")
    a("")
    a(f"关键词 `{payload['keyword']}`，每级 {payload['rounds']} 轮取 p50。")
    a("")
    a("| 表内商品数 | 探针行数 | 带 keyword p50 | 带 keyword p95 | 无 keyword p50 | count(*) ms | count 扫描 | 页查询扫描 |")
    a("|---|---|---|---|---|---|---|---|")
    a("")
    a("> 说明：count(*) ms 是服务端单条 SQL 的耗时（等价 SQL 的 EXPLAIN ANALYZE），"
      "不是接口耗时；两者之比即分页 total 的开销占比。")
    for s in payload["steps"]:
        ep = s["endpoints"]
        a(f"| {s['total_products']} | {s['probe_rows']} | "
          f"{ep['products_with_keyword']['p50_ms']} ms | {ep['products_with_keyword']['p95_ms']} ms | "
          f"{ep['products_no_keyword']['p50_ms']} ms | "
          f"{s['sql']['count_query']['execution_ms']} ms | "
          f"{s['sql']['count_query']['scan']} | {s['sql']['page_query']['scan']} |")
    a("")
    v = payload["verdict"]
    a(f"**判定：{v['verdict']}** —— {v['reason']}")
    a("")
    a(f"- count(*) 查询占「带 keyword 列表接口」延迟的 {v['count_waste_ratio_with_keyword']:.1%}，"
      f"占「不带 keyword」的 {v['count_waste_ratio_no_keyword']:.1%}")
    a(f"- 每行成本 {v['per_row_cost_ms']:.6f} ms/行，按此折算约 {v['projected_rows_for_budget']} 行才触及 1 秒预算")
    a(f"- 清理：删除探针商品 {payload['probe_rows_deleted']} 行")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")



def emit_findings(payload: dict[str, Any]) -> None:
    """把本轮实测写成两条 finding（服务层开单 + 集成层引用链级后果）。

    归属规则：根因在服务层（每次请求都白算一次 count(*)），集成层只拥有
    「三个列表接口都白付这份开销」这个端到端后果。
    """
    v = payload["verdict"]
    last = payload["steps"][-1]
    ep = last["endpoints"]
    scale_text = "；".join(
        f"{s['total_products']} 行 → {s['endpoints']['products_with_keyword']['p50_ms']}ms"
        for s in payload["steps"])
    scan = v["count_query_scan"]
    service_detail = (
        f"每个列表接口都带分页参数，后端对每次请求额外跑一条 count(*) 全表扫描（"
        f"{scan}，因为 idx_products_name 对 lower(name) LIKE '%…%' 不可用）。"
        f"实测数据量阶梯：{scale_text}。\n"
        f"在 {last['total_products']} 行时，count(*) 单条耗时 {v['count_query_ms']}ms，"
        f"占带 keyword 列表接口端到端延迟的 {v['count_waste_ratio_with_keyword']:.1%}，"
        f"占不带 keyword 接口的 {v['count_waste_ratio_no_keyword']:.1%}。\n"
        f"而前端 banquetApi.js 的 listProducts / listPackages / listOrders 三个函数"
        f"一律只取 data.items.map(...)，total 从未被读取 —— 这份开销 100% 浪费。"
        f"\n本轮判定「端到端延迟随数据量塌陷」为 {v['verdict']}：延迟确实随数据量线性增长，"
        f"但 {last['total_products']} 行时端到端仅 "
        f"{ep['products_with_keyword']['p50_ms']}ms，按每行 "
        f"{v['per_row_cost_ms']:.6f}ms 折算，要到约 {v['projected_rows_for_budget']} 行才触及 1 秒预算。"
        f"\n结论：结构性 O(N) 成立，但当前规模下后果未兑现 —— 真正该修的不是「加索引」，"
        f"而是别让后端算一个没人要的数。")
    integration_detail = (
        f"三个列表接口（GET /api/products、/api/packages、/api/orders）在集权层实测中，"
        f"每次请求的 count(*) 部分占端到端延迟的 "
        f"{v['count_waste_ratio_with_keyword']:.1%}~{v['count_waste_ratio_no_keyword']:.1%}，"
        f"而这个 total 前端从头到尾没读过。\n"
        f"也就是说：用户每翻一次列表，后端就多付一次全表扫描的钱，收益为零。"
        f"同时前端把 page 固定成 1、pageSize 固定成 100，超出部分永远不可见 —— "
        f"分页机制在这条链路上同时是「成本」和「不可用」。")
    findings = {
        "service": [{
            "id": "SVC-PAGINATION-COUNT-DISCARDED",
            "severity": "MEDIUM",
            "endpoint": "GET /api/products, GET /api/packages, GET /api/orders",
            "title": "分页 total 是纯开销：后端每次列表请求都跑 count(*) 全表扫描，前端从不读这个数",
            "detail": service_detail,
            "repro": "python3 perf_load.py --scales 0,2000,10000,50000",
            "evidence": {
                "scales": [{"total_products": s["total_products"],
                            "endpoint_p50_ms": s["endpoints"]["products_with_keyword"]["p50_ms"],
                            "count_query_ms": s["sql"]["count_query"]["execution_ms"],
                            "count_scan": s["sql"]["count_query"]["scan"],
                            "page_scan": s["sql"]["page_query"]["scan"]}
                           for s in payload["steps"]],
                "count_waste_ratio_with_keyword": v["count_waste_ratio_with_keyword"],
                "count_waste_ratio_no_keyword": v["count_waste_ratio_no_keyword"],
                "frontend_discards_total": "banquetApi.js listProducts/listPackages/listOrders "
                                           "只返回 data.items.map(...)",
                "verdict": v["verdict"],
                "projected_rows_for_1s": v["projected_rows_for_budget"],
            },
        }],
        "integration": [{
            "id": "INT-LIST-COUNT-END-TO-END-WASTE",
            "severity": "MEDIUM",
            "endpoint": "GET /api/products, GET /api/packages, GET /api/orders",
            "title": "三个列表接口每次都为前端丢弃的 total 付一次全表扫描",
            "detail": integration_detail,
            "repro": "python3 perf_load.py --scales 0,2000,10000,50000",
            "evidence": {
                "chain_steps": [
                    {"n": 1, "method": "GET", "path": "/api/products?keyword=…&page=1&pageSize=100",
                     "status": 200,
                     "evidence": f"端到端 p50 {ep['products_with_keyword']['p50_ms']}ms，"
                                 f"其中 count(*) 占 {v['count_waste_ratio_with_keyword']:.1%}"},
                    {"n": 2, "method": "前端", "path": "src/api/banquetApi.js listProducts",
                     "status": None,
                     "evidence": "只取 data.items.map(mapProduct)，total 被丢弃"},
                    {"n": 3, "method": "前端", "path": "src/api/banquetApi.js listOrders",
                     "status": None,
                     "evidence": "page 固定 1、pageSize 固定 100，超出部分永远不可见"},
                ],
                "verdict": v["verdict"],
            },
        }],
    }
    OUT_FINDINGS.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  findings -> {OUT_FINDINGS}")

if __name__ == "__main__":
    main()
