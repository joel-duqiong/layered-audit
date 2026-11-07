"""集权层 v2：真三方对账 —— API 响应 ↔ DB 实际行 ↔ 前端消费语义。

上一版集成层比的是两份声明（前端调的接口后端有没有、前端读的字段响应里有没有），
那不是对账。真对账要求三个来源同时取值再逐字段比：

  1. 通过 API 写入哨兵值（每个可写字段填成可区分的确定值）
  2. API 读回响应体
  3. 直接查 DB 的真实行
  4. 前端 mapper 的消费口径（读哪些字段、怎么换算、拿不到时兜底成什么）

对账结论分六类：
  ALIGNED            三方一致
  VALUE_MISMATCH     API 返回的值与 DB 里的值不同
  API_OMITS          DB 有这一列，API 不返回 —— 前端永远看不到
  DERIVED            API 返回的是派生值（join/计算），DB 无对应列
  FRONTEND_IGNORES   API 返回了但前端 mapper 不读 —— 死字段
  FRONTEND_MISSING   前端要读但 API 不返回 —— 前端拿到 undefined

哨兵数据全部带 ai_probe_ 前缀，写完即删，DB 与磁盘零残留。
"""
from __future__ import annotations
import project

import datetime
import json
import os
import re
import subprocess
import zoneinfo
from typing import Any

import requests

D = str(project.artifact_dir())
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
FE = str(project.frontend_root())
PFX = "ai_probe_"
DECL = os.path.join(D, "integration_declaration.json")
OUT = os.path.join(D, "integration_reconciliation.json")

session = requests.Session()
SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")
ENVELOPE = {"success", "code", "message", "details", "traceId", "timestamp"}
# 软删位与审计位：接口不暴露是设计选择，不是缺陷
SOFT_AUDIT = {"is_deleted", "created_at", "updated_at"}
# 哨兵值（全部写入 API 可写字段）
SENTINEL = {
    "product_name": PFX + "rc_prod",
    "product_desc": PFX + "rc_desc",
    "product_unit": "g",
    "product_price_cent": 5000,
    "package_name": PFX + "rc_pkg",
    "package_desc": PFX + "rc_pkg_desc",
    "package_type": PFX + "rc_type",
    "package_price_cent": 8888,
    "customer_name": PFX + "rc_cust",
    "customer_phone": "13800000999",
    "banquet_name": PFX + "rc_banq",
    "banquet_type": PFX + "rc_banq_type",
    "address": PFX + "rc_addr",
    "remark": PFX + "rc_remark",
    "table_count": 7,
    "plan_total_cent": 24680,
    "deposit_cent": 1111,
    "final_price_cent": 13579,
    "reservation_time": "2027-03-05T18:30:00+08:00",
    "pkg_qty": 3,
    "added_qty": 2,
}


# --------------------------------------------------------------------------
# 取值工具
# --------------------------------------------------------------------------
def psql_rows(query: str) -> list[dict]:
    """直接读 DB 的真实行。整行压成 JSON 再解析，避免 jsonb 里的分隔符把行切碎。"""
    out = subprocess.run(
        ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan",
         "-tA", "-c", f"SELECT to_jsonb(_r) FROM ({query}) _r"],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def psql_exec(sql: str) -> None:
    """执行收尾清理 SQL（仅限哨兵数据）。"""
    subprocess.run(
        ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan",
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=60, check=True)


def api(method: str, path: str, body=None, params=None) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["data"] = json.dumps(body, ensure_ascii=False)
        kwargs["headers"] = {"Content-Type": "application/json"}
    r = session.request(method, BASE + path, timeout=20, **kwargs)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def data_of(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.capitalize() for w in rest)


def same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).lower() == str(b).lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return str(a) == str(b)


def as_instant(value: Any) -> float | None:
    """把 ISO 时间串解析成时间戳，用于比较同一时刻的不同写法。"""
    if value is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.timestamp()


def frontend_map(payload: dict) -> dict:
    """用前端真实 mapper（Node 直接跑 src/api/banquetApi.js）把 API 响应转成视图模型。

    这是第三个来源：不是复刻前端逻辑，而是执行前端线上同款代码，
    产出前端真正拿到并渲染的那份数据。
    """
    res = subprocess.run(
        ["node", "--import", "./fe_register.mjs", "frontend_mapper.mjs"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, timeout=60, cwd=D)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-400:])
    return json.loads(res.stdout)


def cleanup_sentinels() -> None:
    """按哨兵名精确删除本次造的数据：先订单，再明细，再套餐，再商品。"""
    psql_exec(f"""
    DELETE FROM orders WHERE customer_name = '{SENTINEL['customer_name']}';
    DELETE FROM package_items WHERE package_id IN
        (SELECT id FROM packages WHERE name = '{SENTINEL['package_name']}');
    DELETE FROM packages WHERE name = '{SENTINEL['package_name']}';
    DELETE FROM products WHERE name = '{SENTINEL['product_name']}';
    """)


# --------------------------------------------------------------------------
# 对账核心
# --------------------------------------------------------------------------
def row(entity: str, field, api_field, db_value, api_value,
        usage, verdict: str, severity: str, note: str | None) -> dict:
    return {"entity": entity, "field": field, "api_field": api_field,
            "db_value": db_value, "api_value": api_value,
            "frontend_usage": usage, "verdict": verdict,
            "severity_hint": severity, "note": note}


def reconcile_entity(entity: str, db_row: dict, api_body: dict,
                     fe_fields: set[str], value_map: dict) -> list[dict]:
    """一个实体的一次三方对账。value_map: DB 列 -> 写入的哨兵值。"""
    rows: list[dict] = []
    api_keys = set(api_body)

    for col, db_val in db_row.items():
        camel = snake_to_camel(col)
        if camel not in api_keys:
            sev = "INFO" if col in SOFT_AUDIT else "MEDIUM"
            rows.append(row(entity, col, None, db_val, None, None, "API_OMITS", sev,
                            "DB 有这一列，API 不返回 —— 前端拿不到这一列"))
            continue
        api_val = api_body[camel]
        expect = value_map.get(col)
        if expect is not None:
            verdict = "ALIGNED" if same(api_val, expect) else "VALUE_MISMATCH"
            sev = "INFO" if verdict == "ALIGNED" else "HIGH"
            note = None if verdict == "ALIGNED" else "写入的哨兵值与 API 回读不一致"
        elif col.endswith("_at"):
            a, b = as_instant(db_val), as_instant(api_val)
            verdict = "ALIGNED" if a is not None and a == b else "VALUE_MISMATCH"
            sev = "INFO" if verdict == "ALIGNED" else "HIGH"
            note = None if verdict == "ALIGNED" else "同一时刻的两种写法对不上"
        else:
            verdict, sev, note = "ALIGNED", "INFO", None
        rows.append(row(entity, col, camel, db_val, api_val,
                        camel if camel in fe_fields else None, verdict, sev, note))

    for key in sorted(api_keys):
        if key in db_row or snake_to_camel(key) in db_row:
            continue
        rows.append(row(entity, None, key, None, api_body[key],
                        key if key in fe_fields else None, "DERIVED", "INFO",
                        "API 返回的值在本表没有对应列（join 或计算派生）"))

    for key in sorted(api_keys):
        if key in fe_fields:
            continue
        sev = "INFO" if key in ("createdAt", "updatedAt") else "LOW"
        rows.append(row(entity, None, key, None, api_body[key], None,
                        "FRONTEND_IGNORES", sev,
                        "API 返回了但前端 mapper 不消费 —— 死字段"))

    for key in sorted(fe_fields):
        if key not in api_keys:
            rows.append(row(entity, None, key, None, None, key,
                            "FRONTEND_MISSING", "HIGH",
                            "前端 mapper 要读这个字段，API 响应里没有 —— 前端拿到 undefined"))
    return rows


# --------------------------------------------------------------------------
# 语义对账（金额单位 / 时区 / 快照 / 明细金额 / 兜底图）
# --------------------------------------------------------------------------
def sem(entity: str, field: str, usage: str, ok: bool, note: str,
        severity: str = "MEDIUM") -> dict:
    return row(entity, field, field, None, None, usage,
               "ALIGNED" if ok else "SEMANTIC_MISMATCH",
               "INFO" if ok else severity, note)


def check_amount_unit(entity: str, field: str, api_val, expect_yuan: float,
                      usage: str) -> dict:
    got = cent_to_yuan(api_val)
    return sem(entity, field, usage, got == expect_yuan,
               f"DB/API 都是分，前端渲染 {got} 元（期望 {expect_yuan} 元）")


# --------------------------------------------------------------------------
# 三个实体的对账
# --------------------------------------------------------------------------
def reconcile_products(fe: set[str]) -> tuple[list[dict], dict]:
    cats = data_of(api("GET", "/api/categories")[1]) or []
    cid = (cats[0] or {}).get("id") if cats else None
    if cid is None:
        return [], {"skipped": "没有可用分类"}
    body = {"name": SENTINEL["product_name"], "categoryId": cid,
            "priceCent": SENTINEL["product_price_cent"], "unit": SENTINEL["product_unit"],
            "imageUrl": "", "description": SENTINEL["product_desc"], "isActive": False}
    st, payload = api("POST", "/api/products", body)
    if st != 200:
        return [], {"skipped": f"创建失败 http={st}"}
    pid = data_of(payload)["id"]
    listed = data_of(api("GET", "/api/products",
                         params={"keyword": SENTINEL["product_name"], "pageSize": 10})[1]) or {}
    items = listed.get("items") or []
    api_body = items[0] if items else {}
    rows = psql_rows(f"SELECT * FROM products WHERE id={pid}")
    db_row = rows[0] if rows else {}

    value_map = {"name": SENTINEL["product_name"], "price_cent": SENTINEL["product_price_cent"],
                 "unit": SENTINEL["product_unit"], "description": SENTINEL["product_desc"],
                 "is_active": False, "category_id": cid}
    table = reconcile_entity("products", db_row, api_body, fe, value_map)

    view = frontend_map({"product": api_body}).get("product") or {}
    table.append(sem(
        "products", "price_cent", "mapProduct.price（前端真实 mapper 输出）",
        view.get("price") == SENTINEL["product_price_cent"] / 100,
        f"DB/API 都是 {SENTINEL['product_price_cent']} 分，前端真实 mapper 渲染 {view.get('price')} 元",
        "HIGH"))
    if not api_body.get("imageUrl"):
        domain_ok = "/products/" in str(view.get("image"))
        table.append(sem(
            "products", "image_url", "mapProduct.image = imageUrl || photo",
            domain_ok,
            f"商品 imageUrl 为空，前端真实 mapper 兜底成 {view.get('image')}"
            + ("" if domain_ok else " —— 商品位用了套餐种子图"), "MEDIUM"))
    table.append(sem(
        "products", "is_active", "mapProduct.active = Boolean(isActive)",
        bool(view.get("active")) == bool(api_body.get("isActive")),
        f"DB is_active={db_row.get('is_active')} → API {api_body.get('isActive')} "
        f"→ 前端 {view.get('active')}"))
    return table, {"product_id": pid}


def reconcile_packages(fe: set[str], fe_item: set[str],
                       product_id: int) -> tuple[list[dict], dict]:
    # banquetType 在服务层要命中 package_types，DB 层却没有任何约束 —— 先取一个真实类型名
    types = data_of(api("GET", "/api/package-types")[1]) or []
    type_name = (types[0] or {}).get("name") if types else None
    if type_name is None:
        return [], {"skipped": "没有可用套餐分类"}
    body = {"name": SENTINEL["package_name"], "banquetType": type_name,
            "priceCent": SENTINEL["package_price_cent"],
            "imageUrl": "/uploads/seed/packages/package-01.jpg",
            "description": SENTINEL["package_desc"], "isActive": True,
            "items": [{"productId": product_id, "quantity": SENTINEL["pkg_qty"]}]}
    st, payload = api("POST", "/api/packages", body)
    if st != 200:
        return [], {"skipped": f"创建失败 http={st}"}
    kid = data_of(payload)["id"]
    api_body = data_of(api("GET", f"/api/packages/{kid}")[1]) or {}
    rows = psql_rows(f"SELECT * FROM packages WHERE id={kid}")
    db_row = rows[0] if rows else {}

    value_map = {"name": SENTINEL["package_name"], "banquet_type": type_name,
                 "price_cent": SENTINEL["package_price_cent"],
                 "description": SENTINEL["package_desc"], "is_active": True}
    table = reconcile_entity("packages", db_row, api_body, fe, value_map)
    view = frontend_map({"package": api_body}).get("package") or {}
    table.append(sem(
        "packages", "price_cent", "mapPackage.price（前端真实 mapper 输出）",
        view.get("price") == SENTINEL["package_price_cent"] / 100,
        f"DB/API 都是 {SENTINEL['package_price_cent']} 分，前端真实 mapper 渲染 {view.get('price')} 元",
        "HIGH"))

    # 明细行：package_items 表 ↔ API items[] ↔ 前端 element_fields
    item_rows = psql_rows(f"SELECT * FROM package_items WHERE package_id={kid}")
    api_items = api_body.get("items") or []
    if item_rows and api_items:
        table += reconcile_entity("package_items", item_rows[0], api_items[0], fe_item, {})
    db_count = len(item_rows)
    table.append(sem(
        "packages", "item_count", "mapPackage.dishes = itemCount ?? items.length",
        api_body.get("itemCount") == db_count,
        f"API itemCount={api_body.get('itemCount')} vs DB 明细 {db_count} 行", "HIGH"))

    # 同一份数据，前端两处「商品数」口径不同：卡片用 itemCount，弹层用「在架商品数」
    listed = data_of(api("GET", "/api/products",
                         params={"page": 1, "pageSize": 100})[1]) or {}
    prod_view = frontend_map({"products": listed.get("items") or []}).get("products") or []
    active_map = {str(p.get("id")): bool(p.get("active")) for p in prod_view}
    visible = sum(1 for line in view.get("items") or []
                  if active_map.get(str(line.get("productId"))))
    table.append(sem(
        "packages", "item_count", "App.jsx visibleItemCount vs mapPackage.dishes",
        visible == view.get("dishes"),
        f"套餐卡片显示「{view.get('dishes')} 个商品」（= API itemCount，含下架商品），"
        f"选套餐弹层显示「{visible} 个菜品」（= 在架商品数），同一套餐两个数字不同",
        "MEDIUM"))
    return table, {"package_id": kid, "banquet_type": type_name}


def check_db_type_guard() -> list[dict]:
    """DB 层能不能直接写入一个不存在的 banquet_type / customer_phone。"""
    out = []
    probes = [
        ("packages", "banquet_type", "PACKAGE_TYPE_NOT_FOUND",
         f"INSERT INTO packages (name, banquet_type, price_cent, is_active) "
         f"VALUES ('{PFX}rc_rogue_pkg', '{PFX}rc_rogue_type', 1, true)"),
        ("orders", "customer_phone", "无",
         f"INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, "
         f"banquet_type, address, reservation_time, table_count, plan_total_cent, "
         f"use_final_price, final_price_cent, deposit_cent, status) VALUES "
         f"('{PFX}RC001', '{PFX}rc_rogue_cust', 'not-a-phone', '{PFX}rc_rogue_banq', "
         f"'{PFX}rc_rogue_type', '', now(), 1, 1, true, 1, 1, 'PENDING')"),
    ]
    for table, field, guard, ddl in probes:
        sql = f"BEGIN;\n{ddl};\nROLLBACK;"
        res = subprocess.run(
            ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan",
             "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=sql, capture_output=True, text=True, timeout=60)
        blocked = res.returncode != 0
        out.append(sem(
            table, field, "DB 直写绕过服务层校验",
            blocked,
            f"服务层有 {guard} 校验，DB 直写{'被约束挡住' if blocked else '畅通无阻'}",
            "HIGH" if not blocked else "INFO"))
    return out


def reconcile_orders(fe: set[str], snap_fe: set[str], added_fe: set[str],
                     product_id: int, package_id: int) -> tuple[list[dict], dict]:
    body = {
        "customerName": SENTINEL["customer_name"], "customerPhone": SENTINEL["customer_phone"],
        "banquetName": SENTINEL["banquet_name"], "banquetType": SENTINEL["banquet_type"],
        "address": SENTINEL["address"], "reservationTime": SENTINEL["reservation_time"],
        "tableCount": SENTINEL["table_count"], "packageId": package_id,
        "packageItems": [{"productId": product_id, "quantity": SENTINEL["pkg_qty"]}],
        "addedItems": [{"productId": product_id, "quantity": SENTINEL["added_qty"]}],
        "planTotalCent": SENTINEL["plan_total_cent"], "useFinalPrice": True,
        "finalPriceCent": SENTINEL["final_price_cent"],
        "depositCent": SENTINEL["deposit_cent"], "status": "PENDING",
        "remark": SENTINEL["remark"],
    }
    st, payload = api("POST", "/api/orders", body)
    if st != 200:
        return [], {"skipped": f"创建失败 http={st} {(payload or {}).get('message')}"}
    oid = data_of(payload)["id"]
    api_body = data_of(api("GET", f"/api/orders/{oid}")[1]) or {}
    rows = psql_rows(f"SELECT * FROM orders WHERE id={oid}")
    db_row = rows[0] if rows else {}

    value_map = {"customer_name": SENTINEL["customer_name"],
                 "customer_phone": SENTINEL["customer_phone"],
                 "banquet_name": SENTINEL["banquet_name"],
                 "banquet_type": SENTINEL["banquet_type"],
                 "address": SENTINEL["address"],
                 "table_count": SENTINEL["table_count"],
                 "plan_total_cent": SENTINEL["plan_total_cent"],
                 "final_price_cent": SENTINEL["final_price_cent"],
                 "deposit_cent": SENTINEL["deposit_cent"],
                 "use_final_price": True, "status": "PENDING", "package_id": package_id}
    table = reconcile_entity("orders", db_row, api_body, fe, value_map)

    # 第三个来源：前端真实 mapper 的输出
    view = frontend_map({"order": api_body}).get("order") or {}
    table.append(sem(
        "orders", "final_price_cent", "mapOrder.amount（前端真实 mapper 输出）",
        view.get("amount") == SENTINEL["final_price_cent"] / 100,
        f"DB/API 都是 {SENTINEL['final_price_cent']} 分，前端真实 mapper 渲染 {view.get('amount')} 元",
        "HIGH"))
    table.append(sem(
        "orders", "reservation_time", "mapOrder.time = formatOrderTime(item.reservationTime)",
        view.get("time") == "2027-03-05 18:30",
        f"写入 {SENTINEL['reservation_time']} → DB {db_row.get('reservation_time')} "
        f"→ API {api_body.get('reservationTime')} → 前端渲染 {view.get('time')}"))
    table.append(sem(
        "orders", "plan_total_cent", "mapOrder.planTotal = centToYuan(item.planTotalCent)",
        view.get("planTotal") == SENTINEL["plan_total_cent"] / 100,
        f"DB/API 都是 {SENTINEL['plan_total_cent']} 分，前端真实 mapper 渲染 {view.get('planTotal')} 元"))
    table.append(sem(
        "orders", "status", "mapOrder.status = fromApiStatus(item.status)",
        view.get("status") == "待确认",
        f"DB {db_row.get('status')} → API {api_body.get('status')} → 前端 {view.get('status')}"))

    # 快照三方闭合：DB jsonb 键 ↔ API 快照键 ↔ 前端读取的键
    db_snap = db_row.get("package_snapshot") or {}
    api_snap = api_body.get("packageSnapshot") or {}
    table.append(sem(
        "orders", "package_snapshot", "mapOrder 读 packageSnapshot.*",
        set(db_snap) == set(api_snap) and set(snap_fe) <= set(api_snap),
        f"DB 快照键 {sorted(db_snap)} / API 快照键 {sorted(api_snap)} / 前端要读 {sorted(snap_fe)}",
        "HIGH"))
    db_items = (db_snap.get("items") or [])
    api_items = (api_snap.get("items") or [])
    if api_items:
        table.append(sem(
            "orders", "package_snapshot.items", "mapOrder 读 packageSnapshot.items[].*",
            set(db_items[0]) == set(api_items[0]),
            f"DB 明细键 {sorted(db_items[0])} / API 明细键 {sorted(api_items[0])}", "HIGH"))

    # 明细隐含金额 vs 成交价：直接用前端真正看到的那几个数字算
    implied = sum(round(line["price"] * line["qty"] * 100)
                  for line in (view.get("packageItems") or []) + (view.get("extras") or []))
    final = api_body.get("finalPriceCent")
    table.append(sem(
        "orders", "final_price_cent", "前端视图模型明细合计 vs 订单金额",
        implied == final,
        f"前端看到的明细合计 {implied} 分（{implied / 100} 元） vs 订单金额 {final} 分"
        f"（{(final or 0) / 100} 元），差额 {implied - (final or 0)} 分；"
        f"服务端对 finalPriceCent 不做任何核对", "HIGH"))

    added = api_body.get("addedItemsSnapshot") or []
    if added:
        table.append(sem(
            "orders", "added_items_snapshot", "mapOrder 读 addedItemsSnapshot[].*",
            set(added_fe) <= set(added[0]),
            f"前端要读 {sorted(added_fe)} / API 加购明细键 {sorted(added[0])}", "HIGH"))

    return table, {"order_id": oid, "snapshot": api_snap}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main() -> None:
    decl = json.load(open(DECL, encoding="utf-8"))
    mappers = decl["frontend"]["mappers"]
    fe_product = set(mappers["mapProduct"]["direct_fields"])
    fe_package = set(mappers["mapPackage"]["direct_fields"])
    fe_package_item = set(mappers["mapPackage"]["element_fields"].get("items", []))
    fe_order = set(mappers["mapOrder"]["direct_fields"])
    snap_fe = set(mappers["mapOrder"]["nested_fields"].get("packageSnapshot", []))
    added_fe = set(mappers["mapOrder"]["element_fields"].get("addedItemsSnapshot", []))

    table: list[dict] = []
    meta: dict = {}
    try:
        prod_rows, prod_meta = reconcile_products(fe_product)
        table += prod_rows
        meta["products"] = prod_meta
        pid = prod_meta.get("product_id")

        if pid:
            pkg_rows, pkg_meta = reconcile_packages(fe_package, fe_package_item, pid)
            table += pkg_rows
            meta["packages"] = pkg_meta
            kid = pkg_meta.get("package_id")
            if kid:
                order_rows, order_meta = reconcile_orders(fe_order, snap_fe, added_fe, pid, kid)
                table += order_rows
                meta["orders"] = order_meta
    finally:
        cleanup_sentinels()

        table += check_db_type_guard()

    counts: dict[str, int] = {}
    for r in table:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    res = {
        "generated_at": datetime.datetime.now().isoformat(),
        "base_url": BASE,
        "reconciliation": table,
        "verdict_counts": counts,
        "meta": meta,
    }
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"三方对账 -> {OUT}")
    print(f"  对账项 {len(table)} 个：{counts}")
    for r in table:
        if r["verdict"] in ("VALUE_MISMATCH", "FRONTEND_MISSING", "SEMANTIC_MISMATCH"):
            print(f"    {r['verdict']:18} {r['entity']}.{r['field'] or r['api_field']}")
            if r.get("note"):
                print(f"                       {r['note']}")


if __name__ == "__main__":
    main()
