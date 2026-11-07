"""动态层：真实执行每个 endpoint 的多种场景，记录实测行为。
所有写操作都只作用在 ai_probe_ 前缀的临时实体上，跑完即清理，不碰种子数据。
"""
import json, os, time, uuid, datetime, requests, copy

D = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
S = requests.Session()
MARK = "ai_probe_"

def load(name):
    return json.load(open(os.path.join(D, name)))

def resolve(spec, schemas):
    if isinstance(spec, dict) and "$ref" in spec:
        return schemas.get(spec["$ref"].split("/")[-1], {})
    return spec or {}

def gen(spec, schemas, ctx, depth=0):
    """按 OpenAPI schema 生成一个"合法"的值。"""
    if depth > 6:
        return None
    s = resolve(spec, schemas)
    if "enum" in s:
        return s["enum"][0]
    t = s.get("type")
    if t == "object" or "properties" in s:
        out = {}
        for k in (s.get("required") or s.get("properties", {}).keys()):
            out[k] = gen(s["properties"][k], schemas, ctx, depth + 1)
        return out
    if t == "array":
        return []
    if t == "boolean":
        return True
    if t in ("integer", "number"):
        return ctx.get("id", 1)
    if t == "string":
        if s.get("format") == "date-time":
            return (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=30)).isoformat()
        return f"{MARK}str"
    return None

def build_body(schema_name, schemas, ctx):
    s = schemas.get(schema_name, {})
    out = {}
    for k in (s.get("required") or []):
        out[k] = gen(s["properties"][k], schemas, ctx)
    # id 类字段用真实 id
    for k, v in list(out.items()):
        if k in ctx and isinstance(v, (int, float)):
            out[k] = ctx[k]
    out["customerName"] = MARK + "probe"
    out["customerPhone"] = "13800000001"
    out["banquetName"] = MARK + "probe"
    return out

def call(method, path, body=None, headers=None):
    h = dict(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    t0 = time.perf_counter()
    r = S.request(method, BASE + path, json=body, headers=h, timeout=20)
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = r.json()
    except Exception:
        j = None
    return {"status": r.status_code, "latency_ms": round(ms, 1),
            "content_type": r.headers.get("Content-Type", ""),
            "json": j}

def brief(obs):
    j = obs["json"]
    if isinstance(j, dict):
        obs["body_keys"] = sorted(j.keys())
        obs["code"] = j.get("code")
        data = j.get("data")
        obs["data_keys"] = sorted(data.keys()) if isinstance(data, dict) else None
        obs["envelope_ok"] = all(k in j for k in ("success", "code", "message"))
        obs["data_type"] = type(data).__name__
    else:
        obs["body_keys"] = None
        obs["envelope_ok"] = False
    return obs

def fixtures(schemas):
    """创建临时实体，供 PUT/PATCH/DELETE 场景使用。"""
    ctx = {}
    def post(p, b):
        r = S.post(BASE + p, json=b, timeout=15).json()
        return r.get("data", {}) if r.get("success") else {}
    cat = post("/api/categories", {"name": MARK + "cat_" + uuid.uuid4().hex[:6]})
    ctx["categoryId"] = cat.get("id")
    ptype = post("/api/package-types", {"name": MARK + "pt_" + uuid.uuid4().hex[:6]})
    ctx["packageTypeId"] = ptype.get("id")
    prod = post("/api/products", {"categoryId": ctx["categoryId"], "name": MARK + "prod_" + uuid.uuid4().hex[:6],
                                  "priceCent": 1, "unit": "份"})
    ctx["productId"] = prod.get("id")
    pkg = post("/api/packages", {"name": MARK + "pkg_" + uuid.uuid4().hex[:6],
                                 "banquetType": "婚宴", "priceCent": 1,
                                 "items": [{"productId": ctx["productId"], "quantity": 1}]})
    ctx["packageId"] = pkg.get("id")
    order = post("/api/orders", {"customerName": MARK + "probe", "customerPhone": "13800000001",
                                 "banquetName": MARK + "probe", "banquetType": "婚宴",
                                 "reservationTime": (datetime.datetime.now(datetime.timezone.utc)
                                     + datetime.timedelta(days=30)).isoformat(),
                                 "tableCount": 1, "packageId": ctx["packageId"],
                                 "planTotalCent": 1, "useFinalPrice": True, "finalPriceCent": 1,
                                 "depositCent": 0, "status": "PENDING"})
    ctx["orderId"] = order.get("id")
    ctx["_ids"] = {"category": cat.get("id"), "packageType": ptype.get("id"),
                   "product": prod.get("id"), "package": pkg.get("id"), "order": order.get("id")}
    return ctx

def upload_scenario(op, path):
    """上传类 endpoint：合法 multipart + 缺失 part。"""
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    out = []
    t0 = time.perf_counter()
    r = S.post(BASE + path, files={"file": ("probe.png", png, "image/png")}, timeout=20)
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = r.json()
    except Exception:
        j = None
    o = brief({"status": r.status_code, "latency_ms": round(ms, 1),
               "content_type": r.headers.get("Content-Type", ""), "json": j})
    o.update({"key": op["key"], "scenario": "valid_multipart", "request": path})
    if j and j.get("data", {}).get("imageUrl"):
        o["_created_file"] = j["data"]["imageUrl"]
    out.append(o)
    # 缺失 part
    r2 = S.post(BASE + path, json={"file": "x"}, timeout=20)
    try:
        j2 = r2.json()
    except Exception:
        j2 = None
    o2 = brief({"status": r2.status_code, "latency_ms": 0, "content_type": "", "json": j2})
    o2.update({"key": op["key"], "scenario": "missing_multipart_part", "request": path})
    out.append(o2)
    return out

def main():
    decl = load("declaration.json")
    schemas = load("openapi.json")["components"]["schemas"]
    ctx = fixtures(schemas)
    print("临时实体:", ctx["_ids"])

    obs = []
    for op in decl["operations"]:
        method, path = op["method"], op["path"]
        body = build_body(op["request_schema"], schemas, ctx) if op["request_schema"] else None

        # 场景1: 合法请求（带一个伪造 Authorization，验证鉴权是否存在）
        p = path
        if "{id}" in p:
            res = next((r for r in ("orders", "products", "packages", "categories", "package-types")
                        if "/" + r in p), "products")
            p = p.replace("{id}", str(ctx["_ids"].get(res)))
        if "/uploads/" in p:
            obs.extend(upload_scenario(op, p))
            continue
        if "/uploads/" in p:
            continue
        r = brief(call(method, p, body, {"Authorization": "Bearer not-a-real-token"}))
        r.update({"key": op["key"], "scenario": "valid", "request": p})
        obs.append(r)

        # 场景2: 缺必填字段
        if body:
            b2 = copy.deepcopy(body)
            k = op["request_required"][0] if op["request_required"] else None
            if k:
                b2.pop(k, None)
                r = brief(call(method, p, b2))
                r.update({"key": op["key"], "scenario": "missing_required:" + k, "request": p})
                obs.append(r)
            # 场景3: 类型错误
            b3 = copy.deepcopy(body)
            k2 = op["request_required"][0] if op["request_required"] else None
            if k2:
                cur = b3.get(k2)
                if isinstance(cur, bool):
                    b3[k2] = "not-a-boolean"
                elif isinstance(cur, (int, float)):
                    b3[k2] = "not-a-number"
                elif isinstance(cur, str):
                    b3[k2] = 12345
                else:
                    b3[k2] = "not-a-valid-type"
                r = brief(call(method, p, b3))
                r.update({"key": op["key"], "scenario": "wrong_type:" + k2, "request": p})
                obs.append(r)

        # 场景4: 资源不存在
        if "{id}" in path:
            p404 = path.replace("{id}", "99999999")
            r = brief(call(method, p404, body))
            r.update({"key": op["key"], "scenario": "not_found", "request": p404})
            obs.append(r)

    json.dump({"ctx": ctx, "observations": obs}, open(os.path.join(D, "observation.json"), "w"),
              ensure_ascii=False, indent=2)
    print(f"实测 {len(obs)} 次 -> observation.json")
    from collections import Counter
    print("  场景分布:", dict(Counter(o["scenario"].split(":")[0] for o in obs)))
    print("  观察到的状态码:", dict(Counter(o["status"] for o in obs)))

if __name__ == "__main__":
    main()
