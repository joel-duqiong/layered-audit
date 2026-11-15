"""oracle_bridge 回归守卫 —— 锁住「判据可核实且可执行」这座桥不许塌的几处。

全部用合成夹具，不依赖真实语料（语料由别的 Agent 在写，会漂）：
  1. 映射忠实：>= 只能变 field_gte_const，> 不许被放宽成 >=
  2. 不可映射必须显式报出并带原因，不许静默丢弃、不许猜语义
  3. refuted / needs_confirmation 永不进判定
  4. 缺字段、类型不兼容 → 不可映射（求值器侧 UNKNOWN），绝不 PASS
  5. 空输入不崩溃
  6. 桥产出的每条原语都真的能被两个求值器执行（不是装饰）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import invariants, rule_assessment  # noqa: E402
from engine.oracle_bridge import (  # noqa: E402
    BUSINESS_TIERS, CLOSED_PRIMITIVES, TECHNICAL_TIERS,
    bridge, map_assertion, to_invariants, to_rule_pack,
)


def oracle(assertion, oid="doc:s:1.x", kind="document", confirmation="confirmed", target=None):
    return {"id": oid, "level": "L0_business_rule", "confirmation": confirmation,
            "gate_eligible": False, "note": "synthetic",
            "source": {"kind": kind, "reference": "s.md:1", "text": "synthetic"},
            "target": {"field": "f"} if target is None else target,
            "assert": assertion}


def cmp_assert(op, value=0, **extra):
    return {"type": "field_cmp", "field": "f", "op": op, "value": value, **extra}


# ---- 1. 映射忠实：符号方向不许放宽 ----
def test_ge_maps_to_gte_const():
    specs, reason = map_assertion(cmp_assert(">="))
    assert reason == ""
    assert specs == [{"type": "field_gte_const", "left": "f", "value": 0}]


def test_le_maps_to_lte_const():
    specs, reason = map_assertion(cmp_assert("<="))
    assert reason == ""
    assert specs == [{"type": "field_lte_const", "left": "f", "value": 0}]


@pytest.mark.parametrize("op", ["<", "=", "!=", "eq"])
def test_operators_without_counterpart_are_unmappable_not_widened(op):
    specs, reason = map_assertion(cmp_assert(op))
    assert specs == []
    assert reason
    assert "不得放宽符号方向" in reason or "无对应原语" in reason


def test_gt_maps_to_strict_gt_const_never_to_gte():
    specs, reason = map_assertion(cmp_assert(">", 0))
    assert reason == ""
    assert specs == [{"type": "field_gt_const", "left": "f", "value": 0}]
    result = bridge([oracle(cmp_assert(">", 0))])
    assert result["mapped"][0]["primitive_to"] == "field_gt_const"
    assert result["mapped"][0]["assert"]["type"] != "field_gte_const"


def test_gt_boundary_is_strict_end_to_end():
    pack = to_rule_pack(bridge([oracle(cmp_assert(">", 0))]), "sample")
    at_boundary = rule_assessment.assess(pack, {"observations": [observation({"f": 0})]})["results"][0]
    above = rule_assessment.assess(pack, {"observations": [observation({"f": 1})]})["results"][0]
    assert at_boundary["status"] == "FAIL"
    assert above["status"] == "PASS"
    widened = to_rule_pack(bridge([oracle(cmp_assert(">=", 0))]), "sample")
    assert rule_assessment.assess(widened, {"observations": [observation({"f": 0})]})["results"][0]["status"] == "PASS"


def test_not_null_and_not_blank_map_without_inventing_a_value():
    for primitive, closed in (("not_null", "field_not_null"), ("not_blank", "field_not_blank")):
        specs, reason = map_assertion({"type": primitive, "field": "f"})
        assert reason == ""
        assert specs == [{"type": closed, "left": "f"}]
        assert "value" not in specs[0]


@pytest.mark.parametrize("primitive,closed", [("positive", "field_gt_const"),
                                              ("positive_or_zero", "field_gte_const")])
def test_positive_annotations_map_to_exact_boundary(primitive, closed):
    specs, reason = map_assertion({"type": primitive, "field": "f"})
    assert reason == ""
    assert specs == [{"type": closed, "left": "f", "value": 0}]


def test_in_maps_to_field_in_set():
    specs, reason = map_assertion({"type": "in", "field": "f", "value": ["A", "B"]})
    assert reason == ""
    assert specs == [{"type": "field_in_set", "left": "f", "value": ["A", "B"]}]


def test_range_bounds_and_compound_range_get_distinct_ids():
    specs, reason = map_assertion({"type": "range", "field": "f", "minimum": 1, "maximum": 9})
    assert reason == ""
    assert [s["type"] for s in specs] == ["field_gte_const", "field_lte_const"]
    result = bridge([oracle({"type": "range", "field": "f", "minimum": 1, "maximum": 9})])
    assert [m["id"] for m in result["mapped"]] == ["doc:s:1.x#1", "doc:s:1.x#2"]
    pack = to_rule_pack(result, "sample")
    assert len({r["id"] for r in pack["rules"]}) == 2


# ---- 2. 不可映射显式报出，不猜语义 ----
@pytest.mark.parametrize("primitive", [
    "not_empty", "assert_true", "assert_false", "size", "pattern", "default",
    "unique", "references", "validate_nested", "acceptance_scenario",
    "confirmed_rule", "needs_confirmation", "has_fields", "set_covers", "forbidden_terms",
])
def test_every_producer_primitive_without_counterpart_is_reported(primitive):
    specs, reason = map_assertion({"type": primitive, "field": "f", "value": ["A"]})
    assert specs == []
    assert reason
    result = bridge([oracle({"type": primitive, "field": "f"})])
    assert result["counts"]["mapped_rules"] == 0
    assert len(result["unmappable"]) == 1
    assert result["unmappable"][0]["reason"] == reason


def test_set_covers_is_not_field_in_set():
    specs, reason = map_assertion({"type": "set_covers", "field": "错误码", "value": ["A", "B"]})
    assert specs == []
    assert "field_in_set" in reason


def test_unit_bearing_constraint_is_not_dimensionless_comparison():
    specs, reason = map_assertion(cmp_assert("<=", 5, unit="MB"))
    assert specs == []
    assert "MB" in reason


def test_unknown_primitive_is_reported_not_guessed():
    specs, reason = map_assertion({"type": "brand_new", "field": "f"})
    assert specs == []
    assert "brand_new" in reason


def test_nothing_is_silently_dropped():
    oracles = [oracle(cmp_assert(">="), oid="a"),
               oracle({"type": "size", "field": "f", "max": 9}, oid="b"),
               oracle({"type": "pattern", "field": "f", "value": "^a$"}, oid="c"),
               oracle(cmp_assert(">="), oid="d", confirmation="needs_confirmation"),
               oracle(cmp_assert(">="), oid="e", confirmation="refuted")]
    result = bridge(oracles)
    c = result["counts"]
    assert c["total"] == 5
    assert c["mapped_rules"] + c["unmappable"] + len(result["excluded"]) == 5
    assert {m["oracle_id"] for m in result["mapped"]} == {"a"}
    assert {u["id"] for u in result["unmappable"]} == {"b", "c"}
    assert {e["id"] for e in result["excluded"]} == {"d", "e"}


# ---- 3. refuted / needs_confirmation 永不进判定 ----
def test_refuted_never_enters_judgment():
    undefined = [{"id": "doc:undefined:s:1", "confirmation": "refuted",
                  "source": {"kind": "document", "reference": "s.md:1", "text": "x"}}]
    result = bridge([oracle(cmp_assert(">="))], undefined)
    assert all(e["id"] != "doc:undefined:s:1" for e in result["mapped"])
    assert any(e["id"] == "doc:undefined:s:1" for e in result["excluded"])
    assert to_rule_pack(result, "sample")["rules"]
    assert all(r["id"] != "doc:undefined:s:1" for r in to_rule_pack(result, "sample")["rules"])
    assert all(i["id"] != "doc:undefined:s:1" for i in to_invariants(result))


def test_refuted_inside_oracles_is_excluded_even_if_mappable():
    result = bridge([oracle(cmp_assert(">="), confirmation="refuted")])
    assert result["counts"]["mapped_rules"] == 0
    assert result["excluded"][0]["confirmation"] == "refuted"


def test_needs_confirmation_is_never_confirmed():
    result = bridge([oracle(cmp_assert(">="), confirmation="needs_confirmation")])
    assert result["counts"]["mapped_rules"] == 0
    assert result["excluded"][0]["confirmation"] == "needs_confirmation"
    assert to_rule_pack(result, "sample")["rules"] == []


# ---- 4. 缺字段 / 类型不兼容 ----
@pytest.mark.parametrize("assertion", [
    {"type": "field_cmp", "op": ">=", "value": 0},
    {"type": "field_cmp", "field": "", "op": ">=", "value": 0},
    {"type": "field_cmp", "field": "  ", "op": ">=", "value": 0},
    {"type": "in"},
    {"type": "range", "minimum": 1},
])
def test_missing_field_is_unmappable(assertion):
    specs, reason = map_assertion(assertion)
    assert specs == []
    assert "缺 field" in reason


@pytest.mark.parametrize("value", ["0", None, True, [0], {}, float("nan"), float("inf")])
def test_type_incompatible_constant_is_unmappable(value):
    specs, reason = map_assertion(cmp_assert(">=", value))
    assert specs == []
    assert "类型不兼容" in reason


@pytest.mark.parametrize("value", [[], None, "A", [1, {}], [1, float("inf")]])
def test_bad_enum_domain_is_unmappable(value):
    specs, reason = map_assertion({"type": "in", "field": "f", "value": value})
    assert specs == []
    assert reason


def test_range_without_bounds_is_unmappable():
    specs, reason = map_assertion({"type": "range", "field": "f"})
    assert specs == []
    assert reason


@pytest.mark.parametrize("assertion", [None, {}, {"type": ""}, "field_cmp", []])
def test_malformed_assertion_is_unmappable(assertion):
    specs, reason = map_assertion(assertion)
    assert specs == []
    assert reason


def test_missing_field_reaches_evaluator_as_unknown_not_pass():
    result = bridge([oracle({"type": "field_cmp", "op": ">=", "value": 0})])
    assert result["counts"]["mapped_rules"] == 0
    assert result["unmappable"][0]["reason"].startswith("field_cmp 缺 field")


# ---- 5. 空输入不崩溃 ----
@pytest.mark.parametrize("oracles", [[], None])
def test_empty_input(oracles):
    result = bridge(oracles or [])
    assert result["counts"]["total"] == 0
    assert result["mapped"] == [] and result["unmappable"] == []
    assert to_rule_pack(result, "sample") == {"schema": "business-rules/1", "target": "sample", "rules": []}
    assert to_invariants(result) == []
    report = rule_assessment.assess(to_rule_pack(result, "sample"), {"observations": []})
    assert report["intake_status"] == "UNKNOWN"


def test_oracle_without_id_or_with_duplicate_id_is_reported():
    result = bridge([{"confirmation": "confirmed", "assert": cmp_assert(">=")},
                     oracle(cmp_assert(">="), oid="dup"), oracle(cmp_assert(">="), oid="dup")])
    assert result["counts"]["mapped_rules"] == 1
    assert len(result["unmappable"]) == 2


# ---- 6. 桥产出的原语真的可执行 ----
def test_emitted_primitives_are_subset_of_closed_set():
    result = bridge([oracle(cmp_assert(">="), oid="a"), oracle(cmp_assert("<="), oid="b"),
                     oracle({"type": "in", "field": "f", "value": ["A"]}, oid="c"),
                     oracle({"type": "range", "field": "f", "minimum": 1}, oid="d"),
                     oracle(cmp_assert(">"), oid="e"),
                     oracle({"type": "not_null", "field": "f"}, oid="g"),
                     oracle({"type": "not_blank", "field": "f"}, oid="h"),
                     oracle({"type": "field_gte_const", "left": "f", "value": 1}, oid="i")])
    emitted = {m["assert"]["type"] for m in result["mapped"]}
    assert emitted <= CLOSED_PRIMITIVES
    assert emitted == {"field_gte_const", "field_lte_const", "field_in_set",
                       "field_gt_const", "field_not_null", "field_not_blank"}


@pytest.mark.parametrize("spec,row,expected", [
    ({"type": "field_gte_const", "left": "f", "value": 0}, {"f": 0}, "pass"),
    ({"type": "field_gte_const", "left": "f", "value": 0}, {"f": -1}, "fail"),
    ({"type": "field_lte_const", "left": "f", "value": 9}, {"f": 9}, "pass"),
    ({"type": "field_lte_const", "left": "f", "value": 9}, {"f": 10}, "fail"),
    ({"type": "field_in_set", "left": "f", "value": ["A", "B"]}, {"f": "A"}, "pass"),
    ({"type": "field_in_set", "left": "f", "value": ["A", "B"]}, {"f": "C"}, "fail"),
    ({"type": "field_gt_const", "left": "f", "value": 0}, {"f": 1}, "pass"),
    ({"type": "field_gt_const", "left": "f", "value": 0}, {"f": 0}, "fail"),
    ({"type": "field_not_null", "left": "f"}, {"f": 0}, "pass"),
    ({"type": "field_not_null", "left": "f"}, {"f": ""}, "pass"),
    ({"type": "field_not_null", "left": "f"}, {"f": None}, "fail"),
    ({"type": "field_not_null", "left": "f"}, {}, "unknown"),
    ({"type": "field_not_blank", "left": "f"}, {"f": "x"}, "pass"),
    ({"type": "field_not_blank", "left": "f"}, {"f": "   "}, "fail"),
    ({"type": "field_not_blank", "left": "f"}, {"f": None}, "fail"),
    ({"type": "field_not_blank", "left": "f"}, {"f": 5}, "unknown"),
    ({"type": "field_not_blank", "left": "f"}, {}, "unknown"),
])
def test_both_evaluators_execute_every_emitted_primitive(spec, row, expected):
    assert invariants.evaluate(row, {"id": "x", "assert": spec})[0] == expected
    rule = {"id": "x", "scope": "s", "assert": spec,
            "source": {"kind": "document", "reference": "s.md:1",
                       "confirmation": "confirmed", "confirmed_by": "document:s.md:1"}}
    obs = {"id": "o", "target": "sample", "scope": "s", "row": row, "reached": True,
           "evidence_refs": ["synthetic:1"]}
    got = rule_assessment.evaluate_observation(rule, obs, "sample")
    assert got == expected.upper()


@pytest.mark.parametrize("value", [0, 1, -1, 1.5, "0", True, False, None, float("nan"), float("inf")])
def test_bridge_mappability_agrees_with_evaluator_decidability(value):
    specs, _ = map_assertion(cmp_assert(">=", value))
    rule = {"id": "x", "scope": "s", "assert": {"type": "field_gte_const", "left": "f", "value": value},
            "source": {"kind": "document", "reference": "s.md:1",
                       "confirmation": "confirmed", "confirmed_by": "document:s.md:1"}}
    obs = {"id": "o", "target": "sample", "scope": "s", "row": {"f": 1}, "reached": True,
           "evidence_refs": ["synthetic:1"]}
    decidable = rule_assessment.evaluate_observation(rule, obs, "sample") in ("PASS", "FAIL")
    assert bool(specs) == decidable


def observation(row, scope="s.md:1", reached=True, refs=None):
    return {"id": "o", "target": "sample", "scope": scope, "row": row, "reached": reached,
            "evidence_refs": ["synthetic:1"] if refs is None else refs}


def pack_for(assertion):
    return to_rule_pack(bridge([oracle(assertion)]), "sample")


def test_no_observations_is_unknown_not_pass():
    pack = pack_for(cmp_assert(">="))
    report = rule_assessment.assess(pack, {"observations": []})
    assert report["intake_status"] == "RULES_PRESENT"
    result = report["results"][0]
    assert result["observed_status"] == "UNKNOWN"
    assert result["status"] == "UNKNOWN"
    assert result["gate_eligible"] is False


def test_missing_column_is_unknown_not_pass():
    pack = pack_for(cmp_assert(">="))
    for obs in ([observation({})], [observation({"f": None})], [observation({"f": "0"})],
                [observation({"f": 0}, reached=False)], [observation({"f": 0}, refs=[])],
                [observation({"f": 0}, scope="other")]):
        result = rule_assessment.assess(pack, {"observations": obs})["results"][0]
        assert result["status"] == "UNKNOWN", obs


def test_synthetic_observation_reaches_pass_and_fail():
    pack = pack_for(cmp_assert(">="))
    assert rule_assessment.assess(pack, {"observations": [observation({"f": 5})]})["results"][0]["status"] == "PASS"
    assert rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]["status"] == "FAIL"


def test_boundary_is_faithful_end_to_end():
    pack = pack_for(cmp_assert(">="))
    assert rule_assessment.assess(pack, {"observations": [observation({"f": 0})]})["results"][0]["status"] == "PASS"
    assert rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]["status"] == "FAIL"


def test_technical_tier_is_legitimately_confirmed_and_can_decide():
    """W11 两 tier 门控：技术约束 tier 是合法 confirmed，可产出判定。

    原意图（未确认来源不得被当成 confirmed）保留在下面三个守卫里；
    过期的只是「ddl 必须被压成 UNKNOWN」这个断言形式（坑 18）。
    """
    for kind in ("ddl", "annotation", "openapi"):
        pack = to_rule_pack(bridge([oracle(cmp_assert(">="), kind=kind)]), "sample")
        result = rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]
        assert result["observed_status"] == "FAIL"
        assert result["status"] == "FAIL"
        assert result["source_confirmation"] == "declared_confirmed"
        assert result["confirmation_tier"] == "technical"
        assert result["gate_eligible"] is False


def test_business_tier_still_decides_and_is_labelled_separately():
    pack = to_rule_pack(bridge([oracle(cmp_assert(">="), kind="document")]), "sample")
    result = rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]
    assert result["status"] == "FAIL"
    assert result["confirmation_tier"] == "business"


def test_unrecognized_kind_is_still_capped_at_unknown():
    pack = to_rule_pack(bridge([oracle(cmp_assert(">="), kind="rumor")]), "sample")
    result = rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "UNKNOWN"
    assert result["source_confirmation"] == "unconfirmed"
    assert result["confirmation_tier"] is None


@pytest.mark.parametrize("drop", ["confirmed_by", "reference"])
def test_missing_provenance_is_still_capped_at_unknown(drop):
    pack = to_rule_pack(bridge([oracle(cmp_assert(">="), kind="ddl")]), "sample")
    pack["rules"][0]["source"].pop(drop)
    result = rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "UNKNOWN"
    assert result["source_confirmation"] == "unconfirmed"


@pytest.mark.parametrize("confirmation", ["needs_confirmation", "refuted", "unconfirmed", ""])
def test_non_confirmed_is_capped_at_unknown_under_new_gate(confirmation):
    pack = to_rule_pack(bridge([oracle(cmp_assert(">="), kind="ddl")]), "sample")
    pack["rules"][0]["source"]["confirmation"] = confirmation
    result = rule_assessment.assess(pack, {"observations": [observation({"f": -1})]})["results"][0]
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "UNKNOWN"


def test_needs_confirmation_and_refuted_never_reach_the_gate_at_all():
    for confirmation in ("needs_confirmation", "refuted"):
        result = bridge([oracle(cmp_assert(">="), kind="ddl", confirmation=confirmation)])
        assert result["counts"]["mapped_rules"] == 0
        assert to_rule_pack(result, "sample")["rules"] == []
        assert to_invariants(result) == []


def test_tier_split_is_reported_and_matches_the_evaluator():
    from engine import rule_assessment as ra
    assert BUSINESS_TIERS == tuple(ra.BUSINESS_KINDS)
    assert TECHNICAL_TIERS == tuple(ra.TECHNICAL_KINDS)
    result = bridge([oracle(cmp_assert(">="), oid="b", kind="document"),
                     oracle(cmp_assert(">="), oid="t1", kind="ddl"),
                     oracle(cmp_assert(">="), oid="t2", kind="annotation"),
                     oracle(cmp_assert(">="), oid="t3", kind="openapi"),
                     oracle(cmp_assert(">="), oid="u", kind="rumor")])
    tiers = result["counts"]["by_confirmation_tier"]
    assert tiers == {"business": 1, "technical": 3, "unrecognized": 1}
    assert sum(tiers.values()) == result["counts"]["mapped_rules"]


# ---- 11. 已武装未触发：求值器不消费的键不得静默忽略或静默销毁 ----
# 语料当前 exclusive/negate/when 全 0 发射、unit 6 条全 needs_confirmation，
# 所以这些闸门尚未被触发。但语义风险是真的：直通逐字带过去而求值器忽略该键
# → 极性被放宽 / 语义被反转；映射路径直接销毁且 reason="" 报成完全可映射。

@pytest.mark.parametrize("primitive,base,extra", [
    ("field_gte_const", {"left": "f", "value": 1}, {"exclusive": True}),
    ("field_gt_const", {"left": "f", "value": 1}, {"exclusive": True}),
    ("field_lte_const", {"left": "f", "value": 9}, {"negate": True}),
    ("field_in_set", {"left": "f", "value": ["A"]}, {"negate": True}),
    ("field_not_null", {"left": "f"}, {"negate": True}),
    ("field_not_blank", {"left": "f"}, {"exclusive": True}),
    ("field_size", {"left": "f", "min": 1}, {"negate": True}),
    ("field_pattern", {"left": "f", "value": "^a$"}, {"exclusive": True}),
    ("field_eq_field", {"left": "f", "right": "g"}, {"negate": True}),
    ("text_not_contains", {"left": "f", "value": ["x"]}, {"negate": True}),
    ("field_gte_const", {"left": "f", "value": 1}, {"some_future_modifier": 1}),
])
def test_unconsumed_keys_rejected_on_passthrough(primitive, base, extra):
    assertion = {"type": primitive, **base, **extra}
    specs, reason = map_assertion(assertion)
    assert specs == []
    assert next(iter(extra)) in reason
    assert "不消费" in reason
    result = bridge([oracle(assertion)])
    assert result["counts"]["mapped_rules"] == 0
    assert result["unmappable"][0]["reason"] == reason


@pytest.mark.parametrize("assertion,extra", [
    ({"type": "range", "field": "f", "minimum": 1, "maximum": 100}, {"exclusive": True}),
    ({"type": "range", "field": "f", "minimum": 1}, {"negate": True}),
    ({"type": "field_cmp", "field": "f", "op": ">=", "value": 1}, {"exclusive": True}),
    ({"type": "field_cmp", "field": "f", "op": ">", "value": 0}, {"negate": True}),
    ({"type": "in", "field": "f", "value": ["A"]}, {"negate": True}),
    ({"type": "not_null", "field": "f"}, {"exclusive": True}),
    ({"type": "not_blank", "field": "f"}, {"negate": True}),
])
def test_unconsumed_keys_rejected_on_mapping_path(assertion, extra):
    specs, reason = map_assertion({**assertion, **extra})
    assert specs == []
    assert next(iter(extra)) in reason
    result = bridge([oracle({**assertion, **extra})])
    assert result["counts"]["mapped_rules"] == 0
    assert len(result["unmappable"]) == 1


def test_exclusive_would_relax_polarity_so_it_must_be_rejected():
    """决定性证据：若放行，求值器忽略 exclusive，边界值被判 pass（极性效果上被破）。"""
    assertion = {"type": "field_gte_const", "left": "f", "value": 1, "exclusive": True}
    naive = {"id": "x", "scope": "s", "assert": assertion,
             "source": {"kind": "document", "reference": "d.md:1",
                        "confirmation": "confirmed", "confirmed_by": "document:d.md:1"}}
    obs = {"id": "o", "target": "T", "scope": "s", "row": {"f": 1},
           "reached": True, "evidence_refs": ["synthetic:1"]}
    assert rule_assessment.evaluate_observation(naive, obs, "T") == "PASS"
    assert invariants.evaluate({"f": 1}, {"id": "x", "assert": assertion})[0] == "pass"
    specs, reason = map_assertion(assertion)
    assert specs == [] and "exclusive" in reason


def test_negate_would_invert_semantics_so_it_must_be_rejected():
    assertion = {"type": "field_in_set", "left": "f", "value": ["A"], "negate": True}
    naive = {"id": "x", "scope": "s", "assert": assertion,
             "source": {"kind": "document", "reference": "d.md:1",
                        "confirmation": "confirmed", "confirmed_by": "document:d.md:1"}}
    obs = {"id": "o", "target": "T", "scope": "s", "row": {"f": "A"},
           "reached": True, "evidence_refs": ["synthetic:1"]}
    assert rule_assessment.evaluate_observation(naive, obs, "T") == "PASS"
    specs, reason = map_assertion(assertion)
    assert specs == [] and "negate" in reason


def test_clean_assertions_are_not_over_rejected():
    for assertion in ({"type": "field_gte_const", "left": "f", "value": 1},
                      {"type": "field_in_set", "left": "f", "value": ["A"]},
                      {"type": "field_not_null", "left": "f"},
                      {"type": "field_size", "left": "f", "min": 1, "max": 9},
                      {"type": "field_eq_field", "left": "f", "right": "g"},
                      {"type": "range", "field": "f", "minimum": 1, "maximum": 100},
                      {"type": "field_cmp", "field": "f", "op": ">=", "value": 0},
                      {"type": "in", "field": "f", "value": ["A"]},
                      {"type": "not_null", "field": "f"}):
        specs, reason = map_assertion(assertion)
        assert specs and reason == "", assertion


def test_range_upper_bound_is_emitted_not_destroyed():
    specs, reason = map_assertion({"type": "range", "field": "f", "minimum": 1, "maximum": 100})
    assert reason == ""
    assert specs == [{"type": "field_gte_const", "left": "f", "value": 1},
                     {"type": "field_lte_const", "left": "f", "value": 100}]
    result = bridge([oracle({"type": "range", "field": "f", "minimum": 1, "maximum": 100})])
    assert result["counts"]["mapped_rules"] == 2
    assert {i["assert"]["value"] for i in to_invariants(result)} == {1, 100}


def test_consumed_keys_table_only_allows_keys_evaluators_really_read():
    import inspect
    import re
    from engine import oracle_bridge as ob
    src = inspect.getsource(invariants.evaluate) + inspect.getsource(rule_assessment.evaluate_observation)
    read = {k for tup in re.findall(r'a\["(\w+)"\]|a\.get\("(\w+)"|spec\["(\w+)"\]|spec\.get\("(\w+)"', src)
            for k in tup if k}
    allowed = {k for keys in ob._CONSUMED_KEYS.values() for k in keys}
    assert (allowed - {"type"}) <= read
    for key in ("left", "right", "value", "min", "max"):
        assert key in allowed


def test_unit_still_gets_its_specific_reason_not_the_generic_one():
    specs, reason = map_assertion({"type": "field_cmp", "field": "f", "op": "<=", "value": 5, "unit": "MB"})
    assert specs == []
    assert "MB" in reason and "无量纲" in reason


def test_invariants_without_rows_is_unknown():
    invs = to_invariants(bridge([oracle(cmp_assert(">="))]))
    assert invariants.evaluate_all([], invs)[0]["status"] == "unknown"
    assert invariants.evaluate_all([], invs)[0]["ok"] is None


def test_invariants_with_synthetic_rows_decides():
    invs = to_invariants(bridge([oracle(cmp_assert(">="))]))
    assert invariants.evaluate_all([{"id": 1, "f": 5}], invs)[0]["status"] == "pass"
    assert invariants.evaluate_all([{"id": 1, "f": -1}], invs)[0]["status"] == "fail"
    assert invariants.evaluate_all([{"id": 1}], invs)[0]["status"] == "unknown"


def test_unmappable_never_appears_in_either_pack():
    result = bridge([oracle(cmp_assert(">="), oid="ok"),
                     oracle({"type": "size", "field": "f", "max": 9}, oid="sz"),
                     oracle({"type": "pattern", "field": "f", "value": "^a$"}, oid="pt"),
                     oracle(cmp_assert(">="), oid="nc", confirmation="needs_confirmation")])
    ids = {r["id"] for r in to_rule_pack(result, "sample")["rules"]}
    assert ids == {"ok"}
    assert {i["id"] for i in to_invariants(result)} == {"ok"}


# ---- 7. 两套词汇契约（LESSONS 坑 11 的防复发守卫）----
# 上游采集器改为直接发射求值器侧原语名后，只认旧词汇的桥把 28 条本来已可执行的
# 判据当成「未知原语」丢掉，实测 47/236 → 14/236。以下守卫锁住两套词汇都认。

VOCABULARY_PAIRS = [
    ({"type": "field_cmp", "field": "f", "op": ">=", "value": 1},
     {"type": "field_gte_const", "left": "f", "value": 1}),
    ({"type": "field_cmp", "field": "f", "op": "<=", "value": 9},
     {"type": "field_lte_const", "left": "f", "value": 9}),
    ({"type": "field_cmp", "field": "f", "op": ">", "value": 0},
     {"type": "field_gt_const", "left": "f", "value": 0}),
    ({"type": "in", "field": "f", "value": ["A", "B"]},
     {"type": "field_in_set", "left": "f", "value": ["A", "B"]}),
    ({"type": "not_null", "field": "f"}, {"type": "field_not_null", "left": "f"}),
    ({"type": "not_blank", "field": "f"}, {"type": "field_not_blank", "left": "f"}),
]

PASSTHROUGH_CASES = [e for _, e in VOCABULARY_PAIRS] + [
    {"type": "field_eq_field", "left": "f", "right": "g"},
    {"type": "field_lte_field", "left": "f", "right": "g"},
    {"type": "field_gte_field", "left": "f", "right": "g"},
]


def test_closed_set_is_exactly_the_evaluator_intersection():
    import inspect
    import re
    inv = set(re.findall(r'kind == "([a-z_]+)"', inspect.getsource(invariants.evaluate)))
    ra = set(re.findall(r'"([a-z][a-z_]+)"', inspect.getsource(rule_assessment.evaluate_observation)))
    assert CLOSED_PRIMITIVES == (inv & ra)
    assert len(CLOSED_PRIMITIVES) == 13
    assert "text_not_contains" in CLOSED_PRIMITIVES


@pytest.mark.parametrize("assertion", PASSTHROUGH_CASES)
def test_every_closed_primitive_passes_through_verbatim(assertion):
    specs, reason = map_assertion(assertion)
    assert reason == ""
    assert specs == [assertion]
    assert "未知原语" not in reason
    result = bridge([oracle(assertion)])
    assert result["counts"]["mapped_rules"] == 1
    assert result["unmappable"] == []
    assert result["mapped"][0]["primitive_from"] == assertion["type"]
    assert result["mapped"][0]["primitive_to"] == assertion["type"]


@pytest.mark.parametrize("harvester,evaluator", VOCABULARY_PAIRS)
def test_both_vocabularies_converge_to_identical_assertion(harvester, evaluator):
    a, reason_a = map_assertion(harvester)
    b, reason_b = map_assertion(evaluator)
    assert reason_a == reason_b == ""
    assert a == b


def test_mixed_vocabulary_corpus_loses_nothing():
    oracles = []
    for i, (harvester, evaluator) in enumerate(VOCABULARY_PAIRS):
        oracles.append(oracle(harvester, oid=f"h{i}"))
        oracles.append(oracle(evaluator, oid=f"e{i}"))
    result = bridge(oracles)
    assert result["counts"]["total"] == len(oracles)
    assert result["counts"]["mapped_rules"] == len(oracles)
    assert result["unmappable"] == []
    assert result["counts"]["numerator"] == len(oracles)


def test_passthrough_copies_and_does_not_alias_input():
    assertion = {"type": "field_gte_const", "left": "f", "value": 1}
    snapshot = dict(assertion)
    specs, _ = map_assertion(assertion)
    specs[0]["value"] = 999
    assert assertion == snapshot


def test_passthrough_rejects_unit_bearing_constraint():
    specs, reason = map_assertion({"type": "field_lte_const", "left": "上传", "value": 5, "unit": "MB"})
    assert specs == []
    assert "MB" in reason


@pytest.mark.parametrize("assertion", [
    {"type": "field_gte_const", "value": 1},
    {"type": "field_gte_const", "left": "", "value": 1},
    {"type": "field_gte_const", "left": "  ", "value": 1},
    {"type": "field_not_null"},
    {"type": "field_not_blank", "left": None},
    {"type": "field_eq_field", "left": "f"},
    {"type": "field_lte_field", "left": "f", "right": ""},
])
def test_passthrough_missing_field_is_unmappable(assertion):
    specs, reason = map_assertion(assertion)
    assert specs == []
    assert reason


@pytest.mark.parametrize("value", ["1", None, True, [1], float("nan"), float("inf")])
def test_passthrough_rejects_incompatible_const(value):
    specs, reason = map_assertion({"type": "field_gt_const", "left": "f", "value": value})
    assert specs == []
    assert "类型不兼容" in reason


@pytest.mark.parametrize("value", [[], None, "A", [1, {}], [1, float("inf")]])
def test_passthrough_rejects_bad_enum_domain(value):
    specs, reason = map_assertion({"type": "field_in_set", "left": "f", "value": value})
    assert specs == []
    assert reason


def test_needs_confirmation_downgrade_is_not_repromoted():
    result = bridge([oracle({"type": "field_lte_const", "left": "上传", "value": 5, "unit": "MB"},
                            confirmation="needs_confirmation")])
    assert result["counts"]["mapped_rules"] == 0
    assert result["excluded"][0]["confirmation"] == "needs_confirmation"
    assert to_rule_pack(result, "sample")["rules"] == []


# ---- 8. 分母口径（LESSONS 坑 20 / 坑 3）----
# 采集器 counts.total 只数 oracles 数组；refuted 在独立顶层键 explicitly_undefined。
# 桥把 refuted 单列排除桶，所以三桶之和 = 处理总数 = oracles + refuted，比 counts.total 多。
# 两个分母必须显式报出，否则下游一眼看成「丢了 4 条判据」。

def refuted_entry(oid="doc:undefined:s:1"):
    return {"id": oid, "confirmation": "refuted",
            "source": {"kind": "document", "reference": "s.md:1", "text": "x"}}


def test_both_denominators_are_explicit_and_close():
    oracles = [oracle(cmp_assert(">="), oid="a"),
               oracle({"type": "size", "field": "f", "max": 9}, oid="b"),
               oracle(cmp_assert(">="), oid="c", confirmation="needs_confirmation")]
    result = bridge(oracles, [refuted_entry(), refuted_entry("doc:undefined:s:2")])
    c = result["counts"]
    assert c["oracles_count"] == 3
    assert c["explicitly_undefined_count"] == 2
    assert c["total_declarations"] == 5
    assert c["total"] == c["total_declarations"] == 5
    assert c["denominator"] == c["oracles_count"] == 3
    assert c["denominator_all_declarations"] == 5
    assert c["denominators_disjoint"] is True
    assert c["three_bucket_sum"] == c["mapped_rules"] + c["unmappable"] + c["excluded"] == 5
    assert c["numerator"] == 1


@pytest.mark.parametrize("n_oracles,n_refuted", [(0, 0), (1, 0), (0, 1), (3, 2), (7, 4)])
def test_three_bucket_sum_always_equals_processed_total(n_oracles, n_refuted):
    oracles = [oracle(cmp_assert(">="), oid=f"o{i}") for i in range(n_oracles)]
    result = bridge(oracles, [refuted_entry(f"doc:undefined:s:{i}") for i in range(n_refuted)])
    c = result["counts"]
    assert c["three_bucket_sum"] == c["total"] == n_oracles + n_refuted
    assert c["oracles_count"] + c["explicitly_undefined_count"] == c["total_declarations"]
    assert c["mapped_rules"] + c["unmappable"] + c["excluded"] == c["total"]


def test_refuted_bucket_raises_processed_total_but_not_oracles_count():
    oracles = [oracle(cmp_assert(">="), oid="a")]
    without = bridge(oracles, [])
    with_refuted = bridge(oracles, [refuted_entry()])
    assert without["counts"]["oracles_count"] == with_refuted["counts"]["oracles_count"] == 1
    assert without["counts"]["total_declarations"] == 1
    assert with_refuted["counts"]["total_declarations"] == 2
    assert with_refuted["counts"]["numerator"] == without["counts"]["numerator"] == 1


def test_refuted_never_enters_judgment_under_new_caliber():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()])
    assert result["counts"]["excluded"] == 1
    assert all(m["oracle_id"] != "doc:undefined:s:1" for m in result["mapped"])
    assert all(r["id"] != "doc:undefined:s:1" for r in to_rule_pack(result, "sample")["rules"])
    assert all(i["id"] != "doc:undefined:s:1" for i in to_invariants(result))
    assert all(e["confirmation"] == "refuted" for e in result["excluded"])


def test_overlapping_denominators_are_reported_not_hidden():
    result = bridge([oracle(cmp_assert(">="), oid="shared")], [refuted_entry("shared")])
    c = result["counts"]
    assert c["denominators_disjoint"] is False
    assert c["denominator_overlap_ids"] == ["shared"]


def test_denominator_caliber_text_names_both_numbers_and_the_gap():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()])
    text = result["denominator_caliber"]
    assert "1" in text and "2" in text
    assert "refuted" in text
    assert "不是丢判据" in text


def test_upstream_caliber_crosscheck_agrees():
    counts = {"oracles_count": 1, "explicitly_undefined_count": 1,
              "total_declarations": 2, "denominators_disjoint": True}
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()], counts)
    assert result["caliber_crosscheck"]["agrees"] is True


@pytest.mark.parametrize("drift", [
    {"oracles_count": 99}, {"explicitly_undefined_count": 99},
    {"total_declarations": 99}, {"denominators_disjoint": False},
])
def test_upstream_caliber_drift_is_flagged_not_absorbed(drift):
    counts = {"oracles_count": 1, "explicitly_undefined_count": 1,
              "total_declarations": 2, "denominators_disjoint": True, **drift}
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()], counts)
    assert result["caliber_crosscheck"]["agrees"] is False


def test_crosscheck_absent_when_upstream_counts_not_supplied():
    assert "caliber_crosscheck" not in bridge([oracle(cmp_assert(">="))])


def test_upstream_absent_caliber_fields_are_not_treated_as_disagreement():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()], {"total": 1})
    cross = result["caliber_crosscheck"]
    assert cross["agrees"] is True
    assert cross["mismatches"] == {}
    assert cross["upstream_declared"] == {"total": 1}
    assert set(cross["upstream_undeclared"]) == {
        "oracles_count", "explicitly_undefined_count", "total_declarations", "denominators_disjoint"}


def test_old_style_total_is_crosschecked_not_absorbed():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [], {"total": 999})
    cross = result["caliber_crosscheck"]
    assert cross["agrees"] is False
    assert cross["mismatches"]["total"] == {"upstream": 999, "bridge": 1}


def test_old_style_total_agreeing_is_accepted():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [], {"total": 1})
    assert result["caliber_crosscheck"]["agrees"] is True


def test_upstream_partially_declared_caliber_compares_only_what_is_declared():
    result = bridge([oracle(cmp_assert(">="), oid="a")], [refuted_entry()],
                    {"oracles_count": 1, "total_declarations": 99})
    cross = result["caliber_crosscheck"]
    assert cross["agrees"] is False
    assert set(cross["mismatches"]) == {"total_declarations"}
    assert cross["upstream_declared"] == {"oracles_count": 1, "total_declarations": 99}
    assert "explicitly_undefined_count" in cross["upstream_undeclared"]


def test_cli_succeeds_on_empty_and_undeclared_caliber(tmp_path, monkeypatch, capsys):
    import json
    from engine import oracle_bridge as ob
    for counts in ({"total": 0, "oracles_count": 0, "explicitly_undefined_count": 0,
                    "total_declarations": 0, "denominators_disjoint": True},
                   {"total": 0}, None):
        harvest = {"oracles": [], "explicitly_undefined": []}
        if counts is not None:
            harvest["counts"] = counts
        src = tmp_path / "h.json"
        src.write_text(json.dumps(harvest), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["oracle_bridge", "--harvested", str(src),
                                          "--target", "T", "--out-report", str(tmp_path / "r.json")])
        assert ob.main() == 0, counts
        assert "三桶之和 0 == 处理总数 0" in capsys.readouterr().out


# ---- 9. 判定忠实性：when 是适用域，丢掉它就是制造假缺陷（V12 实证）----
# 两个求值器都把 when 当一级字段消费（invariants.applies / rule_assessment.evaluate_observation）。
# 非适用行必须 NOT_APPLICABLE，不得变 FAIL，也不得变 PASS。

VIP_WHEN = {"field": "vip", "op": "eq", "value": True}


def vip_oracle(assertion=None, **extra):
    return {**oracle(assertion or cmp_assert(">="), oid="vip1"), "when": VIP_WHEN, **extra}


def non_vip_observation(pack):
    return [{"id": "o", "target": "T", "scope": pack["rules"][0]["scope"],
             "row": {"vip": False, "f": -1}, "reached": True, "evidence_refs": ["synthetic:1"]}]


@pytest.mark.parametrize("assertion", [
    cmp_assert(">="), cmp_assert(">"), {"type": "in", "field": "f", "value": ["A"]},
    {"type": "range", "field": "f", "minimum": 1},
    {"type": "not_null", "field": "f"}, {"type": "not_blank", "field": "f"},
    {"type": "field_gte_const", "left": "f", "value": 0},
])
def test_when_survives_both_mapping_and_passthrough(assertion):
    result = bridge([vip_oracle(assertion)])
    assert result["counts"]["mapped_oracles"] == 1
    for item in result["mapped"]:
        assert item["when"] == VIP_WHEN
    for rule in to_rule_pack(result, "T")["rules"]:
        assert rule["when"] == VIP_WHEN
    for inv in to_invariants(result):
        assert inv["when"] == VIP_WHEN


def test_non_applicable_row_is_not_applicable_not_fail():
    result = bridge([vip_oracle()])
    pack = to_rule_pack(result, "T")
    got = rule_assessment.assess(pack, {"observations": non_vip_observation(pack)})["results"][0]
    assert got["observed_status"] == "NOT_APPLICABLE"
    assert got["status"] == "NOT_APPLICABLE"


def test_non_applicable_row_is_not_fail_in_invariants_either():
    invs = to_invariants(bridge([vip_oracle()]))
    rows = [{"id": 1, "vip": False, "f": -1}]
    assert invariants.applies(rows[0], invs[0]) is False
    verdict = invariants.evaluate_all(rows, invs)[0]
    assert verdict["status"] != "fail"
    assert verdict["status"] == "unknown"
    assert verdict["skipped"] == 1 and verdict["checked"] == 0


def test_applicable_row_still_decides_after_when_preserved():
    result = bridge([vip_oracle()])
    pack = to_rule_pack(result, "T")
    for value, expected in ((5, "PASS"), (-1, "FAIL")):
        obs = [{"id": "o", "target": "T", "scope": pack["rules"][0]["scope"],
                "row": {"vip": True, "f": value}, "reached": True, "evidence_refs": ["synthetic:1"]}]
        got = rule_assessment.assess(pack, {"observations": obs})["results"][0]
        assert got["status"] == expected


def test_when_is_carried_verbatim_not_rewritten():
    for when in (VIP_WHEN, [VIP_WHEN, {"field": "x", "op": "ne", "value": 0}], []):
        result = bridge([vip_oracle(**{"when": when})])
        assert result["mapped"][0]["when"] == when
        assert to_rule_pack(result, "T")["rules"][0]["when"] == when
        assert to_invariants(result)[0]["when"] == when


def test_empty_when_list_is_preserved_and_yields_unknown():
    result = bridge([vip_oracle(**{"when": []})])
    pack = to_rule_pack(result, "T")
    assert pack["rules"][0]["when"] == []
    obs = [{"id": "o", "target": "T", "scope": pack["rules"][0]["scope"],
            "row": {"vip": True, "f": 5}, "reached": True, "evidence_refs": ["synthetic:1"]}]
    assert rule_assessment.assess(pack, {"observations": obs})["results"][0]["status"] == "UNKNOWN"


def test_severity_is_carried_to_invariants():
    result = bridge([vip_oracle(severity="HIGH")])
    assert to_invariants(result)[0]["severity"] == "HIGH"
    assert result["mapped"][0]["severity"] == "HIGH"


def test_absent_when_is_not_fabricated():
    result = bridge([oracle(cmp_assert(">="))])
    assert "when" not in result["mapped"][0]
    assert "when" not in to_rule_pack(result, "T")["rules"][0]
    assert "when" not in to_invariants(result)[0]


def test_every_field_the_evaluators_read_is_emitted():
    import inspect
    import re
    inv_src = inspect.getsource(invariants)
    ra_src = inspect.getsource(rule_assessment)
    inv_reads = set(re.findall(r'(?:inv|invariant)\.get\("(\w+)"', inv_src)) | set(
        re.findall(r'(?:inv|invariant)\["(\w+)"\]', inv_src))
    ra_reads = set(re.findall(r'rule\.get\("(\w+)"', ra_src)) | set(
        re.findall(r'rule\["(\w+)"\]', ra_src))
    assert "when" in inv_reads and "when" in ra_reads
    result = bridge([vip_oracle(severity="HIGH")])
    inv = to_invariants(result)[0]
    rule = to_rule_pack(result, "T")["rules"][0]
    assert inv_reads <= set(inv) | {"assert"}
    assert ra_reads <= set(rule)


# ---- 10. 复合 range：一条判据出多条规则，三桶按判据数闭合 ----

def compound_range_oracle(oid="r1"):
    return oracle({"type": "range", "field": "f", "minimum": 1, "maximum": 9}, oid=oid)


def test_compound_range_closes_by_oracle_caliber():
    result = bridge([compound_range_oracle()])
    c = result["counts"]
    assert c["mapped_oracles"] == 1
    assert c["mapped_rules"] == 2
    assert c["bucket_caliber"] == "oracles"
    assert c["three_bucket_sum"] == c["total"] == 1
    assert c["mapped_rules"] >= c["mapped_oracles"]


@pytest.mark.parametrize("n", [1, 2, 5])
def test_many_compound_ranges_still_close(n):
    result = bridge([compound_range_oracle(f"r{i}") for i in range(n)])
    c = result["counts"]
    assert c["mapped_oracles"] == n
    assert c["mapped_rules"] == 2 * n
    assert c["three_bucket_sum"] == c["total"] == n


def test_compound_range_mixed_with_other_buckets_closes():
    result = bridge([compound_range_oracle("r1"), oracle(cmp_assert(">="), oid="ok"),
                     oracle({"type": "size", "field": "f", "max": 9}, oid="sz"),
                     oracle(cmp_assert(">="), oid="nc", confirmation="needs_confirmation")],
                    [refuted_entry()])
    c = result["counts"]
    assert c["three_bucket_sum"] == c["total"] == 5
    assert c["mapped_oracles"] == 2 and c["mapped_rules"] == 3
    assert c["unmappable"] == 1 and c["excluded"] == 2


def test_compound_range_cli_does_not_fail_on_legal_input(tmp_path, monkeypatch, capsys):
    import json
    from engine import oracle_bridge as ob
    harvest = {"oracles": [compound_range_oracle()], "explicitly_undefined": [],
               "counts": {"total": 1, "oracles_count": 1, "explicitly_undefined_count": 0,
                          "total_declarations": 1, "denominators_disjoint": True}}
    src = tmp_path / "h.json"
    src.write_text(json.dumps(harvest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["oracle_bridge", "--harvested", str(src),
                                      "--target", "T", "--out-report", str(tmp_path / "r.json")])
    assert ob.main() == 0
    printed = capsys.readouterr().out
    assert "三桶之和 1 == 处理总数 1" in printed
    assert "mapped_rules=2 >= mapped_oracles=1" in printed


def test_compound_range_rules_keep_distinct_ids_and_same_when():
    result = bridge([vip_oracle({"type": "range", "field": "f", "minimum": 1, "maximum": 9})])
    rules = to_rule_pack(result, "T")["rules"]
    assert [r["id"] for r in rules] == ["vip1#1", "vip1#2"]
    assert all(r["when"] == VIP_WHEN for r in rules)
    assert all(i["when"] == VIP_WHEN for i in to_invariants(result))


def test_empty_input_denominators_are_zero_and_close():
    c = bridge([], [])["counts"]
    assert c["oracles_count"] == c["explicitly_undefined_count"] == c["total_declarations"] == 0
    assert c["three_bucket_sum"] == c["total"] == 0
    assert c["denominators_disjoint"] is True


def test_cli_reports_both_denominators_and_closes(tmp_path, monkeypatch, capsys):
    import json
    from engine import oracle_bridge as ob
    harvest = {"oracles": [oracle(cmp_assert(">="), oid="a"),
                           oracle({"type": "size", "field": "f", "max": 9}, oid="b")],
               "explicitly_undefined": [refuted_entry()],
               "counts": {"oracles_count": 2, "explicitly_undefined_count": 1,
                          "total_declarations": 3, "denominators_disjoint": True}}
    src = tmp_path / "h.json"
    src.write_text(json.dumps(harvest, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["oracle_bridge", "--harvested", str(src),
                                      "--target", "T", "--out-report", str(out)])
    assert ob.main() == 0
    printed = capsys.readouterr().out
    assert "1/2" in printed and "1/3" in printed
    assert "处理总数 3 = oracles 2 + refuted 独立桶 1" in printed
    assert "三桶之和 3 == 处理总数 3" in printed
    saved = json.loads(out.read_text())
    assert saved["counts"]["three_bucket_sum"] == saved["counts"]["total"] == 3
    assert saved["caliber_crosscheck"]["agrees"] is True


def test_cli_fails_loudly_when_upstream_caliber_drifts(tmp_path, monkeypatch, capsys):
    import json
    from engine import oracle_bridge as ob
    harvest = {"oracles": [oracle(cmp_assert(">="), oid="a")], "explicitly_undefined": [],
               "counts": {"oracles_count": 999, "explicitly_undefined_count": 0,
                          "total_declarations": 999, "denominators_disjoint": True}}
    src = tmp_path / "h.json"
    src.write_text(json.dumps(harvest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["oracle_bridge", "--harvested", str(src),
                                      "--target", "T", "--out-report", str(tmp_path / "r.json")])
    assert ob.main() == 1
    assert "口径与上游采集器不一致" in capsys.readouterr().out