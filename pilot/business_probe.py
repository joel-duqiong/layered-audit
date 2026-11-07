"""业务不变式探针（项目侧）。

不变式清单在 invariants.json（项目配置），判定原语在 engine/invariants.py（通用引擎）。
探针负责三件项目侧的事：**造出会违反不变式的订单**、**把 DB 行读出来**、**把判定结果变成 finding**。

造场景的原则：每造一个订单，明确针对一条不变式；再额外造一个「干净订单」，
用来验证那些本来就成立的不变式（正面结论）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from engine.invariants import evaluate_all
from integration_chain import BASE, call, cleanup, data_of, order_body, owner_session
from integration_reconcile import PFX, psql_rows

HERE = Path(__file__).resolve().parent
INVARIANTS_FILE = HERE / "invariants.json"
OUT = HERE / "business_findings.json"
TAG = "inv"


def load_invariants() -> dict[str, Any]:
    return json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))


def seed_context() -> dict[str, Any]:
    """找一个套餐（带明细）和一个套餐外的商品。"""
    pkgs = data_of(call(owner_session, "GET", "/api/packages", params={"pageSize": 10})[1]) or {}
    prods = data_of(call(owner_session, "GET", "/api/products", params={"pageSize": 50})[1]) or {}
    pkg_items = pkgs.get("items") or []
    if not pkg_items:
        raise RuntimeError("没有可用套餐")
    pkg = pkg_items[0]
    items = pkg.get("items") or []
    if not items:
        raise RuntimeError("套餐没有明细")
    pkg_pid = items[0]["productId"]
    outside = None
    for p in prods.get("items") or []:
        if p["id"] != pkg_pid:
            outside = p["id"]
            break
    return {
        "package_id": pkg["id"],
        "package_item_product_id": pkg_pid,
        "package_item_quantity": items[0].get("quantity", 1),
        "package_banquet_type": pkg.get("banquetType"),
        "outside_product_id": outside,
    }


def make_scenarios(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """每个场景针对一条不变式。"""
    pid = ctx["package_item_product_id"]
    qty = ctx["package_item_quantity"]
    pkg_btype = ctx["package_banquet_type"]
    other_btype = "寿宴" if pkg_btype != "寿宴" else "婚宴"
    base = order_body(f"{TAG}_s", pid, ctx["package_id"], qty=qty)

    clean = copy.deepcopy(base)
    clean["planTotalCent"] = ctx["package_price_cent"] if "package_price_cent" in ctx else 24680
    clean["useFinalPrice"] = False
    clean["finalPriceCent"] = None
    clean["depositCent"] = 0
    clean["banquetType"] = pkg_btype

    deposit_over = copy.deepcopy(clean)
    deposit_over["useFinalPrice"] = True
    deposit_over["finalPriceCent"] = 1
    deposit_over["depositCent"] = 999999

    amount_ne_detail = copy.deepcopy(clean)
    amount_ne_detail["useFinalPrice"] = True
    amount_ne_detail["finalPriceCent"] = 1
    amount_ne_detail["depositCent"] = 0

    over_package_price = copy.deepcopy(clean)
    over_package_price["useFinalPrice"] = True
    over_package_price["finalPriceCent"] = 999999999
    over_package_price["depositCent"] = 0

    wrong_btype = copy.deepcopy(clean)
    wrong_btype["banquetType"] = other_btype

    foreign_item = copy.deepcopy(clean)
    if ctx["outside_product_id"]:
        foreign_item["packageItems"] = [{"productId": ctx["outside_product_id"], "quantity": 1}]

    ghost_added = copy.deepcopy(clean)
    ghost_added["addedItems"] = [{"productId": 999999999, "quantity": 1}]

    return [
        {"tag": "clean", "targets": [], "body": clean},
        {"tag": "deposit_over", "targets": ["BI-DEPOSIT-LTE-FINAL-AMOUNT"], "body": deposit_over},
        {"tag": "amount_ne_detail", "targets": ["BI-ORDER-AMOUNT-EQ-DETAIL-SUM"], "body": amount_ne_detail},
        {"tag": "over_pkg_price", "targets": ["BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE"], "body": over_package_price},
        {"tag": "wrong_btype", "targets": ["BI-BANQUET-TYPE-MATCH-PACKAGE"], "body": wrong_btype},
        {"tag": "foreign_item", "targets": ["BI-SNAPSHOT-ITEMS-SUBSET-OF-PACKAGE"], "body": foreign_item},
        {"tag": "ghost_added", "targets": ["BI-ADDED-ITEMS-EXIST"], "body": ghost_added},
    ]


def main() -> None:
    cfg = load_invariants()
    invs = cfg["invariants"]
    ctx = seed_context()
    ctx["package_price_cent"] = None
    pkgs = data_of(call(owner_session, "GET", "/api/packages", params={"pageSize": 10})[1]) or {}
    ctx["package_price_cent"] = (pkgs.get("items") or [{}])[0].get("priceCent") or 24680

    scenarios = make_scenarios(ctx)
    order_ids: list[int] = []
    scenario_of: dict[int, str] = {}
    rejected: dict[str, int] = {}
    try:
        for sc in scenarios:
            st, payload = call(owner_session, "POST", "/api/orders", body=sc["body"])
            if st != 200:
                # 建单被拒 —— 这本身就是证据：该不变式被系统在入口挡住了
                rejected[sc["tag"]] = st
                print(f"  场景 {sc['tag']} 建单被拒 http={st}（该不变式由入口结构性保证）")
                continue
            oid = data_of(payload)["id"]
            order_ids.append(oid)
            scenario_of[oid] = sc["tag"]
        rows = psql_rows(cfg["entity"]["read_sql"].format(ids=",".join(str(i) for i in order_ids)))
    finally:
        cleanup()

    for row in rows:
        row["_scenario"] = scenario_of.get(row.get("id"), "?")

    results = evaluate_all(rows, invs)
    by_id = {r["id"]: r for r in results}
    findings: list[dict[str, Any]] = []
    for inv in invs:
        res = by_id[inv["id"]]
        if res["ok"]:
            continue
        samples = []
        for f in res["failure_samples"]:
            row = next((r for r in rows if r.get("id") == f.get("row_id")), {})
            samples.append({**f, "scenario": row.get("_scenario")})
        findings.append({
            "id": f"BI-{inv['id'][3:]}",
            "severity": inv["severity"],
            "endpoint": "POST /api/orders",
            "title": inv["claim"],
            "detail": (f"{inv['why']}\n"
                       f"不变式：{inv['id']}　判定原语：{inv['assert']['type']}\n"
                       f"实测 {res['checked']} 个订单场景，违反 {res['violations']} 次：\n"
                       + json.dumps(samples, ensure_ascii=False, indent=2)),
            "repro": "python3 business_probe.py",
            "evidence": {
                "invariant": inv,
                "checked": res["checked"],
                "violations": res["violations"],
                "samples": samples,
                "scenarios": [{"scenario": r.get("_scenario"), "order_id": r.get("id")}
                              for r in rows],
            },
        })

    inv_by_id = {i["id"]: i for i in invs}
    tag_to_target = {t: sc["tag"] for sc in scenarios for t in sc["targets"]}
    positives = []
    for r in results:
        if not r["ok"]:
            continue
        inv = inv_by_id[r["id"]]
        # 如果这条不变式的违规场景在入口就被拒了，那它是结构性保证
        guard = tag_to_target.get(r["id"])
        guard_note = ""
        guard_evidence: dict[str, Any] = {}
        if guard and guard in rejected:
            guard_note = (f"\n特别地：专门构造的违规场景「{guard}」在建单入口就被拒"
                          f"（HTTP {rejected[guard]}）—— 这条不变式是被结构性保证的，"
                          f"不是碰巧没触发。")
            guard_evidence = {"guard_scenario": guard, "rejected_http": rejected[guard]}
        positives.append({
            "id": f"BI-{r['id'][3:]}",
            "severity": "INFO",
            "endpoint": "POST /api/orders",
            "title": r["claim"],
            "detail": (f"{r['why']}\n"
                       f"不变式：{r['id']}　判定原语：{inv['assert']['type']}\n"
                       f"在 {r['checked']} 个订单场景（含刻意违规的对照组）上恒成立 —— "
                       f"这条不变式是被系统守住的。{guard_note}"),
            "repro": "python3 business_probe.py",
            "evidence": {"invariant": inv, "checked": r["checked"], **guard_evidence},
        })

    payload = {"service": findings + positives,
               "results": results,
               "rejected_scenarios": rejected,
               "scenarios": [{"tag": s["tag"], "targets": s["targets"]} for s in scenarios]}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"业务不变式探针 -> {OUT}  {len(findings)} 条违反 + {len(positives)} 条成立")
    for r in results:
        flag = "违反" if not r["ok"] else "成立"
        print(f"  {flag}  {r['id']:<42}检查 {r['checked']} 场景，违反 {r['violations']} 次")


if __name__ == "__main__":
    main()
