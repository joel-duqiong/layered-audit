"""集权层动态实测。

驱动源不是「后端接口清单」，而是**前端真正会发出的那一串 HTTP**：
  1) 前端端点实调 —— 每个前端 endpoint 用真实数据打一遍，记录响应形状
  2) 录制流量回放 —— 三个月前录的真实请求再打一遍，看契约有没有漂移
  3) 端到端业务场景 —— 只有跨层组合才成立的缺陷（价格、套餐明细、列表截断）

所有写操作一律带 ai_probe_ 前缀，由 cleanup.sh 回收；绝不碰种子数据。
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import time

import requests

D = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
PFX = "ai_probe_"
DECL = os.path.join(D, "integration_declaration.json")
OUT = os.path.join(D, "integration_observation.json")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

session = requests.Session()


def call(method: str, path: str, body=None, params=None, files=None):
    """返回 (status, latency_ms, json_or_none, text)。"""
    url = f"{BASE}{path}"
    kwargs = {"params": params} if params else {}
    if files is not None:
        kwargs["files"] = files
    elif body is not None:
        kwargs["data"] = json.dumps(body, ensure_ascii=False)
        kwargs["headers"] = {"Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        r = session.request(method, url, timeout=15, **kwargs)
    except requests.RequestException as exc:  # 网络层异常也算观测事实
        return 0, int((time.perf_counter() - t0) * 1000), None, str(exc)
    latency = int((time.perf_counter() - t0) * 1000)
    try:
        payload = r.json()
    except ValueError:
        payload = None
    return r.status_code, latency, payload, r.text[:400]


def data_of(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def resolve_ids(path: str, created: dict) -> str:
    """把 {product_id} 之类的占位符替换成实际 id（None 说明上游没建出来）。"""
    out = path
    for key, value in created.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def shape_of(payload):
    """把响应体压成可比对的形状描述。"""
    d = data_of(payload)
    out = {"envelope": sorted(payload.keys()) if isinstance(payload, dict) else None}
    if isinstance(d, dict):
        out["data_keys"] = sorted(d.keys())
        out["data_type"] = "object"
        items = d.get("items")
        if isinstance(items, list):
            out["items_count"] = len(items)
            out["total"] = d.get("total")
            out["page"] = d.get("page")
            out["pageSize"] = d.get("pageSize")
            if items and isinstance(items[0], dict):
                out["item_keys"] = sorted(items[0].keys())
    elif isinstance(d, list):
        out["data_type"] = "array"
        out["items_count"] = len(d)
        if d and isinstance(d[0], dict):
            out["item_keys"] = sorted(d[0].keys())
    else:
        out["data_type"] = type(d).__name__
        out["data_value"] = d if isinstance(d, (str, int, float, bool)) or d is None else None
    return out


# --------------------------------------------------------------------------
# 1) 前端端点实调
# --------------------------------------------------------------------------
def build_calls(ctx):
    """镜像前端 banquetApi.js 的真实调用序列。写操作只作用在 ai_probe_ 数据上。"""
    ts = ctx["ts"]
    calls = [
        ("GET /api/categories", "GET", "/api/categories", None, None),
        ("POST /api/categories", "POST", "/api/categories", {"name": f"{PFX}cat_{ts}"}, None),
        ("GET /api/package-types", "GET", "/api/package-types", None, None),
        ("POST /api/package-types", "POST", "/api/package-types", {"name": f"{PFX}ptype_{ts}"}, None),
        ("GET /api/products", "GET", "/api/products", None, {"page": 1, "pageSize": 100}),
        ("POST /api/products", "POST", "/api/products", {
            "name": f"{PFX}prod_{ts}", "categoryId": ctx["category_id"], "priceCent": 8800,
            "unit": "份", "imageUrl": "/uploads/seed/products/product-01.jpg",
            "description": "探针商品", "isActive": True}, None),
        ("GET /api/packages", "GET", "/api/packages", None, {"page": 1, "pageSize": 100}),
        ("GET /api/orders", "GET", "/api/orders", None, {"page": 1, "pageSize": 100}),
    ]
    return calls


def run_frontend_calls(ctx):
    results = []
    ts = ctx["ts"]
    created = {"category_id": None, "package_type_id": None, "product_id": None,
               "package_id": None, "order_id": None}

    # 1) 先把前端列表端点全部实调一遍（只读）
    for key, method, path, body, params in build_calls(ctx):
        if key.startswith(("POST",)):
            continue
        status, latency, payload, text = call(method, path, body=body, params=params)
        results.append({"key": key, "method": method, "path": path, "params": params,
                        "status": status, "latency_ms": latency, "shape": shape_of(payload),
                        "error": None if status == 200 else text})

    # 2) 写链路：建 -> 改 -> 删，全程 ai_probe_ 前缀
    steps = [
        ("POST /api/categories", "POST", "/api/categories", {"name": f"{PFX}cat_{ts}"}, None, "category"),
        ("DELETE /api/categories/{id}", "DELETE", "/api/categories/{category_id}", None, None, None),
        ("POST /api/package-types", "POST", "/api/package-types", {"name": f"{PFX}ptype_{ts}"}, None, "package_type"),
        ("DELETE /api/package-types/{id}", "DELETE", "/api/package-types/{package_type_id}", None, None, None),
        ("POST /api/products", "POST", "/api/products", {
            "name": f"{PFX}prod_{ts}", "categoryId": ctx["category_id"], "priceCent": 8800,
            "unit": "份", "imageUrl": "/uploads/seed/products/product-01.jpg",
            "description": "探针商品", "isActive": True}, None, "product"),
        ("PUT /api/products/{id}", "PUT", "/api/products/{product_id}", {
            "name": f"{PFX}prod_{ts}_v2", "categoryId": ctx["category_id"], "priceCent": 9900,
            "unit": "份", "imageUrl": "/uploads/seed/products/product-01.jpg",
            "description": "探针商品改", "isActive": True}, None, None),
        ("PATCH /api/products/{id}/status", "PATCH", "/api/products/{product_id}/status",
         {"isActive": False}, None, None),
        ("DELETE /api/products/{id}", "DELETE", "/api/products/{product_id}", None, None, None),
    ]
    for key, method, path, body, params, create_kind in steps:
        path = resolve_ids(path, created)
        if create_kind:
            status, latency, payload, text = call(method, path, body=body, params=params)
            d = data_of(payload)
            if status == 200 and isinstance(d, dict) and d.get("id"):
                created[f"{create_kind}_id"] = d["id"]
            results.append({"key": key, "method": method, "path": path, "params": params,
                            "status": status, "latency_ms": latency, "shape": shape_of(payload),
                            "error": None if status == 200 else text})
            continue
        status, latency, payload, text = call(method, path, body=body, params=params)
        results.append({"key": key, "method": method, "path": path, "params": params,
                        "status": status, "latency_ms": latency, "shape": shape_of(payload),
                        "error": None if status < 400 else text})

    # 3) 套餐：建（带明细）-> 改 -> 上下架 -> 删
    pkg_items = [{"productId": ctx["product_id"], "quantity": 2}]
    for key, method, path, body, create_kind in [
        ("POST /api/packages", "POST", "/api/packages", {
            "name": f"{PFX}pkg_{ts}", "banquetType": "婚宴", "priceCent": 128000,
            "imageUrl": "/uploads/seed/packages/package-01.jpg", "description": "探针套餐",
            "isActive": True, "items": pkg_items}, "package"),
        ("PUT /api/packages/{id}", "PUT", "/api/packages/{package_id}", {
            "name": f"{PFX}pkg_{ts}_v2", "banquetType": "婚宴", "priceCent": 138000,
            "imageUrl": "/uploads/seed/packages/package-01.jpg", "description": "探针套餐改",
            "isActive": True, "items": pkg_items}, None),
        ("PATCH /api/packages/{id}/status", "PATCH", "/api/packages/{package_id}/status",
         {"isActive": False}, None),
        ("DELETE /api/packages/{id}", "DELETE", "/api/packages/{package_id}", None, None),
    ]:
        path = resolve_ids(path, created)
        status, latency, payload, text = call(method, path, body=body)
        d = data_of(payload)
        if create_kind and status == 200 and isinstance(d, dict) and d.get("id"):
            created[f"{create_kind}_id"] = d["id"]
        results.append({"key": key, "method": method, "path": path, "params": None,
                        "status": status, "latency_ms": latency, "shape": shape_of(payload),
                        "error": None if status < 400 else text})

    # 4) 订单：建 -> 改 -> 改状态
    order_body = {
        "customerName": f"{PFX}customer_{ts}", "customerPhone": "13800000000",
        "banquetName": f"{PFX}banquet_{ts}", "banquetType": "婚宴",
        "address": "探针地址", "reservationTime": "2026-12-01T18:00:00+08:00",
        "tableCount": 10, "packageId": ctx["package_id"],
        "packageItems": [{"productId": ctx["product_id"], "quantity": 2}],
        "addedItems": [{"productId": ctx["other_product_id"], "quantity": 1}],
        "planTotalCent": 128000, "useFinalPrice": False, "finalPriceCent": None,
        "depositCent": 20000, "status": "PENDING", "remark": "探针订单",
    }
    for key, method, path, body, create_kind in [
        ("POST /api/orders", "POST", "/api/orders", order_body, "order"),
        ("PUT /api/orders/{id}", "PUT", "/api/orders/{order_id}", dict(order_body, remark="探针订单改"), None),
        ("PATCH /api/orders/{id}/status", "PATCH", "/api/orders/{order_id}/status",
         {"status": "DEPOSIT_PAID"}, None),
    ]:
        path = resolve_ids(path, created)
        status, latency, payload, text = call(method, path, body=body)
        d = data_of(payload)
        if create_kind and status == 200 and isinstance(d, dict) and d.get("id"):
            created[f"{create_kind}_id"] = d["id"]
        results.append({"key": key, "method": method, "path": path, "params": None,
                        "status": status, "latency_ms": latency, "shape": shape_of(payload),
                        "error": None if status < 400 else text})

    # 5) 上传（前端只发 FormData.file）
    files = {"file": ("probe.png", PNG_1PX, "image/png")}
    status, latency, payload, text = call("POST", "/api/uploads/product-image", files=files)
    results.append({"key": "POST /api/uploads/product-image", "method": "POST",
                    "path": "/api/uploads/product-image", "params": None,
                    "status": status, "latency_ms": latency, "shape": shape_of(payload),
                    "error": None if status < 400 else text})
    upload_url = None
    d = data_of(payload)
    if isinstance(d, dict):
        upload_url = d.get("imageUrl")

    return results, created, upload_url


# --------------------------------------------------------------------------
# 2) 录制流量回放
# --------------------------------------------------------------------------
def replay_recordings(rows):
    out = []
    for r in rows:
        method, path = r["method"], r["path"]
        params = dict(p.split("=", 1) for p in (r.get("query") or "").split("&") if "=" in p) or None
        status, latency, payload, text = call(method, path, params=params)
        out.append({
            "method": method, "path": path, "query": r.get("query") or "",
            "recorded_status": r["status"], "current_status": status,
            "recorded_latency_ms": r.get("latency_ms"), "current_latency_ms": latency,
            "recorded_ts": r.get("ts"), "seq": r.get("seq"),
            "drift": status != r["status"],
            "error_text": None if status == r["status"] else text[:200],
        })
    return out


# --------------------------------------------------------------------------
# 3) 端到端业务场景（跨层组合才成立的缺陷）
# --------------------------------------------------------------------------
def order_payload(ctx, **over):
    body = {
        "customerName": f"{PFX}sc_customer_{ctx['ts']}", "customerPhone": "13800000001",
        "banquetName": f"{PFX}sc_banquet_{ctx['ts']}", "banquetType": "婚宴",
        "address": "场景地址", "reservationTime": "2026-12-20T18:00:00+08:00",
        "tableCount": 8, "packageId": ctx["package_id"],
        "packageItems": [{"productId": ctx["package_item_product_id"], "quantity": 1}],
        "addedItems": [],
        "planTotalCent": ctx["package_price_cent"], "useFinalPrice": False, "finalPriceCent": None,
        "depositCent": 1000, "status": "PENDING", "remark": "场景",
    }
    body.update(over)
    return body


def run_scenarios(ctx):
    sc = []

    # SC01 客户端自定价：套餐 128000 分，客户端报 1 分
    body = order_payload(ctx, planTotalCent=1, useFinalPrice=True, finalPriceCent=1)
    status, latency, payload, _ = call("POST", "/api/orders", body=body)
    d = data_of(payload)
    sc.append({
        "id": "SC01_CLIENT_CONTROLLED_PRICE",
        "title": "客户端自报成交价：套餐 1280.00 元，客户端报 0.01 元",
        "expectation": "服务端应按套餐价自行计算，最少应拒绝明显偏离的成交价（4xx）",
        "actual_status": status,
        "actual_final_price_cent": d.get("finalPriceCent") if isinstance(d, dict) else None,
        "actual_plan_total_cent": d.get("planTotalCent") if isinstance(d, dict) else None,
        "snapshot_price_cent": (d.get("packageSnapshot") or {}).get("priceCent") if isinstance(d, dict) and isinstance(d.get("packageSnapshot"), dict) else None,
        "latency_ms": latency,
        "verdict": "FAIL" if status == 200 else "PASS",
        "evidence": {"order_id": d.get("id") if isinstance(d, dict) else None},
    })

    # SC02 套餐外商品混入明细
    body = order_payload(ctx, packageItems=[{"productId": ctx["other_product_id"], "quantity": 1}])
    status, latency, payload, _ = call("POST", "/api/orders", body=body)
    d = data_of(payload)
    snap = d.get("packageSnapshot") if isinstance(d, dict) else None
    sc.append({
        "id": "SC02_FOREIGN_ITEM_IN_PACKAGE",
        "title": "把不属于该套餐的商品写进套餐明细",
        "expectation": "明细必须属于所选套餐，否则应 4xx",
        "actual_status": status,
        "snapshot_items": (snap or {}).get("items") if isinstance(snap, dict) else None,
        "latency_ms": latency,
        "verdict": "FAIL" if status == 200 else "PASS",
        "evidence": {"order_id": d.get("id") if isinstance(d, dict) else None},
    })

    # SC03 明细数量与套餐不符（加量不加价）
    body = order_payload(ctx, packageItems=[{"productId": ctx["package_item_product_id"], "quantity": 99}])
    status, latency, payload, _ = call("POST", "/api/orders", body=body)
    d = data_of(payload)
    snap = d.get("packageSnapshot") if isinstance(d, dict) else None
    items = (snap or {}).get("items") if isinstance(snap, dict) else None
    sc.append({
        "id": "SC03_QUANTITY_OVERRIDE",
        "title": "明细数量改成 99，价格仍是套餐原价",
        "expectation": "数量被改后价格应随之重算，或应拒绝",
        "actual_status": status,
        "snapshot_items": items,
        "final_price_cent": d.get("finalPriceCent") if isinstance(d, dict) else None,
        "latency_ms": latency,
        "verdict": "FAIL" if status == 200 else "PASS",
        "evidence": {"order_id": d.get("id") if isinstance(d, dict) else None},
    })

    # SC04 字段名写错 -> 静默丢弃 -> 明细回退成套餐默认
    body = order_payload(ctx)
    body["packageItem"] = body.pop("packageItems")
    status, latency, payload, _ = call("POST", "/api/orders", body=body)
    d = data_of(payload)
    snap = d.get("packageSnapshot") if isinstance(d, dict) else None
    sc.append({
        "id": "SC04_UNKNOWN_FIELD_SILENT_DROP",
        "title": "packageItems 写成 packageItem：后端静默忽略并回退套餐默认明细",
        "expectation": "未知字段应报错（fail-on-unknown-properties），否则前端拼错字段无任何反馈",
        "actual_status": status,
        "snapshot_items": (snap or {}).get("items") if isinstance(snap, dict) else None,
        "latency_ms": latency,
        "verdict": "FAIL" if status == 200 else "PASS",
        "evidence": {"order_id": d.get("id") if isinstance(d, dict) else None},
    })

    # SC05 前端列表截断：前端固定 pageSize=100 且丢弃 total
    trunc_total = None
    for i in range(105):
        call("POST", "/api/products", body={
            "name": f"{PFX}trunc_{i:03d}", "categoryId": ctx["category_id"], "priceCent": 100,
            "unit": "份", "imageUrl": "", "description": "截断探针", "isActive": True})
    status, latency, payload, _ = call("GET", "/api/products", params={"page": 1, "pageSize": 100})
    shape = shape_of(payload)
    trunc_total = shape.get("total")
    sc.append({
        "id": "SC05_LIST_TRUNCATION",
        "title": "前端列表固定 pageSize=100 且丢弃 total：超出部分永远不可见",
        "expectation": "列表要么分页、要么前端读 total 提示截断",
        "actual_status": status,
        "items_count": shape.get("items_count"),
        "total": trunc_total,
        "latency_ms": latency,
        "verdict": "FAIL" if (trunc_total or 0) > (shape.get("items_count") or 0) else "PASS",
        "evidence": {"frontend_call": "GET /api/products?page=1&pageSize=100",
                     "frontend_code": "return data.items.map(mapProduct);  // total 未被读取"},
    })

    # SC06 枚举越界
    enum_probe = []
    for value in ctx["status_enum_values"] + ctx["date_scope_values"]:
        st, _, pl, _ = call("GET", "/api/orders", params={"status": value} if value in ctx["status_enum_values"]
                             else {"dateScope": value})
        enum_probe.append({"value": value, "status": st})
    unknown_status = call("GET", "/api/orders", params={"status": "待确认"})[0]
    sc.append({
        "id": "SC06_ENUM_RANGE",
        "title": "订单状态 / 日期范围枚举：前端取值 vs 后端取值 vs DB CHECK",
        "expectation": "前端枚举的每个取值后端都必须认",
        "actual_status": None,
        "probes": enum_probe,
        "chinese_status_status_code": unknown_status,
        "verdict": "PASS" if all(p["status"] == 200 for p in enum_probe) else "FAIL",
        "evidence": {},
    })

    return sc


# --------------------------------------------------------------------------
# 4) 前端读取字段 vs 实际响应字段
# --------------------------------------------------------------------------
def check_response_fields(decl, calls):
    """前端 mapper 读取的字段，在真实响应里是否都存在。"""
    out = {}
    by_key = {c["key"]: c for c in calls}
    pairs = [
        ("mapProduct", "GET /api/products"),
        ("mapPackage", "GET /api/packages"),
        ("mapOrder", "GET /api/orders"),
    ]
    for mapper, key in pairs:
        m = decl["frontend"]["mappers"].get(mapper)
        c = by_key.get(key)
        if not m or not c:
            continue
        item_keys = set(c["shape"].get("item_keys") or [])
        missing = sorted(f for f in m["direct_fields"] if f not in item_keys)
        out[mapper] = {
            "source_call": key,
            "expected_fields": m["direct_fields"],
            "actual_item_keys": sorted(item_keys),
            "missing": missing,
            "verdict": "PASS" if not missing else "FAIL",
        }
    return out


def main() -> None:
    decl = json.load(open(DECL, encoding="utf-8"))
    ts = datetime.datetime.now().strftime("%H%M%S")

    status, _, payload, _ = call("GET", "/api/health")
    if status != 200:
        raise SystemExit(f"后端不可用: {status} {BASE}")

    cats = data_of(call("GET", "/api/categories")[2]) or []
    ptypes = data_of(call("GET", "/api/package-types")[2]) or []
    prods = data_of(call("GET", "/api/products", params={"page": 1, "pageSize": 5})[2]) or {}
    pkgs = data_of(call("GET", "/api/packages", params={"page": 1, "pageSize": 5})[2]) or {}
    orders = data_of(call("GET", "/api/orders", params={"page": 1, "pageSize": 5})[2]) or {}

    prod_items = prods.get("items") or []
    pkg_items = pkgs.get("items") or []
    ctx = {
        "ts": ts,
        "category_id": (cats[0] or {}).get("id") if cats else None,
        "package_type_id": (ptypes[0] or {}).get("id") if ptypes else None,
        "product_id": (prod_items[0] or {}).get("id") if prod_items else None,
        "other_product_id": (prod_items[1] or {}).get("id") if len(prod_items) > 1 else None,
        "package_id": (pkg_items[0] or {}).get("id") if pkg_items else None,
        "package_price_cent": (pkg_items[0] or {}).get("priceCent") if pkg_items else None,
        "package_item_product_id": ((pkg_items[0] or {}).get("items") or [{}])[0].get("productId") if pkg_items else None,
        "order_id": ((orders.get("items") or [{}])[0] or {}).get("id") if orders.get("items") else None,
        "status_enum_values": decl["frontend"]["enums"].get("statusToApi", {}).get("values", []),
        "date_scope_values": decl["frontend"]["enums"].get("dateScopeToApi", {}).get("values", []),
    }
    if not ctx["package_item_product_id"]:
        ctx["package_item_product_id"] = ctx["product_id"]

    calls, created, upload_url = run_frontend_calls(ctx)
    sc = run_scenarios(ctx)
    replay = replay_recordings(decl["recordings"]["raw"])
    field_check = check_response_fields(decl, calls)

    obs = {
        "generated_at": datetime.datetime.now().isoformat(),
        "base_url": BASE,
        "ctx": ctx,
        "frontend_calls": calls,
        "recording_replay": replay,
        "scenarios": sc,
        "response_field_check": field_check,
        "created_upload_files": [upload_url] if upload_url else [],
    }
    json.dump(obs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"集权层实测 -> {OUT}")
    print(f"  前端端点实调 {len(calls)} 次")
    for c in calls:
        flag = "" if c["status"] < 400 else f"  <-- {c['status']}"
        print(f"    {c['status']:3} {c['method']:6} {c['key']:32} {c['latency_ms']:>4}ms{flag}")
    drift = [r for r in replay if r["drift"]]
    print(f"  录制流量回放 {len(replay)} 条, 漂移 {len(drift)} 条")
    for r in drift:
        print(f"    漂移 {r['method']} {r['path']}?{r['query']} 录制={r['recorded_status']} 现在={r['current_status']}")
    print("  业务场景：")
    for s in sc:
        print(f"    {s['verdict']:4} {s['id']:34} status={s.get('actual_status')}")
    print("  前端字段核对：")
    for k, v in field_check.items():
        print(f"    {v['verdict']:4} {k:12} 缺失字段={v['missing'] or '无'}")


if __name__ == "__main__":
    main()
