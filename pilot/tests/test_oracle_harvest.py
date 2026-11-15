"""oracle_harvest 回归守卫 —— 锁住独立验证者抓到的四个真缺陷。

每个测试对应一个曾经存在的洞，全部用合成夹具，不依赖真实语料：
  1. reference 行号必须指向规则自己所在行（DDL 逐约束 / record 逐注解）
  2. 反引号剥离不得污染枚举值；整行包反引号的 CHECK 仍要采到
  3. 验收条款载荷保留原文反引号
  4. markdown 确认表被采到；未定义仍 refuted；需产品确认/文档陈旧不得当 confirmed；
     散文规则进 untranscribed 而非静默丢弃
"""
from __future__ import annotations

import os
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

# ---- 出处可移植：声明根 + 相对路径，既 cwd 无关又跨机器（锁 schema_version 2）----
def _build_provenance_fixture(base: Path) -> Path:
    """四类输入全给（相对路径）+ 符号链接 openapi。返回采集 cwd。

    四类齐全才能杀 migrations/java_roots 那两处变异；符号链接用来锁 basename 不撕裂。
    """
    import json
    base.mkdir(parents=True, exist_ok=True)
    (base / "d.md").write_text("## Check\n\n- `price_cent >= 0`\n", encoding="utf-8")
    (base / "mig").mkdir()
    (base / "mig" / "V1__t.sql").write_text(
        "CREATE TABLE t (\n  id BIGINT NOT NULL\n);\n", encoding="utf-8")
    (base / "src").mkdir()
    (base / "src" / "ThingRequest.java").write_text(
        "public record ThingRequest(\n    @NotNull Long id\n) {}\n", encoding="utf-8")
    (base / "real").mkdir()
    (base / "real" / "spec.json").write_text(json.dumps(
        {"components": {"schemas": {"Thing": {
            "required": ["name"],
            "properties": {"name": {"type": "string"}}}}}}), encoding="utf-8")
    (base / "link.json").symlink_to(base / "real" / "spec.json")
    return base


def _verify_provenance(oh, report: dict, root_override=None) -> int:
    """消费者视角：只用产物重建出处，逐条回原文。返回核到的判据数。

    不变式：重建路径绝对且存在；pointer 型 basename 与 inputs 不撕裂；行号在界内。
    """
    import json
    resolved = oh.resolve_inputs(report, root_override=root_override)
    recorded = list(resolved["docs"]) + list(resolved["migrations"]) + list(resolved["java_roots"])
    if resolved["openapi"]:
        recorded.append(resolved["openapi"])
    assert recorded, "产物没记任何输入"
    for p in recorded:
        assert p.is_absolute(), f"重建后仍非绝对路径: {p}"
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
            assert ref.partition(":")[0] == resolved["openapi"].name, \
                f"pointer 出处 basename 与 inputs 撕裂: {ref}"
            spec = json.loads(resolved["openapi"].read_text())
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
    return checked


def test_provenance_is_cwd_independent_and_self_sufficient(tmp_path, monkeypatch):
    """消费者只拿产物、不知采集时 cwd，也要能解析每条判据出处（坑 69 不回归）。

    新不变式：产物记**根相对路径**（不再机器相关），消费者用 resolve_inputs 重建。
    退回绝对路径记录 / 丢根 / 退回 cwd 依赖，都会被变异矩阵判红。
    """
    import json
    from engine import oracle_harvest as oh

    harvest_cwd = _build_provenance_fixture(tmp_path / "harvest")
    monkeypatch.chdir(harvest_cwd)
    monkeypatch.setattr(sys, "argv", [
        "oracle_harvest", "--docs", "d.md", "--migrations", "mig",
        "--java-root", "src", "--openapi", "link.json", "--out", "out.json"])
    oh.main()
    report = json.loads((harvest_cwd / "out.json").read_text())

    assert report["schema_version"] == 2
    assert report["inputs"]["root"], "产物必须声明根绑定"
    raw = list(report["inputs"]["docs"]) + list(report["inputs"]["migrations"]) \
        + list(report["inputs"]["java_roots"]) + [report["inputs"]["openapi"]]
    for p in raw:
        assert not Path(p).is_absolute(), f"出处仍是机器相关绝对路径，不可移植: {p}"

    # 换一个 cwd 当消费者：此后只许用产物 + resolve_inputs
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.chdir(consumer)
    checked = _verify_provenance(oh, report)
    kinds = {o["source"]["kind"] for o in report["oracles"]}
    assert {"document", "ddl", "annotation", "openapi"} <= kinds, \
        f"夹具必须覆盖四类输入，否则对应那处变异杀不死；实际 {kinds}"
    assert any("#" in o["source"]["reference"] for o in report["oracles"]), \
        "夹具没产生 pointer 型判据，这条守卫会是空的"
    assert checked > 0


def test_provenance_root_portable_across_machines(tmp_path, monkeypatch):
    """新不变式：换 root（模拟另一台机器）后仍能逐条回原文。

    把整棵输入树搬到新根、删掉旧根，只重声明 root —— 全部判据出处仍须解析。
    """
    import json
    import shutil
    from engine import oracle_harvest as oh

    machine_a = _build_provenance_fixture(tmp_path / "machineA")
    monkeypatch.chdir(machine_a)
    monkeypatch.setattr(sys, "argv", [
        "oracle_harvest", "--docs", "d.md", "--migrations", "mig",
        "--java-root", "src", "--openapi", "link.json", "--out", "out.json"])
    oh.main()
    report = json.loads((machine_a / "out.json").read_text())
    out_copy = tmp_path / "report.json"
    out_copy.write_text(json.dumps(report), encoding="utf-8")

    # 模拟另一台机器：整棵树搬到 machineB，旧根不复存在
    machine_b = tmp_path / "machineB"
    shutil.move(str(machine_a), str(machine_b))
    assert not machine_a.exists()
    report = json.loads(out_copy.read_text())

    monkeypatch.chdir(tmp_path)  # 消费者 cwd 与任何根都无关
    checked = _verify_provenance(oh, report, root_override=machine_b)
    assert checked == len(report["oracles"]) > 0


def test_resolve_inputs_backward_compatible_with_absolute_artifact(tmp_path):
    """向后兼容（限定口径）：**全绝对路径**旧产物（schema_version 1，无 root）原样返回。

    注意：这只覆盖全绝对旧产物。裸相对无 root 的坑69原缺陷态由
    test_resolve_inputs_real_legacy_artifact_fails_loud 喂真产物锁定（显式抛错）。
    """
    from engine import oracle_harvest as oh
    f = tmp_path / "V1__t.sql"
    f.write_text("CREATE TABLE t (\n  id BIGINT NOT NULL\n);\n", encoding="utf-8")
    old = {"schema_version": 1, "inputs": {
        "migrations": [str(f)], "java_roots": [], "docs": [], "openapi": None}}
    resolved = oh.resolve_inputs(old)
    assert resolved["migrations"] == [f]
    assert resolved["migrations"][0].exists()
    # 混合：绝对路径条目即便声明了 root 也原样保留（绝对路径优先）
    mixed = {"inputs": {"root": str(tmp_path), "migrations": [str(f)],
                        "java_roots": [], "docs": [], "openapi": None}}
    assert oh.resolve_inputs(mixed)["migrations"] == [f]


def test_resolve_inputs_real_legacy_artifact_fails_loud():
    """必修1（坑142）：守卫喂**仓库里真那份**旧产物，不是合成理想化的旧产物。

    真旧产物正是坑69原缺陷态（openapi="openapi.json" 裸相对、无 root）→ 助手显式抛错
    （这条路径分子是 0，不是 215；215 是绕开助手字面读 inputs 的严格算法 A 的数）。
    抛错是设计正确（绝不猜 cwd）；全绝对条目本身仍可解析，兼容口径限定在全绝对旧产物。
    """
    import json
    import pytest
    from engine import oracle_harvest as oh
    pilot = Path(__file__).resolve().parent.parent
    real = pilot / "oracles_harvested.json"
    if not real.is_file():
        pytest.skip("真实旧产物不在本机")
    old = json.loads(real.read_text(encoding="utf-8"))
    assert old.get("schema_version") == 1 and not old["inputs"].get("root")
    assert old["inputs"]["openapi"] == "openapi.json", "真旧产物应是坑69原缺陷态"
    with pytest.raises(ValueError, match="openapi.json"):
        oh.resolve_inputs(old)
    # 全绝对条目（migrations/java_roots/docs）单独喂仍可解析 —— 兼容口径的真实边界
    abs_only = {"inputs": {"migrations": old["inputs"]["migrations"],
                           "java_roots": old["inputs"]["java_roots"],
                           "docs": old["inputs"]["docs"], "openapi": None}}
    r = oh.resolve_inputs(abs_only)
    assert r["migrations"] and all(p.is_absolute() for p in r["migrations"])


def test_resolve_inputs_relative_without_root_raises(tmp_path):
    """相对路径却无根可声明 → 显式抛错，绝不静默退回 cwd（那会重新引入坑 69）。"""
    import pytest
    from engine import oracle_harvest as oh
    bad = {"inputs": {"migrations": ["mig/V1__t.sql"], "java_roots": [],
                      "docs": [], "openapi": None}}
    with pytest.raises(ValueError):
        oh.resolve_inputs(bad)


def test_resolve_inputs_rejects_relative_root_override(tmp_path, monkeypatch):
    """必修2（坑144）：相对 root_override 显式拒绝 —— 「cwd 永不参与」对参数侧也成立。

    绝对 override 则与 cwd 无关：重建结果是绝对路径，重建后换 cwd 仍存在（不漂）。
    """
    import pytest
    from engine import oracle_harvest as oh
    report = {"inputs": {"root": str(tmp_path), "migrations": ["mig/V1.sql"],
                         "java_roots": [], "docs": [], "openapi": None}}
    with pytest.raises(ValueError, match="绝对路径"):
        oh.resolve_inputs(report, root_override="relative/root")
    (tmp_path / "mig").mkdir()
    (tmp_path / "mig" / "V1.sql").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = oh.resolve_inputs(report, root_override=str(tmp_path))["migrations"][0]
    assert p.is_absolute() and p.exists()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert p.exists(), "绝对 override 的重建结果随 cwd 漂移（坑144 回归）"


def test_resolve_inputs_rejects_relative_artifact_root():
    """**无绝对 override 时**产物声明的 root 若是相对路径，显式拒绝（cwd 入口逐个堵，坑144）。

    有绝对 override 时相对产物 root 被遮蔽、不参与重建 —— 那是合法接受，不是缺陷。
    """
    import pytest
    from engine import oracle_harvest as oh
    bad = {"inputs": {"root": "relative/root", "migrations": ["mig/V1.sql"],
                      "java_roots": [], "docs": [], "openapi": None}}
    with pytest.raises(ValueError, match="绝对路径"):
        oh.resolve_inputs(bad)
    # 绝对 override 遮蔽相对产物 root：接受，且 cwd 未参与（重建为 override 下的绝对路径）
    ok = oh.resolve_inputs(bad, root_override="/abs/root")
    assert ok["migrations"] == [Path("/abs/root/mig/V1.sql")]


def test_provenance_legit_symlink_not_false_red(tmp_path, monkeypatch):
    """反向（坑 78）：合法符号链接输入（路径非规范）不得被误红 —— basename 一致即合法。"""
    import json
    from engine import oracle_harvest as oh
    harvest_cwd = _build_provenance_fixture(tmp_path / "harvest")
    monkeypatch.chdir(harvest_cwd)
    monkeypatch.setattr(sys, "argv", [
        "oracle_harvest", "--docs", "d.md", "--migrations", "mig",
        "--java-root", "src", "--openapi", "link.json", "--out", "out.json"])
    oh.main()
    report = json.loads((harvest_cwd / "out.json").read_text())
    # openapi 经符号链接采集，记录的是解析后的规范相对路径；reference basename 必须一致
    assert report["inputs"]["openapi"].endswith("spec.json")
    monkeypatch.chdir(tmp_path)
    _verify_provenance(oh, report)  # 不抛即合法不误红


# ---- 入库变异矩阵（坑 123）：精确复刻缺陷态，指名红名；空对照与等价变异须存活 ----
# 每个变异只改一处、复刻一个可移植性缺陷态；消费者脚本独立重建出处并逐条回原文。
_CONSUMER_SCRIPT = r'''
import importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("oh_mut", sys.argv[1])
oh = importlib.util.module_from_spec(spec); spec.loader.exec_module(oh)
fails = []
def check(name, fn):
    try:
        fn()
    except Exception as e:
        fails.append(f"RED:{name}: {type(e).__name__} {e}")
work = Path(tempfile.mkdtemp())
A = work / "A"; A.mkdir()
(A / "d.md").write_text("## Check\n\n- `price_cent >= 0`\n", encoding="utf-8")
(A / "mig").mkdir(); (A / "mig" / "V1__t.sql").write_text("CREATE TABLE t (\n  id BIGINT NOT NULL\n);\n", encoding="utf-8")
(A / "src").mkdir(); (A / "src" / "ThingRequest.java").write_text("public record ThingRequest(\n    @NotNull Long id\n) {}\n", encoding="utf-8")
(A / "real").mkdir(); (A / "real" / "spec.json").write_text(json.dumps({"components": {"schemas": {"Thing": {"required": ["name"], "properties": {"name": {"type": "string"}}}}}}), encoding="utf-8")
(A / "link.json").symlink_to(A / "real" / "spec.json")
os.chdir(A)
sys.argv = ["oh", "--docs", "d.md", "--migrations", "mig", "--java-root", "src", "--openapi", "link.json", "--out", "out.json"]
oh.main()
report = json.loads((A / "out.json").read_text())

def verify(root_override=None):
    r = oh.resolve_inputs(report, root_override=root_override)
    rec = list(r["docs"]) + list(r["migrations"]) + list(r["java_roots"]) + ([r["openapi"]] if r["openapi"] else [])
    for p in rec:
        assert p.is_absolute() and p.exists(), f"bad path {p}"
    for o in report["oracles"]:
        ref = o["source"]["reference"]
        if "#" in ref:
            assert ref.partition(":")[0] == r["openapi"].name, "torn basename"
            json.loads(r["openapi"].read_text())
        else:
            name, _, ln = ref.partition(":")
            m = [p for p in rec if p.name == name]
            assert m, f"no source for {name}"
            assert 1 <= int(ln) <= len(m[0].read_text(encoding="utf-8").splitlines())

def _root_declared():
    assert report["inputs"].get("root"), "root missing"
check("root_declared", _root_declared)
def _relative():
    raw = list(report["inputs"]["docs"]) + list(report["inputs"]["migrations"]) + list(report["inputs"]["java_roots"]) + [report["inputs"]["openapi"]]
    for p in raw:
        assert not Path(p).is_absolute(), f"absolute recorded {p}"
check("recorded_relative", _relative)
def _cwd():
    B = work / "B"; B.mkdir(exist_ok=True); os.chdir(B)
    verify()
check("cwd_independent", _cwd)
def _compat():
    f = A / "mig" / "V1__t.sql"
    old = {"schema_version": 1, "inputs": {"migrations": [str(f)], "java_roots": [], "docs": [], "openapi": None}}
    rr = oh.resolve_inputs(old)
    assert rr["migrations"] == [f] and rr["migrations"][0].exists(), "old absolute artifact broken"
check("backward_compatible", _compat)
def _portable():
    C = work / "C"
    shutil.move(str(A), str(C))
    verify(root_override=C)
check("root_portable", _portable)
if fails:
    print("\n".join(fails)); sys.exit(1)
print("OK all provenance invariants hold")
'''

# (变异名, 原文锚点, 缺陷态替换, 期望指名红名)
_PROVENANCE_MUTATIONS = [
    ("M1_root_dropped",
     '"root": str(root) if root is not None else None,',
     '"root": None,',
     "root_declared"),
    ("M2_absolute_recorded",
     'return str(p.relative_to(root)) if root is not None else str(p)',
     'return str(p)',
     "recorded_relative"),
    ("M3_cwd_fallback",
     'base = Path(root)\n',
     'base = Path.cwd()\n',
     "cwd_independent"),
    ("M4_backward_compat_mangled",
     'if pp.is_absolute():\n            return pp',
     'if pp.is_absolute():\n            return (base or Path.cwd()) / pp.name',
     "backward_compatible"),
    ("M5_ignore_root_override",
     'base = Path(root_override)\n',
     'base = Path(root)\n',
     "root_portable"),
]


def _run_consumer(engine_file: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    import subprocess
    script = tmp_path / "consumer.py"
    script.write_text(_CONSUMER_SCRIPT, encoding="utf-8")
    # PATH 挡板（做法 49）：采集器只读文件，但任何变异都不许调起外部工具链
    blocker = tmp_path / "blocker"
    blocker.mkdir(exist_ok=True)
    for tool in ("javac", "java", "mvn", "docker", "psql"):
        stub = blocker / tool
        stub.write_text(f'#!/bin/sh\necho "W24 PATH BLOCKER: {tool} called (forbidden)" >&2\nexit 99\n')
        stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{blocker}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run([sys.executable, str(script), str(engine_file)],
                          capture_output=True, text=True, timeout=120, env=env)


def test_provenance_mutation_matrix_kills_each_defect(tmp_path):
    """五个精确变异各复刻一个可移植性缺陷态，必须被指名守卫判红（坑 78/123）。"""
    import shutil
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    for name, anchor, defect, expected_red in _PROVENANCE_MUTATIONS:
        assert src.count(anchor) == 1, f"{name}: 锚点不唯一/不存在，变异矩阵须更新"
        sandbox = tmp_path / name
        sandbox.mkdir()
        mutated = sandbox / "oracle_harvest.py"
        mutated.write_text(src.replace(anchor, defect), encoding="utf-8")
        proc = _run_consumer(mutated, sandbox)
        assert proc.returncode != 0, f"{name} 存活（守卫无牙）: {proc.stdout[-300:]}"
        assert f"RED:{expected_red}" in proc.stdout, \
            f"{name} 未命中指名红名 {expected_red}: {proc.stdout[-400:]}"


def test_provenance_mutation_empty_control_and_equivalent_survive(tmp_path):
    """双向（坑 78 反向）：空对照与等价变异必须全绿 —— 守卫不误红合法实现。"""
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    # 空对照：未变异引擎
    ctrl = tmp_path / "control"
    ctrl.mkdir()
    (ctrl / "oracle_harvest.py").write_text(src, encoding="utf-8")
    p0 = _run_consumer(ctrl / "oracle_harvest.py", ctrl)
    assert p0.returncode == 0, f"空对照红 = 守卫自己错了（坑 92）: {p0.stdout[-400:]}"
    # 等价变异：`or []` → `, []`。产物里 docs 恒为列表（非 None），两种写法语义相同。
    # （曾误以为「去掉 is_absolute 分支」等价 —— 实测被 backward_compatible 判红：
    #  旧产物无 root，绝对路径会落到 base is None 抛错。pathlib 等价只在 base 非 None 时成立。）
    anchor = 'inputs.get("docs") or []'
    assert src.count(anchor) == 1
    eq = tmp_path / "equivalent"
    eq.mkdir()
    (eq / "oracle_harvest.py").write_text(
        src.replace(anchor, 'inputs.get("docs", [])'), encoding="utf-8")
    p1 = _run_consumer(eq / "oracle_harvest.py", eq)
    assert p1.returncode == 0, f"等价变异被误红（守卫过度）: {p1.stdout[-400:]}"


# ---- 必修3（坑143 N1/N2、做法70 N5）：生产者侧根覆盖 + 消费侧异名符号链接 ----
# 入库合成夹具 cwd==root 且单根 → 生产者侧「拿 cwd 当根」(N1) 与「根漏算输入类」(N2)
# 都不可区分；采集侧符号链接已被引擎 resolve → 「重建强制 resolve」(N5) 不可达。
# 本脚本用 cwd≠root / 多根 / 消费侧异名符号链接三种夹具把这些缺陷态变可达，崩溃映射到指名红。
_ROOT_COVERAGE_SCRIPT = r'''
import importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("oh_rc", sys.argv[1])
oh = importlib.util.module_from_spec(spec); spec.loader.exec_module(oh)
mode = sys.argv[2]
fails = []
def check(name, fn):
    try:
        fn()
    except Exception as e:
        fails.append(f"RED:{name}: {type(e).__name__} {e}")
work = Path(tempfile.mkdtemp())
parent = work / "parent"; parent.mkdir()
repoA = parent / "repoA"; repoA.mkdir()
(repoA / "mig").mkdir(); (repoA / "mig" / "V1__t.sql").write_text("CREATE TABLE t (\n  id BIGINT NOT NULL\n);\n", encoding="utf-8")
(repoA / "src").mkdir(); (repoA / "src" / "ThingRequest.java").write_text("public record ThingRequest(\n    @NotNull Long id\n) {}\n", encoding="utf-8")
SPEC = json.dumps({"components": {"schemas": {"Thing": {"required": ["name"], "properties": {"name": {"type": "string"}}}}}})
if mode == "n2":
    repoB = parent / "repoB"; repoB.mkdir()
    (repoB / "docs").mkdir(); (repoB / "docs" / "d.md").write_text("## Check\n\n- `price_cent >= 0`\n", encoding="utf-8")
    (repoB / "spec.json").write_text(SPEC, encoding="utf-8")
    docs = repoB / "docs"; openapi = repoB / "spec.json"
else:
    (repoA / "d.md").write_text("## Check\n\n- `price_cent >= 0`\n", encoding="utf-8")
    (repoA / "spec.json").write_text(SPEC, encoding="utf-8")
    docs = repoA / "d.md"; openapi = repoA / "spec.json"
os.chdir(parent)  # n1: cwd=parent 是 root(repoA) 的祖先 → cwd != root
out = work / "out.json"
def harvest():
    sys.argv = ["oh", "--migrations", str(repoA / "mig"), "--java-root", str(repoA / "src"),
                "--docs", str(docs), "--openapi", str(openapi), "--out", str(out)]
    oh.main()
check("producer_root_coverage", harvest)  # 生产者崩溃（根漏算→relative_to 抛错）映射到指名红
if out.exists():
    report = json.loads(out.read_text(encoding="utf-8"))
    def verify(root_override=None):
        r = oh.resolve_inputs(report, root_override=root_override)
        rec = list(r["docs"]) + list(r["migrations"]) + list(r["java_roots"]) + ([r["openapi"]] if r["openapi"] else [])
        for p in rec:
            assert p.is_absolute() and p.exists(), f"bad path {p}"
        for o in report["oracles"]:
            ref = o["source"]["reference"]
            if "#" in ref:
                assert ref.partition(":")[0] == r["openapi"].name, "torn basename"
                json.loads(r["openapi"].read_text(encoding="utf-8"))
            else:
                name, _, ln = ref.partition(":")
                m = [p for p in rec if p.name == name]
                assert m, f"no source for {name}"
                assert 1 <= int(ln) <= len(m[0].read_text(encoding="utf-8").splitlines())
    if mode in ("n1", "n2"):
        check("root_covers_all_inputs", lambda: verify())
    elif mode == "n5":
        def _n5():
            root = Path(report["inputs"]["root"])
            C_root = work / "C_repoA"
            shutil.copytree(root, C_root)
            oa_rel = Path(report["inputs"]["openapi"])
            target_oa = C_root / oa_rel
            content = target_oa.read_text(encoding="utf-8")
            target_oa.unlink()
            (target_oa.parent / "spec-v2.json").write_text(content, encoding="utf-8")
            target_oa.symlink_to("spec-v2.json")  # 目标机上 openapi 是异名符号链接
            verify(root_override=C_root)
        check("no_basename_tearing", _n5)  # 夹具搭建也在 check 内：跨模式变异崩溃亦映射到指名红
if fails:
    print("\n".join(fails)); sys.exit(1)
print("OK root coverage invariants hold")
'''

# (变异名, 原文锚点, 缺陷态, 可达夹具模式, 期望指名红名)
_COVERAGE_MUTATIONS = [
    ("N1_cwd_as_root",
     'return str(p.relative_to(root)) if root is not None else str(p)',
     'return str(p.relative_to(Path.cwd())) if root is not None else str(p)',
     "n1", "root_covers_all_inputs"),
    ("N2_root_omits_input_classes",
     '_provenance_root(ddl_files + java_files + doc_files + ([openapi] if openapi else []))',
     '_provenance_root(ddl_files + java_files)',
     "n2", "producer_root_coverage"),
    ("N5_consumer_resolve_tears",
     'return base / pp',
     'return (base / pp).resolve()',
     "n5", "no_basename_tearing"),
]


def _run_coverage(engine_file: Path, tmp_path: Path, mode: str) -> subprocess.CompletedProcess:
    import subprocess
    script = tmp_path / "coverage.py"
    script.write_text(_ROOT_COVERAGE_SCRIPT, encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.mkdir(exist_ok=True)
    for tool in ("javac", "java", "mvn", "docker", "psql"):
        stub = blocker / tool
        stub.write_text(f'#!/bin/sh\necho "W24 PATH BLOCKER: {tool} called (forbidden)" >&2\nexit 99\n')
        stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{blocker}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run([sys.executable, str(script), str(engine_file), mode],
                          capture_output=True, text=True, timeout=120, env=env)


def test_root_coverage_mutation_matrix_kills_N1_N2_N5(tmp_path):
    """必修3：三个 V22 锚点在可达夹具下各被指名判红（坑143 N1/N2、做法70 N5）。"""
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    for name, anchor, defect, mode, expected_red in _COVERAGE_MUTATIONS:
        assert src.count(anchor) == 1, f"{name}: 锚点不唯一/不存在"
        sandbox = tmp_path / name
        sandbox.mkdir()
        mutated = sandbox / "oracle_harvest.py"
        mutated.write_text(src.replace(anchor, defect), encoding="utf-8")
        proc = _run_coverage(mutated, sandbox, mode)
        assert proc.returncode != 0, f"{name} 存活（守卫无牙）: {proc.stdout[-300:]}"
        assert f"RED:{expected_red}" in proc.stdout, \
            f"{name} 未命中指名红名 {expected_red}: {proc.stdout[-400:]}"


def test_root_coverage_empty_control_all_modes(tmp_path):
    """空对照在三种夹具下全绿（坑92：空对照红=守卫自己错）。"""
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    for mode in ("n1", "n2", "n5"):
        ctrl = tmp_path / f"ctrl_{mode}"
        ctrl.mkdir()
        (ctrl / "oracle_harvest.py").write_text(src, encoding="utf-8")
        p = _run_coverage(ctrl / "oracle_harvest.py", ctrl, mode)
        assert p.returncode == 0, f"空对照 {mode} 红: {p.stdout[-400:]}"


def test_root_coverage_N1_N2_survive_in_wrong_fixture(tmp_path):
    """夹具依赖性自证（坑61/做法70）：N1 在 cwd==root 夹具存活、N2 在单根夹具存活。

    证明旧入库夹具确实测不出这两个生产者侧缺陷，新夹具不是装饰。
    """
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    n1 = _COVERAGE_MUTATIONS[0]
    sb = tmp_path / "n1_wrong"
    sb.mkdir()
    (sb / "oracle_harvest.py").write_text(src.replace(n1[1], n1[2]), encoding="utf-8")
    p = _run_consumer(sb / "oracle_harvest.py", sb)  # 原消费夹具 cwd==root 单根
    assert p.returncode == 0, f"N1 应在 cwd==root 夹具存活（证明旧夹具缺口真实）: {p.stdout[-300:]}"
    n2 = _COVERAGE_MUTATIONS[1]
    sb2 = tmp_path / "n2_wrong"
    sb2.mkdir()
    (sb2 / "oracle_harvest.py").write_text(src.replace(n2[1], n2[2]), encoding="utf-8")
    p2 = _run_coverage(sb2 / "oracle_harvest.py", sb2, "n1")  # 单根夹具
    assert p2.returncode == 0, f"N2 应在单根夹具存活: {p2.stdout[-300:]}"


def test_root_coverage_N4_equivalent_survives(tmp_path):
    """N4 真等价变异（去 `if p is not None`，调用方永不含 None）三夹具均须存活（不误红）。"""
    from engine import oracle_harvest as oh
    src = Path(oh.__file__).read_text(encoding="utf-8")
    anchor = 'parents = [str(p.parent) for p in paths if p is not None]'
    assert src.count(anchor) == 1
    defect = 'parents = [str(p.parent) for p in paths]'
    for mode in ("n1", "n2", "n5"):
        sb = tmp_path / f"n4_{mode}"
        sb.mkdir()
        (sb / "oracle_harvest.py").write_text(src.replace(anchor, defect), encoding="utf-8")
        p = _run_coverage(sb / "oracle_harvest.py", sb, mode)
        assert p.returncode == 0, f"N4 等价变异在 {mode} 被误红: {p.stdout[-400:]}"
