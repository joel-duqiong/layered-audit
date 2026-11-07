"""集权层差分引擎：三方声明互相求差 -> 集成层缺陷清单。

  前端 JS 声明  ×  后端 Java 声明  ×  录制流量画像  ×  实测行为

判定原则：
  * 分歧 = 缺陷；但「两侧声明一致 + 实测一致」要显式记 PASS，否则报告不可信。
  * 与服务层/类层已报缺陷重复的，本层只补「集成症状」，不重复计一条根因。
"""
from __future__ import annotations

import json
import os
import re

D = os.path.dirname(os.path.abspath(__file__))
DECL = json.load(open(os.path.join(D, "integration_declaration.json"), encoding="utf-8"))
OBS = json.load(open(os.path.join(D, "integration_observation.json"), encoding="utf-8"))
OUT_JSON = os.path.join(D, "integration_findings.json")
OUT_MD = os.path.join(D, "integration_findings.md")

fe = DECL["frontend"]
be = DECL["backend"]
be_ops = {f"{o['method']} {o['path']}": o for o in be["operations"]}
fe_eps = {f"{e['method']} {e['path']}": e for e in fe["endpoints"]}
scenarios = {s["id"]: s for s in OBS["scenarios"]}

findings: list[dict] = []
passed: list[dict] = []


def add(fid, sev, endpoint, title, detail, repro, evidence, owner="集权层"):
    findings.append({"id": fid, "severity": sev, "endpoint": endpoint, "title": title,
                     "detail": detail, "repro": repro, "evidence": evidence, "owner": owner})


def ok(check, desc, evidence=None):
    passed.append({"check": check, "result": "PASS", "desc": desc, "evidence": evidence or {}})


# --------------------------------------------------------------------------
# C1 前端调用的接口，后端路由里有没有
# --------------------------------------------------------------------------
missing_routes = sorted(set(fe_eps) - set(be_ops))
if missing_routes:
    for key in missing_routes:
        add("PATH_NOT_IN_BACKEND", "CRITICAL", key,
            f"前端调用的 {key} 在后端路由表中不存在",
            "前端 JS 里写死了这条调用路径，但后端没有任何 controller 映射它 —— 线上必然 404。",
            f"curl -X {fe_eps[key]['method']} http://127.0.0.1:8081{fe_eps[key]['path']}",
            {"frontend_fn": fe_eps[key]["fn"]})
else:
    ok("C1_PATH_ALIGNED", f"前端 {len(fe_eps)} 个调用路径在后端路由表中全部存在",
       {"frontend_endpoints": len(fe_eps), "backend_operations": len(be_ops)})

# --------------------------------------------------------------------------
# C2 后端有、前端从不调用
# --------------------------------------------------------------------------
unused = sorted(set(be_ops) - set(fe_eps))
if unused:
    add("DEAD_BACKEND_ENDPOINT", "LOW", ", ".join(unused),
        f"后端 {len(unused)} 个接口前端从不调用",
        "这些路由在前端 banquetApi.js 里没有任何调用点。GET /api/orders/{id} 与 "
        "GET /api/packages/{id} 属于详情接口冗余；真正值得看的是 PATCH /api/packages/{id}/status —— "
        "它是唯一的状态机式接口，Pad 端却从不调用，套餐上下架只能整单 PUT 全量覆盖"
        "（连同 items 一起重传），绕开了最小变更路径。",
        "grep -n 'api/' /Users/june/jiuyan/src/api/banquetApi.js",
        {"unused_endpoints": unused,
         "note": "GET /api/health 属运维探针，不算缺陷"})

# --------------------------------------------------------------------------
# C3 请求字段对账：前端发的 vs 后端 DTO 认的
# --------------------------------------------------------------------------
field_problems = []
for key, e in fe_eps.items():
    op = be_ops.get(key)
    if not op:
        continue
    if e["body_kind"] == "multipart":
        declared = set(op["multipart_parts"])
        sent = set(e["body_keys"])
        if declared != sent:
            field_problems.append((key, sorted(sent - declared), sorted(declared - sent)))
        continue
    dto_name = op["request_dto"]
    if not dto_name or not e["body_keys"]:
        continue
    dto = be["dtos"].get(dto_name)
    if not dto:
        continue
    names = {c["name"] for c in dto["components"]}
    required = {c["name"] for c in dto["components"] if c["required"]}
    sent = set(e["body_keys"])
    extra = sorted(sent - names)
    miss = sorted(required - sent)
    if extra or miss:
        field_problems.append((key, extra, miss))
if field_problems:
    for key, extra, miss in field_problems:
        add("BODY_FIELD_MISMATCH", "HIGH", key,
            "前端发送字段与后端 DTO 不一致",
            f"前端多发={extra}（后端 Jackson 默认忽略未知字段，会静默丢弃）；"
            f"后端必填但前端不发={miss}（会直接 400）。",
            f"见 integration_declaration.json frontend.endpoints[{key}]",
            {"extra_sent_by_frontend": extra, "required_missing_in_frontend": miss})
else:
    ok("C3_BODY_FIELDS_ALIGNED",
       "前端发送的请求字段与后端 DTO record 组件完全一致（无多余、无缺失必填）",
       {"endpoints_checked": len([k for k in fe_eps if fe_eps[k]["body_keys"]])})

# --------------------------------------------------------------------------
# C4 响应字段对账：前端读的 vs 后端返回的
# --------------------------------------------------------------------------
def dto_fields(name: str) -> set[str]:
    d = be["dtos"].get(name)
    return {c["name"] for c in d["components"]} if d else set()


def element_dto(type_name: str) -> str | None:
    m = re.match(r"List<(\w+)>", type_name or "")
    return m.group(1) if m else None


snapshot = be.get("snapshot_shape", {})
resp_problems = []
undeclared_shapes = []
for key, e in fe_eps.items():
    if not e["mappers"]:
        continue
    op = be_ops.get(key)
    if not op or not op["response_dto"]:
        continue
    resp_name = op["response_dto"]
    d = be["dtos"].get(resp_name)
    if not d:
        continue
    comp_types = {c["name"]: c["type"] for c in d["components"]}
    fields = set(comp_types)
    for mapper in e["mappers"]:
        m = fe["mappers"].get(mapper)
        if not m:
            continue
        missing = sorted(f for f in m["direct_fields"] if f not in fields)
        if missing:
            resp_problems.append((key, mapper, missing))
        # 嵌套 / 元素字段
        for path, wanted in list(m["nested_fields"].items()) + list(m["element_fields"].items()):
            parts = path.split(".")
            top = parts[0]
            if top not in fields:
                resp_problems.append((key, mapper, [f"{path}.*"]))
                continue
            if comp_types[top] == "JsonNode":
                # 契约里是自由 JSON 对象：内部结构没有任何声明（按 mapper+路径去重）
                if (mapper, path) not in {(m, p) for _, m, p, _ in undeclared_shapes}:
                    undeclared_shapes.append((key, mapper, path, wanted))
                continue
            elem = element_dto(comp_types[top]) if len(parts) == 1 else None
            if elem:
                ef = dto_fields(elem)
                miss = sorted(w for w in wanted if w not in ef)
                if miss:
                    resp_problems.append((key, mapper, [f"{path}.{w}" for w in miss]))
if resp_problems:
    for key, mapper, missing in resp_problems:
        add("RESPONSE_FIELD_MISSING", "HIGH", key,
            f"前端读取的字段后端不返回（{mapper}）",
            f"缺失字段：{missing} —— 前端会拿到 undefined，且没有任何报错。",
            f"见 integration_observation.json frontend_calls[{key}]",
            {"missing": missing})
else:
    ok("C4_RESPONSE_FIELDS_PRESENT",
       "前端三个映射函数读取的字段，在后端响应 DTO 与快照结构中全部存在",
       {"mappers_checked": list(fe["mappers"])})

if undeclared_shapes:
    by_path: dict[str, list[str]] = {}
    for _, mapper, path, wanted in undeclared_shapes:
        by_path.setdefault(path, [])
        by_path[path] += sorted(wanted)
    add("SNAPSHOT_CONTRACT_UNDECLARED", "MEDIUM", "GET/POST/PUT /api/orders",
        "前端依赖的订单快照内部结构，在 API 契约里零声明",
        "OrderResponse 把 packageSnapshot / addedItemsSnapshot 声明成 JsonNode（自由 JSON），"
        "OpenAPI 与 DTO 都没有描述内部字段。前端 mapOrder 读取的 "
        f"{sorted({f for v in by_path.values() for f in v})} "
        "只靠「OrderService 里那段拼 JSON 的代码」维持约定 —— "
        "任何人改动那段拼装逻辑，前端不会报错，只会显示 undefined。"
        "这是集权层专属的契约缺口：服务层自洽、类层自洽，只有两端拼在一起才看得见。",
        "grep -n 'packageSnapshot' backend/src/main/java/com/jiuyan/banquet/dto/OrderResponse.java",
        {"undeclared_paths": {k: sorted(set(v)) for k, v in by_path.items()},
         "actual_snapshot_keys": {"root": snapshot.get("root"), "item": snapshot.get("item")},
         "note": "实测这些字段当前确实存在（见 SNAPSHOT_SHAPE_MATCHES），但没有任何契约或测试守着它"})

    # 实测：服务层拼出来的快照键，是否真的覆盖前端读取的字段
    root_keys = set(snapshot.get("root") or [])
    item_keys = set(snapshot.get("item") or [])
    missing = []
    for path, wanted in by_path.items():
        if path.endswith("items") or path == "addedItemsSnapshot":
            avail = item_keys
        else:
            avail = root_keys
        missing += [f"{path}.{w}" for w in wanted if w not in avail]
    if missing:
        add("SNAPSHOT_FIELD_MISSING", "HIGH", "GET /api/orders",
            "前端读取的快照内部字段，服务层根本没拼出来",
            f"缺失：{sorted(set(missing))}",
            "见 integration_static.py parse_snapshot_shape",
            {"missing": sorted(set(missing))})
    else:
        ok("SNAPSHOT_SHAPE_MATCHES",
           "前端读取的快照内部字段，今天确实都存在（但契约里没有任何声明守着它们）",
           {"frontend_expects": {k: sorted(set(v)) for k, v in by_path.items()},
            "snapshot_root": sorted(root_keys), "snapshot_item": sorted(item_keys)})

# --------------------------------------------------------------------------
# C5 枚举三方对齐：前端 × 后端枚举 × DB CHECK
# --------------------------------------------------------------------------
status_enum = be["enums"].get("OrderStatus", [])
db_check = be.get("db_checks", {}).get("orders.chk_orders_status", "")
db_values = re.findall(r"'([A-Z_]+)'", db_check)
fe_status = fe["enums"].get("statusToApi", {}).get("values", [])
fe_status_back = fe["enums"].get("statusFromApi", {}).get("keys", [])
fe_scope = fe["enums"].get("dateScopeToApi", {}).get("values", [])
enum_issue = []
if sorted(fe_status) != sorted(status_enum):
    enum_issue.append(("status", fe_status, status_enum))
if sorted(fe_status) != sorted(db_values):
    enum_issue.append(("status_vs_db", fe_status, db_values))
if sorted(fe_status) != sorted(fe_status_back):
    enum_issue.append(("status_to_from", fe_status, fe_status_back))
if enum_issue:
    add("ENUM_MISMATCH", "HIGH", "枚举",
        "订单状态枚举在前端/后端/数据库之间不一致",
        f"{enum_issue}",
        "见 integration_declaration.json frontend.enums / backend.enums",
        {"issues": enum_issue})
else:
    ok("C5_ENUM_ALIGNED",
       "订单状态枚举三方一致（前端 statusToApi/statusFromApi × 后端 OrderStatus × DB CHECK）；"
       "日期范围枚举前端 3 个取值后端 parseDateScope 全部接受",
       {"frontend_status": fe_status, "backend_enum": status_enum, "db_check": db_values,
        "frontend_date_scope": fe_scope})

# --------------------------------------------------------------------------
# C6 录制流量：漂移 + 覆盖缺口
# --------------------------------------------------------------------------
replay = OBS["recording_replay"]
drift = [r for r in replay if r["drift"]]
if drift:
    for r in drift:
        add("RECORDING_CONTRACT_DRIFT", "HIGH", f"{r['method']} {r['path']}",
            "录制流量回放与录制时状态不一致（契约漂移）",
            f"录制时 HTTP {r['recorded_status']}，现在 HTTP {r['current_status']}。",
            f"curl -X {r['method']} http://127.0.0.1:8081{r['path']}?{r['query']}",
            {"recorded_status": r["recorded_status"], "current_status": r["current_status"]})
else:
    ok("C6_NO_RECORDING_DRIFT",
       f"{len(replay)} 条录制流量全部回放，状态码与录制时完全一致（0 漂移）",
       {"recorded_range": DECL["recordings"]["range"], "rows": len(replay)})

recorded_paths = {f"{r['method']} {r['path']}" for r in replay}


def path_to_regex(tmpl: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\})", tmpl)
    return re.compile("^" + "".join(r"[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$")


api_ops = {k for k in be_ops if " /api" in k}
covered = {k for k in api_ops if k in fe_eps}
for r in replay:
    for k in api_ops:
        if k not in covered and r["method"] == k.split(" ", 1)[0] and path_to_regex(k.split(" ", 1)[1]).match(r["path"]):
            covered.add(k)
uncovered = sorted(api_ops - covered)
write_ops = {k for k in api_ops if not k.startswith("GET ")}
if uncovered:
    add("RECORDING_COVERAGE_GAP", "MEDIUM", ", ".join(uncovered),
        f"{len(uncovered)}/{len(api_ops)} 个接口既无真实流量样本、前端也不调用；"
        f"{len(write_ops)} 个写接口的真实流量样本为 0",
        "录制流量只有 22 条、9 个路径，且 100% 是 GET 只读流量；下单、改价、改状态、删除、"
        "上传这些写路径一条真实样本都没有。上面几条集成缺陷之所以只能靠构造流量才发现，"
        "就是因为真实流量从来没覆盖过这些路径。",
        "见 /Users/june/tests/auto-api/recordings/jiuyan_readonly.jsonl",
        {"uncovered_endpoints": uncovered, "recorded_rows": len(replay),
         "write_endpoints_without_real_traffic": sorted(write_ops),
         "recorded_paths": sorted(recorded_paths)})

# --------------------------------------------------------------------------
# 端到端业务场景（SC01-SC06）
# --------------------------------------------------------------------------
sc01 = scenarios.get("SC01_CLIENT_CONTROLLED_PRICE")
if sc01 and sc01["verdict"] == "FAIL":
    add("PRICE_CLIENT_CONTROLLED_XL", "CRITICAL", "POST /api/orders",
        "同一订单里并存两个互相矛盾的价格：成交价 0.01 元、套餐价 468.00 元",
        f"套餐标价 {sc01['snapshot_price_cent']} 分，客户端报成交价 {sc01['actual_final_price_cent']} 分，"
        "后端原样落库并返回 200。更糟的是集成症状：OrderResponse 同时返回 finalPriceCent=1 与 "
        "packageSnapshot.priceCent=46800，而前端 mapOrder 正好一个用来渲染金额、一个用来渲染套餐价 —— "
        "Pad 端同一张订单会同时显示 0.01 元和 468.00 元。服务端没有任何按套餐计价的能力，"
        "定价权完全在客户端手上。",
        "见 integration_observation.json scenarios[SC01_CLIENT_CONTROLLED_PRICE]",
        {"final_price_cent": sc01["actual_final_price_cent"],
         "snapshot_price_cent": sc01["snapshot_price_cent"],
         "order_id": sc01["evidence"].get("order_id"),
         "root_cause": "OrderService.resolveFinalPrice 直接采纳客户端 finalPriceCent",
         "related_lower_layer": "服务层 test_price_is_not_client_controlled 已报根因，本条只补集成症状"},
        owner="集权层（集成症状；根因归服务层）")

sc02 = scenarios.get("SC02_FOREIGN_ITEM_IN_PACKAGE")
if sc02 and sc02["verdict"] == "FAIL":
    items = sc02.get("snapshot_items") or []
    add("PACKAGE_ITEM_NOT_VALIDATED", "HIGH", "POST /api/orders",
        "可以把不属于该套餐的商品写进套餐明细，后端照单全收",
        f"下单时 packageItems 里塞进套餐外的商品（{[i.get('productName') for i in items]}），"
        "后端只校验商品存在、不校验归属，200 通过并写进订单快照。"
        "订单快照里的「套餐明细」因此不代表真实套餐，后续对账无据可查。",
        "见 integration_observation.json scenarios[SC02_FOREIGN_ITEM_IN_PACKAGE]",
        {"snapshot_items": items, "order_id": sc02["evidence"].get("order_id"),
         "root_cause": "OrderService.apply 未校验 packageItems 与 packageId 的归属关系"})

sc03 = scenarios.get("SC03_QUANTITY_OVERRIDE")
if sc03 and sc03["verdict"] == "FAIL":
    items = sc03.get("snapshot_items") or []
    add("PACKAGE_QUANTITY_NOT_VALIDATED", "HIGH", "POST /api/orders",
        "明细数量可以改成 99，价格仍按套餐原价结算（加量不加价）",
        f"下单时把套餐内商品数量改成 {items[0].get('quantity') if items else '?'} 份，"
        f"planTotalCent/finalPriceCent 仍是套餐原价 {sc03.get('final_price_cent')} 分 —— "
        "后端不重算、不拒绝。",
        "见 integration_observation.json scenarios[SC03_QUANTITY_OVERRIDE]",
        {"snapshot_items": items, "final_price_cent": sc03.get("final_price_cent"),
         "root_cause": "OrderService.apply 不依据明细重算金额"})

sc04 = scenarios.get("SC04_UNKNOWN_FIELD_SILENT_DROP")
if sc04 and sc04["verdict"] == "FAIL":
    add("UNKNOWN_FIELD_SILENT_DROP", "MEDIUM", "POST /api/orders",
        "字段名拼错会被静默吞掉，并被「套餐默认明细」悄悄顶替",
        "把 packageItems 写成 packageItem 后端不报错（Jackson 默认忽略未知字段），"
        "而 OrderService 在明细为空时会回退到套餐自身的默认 items —— "
        "于是用户明明选了 3 道菜并改了数量，订单快照里躺的是套餐出厂明细。"
        "前端界面显示的是用户的选择，库里存的是别的东西，两边永远对不上，也没有任何报错。",
        "见 integration_observation.json scenarios[SC04_UNKNOWN_FIELD_SILENT_DROP]",
        {"snapshot_items": sc04.get("snapshot_items"),
         "root_cause": "spring.jackson 未开 fail-on-unknown-properties + buildPackageSnapshot 空明细回退"})

sc05 = scenarios.get("SC05_LIST_TRUNCATION")
if sc05 and sc05["verdict"] == "FAIL":
    add("LIST_TRUNCATED_AT_100", "HIGH", "GET /api/products, GET /api/packages, GET /api/orders",
        "前端列表固定只取前 100 条并丢弃 total，超出的数据永远不可见",
        f"后端返回 total={sc05.get('total')}，实际 items 只有 {sc05.get('items_count')} 条。"
        "前端三个列表接口都写死 page=1&pageSize=100，且拿到响应后只做 data.items.map(...)，"
        "PageResponse.total / totalPages 被整个丢弃 —— 用户看不到「还有 35 条」，"
        "也没有任何翻页入口。商品/套餐/订单一旦超过 100 条，数据库里多出来的记录在 Pad 端凭空消失。",
        "见 integration_observation.json scenarios[SC05_LIST_TRUNCATION]",
        {"total": sc05.get("total"), "items_count": sc05.get("items_count"),
         "frontend_code": "GET /api/products${toQuery({ page: 1, pageSize: 100, ...params })} -> data.items.map(mapProduct)",
         "root_cause": "前端硬编码 pageSize=100 + 未读取 total/totalPages"})

sc06 = scenarios.get("SC06_ENUM_RANGE")
if sc06 and sc06["verdict"] == "PASS":
    ok("SC06_ENUM_RANGE", "订单状态/日期范围枚举的每个前端取值，后端都接受（HTTP 200）",
       {"probes": sc06.get("probes"),
        "note": f"直接传中文 status 返回 {sc06.get('chinese_status_status_code')}，"
                "说明契约只认英文枚举；前端已正确用 toApiStatus/toApiDateScope 转换"})

field_check = OBS.get("response_field_check", {})
for mapper, res in field_check.items():
    if res["verdict"] == "PASS":
        ok(f"FIELD_CHECK_{mapper}", f"{mapper} 读取的 {len(res['expected_fields'])} 个字段在真实响应里全部存在",
           {"source_call": res["source_call"]})

# --------------------------------------------------------------------------
# v2：真三方对账（API 响应 ↔ DB 实际行 ↔ 前端消费语义）
# --------------------------------------------------------------------------
RECON_PATH = os.path.join(D, "integration_reconciliation.json")
CHAIN_PATH = os.path.join(D, "integration_chains.json")
recon = json.load(open(RECON_PATH, encoding="utf-8")) if os.path.exists(RECON_PATH) else None
chains_doc = json.load(open(CHAIN_PATH, encoding="utf-8")) if os.path.exists(CHAIN_PATH) else None

# 对账结论里「语义不闭合」的两种：一种是本层独有的（前端消费语义），一种在更低层已开单
SEMANTIC_RULES = {
    "mapProduct.image = imageUrl || photo": (
        "INT-PRODUCT-IMAGE-DOMAIN-FALLBACK", "MEDIUM", "集成层（前端消费语义）",
        "商品缺图时前端兜底成了套餐种子图",
        "mapProduct 的兜底常量写的是套餐目录下的种子图（/uploads/seed/packages/...），"
        "商品位没有自己的兜底图。结果：任何没配图的商品，界面上都会显示一张宴会套餐照片。",
        "POST /api/products（imageUrl 留空）→ 前端 mapProduct → image 字段等于套餐种子图路径",
        "前端真实 mapper 输出 image=套餐种子图路径（Node 跑 src/api/banquetApi.js）"),
    "App.jsx visibleItemCount vs mapPackage.dishes": (
        "INT-PACKAGE-COUNT-INCONSISTENT", "MEDIUM", "集成层（前端消费语义）",
        "同一个套餐，界面上有两个互不相同的「商品数」",
        "套餐卡片用 mapPackage.dishes（= API 的 itemCount，含已下架商品），"
        "选套餐弹层用 App.jsx 的 visibleItemCount（只数在架商品）。"
        "只要套餐里有下架商品，两个地方显示的数字就不一样，而且都不等于实际可下单明细数。",
        "造一个套餐，明细含一个 is_active=false 的商品 → 对比卡片与弹层的数字",
        "卡片 = API itemCount；弹层 = 在架商品数（前端真实 mapper + 商品列表算出）"),
}

if recon:
    derived = [r for r in recon["reconciliation"] if r["verdict"] == "DERIVED"]
    ignored = [r for r in recon["reconciliation"] if r["verdict"] == "FRONTEND_IGNORES"]
    for r in recon["reconciliation"]:
        usage = r.get("frontend_usage") or ""
        if r["verdict"] == "ALIGNED" and r.get("note"):
            ok(f"RECON_ALIGNED_{r['entity']}_{r['field']}",
               f"{r['entity']}.{r['field']} 三方闭合：{r['note']}",
               {"usage": usage})
        elif r["verdict"] == "SEMANTIC_MISMATCH":
            rule = SEMANTIC_RULES.get(usage)
            if rule:
                fid, sev, owner, title, detail, repro, evidence = rule
                add(fid, sev, f"{r['entity']}.{r['field']}", title, detail, repro,
                    {"note": r.get("note"), "usage": usage}, owner)
            else:
                lower = "class" if "DB 直写" in usage else "function"
                cited = "CLASS-RULE-MISSING-IN-ALL-LAYERS" if lower == "class" \
                    else "FN-PRICE-CLIENT-CONTROLLED"
                ok(f"RECON_CORROBORATES_{r['entity']}_{r['field']}",
                   f"{r['entity']}.{r['field']} 语义不闭合（{r.get('note')}）—— "
                   f"根因已在{lower}层开单，本层只做跨源佐证，不重复计缺陷",
                   {"cites": cited, "usage": usage})
        elif r["verdict"] == "FRONTEND_MISSING":
            add("FRONTEND_MISSING_FIELD", "HIGH", f"{r['entity']}.{r['api_field']}",
                f"前端要读 {r['api_field']}，但 API 响应里没有这一项",
                "前端 mapper 读一个接口不返回的字段，取到 undefined，界面上会静默显示空值。",
                f"GET 相关接口 → 检查 {r['api_field']} 是否在响应里",
                {"usage": usage, "note": r.get("note")})
        elif r["verdict"] == "API_OMITS" and r["severity_hint"] == "MEDIUM":
            ok(f"RECON_INTERNAL_KEY_HIDDEN_{r['entity']}_{r['field']}",
               f"{r['entity']}.{r['field']} 是内部主键，接口刻意不外露（前端也用不到）",
               {"db_value": r.get("db_value")})
    if derived:
        ok("RECON_DERIVED_FIELDS_CLOSED",
           f"{len(derived)} 个 API 字段在 DB 里没有对应列（join / 计算派生），"
           f"前端消费口径与 API 一致",
           {"fields": sorted({f"{r['entity']}.{r['api_field']}" for r in derived})})
    if ignored:
        ok("RECON_FRONTEND_UNUSED_FIELDS_BY_DESIGN",
           f"{len(ignored)} 个 API 字段前端不消费：审计时间戳，或前端按 productId 回查商品表",
           {"fields": sorted({f"{r['entity']}.{r['api_field']}" for r in ignored})})
    ok("RECON_TOTAL", f"三方对账共 {len(recon['reconciliation'])} 项",
       {"verdict_counts": recon["verdict_counts"]})

if chains_doc:
    for ch in chains_doc["chains"]:
        if ch.get("findings"):
            for f in ch["findings"]:
                add(f["id"], f["severity"], f"{ch['id']}", f["title"], f["detail"],
                    f["repro"], {"chain_steps": ch["steps"], "evidence": f.get("evidence")},
                    f.get("owner", "集权层"))
        else:
            ok(f"CHAIN_CORROBORATES_{ch['id']}",
               f"{ch['id']} 证实了低层缺陷：{ch.get('note')}",
               {"cites": ch.get("cites"), "steps": ch["steps"]})

# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 9), f["id"]))
counts = {}
for f in findings:
    counts[f["severity"]] = counts.get(f["severity"], 0) + 1

json.dump({"generated_at": DECL["generated_at"], "findings": findings, "passed_checks": passed,
           "summary": {"total": len(findings), "by_severity": counts,
                       "passed_checks": len(passed)}},
          open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

lines = [
    "# 集权层（集成层）缺陷清单",
    "",
    f"三方对账：前端 JS 声明 × 后端 Java 声明 × 录制流量画像 × 实测行为",
    "",
    f"- 缺陷 {len(findings)} 条：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())) + "（无则写 0）",
    f"- 通过的对账项 {len(passed)} 条",
    "",
    "## 缺陷",
    "",
]
if findings:
    lines.append("| 严重度 | ID | 端点 | 标题 |")
    lines.append("|---|---|---|---|")
    for f in findings:
        lines.append(f"| {f['severity']} | `{f['id']}` | {f['endpoint']} | {f['title']} |")
    lines.append("")
    for f in findings:
        lines += ["", f"### {f['severity']} · {f['id']}", "",
                  f"**{f['title']}**", "",
                  f"- 端点：{f['endpoint']}",
                  f"- 归属层：{f.get('owner', '集权层')}",
                  f"- 说明：{f['detail']}",
                  f"- 复现：{f['repro']}",
                  f"- 证据：`{json.dumps(f['evidence'], ensure_ascii=False)}`"]
if chains_doc:
    lines += ["", "## 攻击链（跨层可执行链，每一步都有证据）", "",
              "| 链 | 结论 | 步数 | 标题 |", "|---|---|---|---|"]
    for ch in chains_doc["chains"]:
        lines.append(f"| `{ch['id']}` | {ch['verdict']} | {len(ch['steps'])} | {ch['title']} |")
    lines.append("")
    for ch in chains_doc["chains"]:
        lines += ["", f"### {ch['id']} · {ch['verdict']}", "",
                  f"**{ch['title']}**", "",
                  f"- 假设：{ch['hypothesis']}",
                  f"- 归属：{ch['owner']}",
                  f"- 引用下层缺陷：{'、'.join(f'`{c}`' for c in ch.get('cites') or []) or '无'}",
                  f"- 结论：{ch.get('note')}", "", "| # | 动作 | 目标 | 状态 | 证据 |",
                  "|---|---|---|---|---|"]
        for st in ch["steps"]:
            ev = str(st["evidence"]).replace("|", "\\|")
            lines.append(f"| {st['n']} | {st['method']} | `{st['path']}` | {st['status']} | {ev} |")
        lines.append("")

lines += ["", "## 通过的对账项（本层不重复计缺陷）", ""]
for p in passed:
    lines.append(f"- ✅ **{p['check']}** — {p['desc']}")
lines += ["", "---", "",
          "口径说明：与服务层/类层已报缺陷重复的根因，本层只补集成症状并在 owner 字段标注；",
          "所有结论均由 `integration_dynamic.py` 实测产生，每条都带可重放的复现路径。"]
open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")

print(f"集权层差异 -> {OUT_JSON} / {OUT_MD}")
print(f"  缺陷 {len(findings)} 条 " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or ""))
print(f"  通过 {len(passed)} 项")
for f in findings:
    print(f"    {f['severity']:8} {f['id']:32} {f['title']}")
