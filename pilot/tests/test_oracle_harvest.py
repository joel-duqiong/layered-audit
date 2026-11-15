"""oracle_harvest 回归守卫 —— 锁住独立验证者抓到的四个真缺陷。

每个测试对应一个曾经存在的洞，全部用合成夹具，不依赖真实语料：
  1. reference 行号必须指向规则自己所在行（DDL 逐约束 / record 逐注解）
  2. 反引号剥离不得污染枚举值；整行包反引号的 CHECK 仍要采到
  3. 验收条款载荷保留原文反引号
  4. markdown 确认表被采到；未定义仍 refuted；需产品确认/文档陈旧不得当 confirmed；
     散文规则进 untranscribed 而非静默丢弃
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.oracle_harvest import (  # noqa: E402
    CONFIRMED, NEEDS_CONFIRMATION, REFUTED, LEVEL_BUSINESS,
    harvest_annotations, harvest_ddl, harvest_document, harvest_openapi,
    demote_unverified_fields, field_inventory, ddl_column_tables, bind_l0_scope,
    doc_nullable_fields, refute_negated_candidates, drop_unverifiable_defaults,
)
from engine import oracle_bridge  # noqa: E402


# ---- 缺陷 2：枚举值不得带反引号 ----
def test_enum_values_no_backtick_pollution(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## Check 约束\n\n- 订单 status 只能是 `PENDING`、`DEPOSIT_PAID`、`COMPLETED`\n",
                   encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    enums = [o for o in oracles if o["assert"]["type"] == "in"]
    assert len(enums) == 1
    assert enums[0]["assert"]["value"] == ["PENDING", "DEPOSIT_PAID", "COMPLETED"]
    assert all("`" not in v for v in enums[0]["assert"]["value"])


# ---- 缺陷 2：整行包反引号的 CHECK 约束仍能采到 ----
def test_whole_line_backtick_check_still_harvested(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## Check 约束\n\n- `price_cent >= 0`\n- `quantity > 0`\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    cmps = [o for o in oracles if o["assert"]["type"] == "field_cmp"]
    assert {o["assert"]["field"] for o in cmps} == {"price_cent", "quantity"}


def test_inline_backtick_not_stripped_from_enum_tail(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## 域\n\n- 状态只能是 `A`、`B`\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    enums = [o for o in oracles if o["assert"]["type"] == "in"]
    assert enums[0]["assert"]["value"] == ["A", "B"]


# ---- 缺陷 3：验收条款载荷保留原文反引号 ----
def test_acceptance_payload_keeps_inline_backticks(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## 验收清单\n\n- `GET /api/health` 正常。\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    acc = [o for o in oracles if o["assert"]["type"] == "acceptance_scenario"]
    assert len(acc) == 1
    assert acc[0]["assert"]["text"] == "`GET /api/health` 正常。"
    assert acc[0]["assert"]["text"].count("`") % 2 == 0


# ---- 缺陷 1：DDL 每条约束用自己所在行号 ----
def test_ddl_each_constraint_own_line(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text(
        "CREATE TABLE orders (\n"
        "    id BIGSERIAL PRIMARY KEY,\n"
        "    price_cent BIGINT NOT NULL,\n"
        "    CONSTRAINT chk_price CHECK (price_cent >= 0),\n"
        "    CONSTRAINT uk_id UNIQUE (id)\n"
        ");\n", encoding="utf-8")
    by_id = {o["id"]: o for o in harvest_ddl([sql])}
    assert by_id["ddl:orders.price_cent.not_null"]["source"]["reference"] == "V1__t.sql:3"
    assert by_id["ddl:orders.chk_price"]["source"]["reference"] == "V1__t.sql:4"
    assert by_id["ddl:orders.uk_id"]["source"]["reference"] == "V1__t.sql:5"


def test_ddl_alter_add_column_points_to_definition_line(tmp_path):
    sql = tmp_path / "V2__t.sql"
    sql.write_text(
        "ALTER TABLE categories\n"
        "    ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE;\n", encoding="utf-8")
    by_id = {o["id"]: o for o in harvest_ddl([sql])}
    assert by_id["ddl:categories.is_deleted.not_null"]["source"]["reference"] == "V2__t.sql:2"


# ---- 缺陷 1：record 每个注解用自己所在行号 ----
def test_record_annotation_own_line(tmp_path):
    java = tmp_path / "Req.java"
    java.write_text(
        "public record Req(\n"
        "    @NotBlank String name,\n"
        "    @NotNull @Min(1) Integer count,\n"
        "    String remark\n"
        ") {\n"
        "}\n", encoding="utf-8")
    by_id = {o["id"]: o for o in harvest_annotations([java])}
    assert by_id["anno:Req.name.notblank"]["source"]["reference"] == "Req.java:2"
    assert by_id["anno:Req.count.notnull"]["source"]["reference"] == "Req.java:3"
    assert by_id["anno:Req.count.min"]["source"]["reference"] == "Req.java:3"


def test_record_components_sharing_line_get_same_ref(tmp_path):
    java = tmp_path / "Pair.java"
    java.write_text(
        "public record Pair(@NotNull Integer a, @Min(0) Long b) {\n}\n", encoding="utf-8")
    by_id = {o["id"]: o for o in harvest_annotations([java])}
    assert by_id["anno:Pair.a.notnull"]["source"]["reference"] == "Pair.java:1"
    assert by_id["anno:Pair.b.min"]["source"]["reference"] == "Pair.java:1"


# ---- 缺陷 1：reference 行号回原文能查到该规则（合成 ≥20 条全量核对）----
def test_reference_lines_resolve_to_rule(tmp_path):
    sql = tmp_path / "V1__t.sql"
    cols = "\n".join(f"    col{i} BIGINT NOT NULL," for i in range(12))
    sql.write_text(f"CREATE TABLE big (\n{cols}\n"
                   "    CONSTRAINT chk_c0 CHECK (col0 >= 0)\n);\n", encoding="utf-8")
    java = tmp_path / "Many.java"
    comps = "\n".join(f"    @NotNull String f{i}," for i in range(10))
    java.write_text(f"public record Many(\n{comps}\n) {{\n}}\n", encoding="utf-8")
    oracles = harvest_ddl([sql]) + harvest_annotations([java])
    assert len(oracles) >= 20
    src = {"V1__t.sql": sql.read_text().splitlines(), "Many.java": java.read_text().splitlines()}
    hit = 0
    for o in oracles:
        fn, ln = o["source"]["reference"].rsplit(":", 1)
        line = src[fn][int(ln) - 1]
        frag = o["source"]["text"].strip().split("\n")[0][:20].strip("`")
        assert frag in line, f"{o['id']} ref {o['source']['reference']} 不含规则原文"
        hit += 1
    assert hit == len(oracles)


# ---- 缺陷 4：markdown 确认表被采到，预期来源保留 ----
def test_confirmed_table_harvested_with_source(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "# 对照\n\n## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n"
        "| --- | --- | --- | --- |\n"
        "| 桌数 > 0 | AGENTS.md | DTO @Min(1) | 一致 |\n"
        "| 订单状态仅 PENDING/COMPLETED | AGENTS.md + docs/API.md | 枚举 | 一致 |\n"
        "| 软删除 | AGENTS.md | is_deleted | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    table = [o for o in oracles if o["id"].endswith(".table")]
    assert len(table) == 3
    assert all(o["confirmation"] == CONFIRMED and o["level"] == LEVEL_BUSINESS for o in table)
    assert "AGENTS.md" in table[0]["note"]
    assert table[0]["assert"] == {"type": "field_cmp", "field": "桌数", "op": ">", "value": 0}
    assert table[1]["assert"] == {"type": "in", "field": "订单状态", "value": ["PENDING", "COMPLETED"]}
    assert table[2]["assert"]["type"] == "confirmed_rule"
    assert table[0]["source"]["reference"] == "check.md:7"


def test_table_row_not_consistent_not_confirmed(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 某规则 | AGENTS.md | 未实现 | 不一致 |\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    assert not [o for o in oracles if o["id"].endswith(".table")]
    assert any("不一致" in u["note"] or "非「一致」" in u["note"] for u in untranscribed)


def test_table_compound_rule_transcribed_not_fabricated(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| seed：6 分类、>=25 商品、>=8 订单 | AGENTS.md | 实测 | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    rows = [o for o in oracles if ".table" in o["id"]]
    cmps = [o["assert"] for o in rows if o["assert"]["type"] == "field_cmp"]
    assert {a["value"] for a in cmps} == {25, 8}
    assert all(a["field"] in ("商品", "订单") for a in cmps)
    assert not any("seed" in a.get("field", "") for a in cmps)


def test_table_compound_upload_enum_and_size(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 上传仅 jpg/jpeg/png/webp，最大 5MB | AGENTS.md | 实测 | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    rows = [o for o in oracles if ".table" in o["id"]]
    assert len(rows) == 2
    enum = next(o["assert"] for o in rows if o["assert"]["type"] == "in")
    assert enum["value"] == ["jpg", "jpeg", "png", "webp"]
    assert enum["field"] == "上传"
    size = next(o["assert"] for o in rows if o["assert"]["type"] == "field_cmp")
    assert size == {"type": "field_cmp", "field": "上传", "op": "<=", "value": 5, "unit": "MB"}


def test_table_structural_rule_registered_with_reason(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 使用成交价时保存成交价，否则保存方案合计 | AGENTS.md | 实测 | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    row = next(o for o in oracles if o["id"].endswith(".table"))
    assert row["assert"]["type"] == "confirmed_rule"
    assert "封闭原语集" in row["note"]


def test_table_structural_primitives_transcribed_literal(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 统一 JSON envelope（success/code/data） | AGENTS.md | 实测 | 一致 |\n"
        "| 错误码覆盖 A_ERROR/B_ERROR | AGENTS.md | 实测 | 一致 |\n"
        "| `orders.x` 必须有值 | AGENTS.md | 实测 | 一致 |\n"
        "| 套餐商品不可重复（同套餐同商品唯一） | AGENTS.md | 实测 | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    by_ref = {}
    for o in oracles:
        if ".table" in o["id"]:
            by_ref.setdefault(o["source"]["reference"], []).append(o["assert"])
    env = by_ref["check.md:5"][0]
    assert env == {"type": "has_fields", "target": "统一 JSON envelope",
                   "fields": ["success", "code", "data"]}
    assert by_ref["check.md:6"][0] == {"type": "set_covers", "field": "错误码",
                                       "value": ["A_ERROR", "B_ERROR"]}
    assert by_ref["check.md:7"][0] == {"type": "not_null", "field": "orders.x"}
    assert by_ref["check.md:8"][0] == {"type": "unique", "fields": ["套餐", "商品"]}
    assert all(o["gate_eligible"] is False for o in oracles if ".table" in o["id"])


# ---- 缺陷 4：未定义章节仍 refuted ----
def test_undefined_section_still_refuted(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text("## 当前未定义\n\n- 定金与成交价的大小关系：当前只要求各自 >= 0。\n", encoding="utf-8")
    oracles, undefined, _ = harvest_document([doc])
    assert len(undefined) == 1
    assert undefined[0]["confirmation"] == REFUTED
    assert not oracles


# ---- 缺陷 4：P1-P5 按原文分级，需产品确认/文档陈旧不得当 confirmed ----
def test_problem_sections_classified(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 发现的问题\n\n"
        "### P1 手机号（建议修复）\n\n- 前端要求：`/^1[3-9]\\d{9}$/`。\n\n"
        "### P2 空校验（疑似缺陷，需产品确认）\n\n- 现状：不校验。\n\n"
        "### P5 文档陈旧点（以代码为准）\n\n- 文档未列 DELETE。\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    nc = [o for o in oracles if o["confirmation"] == NEEDS_CONFIRMATION]
    assert len(nc) == 1 and "P2" in nc[0]["source"]["text"]
    p1 = [o for o in oracles if o["assert"]["type"] == "pattern"]
    assert len(p1) == 1 and p1[0]["confirmation"] == CONFIRMED
    p5 = [u for u in untranscribed if "DELETE" in u["source"]["text"]]
    assert len(p5) == 1 and p5[0]["confirmation"] == REFUTED
    assert not any("P2" in o["source"].get("text", "") and o["confirmation"] == CONFIRMED
                   for o in oracles)


# ---- 缺陷 4：散文规则进 untranscribed 而非静默丢弃 ----
def test_prose_rules_untranscribed_not_dropped(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text(
        "## 订单快照规范\n\n订单详情优先读快照，不要实时查套餐当前内容。\n\n"
        "## 价格规则\n\n- 不要出现打折字样。\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    texts = [u["source"]["text"] for u in untranscribed]
    assert any("优先读快照" in t for t in texts)
    assert any("打折" in t for t in texts)
    assert not oracles


# ---- OpenAPI 引用是合法 JSON Pointer，必须能解析到真实字段 spec（锁可解析性，非字符串形式）----
def _resolve_pointer(doc, ptr):
    cur = doc
    for part in ptr.lstrip("#/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def test_openapi_pointer_resolves_to_field_spec(tmp_path):
    import json
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps({"components": {"schemas": {
        "Order": {"required": ["status"], "properties": {
            "status": {"type": "string", "enum": ["A", "B"]},
            "count": {"type": "integer", "minimum": 0}}}}}}), encoding="utf-8")
    oracles = harvest_openapi(spec)
    doc = json.loads(spec.read_text())
    assert oracles
    for o in oracles:
        ptr = o["source"]["reference"].split(":", 1)[1]
        assert "/properties/" in ptr
        resolved = _resolve_pointer(doc, ptr)
        name, field = o["target"]["schema"], o["target"]["field"]
        assert resolved is doc["components"]["schemas"][name]["properties"][field]


def test_openapi_pointer_escapes_special_chars(tmp_path):
    import json
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps({"components": {"schemas": {
        "S": {"properties": {"a/b~c": {"type": "string", "enum": ["x"]}}}}}}), encoding="utf-8")
    oracles = harvest_openapi(spec)
    doc = json.loads(spec.read_text())
    ptr = oracles[0]["source"]["reference"].split(":", 1)[1]
    assert "~1" in ptr and "~0" in ptr
    assert _resolve_pointer(doc, ptr) is doc["components"]["schemas"]["S"]["properties"]["a/b~c"]


# ---- 支柱(1)：可执行性闭环 —— 采集器发原始词汇，桥映射，散文名词降级 ----
def test_prose_field_demoted_not_confirmed(tmp_path):
    doc = tmp_path / "check.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 金额字段 >= 0 | AGENTS.md | 实测 | 一致 |\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    oracles = demote_unverified_fields(oracles, {"price_cent"})
    row = next(o for o in oracles if o["id"].endswith(".table"))
    assert row["confirmation"] == NEEDS_CONFIRMATION
    assert row["assert"]["field"] == "金额字段"


def test_verified_field_feeds_consumer_via_bridge(tmp_path):
    from engine import rule_assessment as ra
    doc = tmp_path / "prd.md"
    doc.write_text("## Check 约束\n\n- `price_cent >= 0`\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    oracles = demote_unverified_fields(oracles, {"price_cent"})
    o = oracles[0]
    assert o["confirmation"] == CONFIRMED and o["assert"]["type"] == "field_cmp"
    result = oracle_bridge.bridge(oracles)
    assert result["counts"]["mapped_oracles"] == 1
    pack = oracle_bridge.to_rule_pack(result, "T")
    for val, expect in ((5, "PASS"), (-1, "FAIL")):
        obs = [{"id": pack["rules"][0]["id"], "target": "T", "scope": pack["rules"][0]["scope"],
                "reached": True, "row": {"price_cent": val}, "evidence_refs": ["r"]}]
        res = ra.assess(pack, {"observations": obs})
        assert res["results"][0]["status"] == expect


def test_non_alignable_reported_by_bridge_with_reason(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text("CREATE TABLE p (\n    flag BOOLEAN NOT NULL DEFAULT TRUE\n);\n", encoding="utf-8")
    oracles = harvest_ddl([sql])
    o = next(x for x in oracles if x["id"] == "ddl:p.flag.default")
    specs, reason = oracle_bridge.map_assertion(o["assert"])
    assert specs == [] and reason
    result = oracle_bridge.bridge(oracles)
    assert any(u["id"] == o["id"] for u in result["unmappable"])


def test_strict_gt_maps_to_strict_not_relaxed(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## Check 约束\n\n- `quantity > 0`\n", encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    oracles = demote_unverified_fields(oracles, {"quantity"})
    o = oracles[0]
    assert o["assert"]["type"] == "field_cmp" and o["assert"]["op"] == ">"
    assert o["confirmation"] == CONFIRMED
    specs, reason = oracle_bridge.map_assertion(o["assert"])
    assert reason == "" and specs[0]["type"] == "field_gt_const"
    assert specs[0]["type"] != "field_gte_const"


def test_interface_table_real_fields_bindable(tmp_path):
    doc = tmp_path / "iface.md"
    doc.write_text(
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        "| POST | `/api/products` | - | CreateProductRequest | priceCent>=0；name 必填。 |\n",
        encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    oracles = demote_unverified_fields(oracles, {"priceCent", "name"})
    cmp = next(o for o in oracles if o["assert"]["type"] == "field_cmp")
    assert cmp["assert"]["field"] == "priceCent" and cmp["confirmation"] == CONFIRMED
    specs, reason = oracle_bridge.map_assertion(cmp["assert"])
    assert specs and specs[0]["type"] == "field_gte_const" and reason == ""


# ---- 覆盖缺口可见：可转录散文不得当散文丢；任何表零静默丢弃 ----
def test_transcribable_prose_not_dumped(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text(
        "## 上传\n\n- 只允许 jpg、jpeg、png、webp。\n- 单文件最大 5MB。\n"
        "- 不要出现“打折、折扣”等明显字样。\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    types = {o["assert"]["type"] for o in oracles}
    assert "in" in types and "field_cmp" in types and "forbidden_terms" in types
    assert not untranscribed


def test_every_table_row_harvested_or_registered(tmp_path):
    doc = tmp_path / "multi.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 桌数 > 0 | A | 实测 | 一致 |\n\n"
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        "| GET | `/api/x` | - | - | 无参数；返回状态。 |\n\n"
        "## 杂项\n\n"
        "| 层面 | 内容 | 工具 |\n| --- | --- | --- |\n"
        "| 冒烟 | health | pytest |\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    assert any(o["id"].endswith(".table") for o in oracles)
    refs = {u["source"]["reference"] for u in untranscribed}
    assert "multi.md:11" in refs
    assert "multi.md:17" in refs


# ---- 跨文件词汇契约：采集器发原始原语，桥做映射；不得越权发射消费方原语 ----
def test_harvester_bridge_vocabulary_contract(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text(
        "CREATE TABLE p (\n    price_cent BIGINT NOT NULL,\n"
        "    name VARCHAR(64) NOT NULL UNIQUE,\n    flag BOOLEAN DEFAULT TRUE,\n"
        "    CONSTRAINT chk CHECK (price_cent >= 0)\n);\n", encoding="utf-8")
    doc = tmp_path / "prd.md"
    doc.write_text("## Check\n\n- `price_cent >= 0`\n- 状态只能是 `A`、`B`\n", encoding="utf-8")
    oracles = harvest_ddl([sql]) + harvest_document([doc])[0]
    closed = oracle_bridge.CLOSED_PRIMITIVES
    seen = set()
    for o in oracles:
        t = o["assert"]["type"]
        seen.add(t)
        assert t not in closed, f"采集器越权发射消费方原语 {t}（映射是桥的职责，会砸端到端计数）"
        specs, reason = oracle_bridge.map_assertion(o["assert"])
        assert specs or "未知原语" not in reason, f"桥不认识采集器原语 {t}: {reason}"
    assert {"field_cmp", "in", "not_null", "unique", "default"} <= seen


# ---- 覆盖可见性：任何 markdown 表格不得静默丢弃 ----
def test_no_markdown_table_silently_dropped(tmp_path):
    import re as _re
    doc = tmp_path / "tables.md"
    doc.write_text(
        "## 已确认一致的规则\n\n"
        "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
        "| 桌数 > 0 | A | 实测 | 一致 |\n\n"
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        "| GET | `/x` | - | - | 无参数。 |\n\n"
        "## 杂项\n\n"
        "| 层面 | 内容 | 工具 |\n| --- | --- | --- |\n"
        "| 冒烟 | health | pytest |\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    covered = ({o["source"]["reference"] for o in oracles}
               | {u["source"]["reference"] for u in untranscribed})
    lines = doc.read_text().splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|") and i + 1 < len(lines) \
                and _re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                assert f"tables.md:{i + 1}" in covered, f"静默丢弃 tables.md:{i + 1}"
                i += 1
        else:
            i += 1


# ---- 分母口径：oracles 与 explicitly_undefined 不重叠，和可核 ----
def test_denominators_disjoint_and_explicit(tmp_path, monkeypatch):
    import json
    from engine import oracle_harvest as oh
    doc = tmp_path / "d.md"
    doc.write_text("## 当前未定义\n\n- 定金关系未定义。\n\n## Check\n\n- `price_cent >= 0`\n",
                   encoding="utf-8")
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["oracle_harvest", "--docs", str(doc), "--out", str(out)])
    oh.main()
    r = json.loads(out.read_text())
    c = r["counts"]
    assert c["oracles_count"] + c["explicitly_undefined_count"] == c["total_declarations"]
    assert c["denominators_disjoint"] is True
    assert c["total"] == c["oracles_count"]
    oids = {o["id"] for o in r["oracles"]}
    uids = {u["id"] for u in r["explicitly_undefined"]}
    assert not (oids & uids)


# ---- 缺陷1：规则粒度零静默丢弃（锁真实语料形态，能杀 M4「只转录首子句」）----
def test_interface_clause_level_no_silent_drop(tmp_path):
    doc = tmp_path / "iface.md"
    cell = ("customerName/customerPhone/status 必填；金额>=0；"
            "useFinalPrice=false 时 finalPriceCent=planTotalCent；packageId 必须存在；"
            "packageItems/addedItems 商品必须存在且不可重复；address 为空保存空字符串。")
    doc.write_text(
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        f"| POST | `/api/orders` | - | CreateOrderRequest | {cell} |\n", encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    types_fields = {(o["assert"]["type"], o["assert"].get("field")) for o in oracles}
    assert ("field_cmp", "金额") in types_fields
    assert ("references", "packageId") in types_fields
    ut_texts = [u["source"]["text"] for u in untranscribed]
    assert any("useFinalPrice=false 时 finalPriceCent=planTotalCent" in t for t in ut_texts)
    assert any("address 为空保存空字符串" in t for t in ut_texts)


def test_interface_size_after_required_transcribed(tmp_path):
    doc = tmp_path / "iface.md"
    doc.write_text(
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        "| POST | `/api/categories` | - | CreateCategoryRequest | name 必填且不超过 64 字符；空白拒绝。 |\n",
        encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    assert any(o["assert"] == {"type": "size", "field": "name", "max": 64} for o in oracles)
    assert any(o["assert"] == {"type": "not_null", "field": "name"} for o in oracles)


# ---- 缺陷2：单元格内字面 | 不得截断（锁真实 L46 形态）----
def test_pipe_in_cell_not_truncated(tmp_path):
    doc = tmp_path / "iface.md"
    doc.write_text(
        "## 接口总览\n\n"
        "| 方法 | 路径 | 参数 | 请求体 | 校验要点 |\n| --- | --- | --- | --- | --- |\n"
        "| POST | `/api/uploads/product-image` | - | multipart: file | "
        "multipart 字段 file；仅 jpg/jpeg/png/webp；Content-Type 必须 image/jpeg|png|webp；最大 5MB。 |\n",
        encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    assert any(o["assert"].get("value") == 5 and o["assert"].get("unit") == "MB" for o in oracles)
    assert any("最大 5MB" in o["source"]["text"] for o in oracles)


# ---- 缺陷3：无提示词的默认值散文不得丢（独知载体）----
def test_no_hint_default_prose_transcribed(tmp_path):
    doc = tmp_path / "api.md"
    doc.write_text("## 查询参数\n\n- `page`: 页码，默认 `1`\n- `pageSize`: 每页数量，默认 `50`\n",
                   encoding="utf-8")
    oracles, _, _ = harvest_document([doc])
    defaults = {o["assert"]["field"]: o["assert"]["value"] for o in oracles
                if o["assert"]["type"] == "default"}
    assert defaults.get("page") == 1 and defaults.get("pageSize") == 50


# ---- 缺陷4：untranscribed 分桶，消费方可区分待物化 ----
def test_untranscribed_bucketed(tmp_path):
    doc = tmp_path / "mix.md"
    doc.write_text(
        "## 价格规则\n\n- 不要出现打折字样。\n\n"
        "## 工作约定\n\n- 有自己的判断和推理，不要只附和。\n", encoding="utf-8")
    _, _, untranscribed = harvest_document([doc])
    buckets = {u["source"]["text"]: u["bucket"] for u in untranscribed}
    assert buckets["不要出现打折字样。"] == "business_rule_pending"
    assert buckets["有自己的判断和推理，不要只附和。"] == "noise"


# ---- 技术约束满足≠业务意图满足：NOT NULL 字符串列派生待确认 not_blank 候选 ----
def test_not_null_string_derives_needs_confirmation_not_blank(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text("CREATE TABLE t (\n    address VARCHAR(255) NOT NULL,\n"
                   "    price_cent BIGINT NOT NULL\n);\n", encoding="utf-8")
    oracles = harvest_ddl([sql])
    nb = [o for o in oracles if o["id"].endswith(".not_blank_candidate")]
    assert len(nb) == 1
    cand = nb[0]
    assert cand["target"]["field"] == "address"
    assert cand["confirmation"] == NEEDS_CONFIRMATION
    assert cand["source"]["kind"] == "inferred"
    assert cand["assert"]["type"] == "not_blank"
    assert cand["gate_eligible"] is False
    result = oracle_bridge.bridge(oracles)
    assert any(e["id"] == cand["id"] for e in result["excluded"])
    assert not any(m["oracle_id"] == cand["id"] for m in result["mapped"])


# ---- 可执行≠可判定：L0 缺 scope 永远 UNKNOWN，须从 DDL 列→表客观绑定 ----
def _scope_fixture(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text(
        "CREATE TABLE orders (\n    deposit_cent BIGINT NOT NULL,\n"
        "    price_cent BIGINT NOT NULL\n);\n"
        "CREATE TABLE products (\n    price_cent BIGINT NOT NULL\n);\n", encoding="utf-8")
    doc = tmp_path / "prd.md"
    doc.write_text("## Check\n\n- `deposit_cent >= 0`\n- `price_cent >= 0`\n", encoding="utf-8")
    return sql, doc


def test_l0_scope_single_table_binding(tmp_path):
    sql, doc = _scope_fixture(tmp_path)
    oracles = harvest_ddl([sql]) + harvest_document([doc])[0]
    oracles, stats = bind_l0_scope(oracles, ddl_column_tables([sql]))
    dep = next(o for o in oracles if o["id"] == "doc:prd:3.deposit_cent")
    assert dep["target"]["table"] == "orders"
    assert dep["target"]["scope_bindable"] is True
    assert dep["target"]["scope_evidence"].startswith("V1__t.sql:")
    assert dep["confirmation"] == CONFIRMED


def test_l0_scope_multi_table_split(tmp_path):
    sql, doc = _scope_fixture(tmp_path)
    oracles = harvest_ddl([sql]) + harvest_document([doc])[0]
    oracles, stats = bind_l0_scope(oracles, ddl_column_tables([sql]))
    pc = [o for o in oracles if o["id"].startswith("doc:prd:4.price_cent")]
    assert {o["target"]["table"] for o in pc} == {"orders", "products"}
    assert len(pc) == 2
    assert all(o["target"]["scope_bindable"] for o in pc)
    assert stats["l0_scope_ambiguous_split"] >= 1


def test_l0_scope_prose_unbindable_not_guessed(tmp_path):
    sql, doc = _scope_fixture(tmp_path)
    prose = tmp_path / "prose.md"
    prose.write_text("## 已确认一致的规则\n\n"
                     "| 规则 | 预期来源 | 实现事实 | 结论 |\n| --- | --- | --- | --- |\n"
                     "| 金额字段 >= 0 | A | 实测 | 一致 |\n", encoding="utf-8")
    oracles = harvest_ddl([sql]) + harvest_document([prose])[0]
    oracles = demote_unverified_fields(oracles, field_inventory(oracles))
    oracles, _ = bind_l0_scope(oracles, ddl_column_tables([sql]))
    row = next(o for o in oracles if o["id"].endswith(".table"))
    assert row["target"].get("scope_bindable") is False
    assert "table" not in row["target"]


def test_l0_scope_binding_does_not_promote_confirmation(tmp_path):
    sql, doc = _scope_fixture(tmp_path)
    oracles = harvest_ddl([sql]) + harvest_document([doc])[0]
    for o in oracles:
        if o["level"] == LEVEL_BUSINESS:
            o["confirmation"] = NEEDS_CONFIRMATION
    oracles, _ = bind_l0_scope(oracles, ddl_column_tables([sql]))
    assert all(o["confirmation"] == NEEDS_CONFIRMATION for o in oracles
               if o["level"] == LEVEL_BUSINESS)


# ---- 不编造红线：文档明写可为空 → 派生候选必须 refuted，不得留待确认 ----
def test_not_blank_candidate_refuted_by_doc_nullable(tmp_path):
    sql = tmp_path / "V1__t.sql"
    sql.write_text("CREATE TABLE orders (\n    address VARCHAR(255) NOT NULL,\n"
                   "    customer_name VARCHAR(64) NOT NULL\n);\n", encoding="utf-8")
    doc = tmp_path / "api.md"
    doc.write_text("## 订单\n\n`address` 可为空；为空时后端保存为空字符串。\n", encoding="utf-8")
    oracles = harvest_ddl([sql])
    oracles = refute_negated_candidates(oracles, doc_nullable_fields([doc]))
    addr = next(o for o in oracles if o["id"] == "ddl:orders.address.not_blank_candidate")
    assert addr["confirmation"] == REFUTED
    assert addr["source"]["kind"] == "document"
    assert addr["source"]["reference"] == "api.md:3"
    cn = next(o for o in oracles if o["id"] == "ddl:orders.customer_name.not_blank_candidate")
    assert cn["confirmation"] == NEEDS_CONFIRMATION
    res = oracle_bridge.bridge(oracles)
    assert any(e["id"] == addr["id"] for e in res["excluded"])
    assert not any(m["oracle_id"] == addr["id"] for m in res["mapped"])


# ---- 不编造：default 字段名不可核实 → 不产出荒谬断言；不误删合法中文主语规则 ----
def test_default_unverifiable_field_not_produced(tmp_path):
    doc = tmp_path / "agents.md"
    doc.write_text("## 技术栈\n\n- Java: 默认使用 Java 21。\n- `page`: 页码，默认 `1`\n",
                   encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    oracles = drop_unverifiable_defaults(oracles, {"page"}, untranscribed)
    assert not any(o["assert"].get("field") == "Java" for o in oracles)
    assert any(o["assert"].get("field") == "page" and o["assert"]["type"] == "default"
               for o in oracles)
    assert any("Java" in u["source"]["text"] for u in untranscribed)


def test_default_gate_preserves_chinese_subject_rules(tmp_path):
    doc = tmp_path / "prd.md"
    doc.write_text("## 上传\n\n- 只允许 jpg、jpeg、png、webp。\n- 单文件最大 5MB。\n",
                   encoding="utf-8")
    oracles, _, untranscribed = harvest_document([doc])
    before = len(oracles)
    oracles = drop_unverifiable_defaults(oracles, set(), untranscribed)
    assert len(oracles) == before
    assert any(o["assert"]["type"] == "in" for o in oracles)
    assert any(o["assert"]["type"] == "field_cmp" for o in oracles)

# ---- 出处自足：产物必须与采集时的 cwd 无关（锁 .resolve()，退回 str(p) 必红）----
def test_provenance_is_cwd_independent_and_self_sufficient(tmp_path, monkeypatch):
    """消费者只拿产物、不知道采集时 cwd，也要能解析每一条判据的出处。

    实测缺陷：inputs.openapi 曾原样记录调用者传入的 'openapi.json'，
    60/341 条判据的出处事后无法解析（指针本身全对，丢的是目录）。
    """
    import json
    from engine import oracle_harvest as oh

    harvest_cwd = tmp_path / "harvest"
    harvest_cwd.mkdir()
    (harvest_cwd / "d.md").write_text("## Check\n\n- `price_cent >= 0`\n", encoding="utf-8")
    # 四类输入全给，且全用相对路径 —— 否则 migrations/java_roots 恒空，那两处变异杀不死
    (harvest_cwd / "mig").mkdir()
    (harvest_cwd / "mig" / "V1__t.sql").write_text(
        "CREATE TABLE t (\n  id BIGINT NOT NULL\n);\n", encoding="utf-8")
    (harvest_cwd / "src").mkdir()
    (harvest_cwd / "src" / "ThingRequest.java").write_text(
        "public record ThingRequest(\n    @NotNull Long id\n) {}\n", encoding="utf-8")
    # 符号链接：reference 的 basename 必须与 inputs 记录一致，否则出处撕裂
    (harvest_cwd / "real").mkdir()
    (harvest_cwd / "real" / "spec.json").write_text(json.dumps(
        {"components": {"schemas": {"Thing": {
            "required": ["name"],
            "properties": {"name": {"type": "string"}}}}}}), encoding="utf-8")
    (harvest_cwd / "link.json").symlink_to(harvest_cwd / "real" / "spec.json")

    monkeypatch.chdir(harvest_cwd)
    monkeypatch.setattr(sys, "argv", [
        "oracle_harvest", "--docs", "d.md", "--migrations", "mig",
        "--java-root", "src", "--openapi", "link.json", "--out", "out.json"])
    oh.main()
    report = json.loads((harvest_cwd / "out.json").read_text())

    # 换一个 cwd 当消费者：此后只许用产物里记的路径
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.chdir(consumer)

    recorded = [Path(p) for p in report["inputs"]["docs"]]
    recorded += [Path(p) for p in report["inputs"]["migrations"]]
    recorded += [Path(p) for p in report["inputs"]["java_roots"]]
    if report["inputs"]["openapi"]:
        recorded.append(Path(report["inputs"]["openapi"]))
    assert recorded, "产物没记任何输入"
    for p in recorded:
        assert p.is_absolute(), f"出处依赖 cwd，消费者无法解析: {p}"
        assert p.exists(), f"出处指向不存在的文件: {p}"

    def resolve_pointer(doc, pointer):
        cur = doc
        for tok in pointer.lstrip("#/").split("/"):
            cur = cur[tok.replace("~1", "/").replace("~0", "~")]
        return cur

    checked = 0
    for o in report["oracles"]:
        ref = o["source"]["reference"]
        if "#" in ref:
            # 撕裂 invariant：reference 的 basename 必须与 inputs 记录的 basename 一致。
            # 「指针能解开」检不到它 —— 符号链接下两侧可能各用一个名字。
            assert ref.partition(":")[0] == Path(report["inputs"]["openapi"]).name, \
                f"pointer 出处 basename 与 inputs 撕裂: {ref}"
            spec = json.loads(Path(report["inputs"]["openapi"]).read_text())
            resolve_pointer(spec, ref.partition(":")[2])
            checked += 1
        else:
            name, _, lineno = ref.partition(":")
            match = [p for p in recorded if p.name == name]
            assert match, f"出处里没有 {name}，无法回原文: {ref}"
            lines = match[0].read_text(encoding="utf-8").splitlines()
            assert 1 <= int(lineno.split("-")[0]) <= len(lines), f"行号越界: {ref}"
            checked += 1
    assert checked == len(report["oracles"]) > 0, "没有真正逐条回原文"
    kinds = {o["source"]["kind"] for o in report["oracles"]}
    assert {"document", "ddl", "annotation", "openapi"} <= kinds, \
        f"夹具必须覆盖四类输入，否则对应那处变异杀不死；实际 {kinds}"
    assert any("#" in o["source"]["reference"] for o in report["oracles"]), \
        "夹具没产生 pointer 型判据，这条守卫会是空的"
