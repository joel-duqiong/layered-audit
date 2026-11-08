"""业务不变式探针（demo-shop，项目侧）。

通用判定在 `pilot/engine/business_verdict.py`（零项目字面量）；
这里只做项目侧的三件事：**造出会违反不变式的业务数据**、**把行读出来**、
**把结论变成 finding**。

这个探针要回答的问题：

    **服务层「额度按报价校验、账本按实付记录」的口径分裂，需要几个条件同时成立？**

三个场景，每个只动一个维度：

    · 普通客户 + 会员装配   -> 结算额 == 报价额   -> 口径一致（不违反）
    · VIP 客户 + 标准装配   -> 结算额 == 报价额   -> 口径一致（不违反）
    · VIP 客户 + 会员装配   -> 结算额 == 报价额×9/10 -> 口径分裂（**违反**）

第三个场景是前两个都换掉一个条件之后的**组合** ——
只有它成立才违反，说明这个缺陷需要两个条件同时成立（这正是涌现的性质）。
前两个场景不是「陪衬」，它们是证明「单条件不违反」的对照组。

零残留：造出来的客户与订单都带 ai_probe_ 前缀，跑完自己删干净，末尾机器校验。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
for p in (str(PILOT), str(PILOT / "engine"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import emergence  # noqa: E402
from engine import business_verdict  # noqa: E402
from emergence_probe import Db, http_post  # noqa: E402

CATALOG_FILE = HERE / "emergence_injections.json"
INVARIANTS_FILE = HERE / "invariants.json"
OUT = HERE / "artifacts" / "business_findings.json"

PFX = "ai_probe_"
QUOTED_CENT = 900000
CREDIT_LIMIT_CENT = 1000000


def _customer(name: str, level: str) -> dict[str, Any]:
    return {"name": name, "level": level, "creditLimitCent": CREDIT_LIMIT_CENT}


def seed_customers(db: Db) -> tuple[str, str]:
    """造两个探针客户：一个普通、一个 VIP。额度给足，避免额度检查先挡下来。"""
    normal = f"{PFX}biz_normal"
    vip = f"{PFX}biz_vip"
    db.exec(f"DELETE FROM shop_order WHERE starts_with(customer_name, '{PFX}')")
    db.exec(f"DELETE FROM shop_customer WHERE starts_with(name, '{PFX}')")
    for cust in (_customer(normal, "NORMAL"), _customer(vip, "VIP")):
        db.exec(f"INSERT INTO shop_customer(name, level, credit_limit_cent) VALUES "
                f"('{cust['name']}', '{cust['level']}', {cust['creditLimitCent']})")
    return normal, vip


def make_scenarios(normal: str, vip: str) -> list[dict[str, Any]]:
    def order(customer: str) -> dict[str, Any]:
        return {"customerName": customer, "quotedAmountCent": QUOTED_CENT, "currency": "CNY"}

    def order_fx(customer: str) -> dict[str, Any]:
        # 非基准币种：**结算额会乘上另一个仓库（python-svc 的 /fx）返回的汇率**。
        # 本地若是 1.0（CNY=100），这条就什么都不会发生 —— 差额来自仓库边界之外。
        return {"customerName": customer, "quotedAmountCent": QUOTED_CENT, "currency": "EUR"}

    return [
        {"tag": "normal_on_vip", "instance": "vip", "targets": ["BI-QUOTA-LEDGER-PARITY"],
         "body": order(normal)},
        {"tag": "vip_on_default", "instance": "default", "targets": ["BI-QUOTA-LEDGER-PARITY"],
         "body": order(vip)},
        {"tag": "vip_on_vip", "instance": "vip", "targets": ["BI-QUOTA-LEDGER-PARITY"],
         "body": order(vip)},
        # 跨仓库场景：币种换成非基准 —— 汇率来自另一个仓库
        {"tag": "fx_on_default", "instance": "default", "targets": ["BI-FX-BASIS-ALIGNMENT"],
         "body": order_fx(normal)},
        {"tag": "fx_on_vip", "instance": "vip", "targets": ["BI-FX-BASIS-ALIGNMENT"],
         "body": order_fx(vip)},
    ]


def cleanup(db: Db) -> int:
    db.exec(f"DELETE FROM shop_order WHERE starts_with(customer_name, '{PFX}')")
    db.exec(f"DELETE FROM shop_customer WHERE starts_with(name, '{PFX}')")
    left = db.exec(f"SELECT count(*) FROM shop_order WHERE starts_with(customer_name, '{PFX}')")
    return int(left or 0)


def main() -> int:
    catalog = emergence.load_catalog(CATALOG_FILE)
    inv_cfg = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    invariants = inv_cfg["invariants"]
    entity = inv_cfg["entities"][0]
    db = Db(catalog.database)

    normal, vip = seed_customers(db)
    scenarios = make_scenarios(normal, vip)

    def post_scenario(sc: dict[str, Any]) -> tuple[int, Any]:
        base = str(catalog.instances[sc["instance"]]["base_url"])
        return http_post(base, "/api/orders", sc["body"])

    def read_rows(ids: list[int]) -> list[dict[str, Any]]:
        sql = entity["read_sql"].format(ids=",".join(str(i) for i in ids))
        return db.rows(sql)

    try:
        payload = business_verdict.run(
            invariants=invariants,
            scenarios=scenarios,
            post_scenario=post_scenario,
            read_rows=read_rows,
            endpoint="POST /api/orders",
            repro="python3 demos/shop/business_probe.py",
        )
    finally:
        left = cleanup(db)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    violations = len([f for f in payload["service"] if f["severity"] != "INFO"])
    positives = len(payload["service"]) - violations
    print(f"业务不变式探针 -> {OUT}  {violations} 条违反 + {positives} 条成立")
    for r in payload["results"]:
        flag = "违反" if not r["ok"] else "成立"
        print(f"  {flag}  {r['id']:<32}检查 {r['checked']} 场景，违反 {r['violations']} 次")
    print(f"残留校验：探针数据行 {left} 条（必须为 0）")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
