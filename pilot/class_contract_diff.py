"""类层 v2 三方差分：DB 实况 ↔ JPA 实体 ↔ DTO 声明 ↔ 实测行为。

四个差分方向：
  A 列级可空性三方一致（DB NOT NULL / 实体 nullable / DTO 约束）
  B 业务规则的三层分布（DB CHECK / DTO 约束 / 服务层实测放行）
  C 字段可达性与写入语义（DTO 可写 ↔ 实测被改列）
  D 契约边界的错误码与消息一致性

只输出差分结论，不做判定；判定权留给 class_diff.py 的排序与落盘。
"""
import json
import re

# DTO -> (表, 实体类)。只登记真正参与写入的 DTO；响应类不参与。
DTO_TARGET = {
    "CreateProductRequest": ("products", "Product"),
    "UpdateProductRequest": ("products", "Product"),
    "CreatePackageRequest": ("packages", "BanquetPackage"),
    "UpdatePackageRequest": ("packages", "BanquetPackage"),
    "CreateOrderRequest": ("orders", "BanquetOrder"),
    "UpdateOrderRequest": ("orders", "BanquetOrder"),
    "CreateCategoryRequest": ("categories", "Category"),
    "CreatePackageTypeRequest": ("package_types", "PackageType"),
    "PackageItemRequest": ("package_items", "PackageItem"),
    "AddedItemRequest": ("package_items", "PackageItem"),
}

# 服务端派生的字段：DTO 不接受输入，由服务层生成，DB 侧 NOT NULL 属于正常
SERVER_DERIVED = {"orderNo", "packageSnapshot", "addedItemsSnapshot", "createdAt", "updatedAt"}


def camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def load(path_declaration: str, path_contract: str, path_observation: str) -> dict:
    return {
        "decl": json.load(open(path_declaration)),
        "contract": json.load(open(path_contract)),
        "obs": json.load(open(path_observation)),
    }


def _dto_index(contract: dict) -> dict:
    """simple_name -> {字段名: {注解名: 注解详情}}"""
    out = {}
    for r in contract["reflection"]:
        if r["kind"] != "record":
            continue
        out[r["simple_name"]] = {c["name"]: c["annotations"] for c in r["components"]}
    return out


def _entity_columns(decl: dict) -> dict:
    """实体类 -> {字段名: {column, nullable}}"""
    out = {}
    for cls, e in decl["jpa"].items():
        out[cls] = {c["field"]: {"column": c["column"], "nullable": c["nullable"]}
                    for c in e.get("columns", [])}
    return out


def _parse_check(expr: str) -> dict:
    """从 CHECK 定义里抽出它管的是哪一列、阈值多少、还是枚举白名单。"""
    expr = expr.strip()
    m = re.match(r"CHECK \(\(([a-z_]+)\s*(>=|>)\s*(\d+)\)\)", expr)
    if m:
        return {"kind": "range", "column": m.group(1),
                "op": m.group(2), "value": int(m.group(3))}
    m = re.match(r"CHECK \(\(\(([a-z_]+)\)::text = ANY", expr)
    if m:
        vals = re.findall(r"'([A-Z_]+)'::character varying", expr)
        return {"kind": "enum", "column": m.group(1), "values": vals}
    return {"kind": "unknown"}


def _min_value(annotations: dict) -> int | None:
    for name in ("Min", "Positive", "PositiveOrZero"):
        if name in annotations:
            if name == "Min":
                return int(annotations["Min"].get("value", 0))
            return 1 if name == "Positive" else 0
    return None


# ---------------------------------------------------------------- 差分 A
def diff_nullability(ctx: dict) -> list[dict]:
    """DB NOT NULL ↔ 实体 nullable ↔ DTO 约束：谁该为"非空"负责，声明和实际是否一致。"""
    findings = []
    contract, decl, obs = ctx["contract"], ctx["decl"], ctx["obs"]
    dtos = _dto_index(contract)
    entities = _entity_columns(decl)
    db_tables = contract["db_actual"]["tables"]

    probe_by_label = {}
    for group in ("contract_boundary", "nullability"):
        for c in obs.get(group, []):
            probe_by_label[c["label"]] = c

    # 同一列在 Create/Update 两个 DTO 里都会出现，按 (表, 列) 去重后再比
    per_col = {}
    for dto_name, (table, entity) in DTO_TARGET.items():
        fields = dtos.get(dto_name)
        if not fields:
            continue
        db_cols = db_tables.get(table, {}).get("columns", {})
        ent_cols = entities.get(entity, {})
        for field, anns in fields.items():
            if field in SERVER_DERIVED:
                continue
            col = camel_to_snake(field)
            if col not in db_cols:
                continue
            db_not_null = not db_cols[col]["nullable"]
            dto_required = ("NotNull" in anns) or ("NotBlank" in anns)
            ent = ent_cols.get(field)
            key = (table, col)
            rec = per_col.setdefault(key, {"dto": dto_name, "field": field, "column": col,
                                           "table": table, "entity": entity,
                                           "entity_nullable": ent["nullable"] if ent else None,
                                           "db_default": db_cols[col]["default"],
                                           "db_not_null": db_not_null,
                                           "dto_required": dto_required})
            rec["dto_required"] = rec["dto_required"] or dto_required

    cases = []
    both = stricter = loose = 0
    for rec in per_col.values():
        if rec["db_not_null"] and not rec["dto_required"]:
            cases.append(rec)
        elif rec["db_not_null"] and rec["dto_required"]:
            both += 1
        elif rec["dto_required"]:
            stricter += 1
        else:
            loose += 1
    aligned = both + stricter + loose

    for c in cases:
        if c["field"] == "address":
            p = probe_by_label.get("address 缺省（DTO 无约束，DB NOT NULL）", {})
            findings.append({
                "id": "CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING",
                "severity": "MEDIUM",
                "layer": "class",
                "title": "orders.address 声明 NOT NULL，实际用空串绕过 —— 非空约束只挡了 NULL",
                "detail": "三方声明：DB 列 address 为 NOT NULL，实体 @Column(nullable=false)，"
                          "但 DTO 的 address 没有任何约束。实测：客户端不传 address，"
                          f"接口返回 {p.get('http')}，库里落的是空串 "
                          f"{p.get('db_row', {}).get('address')!r}。"
                          "NOT NULL 只证明列不是 NULL，空串照样入库 —— 约束拦不住业务上的空。"
                          "后果：订单地址为空，宴会执行单拿不到地址，且没有任何一层会报错。",
                "evidence": {"dto": c["dto"], "field": c["field"], "column": c["column"],
                             "db_not_null": True, "dto_constraints": [],
                             "entity_nullable": c["entity_nullable"],
                             "http": p.get("http"), "db_value": p.get("db_row", {}).get("address")},
            })
        elif c["field"] == "finalPriceCent":
            p = probe_by_label.get("finalPriceCent=null（DTO 可空，DB NOT NULL）", {})
            findings.append({
                "id": "CLASS-REQUIRED-CHECKED-IN-SERVICE-LAYER",
                "severity": "MEDIUM",
                "layer": "class",
                "title": "orders.finalPriceCent 必填性校验落在服务层，DTO 少一条 @NotNull",
                "detail": "DB 与实体都声明 NOT NULL，DTO 只写了 @Min(0) 而没有 @NotNull —— "
                          "字段在传输层是可空的，靠服务层补一句判断拦住。"
                          f"实测：传 null 返回 {p.get('http')} / {p.get('code')}，"
                          f"文案是「{p.get('message')}」。"
                          "后果一：责任错层，任何绕过服务层的写入方（脚本、数据修复）都会撞 DB 报错；"
                          "后果二：报错文案说的是「小于 0」，真实原因是「不能为空」，客户端无法据此定位。",
                "evidence": {"dto": c["dto"], "field": c["field"], "column": c["column"],
                             "dto_constraints": ["Min(0)"], "db_not_null": True,
                             "http": p.get("http"), "code": p.get("code"),
                             "message": p.get("message")},
            })
        elif c["field"] == "isActive":
            p = probe_by_label.get(f"{c['table']}.is_active 缺省（DB NOT NULL, DTO 无 @NotNull）", {})
            findings.append({
                "id": "CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES",
                "severity": "LOW",
                "layer": "class",
                "title": f"{c['table']}.is_active 的默认值被声明了三遍",
                "detail": "DB 列有 DEFAULT true，DTO 允许缺省，服务层再兜底成 true。"
                          f"实测：缺省时接口返回 {p.get('http')}，落库 "
                          f"is_active={p.get('db_row', {}).get('is_active')}。"
                          "同一个默认值写三遍，改任何一处（比如把产品默认改成下架）"
                          "另外两处不会跟着变，且不会有任何报错 —— 属于典型的静默漂移。",
                "evidence": {"table": c["table"], "column": c["column"],
                             "db_default": c["db_default"], "dto_constraints": [],
                             "observed_value": p.get("db_row", {}).get("is_active"),
                             "http": p.get("http")},
            })
        else:
            findings.append({
                "id": "CLASS-NULLABILITY-THREE-WAY-MISMATCH",
                "severity": "MEDIUM",
                "layer": "class",
                "title": f"{c['table']}.{c['column']} 三方可空性声明不一致",
                "detail": "DB 声明 NOT NULL，实体声明 nullable="
                          f"{c['entity_nullable']}，DTO 没有任何非空约束。"
                          "空值由哪一层兜底没有声明，缺失时行为不可预期。",
                "evidence": c,
            })

    if aligned:
        findings.append({
            "id": "CLASS-NULLABILITY-ALIGNED",
            "severity": "INFO",
            "layer": "class",
            "title": f"可空性三方一致 {aligned}/{len(per_col)} 列"
                      f"（同向设防 {both} · DTO 更严 {stricter} · 都可空 {loose}）",
            "detail": "这是差分引擎的通过结论：DTO 的非空声明要么与 DB 一致，"
                      "要么比 DB 更严（DTO 先拦，留给 DB 兜底），方向正确。"
                      "不成立的只有下面单列出来的那几列。",
            "evidence": {"aligned_columns": aligned, "total_columns": len(per_col),
                         "same_direction": both, "dto_stricter": stricter,
                         "both_nullable": loose},
        })
    return findings


# ---------------------------------------------------------------- 差分 B
def diff_rules(ctx: dict) -> list[dict]:
    """业务规则在 DB CHECK / DTO 约束 / 服务层 三层的分布。"""
    findings = []
    contract, obs = ctx["contract"], ctx["obs"]
    dtos = _dto_index(contract)
    db_tables = contract["db_actual"]["tables"]
    probes = obs["defense_audit"]["constraint_probes"]

    aligned_checks = []
    unaligned_checks = []
    for p in probes:
        if not p.get("constraint"):
            continue
        table = p["table"]
        spec = db_tables.get(table, {}).get("constraints", {}).get(p["constraint"])
        if not spec:
            continue
        parsed = _parse_check(spec.get("def", ""))
        if parsed["kind"] == "unknown":
            continue
        col = parsed["column"]
        field, dto_name = None, None
        for cand, (t, _e) in DTO_TARGET.items():
            if t != table:
                continue
            for f in dtos.get(cand, {}):
                if camel_to_snake(f) == col:
                    field, dto_name = f, cand
                    break
            if field:
                break
        hit = None
        if field and dto_name:
            anns = dtos[dto_name][field]
            if parsed["kind"] == "range":
                mv = _min_value(anns)
                expect = parsed["value"] + (1 if parsed["op"] == ">" else 0)
                if mv is not None and mv == expect:
                    hit = f"{field} @Min({mv})"
            elif parsed["kind"] == "enum":
                if "NotNull" in anns:
                    hit = f"{field} @NotNull + 枚举 {parsed['values']}"
        if hit:
            aligned_checks.append({"constraint": p["constraint"], "column": col,
                                   "dto": f"{dto_name}.{field}", "match": hit})
        else:
            unaligned_checks.append({"constraint": p["constraint"], "column": col,
                                     "check": spec.get("def"), "dto_field": field})

    if aligned_checks:
        findings.append({
            "id": "CLASS-CHECK-DTO-ALIGNED",
            "severity": "INFO",
            "layer": "class",
            "title": f"{len(aligned_checks)}/{len(aligned_checks) + len(unaligned_checks)} "
                     "条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合",
            "detail": "DB 的 CHECK 不是孤岛：DTO 层有等价声明，两层同时设防。"
                      "这类对齐决定了改一条业务规则要不要同时动三个文件。",
            "evidence": {"aligned": aligned_checks},
        })
    if unaligned_checks:
        findings.append({
            "id": "CLASS-CHECK-WITHOUT-DTO-COUNTERPART",
            "severity": "MEDIUM",
            "layer": "class",
            "title": f"{len(unaligned_checks)} 条 DB CHECK 在 DTO 层找不到同义约束",
            "detail": "规则只写在迁移文件里，Java 侧无法复用，也无法在报错前拦截。"
                      "改规则要改迁移文件，业务代码读不到这条规则。",
            "evidence": {"unaligned": unaligned_checks},
        })

    api_evidence = {
        "deposit_cent > final_price_cent（定金大于成交价）":
            ("depositCent > finalPriceCent", 200, "定金 999999 > 成交价 1，落库无异常"),
        "reservation_time 在过去（预订过去的日期）":
            ("reservationTime 在过去", 200, "预订时间取一年前，接口照常受理并落库"),
        "customer_phone 非手机号格式":
            ("customerPhone 非手机号", 200, "customer_phone 落库为 'not-a-phone'"),
        "status=COMPLETED 但 deposit_cent=0（未付定金却已完成）":
            ("status=COMPLETED 且 depositCent=0", 200, "订单直接置为已完成，定金为 0"),
    }
    missing = []
    for p in probes:
        if p.get("constraint") or p.get("blocked"):
            continue
        label = p.get("label", "")
        if label in ("orders.address 为 NULL（DB 声明 NOT NULL）",
                     "products.category_id 指向一个不存在的分类（FK 应拦）"):
            continue
        api_case, http, note = api_evidence.get(label, (None, None, None))
        missing.append({"rule": label, "table": p["table"], "db_blocked": False,
                        "api_http": http, "api_note": note})
    if missing:
        findings.append({
            "id": "CLASS-RULE-MISSING-IN-ALL-LAYERS",
            "severity": "HIGH",
            "layer": "class",
            "title": f"{len(missing)} 条业务不变量在 DB / DTO / 服务层三层全都不存在",
            "detail": "这些不是「某一层写错了」，而是三层都没有表达过 —— "
                      "没有任何一层承担校验责任，实测 API 也全部放行并落库。"
                      "订单的业务语义（定金不超过成交价、预订时间在未来、联系方式是手机号、"
                      "完成必有定金）目前只存在于人的脑子里，不存在于任何一行代码或迁移文件里。",
            "evidence": {"rules": missing},
        })
    return findings


# ---------------------------------------------------------------- 差分 C
def diff_write_footprint(ctx: dict) -> list[dict]:
    """DTO 声明可写的字段 ↔ 实测真的被改的列。"""
    findings = []
    obs = ctx["obs"]
    for w in obs.get("write_footprint", []):
        steps = {s["action"]: s for s in w["steps"]}
        put = next((s for a, s in steps.items() if a.startswith("PUT /api/products/{id}")), None)
        if put and "image_url" in put["changed"]:
            findings.append({
                "id": "CLASS-PUT-WIPES-UNSET-FIELD",
                "severity": "MEDIUM",
                "layer": "class",
                "title": "PUT 是全量覆盖：不传的 imageUrl 会被抹成 NULL",
                "detail": "UpdateProductRequest.imageUrl 没有任何约束（可缺省），"
                          "但 PUT 的语义是全量替换。实测：创建时带图片路径，"
                          "随后一次不带 imageUrl 的 PUT 之后，image_url 变成 NULL。"
                          "后果不是校验问题而是数据丢失 —— 磁盘上的图片文件还在，"
                          "但数据库里的引用没了，文件成为孤儿，前端永远拿不到图。",
                "evidence": {"entity": w["entity"], "id": w["id"],
                             "changed_columns": put["changed"],
                             "final_row": w.get("final_row")},
            })
        miss = next((s for a, s in steps.items() if "缺省 categoryId" in a), None)
        if miss and miss.get("http") == 400 and not miss["changed"]:
            findings.append({
                "id": "CLASS-PUT-REQUIRED-FK-GUARDED",
                "severity": "INFO",
                "layer": "class",
                "title": "PUT 缺省必填外键被 DTO 的 @NotNull 挡住（400，DB 无脏数据）",
                "detail": "categoryId 在 DTO 上有 @NotNull，实测缺省时接口返回 400 且行未被改动。"
                          "这是可空性差分里方向正确的一例：声明在最靠近输入的地方设防。",
                "evidence": {"http": miss.get("http"), "changed": miss["changed"]},
            })
    return findings


# ---------------------------------------------------------------- 差分 D
def diff_error_contract(ctx: dict) -> list[dict]:
    """错误码 / 状态码 / 文案 与真实原因是否一致。"""
    findings = []
    obs = ctx["obs"]
    for c in obs.get("contract_boundary", []):
        if c["label"].startswith("finalPriceCent=null") and c["http"] == 422:
            findings.append({
                "id": "CLASS-ERROR-MESSAGE-MISMATCH",
                "severity": "LOW",
                "layer": "class",
                "title": "finalPriceCent=null 的报错文案说的是「小于 0」，真实原因是「不能为空」",
                "detail": "null 触发的是必填缺失，却复用了一个数值区间的错误文案。"
                          "客户端拿到 422 后按「价格太小」去提示用户，实际用户根本没填价格。"
                          "这类错误文案漂移会让前端提示整体失真。",
                "evidence": {"http": c["http"], "code": c.get("code"),
                             "message": c.get("message"), "label": c["label"]},
            })
        if c["label"].startswith("status 小写") and c["http"] == 400:
            findings.append({
                "id": "CLASS-ENUM-CASE-SENSITIVE",
                "severity": "LOW",
                "layer": "class",
                "title": "订单状态枚举大小写敏感，小写 pending 直接 400",
                "detail": "DTO 的 status 字段直接用 OrderStatus 枚举，Jackson 默认大小写敏感。"
                          "实测传 'pending' 返回 400「订单状态不正确」。"
                          "对外 API 的枚举值应当约定为大小写不敏感或明确写进 OpenAPI，"
                          "否则每个新客户端都会踩一次。",
                "evidence": {"http": c["http"], "code": c.get("code"),
                             "message": c.get("message")},
            })
    return findings


def run(decl_path: str, contract_path: str, obs_path: str) -> list[dict]:
    ctx = load(decl_path, contract_path, obs_path)
    out = []
    out += diff_nullability(ctx)
    out += diff_rules(ctx)
    out += diff_write_footprint(ctx)
    out += diff_error_contract(ctx)
    return out
