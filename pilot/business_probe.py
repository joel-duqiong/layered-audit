"""业务不变式探针（项目侧）。

不变式清单在 invariants.json（项目配置），判定原语在 engine/invariants.py（通用引擎）。
探针负责三件项目侧的事：**造出会违反不变式的订单**、**把 DB 行读出来**、**把判定结果变成 finding**。

造场景的原则：每造一个订单，明确针对一条不变式；再额外造一个「干净订单」，
用来验证那些本来就成立的不变式（正面结论）。
"""
from __future__ import annotations
import project

import copy
import json
from pathlib import Path
from typing import Any

from engine import business_verdict
from integration_chain import BASE, call, cleanup, data_of, order_body, owner_session
from integration_reconcile import PFX, psql_rows

HERE = project.artifact_dir()
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

    def post_scenario(sc: dict[str, Any]) -> tuple[int, Any]:
        """造一笔业务单据 —— 项目侧的事（造什么、造在哪）。"""
        return call(owner_session, "POST", "/api/orders", body=sc["body"])

    def read_rows(ids: list[int]) -> list[dict[str, Any]]:
        """把造出来的行读出来 —— 项目侧的事（读哪张表、读什么形状）。"""
        return psql_rows(cfg["entity"]["read_sql"].format(ids=",".join(str(i) for i in ids)))

    # 判定与开单是通用逻辑，在 engine/business_verdict.py 里
    try:
        payload = business_verdict.run(
            invariants=invs,
            scenarios=scenarios,
            post_scenario=post_scenario,
            read_rows=read_rows,
            endpoint="POST /api/orders",
            repro="python3 business_probe.py",
        )
    finally:
        cleanup()

    for tag, status in sorted(payload["rejected_scenarios"].items()):
        print(f"  场景 {tag} 建单被拒 http={status}（该不变式由入口结构性保证）")

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    violations = len([f for f in payload["service"] if f["severity"] != "INFO"])
    positives = len(payload["service"]) - violations
    suspects = len(payload.get("unconfirmed_suspects") or [])
    refuted = len(payload.get("refuted_observations") or [])
    print(f"业务不变式探针 -> {OUT}")
    print(f"  进台账：{violations} 条违反 + {positives} 条成立")
    print(f"  不进台账：{suspects} 条待确认疑点 + {refuted} 条被文档否定的观测")
    if suspects or refuted:
        print("  ↑ 「不进台账」的观测本身是真的，但「算不算缺陷」缺 oracle —— "
              "需要业务方确认或文档依据")
    for r in payload["results"]:
        flag = "违反" if not r["ok"] else "成立"
        print(f"  {flag}  {r['id']:<42}检查 {r['checked']} 场景，违反 {r['violations']} 次")


if __name__ == "__main__":
    main()
