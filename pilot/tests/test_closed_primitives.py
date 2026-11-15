"""新增封闭原语的语义锁 —— field_not_null / field_not_blank / field_gt_const。

Ashby 必要多样性：放大封闭原语集 = 放大调节器多样性，解锁采集器里
not_null / not_blank / 严格 > 三类判据。但放大多样性不许放松三态纪律：

  * 缺字段 -> UNKNOWN，绝不是 PASS（字段名拼错不得得到一张通过的成绩单）
  * null -> not_null / not_blank 判 FAIL（明确违反），不是 UNKNOWN
  * 类型不兼容 / 无观测 -> UNKNOWN，绝不把 unknown 变成 pass
  * field_gt_const 是严格大于：等于常量必须 FAIL，不得用 >= 冒充（D4 极性红线）

两个求值器（invariants / rule_assessment）必须对同一输入给出同构结论。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import invariants, rule_assessment  # noqa: E402
from engine.invariants import (  # noqa: E402
    FAIL, NOT_APPLICABLE, PASS, UNKNOWN, InvariantConfigError, deviation, evaluate, evaluate_all,
)

NOT_NULL = {"id": "T-NN", "assert": {"type": "field_not_null", "left": "f"}}
NOT_BLANK = {"id": "T-NB", "assert": {"type": "field_not_blank", "left": "f"}}
GT = {"id": "T-GT", "assert": {"type": "field_gt_const", "left": "f", "value": 0}}

SOURCE = {"kind": "user", "reference": "requirement:1",
          "confirmation": "confirmed", "confirmed_by": "reviewer"}


def spec_of(inv):
    return inv["assert"]


def assess_one(spec, row, source=None, reached=True, refs=None, scope="example", obs_scope=None):
    rule = {"id": "r", "scope": scope, "source": dict(SOURCE if source is None else source),
            "assert": spec}
    obs = {"id": "o", "target": "sample", "scope": scope if obs_scope is None else obs_scope,
           "row": row, "reached": reached,
           "evidence_refs": ["evidence:1"] if refs is None else refs}
    pack = {"schema": "business-rules/1", "target": "sample", "rules": [rule]}
    return rule_assessment.assess(pack, {"observations": [obs]})["results"][0]


# ---- field_not_null：缺字段 / null / 有值 三态必须分开 ----
@pytest.mark.parametrize("row,expected", [
    ({}, UNKNOWN),                     # 缺字段：判不出来，不是 PASS
    ({"other": 1}, UNKNOWN),           # 字段名拼错：同样判不出来
    ({"f": None}, FAIL),               # null 是明确违反
    ({"f": 0}, PASS),                  # 0 是非 null 值
    ({"f": ""}, PASS),                 # 空串也是非 null（空白是 not_blank 的事）
    ({"f": False}, PASS),
    ({"f": []}, PASS),
])
def test_not_null_three_state(row, expected):
    verdict, detail = evaluate(row, NOT_NULL)
    assert verdict == expected
    assert detail["ok"] is (True if expected == PASS else False if expected == FAIL else None)


def test_not_null_missing_field_is_not_pass_in_summary():
    result = evaluate_all([{"id": 1}], [NOT_NULL])[0]
    assert result["status"] == UNKNOWN
    assert result["ok"] is None
    assert result["unknown_rows"] == 1


def test_not_null_null_row_fails_in_summary():
    result = evaluate_all([{"id": 1, "f": None}], [NOT_NULL])[0]
    assert result["status"] == FAIL
    assert result["violations"] == 1


# ---- field_not_blank：null / 空串 / 纯空白 / 类型不兼容 ----
@pytest.mark.parametrize("row,expected", [
    ({}, UNKNOWN),
    ({"f": None}, FAIL),
    ({"f": ""}, FAIL),
    ({"f": "   "}, FAIL),
    ({"f": "\t\n "}, FAIL),
    ({"f": "x"}, PASS),
    ({"f": " x "}, PASS),
    ({"f": 0}, UNKNOWN),               # 类型不兼容：not_blank 只认字符串
    ({"f": 123}, UNKNOWN),
    ({"f": True}, UNKNOWN),
    ({"f": ["x"]}, UNKNOWN),
    ({"f": {"a": 1}}, UNKNOWN),
])
def test_not_blank_three_state(row, expected):
    assert evaluate(row, NOT_BLANK)[0] == expected


def test_not_blank_missing_field_is_unknown_not_pass():
    verdict, detail = evaluate({"id": 1}, NOT_BLANK)
    assert verdict == UNKNOWN
    assert detail["ok"] is None


# ---- field_gt_const：严格大于，符号方向不许放宽 ----
@pytest.mark.parametrize("row,expected", [
    ({"f": 1}, PASS),
    ({"f": 0}, FAIL),                  # 等于常量必须 FAIL —— 用 >= 冒充就会在这里假绿
    ({"f": -1}, FAIL),
    ({}, UNKNOWN),
    ({"f": None}, UNKNOWN),
    ({"f": "1"}, UNKNOWN),             # 类型不兼容
    ({"f": True}, UNKNOWN),            # bool 不是数
    ({"f": [1]}, UNKNOWN),
    ({"f": math.nan}, UNKNOWN),        # 非有限数值判不出来
    ({"f": math.inf}, UNKNOWN),
])
def test_gt_const_strict_and_three_state(row, expected):
    assert evaluate(row, GT)[0] == expected


def test_gt_const_boundary_is_fail_not_pass():
    assert evaluate({"f": 0}, GT)[0] == FAIL
    gte = {"id": "T-GTE", "assert": {"type": "field_gte_const", "left": "f", "value": 0}}
    assert evaluate({"f": 0}, gte)[0] == PASS   # 对照：>= 在边界是 PASS，两者不得混


def test_gt_const_non_numeric_constant_is_unknown():
    for value in ("0", None, True, [0]):
        inv = {"id": "T-GT", "assert": {"type": "field_gt_const", "left": "f", "value": value}}
        assert evaluate({"f": 1}, inv)[0] == UNKNOWN


def test_gt_const_missing_field_is_unknown_not_pass():
    verdict, detail = evaluate({"id": 1}, GT)
    assert verdict == UNKNOWN
    assert detail["ok"] is None


# ---- rule_assessment 侧：同一套语义，无观测必须 UNKNOWN ----
@pytest.mark.parametrize("spec,row,expected", [
    (spec_of(NOT_NULL), {}, "UNKNOWN"),
    (spec_of(NOT_NULL), {"f": None}, "FAIL"),
    (spec_of(NOT_NULL), {"f": 0}, "PASS"),
    (spec_of(NOT_BLANK), {}, "UNKNOWN"),
    (spec_of(NOT_BLANK), {"f": None}, "FAIL"),
    (spec_of(NOT_BLANK), {"f": "  "}, "FAIL"),
    (spec_of(NOT_BLANK), {"f": "x"}, "PASS"),
    (spec_of(NOT_BLANK), {"f": 5}, "UNKNOWN"),
    (spec_of(GT), {}, "UNKNOWN"),
    (spec_of(GT), {"f": None}, "UNKNOWN"),
    (spec_of(GT), {"f": 1}, "PASS"),
    (spec_of(GT), {"f": 0}, "FAIL"),
    (spec_of(GT), {"f": "1"}, "UNKNOWN"),
    (spec_of(GT), {"f": True}, "UNKNOWN"),
])
def test_rule_assessment_new_primitives(spec, row, expected):
    assert assess_one(spec, row)["status"] == expected


def test_rule_assessment_no_observations_is_unknown():
    pack = {"schema": "business-rules/1", "target": "sample",
            "rules": [{"id": inv["id"], "scope": "example", "source": SOURCE,
                       "assert": spec_of(inv)} for inv in (NOT_NULL, NOT_BLANK, GT)]}
    report = rule_assessment.assess(pack, {"observations": []})
    assert report["intake_status"] == "RULES_PRESENT"
    for result in report["results"]:
        assert result["status"] == "UNKNOWN"
        assert result["observed_status"] == "UNKNOWN"
        assert result["gate_eligible"] is False


def test_rule_assessment_unreached_or_bad_refs_stay_unknown():
    for kwargs in ({"reached": False}, {"refs": []}, {"obs_scope": "other"}):
        assert assess_one(spec_of(NOT_NULL), {"f": 1}, **kwargs)["status"] == "UNKNOWN"


def test_rule_assessment_confirmed_technical_kind_reaches_verdict():
    # 双 tier 门控：ddl 作者写下即确认（技术 tier），观测违反 → FAIL 不再被洗成 UNKNOWN
    ddl = {"kind": "ddl", "reference": "schema:t", "confirmation": "confirmed",
           "confirmed_by": "ddl:schema:t"}
    result = assess_one(spec_of(NOT_NULL), {"f": None}, source=ddl)
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "FAIL"
    assert result["confirmation_tier"] == "technical"
    assert result["source_confirmation"] == "declared_confirmed"


def test_rule_assessment_unconfirmed_source_stays_unknown_even_on_violation():
    unconfirmed = {"kind": "user", "reference": "requirement:1", "confirmation": "unconfirmed",
                   "confirmed_by": "reviewer"}
    result = assess_one(spec_of(NOT_NULL), {"f": None}, source=unconfirmed)
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "UNKNOWN"


# ---- 两个求值器同构：同一输入不得给出不同结论 ----
@pytest.mark.parametrize("inv,row", [
    (NOT_NULL, {}), (NOT_NULL, {"f": None}), (NOT_NULL, {"f": 0}),
    (NOT_BLANK, {}), (NOT_BLANK, {"f": None}), (NOT_BLANK, {"f": ""}),
    (NOT_BLANK, {"f": "x"}), (NOT_BLANK, {"f": 3}),
    (GT, {}), (GT, {"f": None}), (GT, {"f": 1}), (GT, {"f": 0}), (GT, {"f": "1"}),
])
def test_evaluators_agree(inv, row):
    verdict, _ = evaluate(row, inv)
    observed = assess_one(spec_of(inv), row)["observed_status"]
    assert observed == verdict.upper()


# ---- 现有原语不回归：两个求值器都仍然认得旧封闭集 ----
@pytest.mark.parametrize("spec,row,expected", [
    ({"type": "field_eq_field", "left": "a", "right": "b"}, {"a": 1, "b": 1}, PASS),
    ({"type": "field_eq_field", "left": "a", "right": "b"}, {"a": 1, "b": 2}, FAIL),
    ({"type": "field_eq_field", "left": "a", "right": "b"}, {}, UNKNOWN),
    ({"type": "field_lte_field", "left": "a", "right": "b"}, {"a": 1, "b": 2}, PASS),
    ({"type": "field_gte_field", "left": "a", "right": "b"}, {"a": 1, "b": 2}, FAIL),
    ({"type": "field_lte_const", "left": "a", "value": 9}, {"a": 9}, PASS),
    ({"type": "field_gte_const", "left": "a", "value": 0}, {"a": 0}, PASS),
    ({"type": "field_gte_const", "left": "a", "value": 0}, {"a": -1}, FAIL),
    ({"type": "field_in_set", "left": "a", "value": ["A"]}, {"a": "A"}, PASS),
    ({"type": "field_in_set", "left": "a", "value": ["A"]}, {"a": "B"}, FAIL),
])
def test_existing_primitives_do_not_regress(spec, row, expected):
    inv = {"id": "T-OLD", "assert": spec}
    assert evaluate(row, inv)[0] == expected
    assert assess_one(spec, row)["observed_status"] == expected.upper()


def test_unknown_primitive_still_raises_config_error():
    with pytest.raises(InvariantConfigError):
        evaluate({"f": 1}, {"id": "X", "assert": {"type": "field_eq_const", "left": "f", "value": 1}})


def test_unsupported_op_still_unknown_in_rule_assessment():
    result = assess_one({"type": "unsupported", "left": "f"}, {"f": 1})
    assert result["status"] == "UNKNOWN"


# ---- 汇总三态与偏离量 ----
def test_summary_mixed_rows_fail_beats_unknown():
    rows = [{"id": 1, "f": None}, {"id": 2}]
    result = evaluate_all(rows, [NOT_NULL])[0]
    assert result["status"] == FAIL
    assert result["violations"] == 1
    assert result["unknown_rows"] == 1


def test_deviation_of_new_primitives():
    _, d = evaluate({"f": None}, NOT_NULL)
    assert deviation(d) == 1.0
    _, d = evaluate({"f": 1}, NOT_NULL)
    assert deviation(d) == 0.0
    _, d = evaluate({"f": " "}, NOT_BLANK)
    assert deviation(d) == 1.0
    _, d = evaluate({"f": -3}, GT)
    assert deviation(d) == 3.0
    _, d = evaluate({}, GT)
    assert deviation(d) == 0.0   # unknown 量不出来，不是「没违反」


# ================= 第二轮：field_size / field_pattern / text_not_contains /
# ================= field_default_when_absent

SIZE = {"id": "T-SZ", "assert": {"type": "field_size", "left": "f", "max": 3}}
SIZE_MIN = {"id": "T-SZ", "assert": {"type": "field_size", "left": "f", "min": 1}}
SIZE_BOTH = {"id": "T-SZ", "assert": {"type": "field_size", "left": "f", "min": 1, "max": 3}}
PATTERN = {"id": "T-PT", "assert": {"type": "field_pattern", "left": "f", "value": "^1[3-9]\\d{9}$"}}
FORBID = {"id": "T-FB", "assert": {"type": "text_not_contains", "left": "f",
                                   "value": ["打折", "折扣", "优惠"]}}
DEFAULT = {"id": "T-DF", "assert": {"type": "field_default_when_absent", "left": "f", "value": 0}}


# ---- field_size：字符数 vs 元素数不得混 ----
@pytest.mark.parametrize("inv,row,expected", [
    (SIZE, {"f": "abc"}, PASS),            # 3 字符 == max 3
    (SIZE, {"f": "abcd"}, FAIL),           # 4 字符 > max 3
    (SIZE, {"f": ["a", "b", "c"]}, PASS),  # 3 元素
    (SIZE, {"f": ["a", "b", "c", "d"]}, FAIL),
    (SIZE_MIN, {"f": []}, FAIL),           # 0 元素 < min 1
    (SIZE_MIN, {"f": ""}, FAIL),           # 0 字符 < min 1
    (SIZE_MIN, {"f": ["x"]}, PASS),
    (SIZE_BOTH, {"f": "ab"}, PASS),
    (SIZE_BOTH, {"f": []}, FAIL),
    (SIZE_BOTH, {"f": "abcd"}, FAIL),
    (SIZE, {}, UNKNOWN),                   # 缺字段
    (SIZE, {"f": None}, UNKNOWN),          # null
    (SIZE, {"f": 123}, UNKNOWN),           # 类型不兼容：数字没有长度语义
    (SIZE, {"f": True}, UNKNOWN),
    (SIZE, {"f": {"a": 1}}, UNKNOWN),      # 对象不是字符串也不是数组，不猜键数
])
def test_size_chars_vs_elements(inv, row, expected):
    assert evaluate(row, inv)[0] == expected


def test_size_detail_marks_counting_kind():
    _, d = evaluate({"f": "abc"}, SIZE)
    assert d["size_kind"] == "chars" and d["size"] == 3
    _, d = evaluate({"f": ["a", "b"]}, SIZE)
    assert d["size_kind"] == "elements" and d["size"] == 2


def test_size_one_char_is_not_one_element():
    # 「字符数 vs 元素数不得混」的判别用例：单元素数组装着 5 字符的串，
    # max=3 按元素数 PASS；若把数组当字符数（或把串当元素数）就会判反。
    assert evaluate({"f": ["abcde"]}, SIZE)[0] == PASS
    assert evaluate({"f": "abcde"}, SIZE)[0] == FAIL


def test_size_bad_bounds_are_config_error():
    for bounds in ({}, {"min": -1}, {"max": "3"}, {"min": True}):
        with pytest.raises(InvariantConfigError):
            evaluate({"f": "a"}, {"id": "X", "assert": {"type": "field_size", "left": "f", **bounds}})


# ---- field_pattern：全匹配，锚定语义不得放宽 ----
@pytest.mark.parametrize("row,expected", [
    ({"f": "13812345678"}, PASS),
    ({"f": "12812345678"}, FAIL),          # 第二位不在 [3-9]
    ({"f": "1381234567"}, FAIL),           # 10 位
    ({"f": "138123456789"}, FAIL),         # 12 位
    ({"f": "x13812345678"}, FAIL),         # 前缀杂质：全匹配不得放过
    ({"f": "13812345678x"}, FAIL),         # 后缀杂质：部分匹配会假绿，全匹配必须红
    ({"f": "13812345678\n"}, FAIL),        # 尾随换行：Python $ 会放过它，JS 原文锚定不会
    ({"f": ""}, FAIL),
    ({}, UNKNOWN),
    ({"f": None}, UNKNOWN),
    ({"f": 13812345678}, UNKNOWN),         # 类型不兼容：数字不做字符串化
])
def test_pattern_fullmatch_not_relaxed(row, expected):
    assert evaluate(row, PATTERN)[0] == expected


def test_pattern_invalid_regex_is_config_error():
    inv = {"id": "X", "assert": {"type": "field_pattern", "left": "f", "value": "["}}
    with pytest.raises(InvariantConfigError):
        evaluate({"f": "a"}, inv)


# ---- text_not_contains：禁用词逐字按声明 ----
@pytest.mark.parametrize("row,expected", [
    ({"f": "本周大促销"}, PASS),
    ({"f": "限时打折"}, FAIL),
    ({"f": "会员折扣价"}, FAIL),
    ({"f": "优惠多多"}, FAIL),
    ({"f": ""}, PASS),                     # 空串不含任何禁用词
    ({"f": "打 折"}, PASS),                # 字面子串，不做归一化（不 invent 语义）
    ({}, UNKNOWN),
    ({"f": None}, UNKNOWN),
    ({"f": 123}, UNKNOWN),
])
def test_forbidden_terms(row, expected):
    assert evaluate(row, FORBID)[0] == expected


def test_forbidden_terms_bad_declaration_is_config_error():
    for value in ([], "打折", ["打折", 3]):
        inv = {"id": "X", "assert": {"type": "text_not_contains", "left": "f", "value": value}}
        with pytest.raises(InvariantConfigError):
            evaluate({"f": "a"}, inv)


# ---- field_default_when_absent：不适用 ≠ 成立 ----
def test_default_field_present_is_not_applicable_not_pass():
    verdict, detail = evaluate({"f": 5}, DEFAULT)
    assert verdict == NOT_APPLICABLE
    assert detail["ok"] is None            # 不适用不得洗成 pass，也不是 fail
    assert "why_not_applicable" in detail


def test_default_field_present_null_is_still_not_applicable():
    assert evaluate({"f": None}, DEFAULT)[0] == NOT_APPLICABLE


def test_default_field_absent_is_unknown_not_pass():
    verdict, detail = evaluate({}, DEFAULT)
    assert verdict == UNKNOWN              # 契约被触发，但结果态未观测，不猜
    assert detail["ok"] is None


def test_default_summary_all_not_applicable_is_unknown_not_pass():
    result = evaluate_all([{"id": 1, "f": 5}, {"id": 2, "f": 6}], [DEFAULT])[0]
    assert result["status"] == UNKNOWN
    assert result["ok"] is None
    assert result["not_applicable_rows"] == 2
    assert result["applicable"] is False


def test_default_summary_mixed_absent_and_present():
    result = evaluate_all([{"id": 1, "f": 5}, {"id": 2}], [DEFAULT])[0]
    assert result["status"] == UNKNOWN     # 一行不适用 + 一行 unknown，没有任何成立证据
    assert result["not_applicable_rows"] == 1
    assert result["unknown_rows"] == 1


# ---- rule_assessment 侧同构 ----
@pytest.mark.parametrize("spec,row,expected", [
    (spec_of(SIZE), {"f": "abc"}, "PASS"),
    (spec_of(SIZE), {"f": "abcd"}, "FAIL"),
    (spec_of(SIZE), {"f": ["a", "b", "c", "d"]}, "FAIL"),
    (spec_of(SIZE), {"f": 123}, "UNKNOWN"),
    (spec_of(SIZE), {}, "UNKNOWN"),
    (spec_of(SIZE_MIN), {"f": []}, "FAIL"),
    (spec_of(PATTERN), {"f": "13812345678"}, "PASS"),
    (spec_of(PATTERN), {"f": "13812345678x"}, "FAIL"),
    (spec_of(PATTERN), {"f": "13812345678\n"}, "FAIL"),
    (spec_of(PATTERN), {"f": 13812345678}, "UNKNOWN"),
    (spec_of(PATTERN), {}, "UNKNOWN"),
    (spec_of(FORBID), {"f": "限时打折"}, "FAIL"),
    (spec_of(FORBID), {"f": "正常文案"}, "PASS"),
    (spec_of(FORBID), {"f": 5}, "UNKNOWN"),
    (spec_of(FORBID), {}, "UNKNOWN"),
    (spec_of(DEFAULT), {"f": 5}, "NOT_APPLICABLE"),
    (spec_of(DEFAULT), {"f": None}, "NOT_APPLICABLE"),
    (spec_of(DEFAULT), {}, "UNKNOWN"),
])
def test_rule_assessment_round2_primitives(spec, row, expected):
    assert assess_one(spec, row)["observed_status"] == expected


@pytest.mark.parametrize("inv,row", [
    (SIZE, {"f": "abc"}), (SIZE, {"f": "abcd"}), (SIZE, {"f": ["a"]}), (SIZE, {}),
    (SIZE_MIN, {"f": []}), (PATTERN, {"f": "13812345678"}), (PATTERN, {"f": "bad"}),
    (FORBID, {"f": "打折"}), (FORBID, {"f": " ok"}), (DEFAULT, {"f": 1}), (DEFAULT, {}),
])
def test_evaluators_agree_round2(inv, row):
    verdict, _ = evaluate(row, inv)
    observed = assess_one(spec_of(inv), row)["observed_status"]
    assert observed == verdict.upper()


def test_rule_assessment_round2_no_observations_is_unknown():
    pack = {"schema": "business-rules/1", "target": "sample",
            "rules": [{"id": inv["id"], "scope": "example", "source": SOURCE,
                       "assert": spec_of(inv)} for inv in (SIZE, PATTERN, FORBID, DEFAULT)]}
    report = rule_assessment.assess(pack, {"observations": []})
    for result in report["results"]:
        assert result["status"] == "UNKNOWN"
        assert result["gate_eligible"] is False


def test_size_deviation():
    _, d = evaluate({"f": "abcde"}, SIZE)
    assert deviation(d) == 2.0
    _, d = evaluate({"f": []}, SIZE_MIN)
    assert deviation(d) == 1.0
    _, d = evaluate({"f": "abc"}, SIZE)
    assert deviation(d) == 0.0


# ---- 常设变异测试（坑 2/做法 26）：每个新守卫必须被变异咬住 ----
# 守卫是上面的行为断言；这里同进程 monkeypatch 改坏实现，断言守卫会红。
# 若守卫无牙（恒过），_caught 返回 False，本测试自己红 —— 可证伪，非同义反复。
# 守卫一律走 invariants.evaluate 模块属性查找，monkeypatch 才咬得到（坑 2：
# 从模块 import 进来的名字是旧引用，patch 模块属性不影响它）。

def _size_guard():
    assert invariants.evaluate({"f": ["abcde"]}, SIZE)[0] == PASS
    assert invariants.evaluate({"f": "abcde"}, SIZE)[0] == FAIL
    assert invariants.evaluate({"f": "abc"}, SIZE)[0] == PASS


def _pattern_guard():
    assert invariants.evaluate({"f": "13812345678x"}, PATTERN)[0] == FAIL
    assert invariants.evaluate({"f": "13812345678\n"}, PATTERN)[0] == FAIL
    assert invariants.evaluate({"f": "13812345678"}, PATTERN)[0] == PASS


def _default_guard():
    assert invariants.evaluate({"f": 5}, DEFAULT)[0] == NOT_APPLICABLE
    assert invariants.evaluate({}, DEFAULT)[0] == UNKNOWN


def _forbid_guard():
    assert invariants.evaluate({"f": "限时打折"}, FORBID)[0] == FAIL
    assert invariants.evaluate({"f": "正常文案"}, FORBID)[0] == PASS


def _caught(monkeypatch, target, attr, replacement, guard):
    monkeypatch.setattr(target, attr, replacement)
    try:
        guard()
    except AssertionError:
        return True
    return False


def test_mutation_size_confuses_chars_and_elements(monkeypatch):
    # M1：把字符串也当「1 个元素」——字符数/元素数混淆，守卫必须红
    def mutated(value):
        if isinstance(value, (str, list)):
            return "elements", 1 if isinstance(value, str) else len(value)
        return None
    assert _caught(monkeypatch, invariants, "_size_of", mutated, _size_guard)


def test_mutation_size_swaps_kind_labels(monkeypatch):
    # M2：把数组的元素数报成 chars —— 计数种类标签守卫必须红
    def mutated(value):
        if isinstance(value, str):
            return "chars", len(value)
        if isinstance(value, list):
            return "chars", len(value)
        return None
    def label_guard():
        _, d = evaluate({"f": ["a", "b"]}, SIZE)
        assert d["size_kind"] == "elements"
    assert _caught(monkeypatch, invariants, "_size_of", mutated, label_guard)


def test_mutation_pattern_relaxed_to_search(monkeypatch):
    # M3：全匹配放宽成部分匹配 —— 后缀杂质假绿，守卫必须红
    import re as _re
    def mutated(text, pattern):
        return _re.search(pattern, text) is not None
    assert _caught(monkeypatch, invariants, "_pattern_matches", mutated, _pattern_guard)


def test_mutation_default_not_applicable_washed_to_pass(monkeypatch):
    # M4：不适用洗成 pass —— 三态铁律红线，守卫必须红
    original = invariants.evaluate
    def mutated(row, inv):
        verdict, detail = original(row, inv)
        if verdict == NOT_APPLICABLE:
            return PASS, {**detail, "verdict": PASS, "ok": True}
        return verdict, detail
    assert _caught(monkeypatch, invariants, "evaluate", mutated, _default_guard)


def test_mutation_default_absent_washed_to_pass(monkeypatch):
    # M5：缺字段（契约触发）洗成 pass —— 守卫必须红
    original = invariants.evaluate
    def mutated(row, inv):
        verdict, detail = original(row, inv)
        if inv["assert"]["type"] == "field_default_when_absent" and verdict == UNKNOWN:
            return PASS, {**detail, "verdict": PASS, "ok": True}
        return verdict, detail
    assert _caught(monkeypatch, invariants, "evaluate", mutated, _default_guard)


def test_mutation_forbidden_terms_always_pass(monkeypatch):
    # M6：禁用词扫描恒 pass —— 守卫必须红
    original = invariants.evaluate
    def mutated(row, inv):
        verdict, detail = original(row, inv)
        if inv["assert"]["type"] == "text_not_contains":
            return PASS, {**detail, "verdict": PASS, "ok": True}
        return verdict, detail
    assert _caught(monkeypatch, invariants, "evaluate", mutated, _forbid_guard)


# ================= 第三轮：确认门控双 tier（业务规则 vs 技术约束）
# 北极星阻塞修复：桥发射 openapi/annotation/ddl/document，旧门控只认 user/document
# → 84% 判据结构上永远 UNKNOWN。双 tier 都是合法 confirmed，但必须分开标记。

def _src(kind, confirmation="confirmed"):
    return {"kind": kind, "reference": "r:1", "confirmation": confirmation,
            "confirmed_by": f"{kind}:r:1"}


@pytest.mark.parametrize("kind", ["ddl", "annotation", "openapi"])
def test_technical_tier_reaches_verdict(kind):
    result = assess_one(spec_of(GT), {"f": -1}, source=_src(kind))
    assert result["confirmation_tier"] == "technical"
    assert result["source_confirmation"] == "declared_confirmed"
    assert result["observed_status"] == "FAIL"
    assert result["status"] == "FAIL"          # 不再被门控洗成 UNKNOWN
    assert result["gate_eligible"] is False    # advisory 性质不变


@pytest.mark.parametrize("kind", ["user", "document"])
def test_business_tier_unchanged(kind):
    result = assess_one(spec_of(GT), {"f": -1}, source=_src(kind))
    assert result["confirmation_tier"] == "business"
    assert result["status"] == "FAIL"


def test_tiers_are_distinguishable_in_report():
    rules = [{"id": f"r-{kind}", "scope": "example", "source": _src(kind), "assert": spec_of(GT)}
             for kind in ("user", "document", "ddl", "annotation", "openapi")]
    obs = {"id": "o", "target": "sample", "scope": "example", "row": {"f": -1},
           "reached": True, "evidence_refs": ["e:1"]}
    report = rule_assessment.assess({"schema": "business-rules/1", "target": "sample", "rules": rules},
                                    {"observations": [obs]})
    tiers = report["confirmation_tiers"]
    assert tiers["business"]["rules"] == 2
    assert tiers["technical"]["rules"] == 3
    assert tiers["business"]["status_counts"] == {"FAIL": 2}
    assert tiers["technical"]["status_counts"] == {"FAIL": 3}
    assert tiers["unrecognized_kind"] == 0
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["r-ddl"]["confirmation_tier"] == "technical"
    assert by_id["r-user"]["confirmation_tier"] == "business"


def test_technical_tier_missing_field_still_unknown():
    result = assess_one(spec_of(GT), {}, source=_src("ddl"))
    assert result["status"] == "UNKNOWN"       # 三态不因 tier 放宽


@pytest.mark.parametrize("kind", ["ddl", "user"])
def test_refuted_never_reaches_verdict(kind):
    result = assess_one(spec_of(GT), {"f": -1}, source=_src(kind, confirmation="refuted"))
    assert result["source_confirmation"] == "unconfirmed"
    assert result["status"] == "UNKNOWN"


def test_unknown_kind_stays_unknown_even_if_declared_confirmed():
    result = assess_one(spec_of(GT), {"f": -1}, source=_src("blog"))
    assert result["confirmation_tier"] is None
    assert result["status"] == "UNKNOWN"


def test_technical_tier_without_confirmer_stays_unknown():
    src = _src("ddl")
    del src["confirmed_by"]
    result = assess_one(spec_of(GT), {"f": -1}, source=src)
    assert result["source_confirmation"] == "unconfirmed"
    assert result["status"] == "UNKNOWN"


def test_gate_union_matches_tier_constants():
    # 门控 union 字面量与 tier 常量是同一份契约的两处载体（坑 11/17）：
    # 从源码独立抽取两边，锁它们一致且不相交，防单方面漂移。
    import inspect
    import re as _re
    src = inspect.getsource(rule_assessment)
    m = _re.search(r'source\.get\("kind"\)\s+in\s+\(([^)]*)\)', src)
    assert m, "门控 union 形态变了 —— 同步更新本守卫与契约缝守卫"
    union = {s.strip().strip('"\'') for s in m.group(1).split(",")}
    assert union == set(rule_assessment.BUSINESS_KINDS) | set(rule_assessment.TECHNICAL_KINDS)
    assert not set(rule_assessment.BUSINESS_KINDS) & set(rule_assessment.TECHNICAL_KINDS)


# ---- 常设变异：双 tier 守卫必须被咬住 ----

def _tier_report():
    rules = [{"id": "r-ddl", "scope": "example", "source": _src("ddl"), "assert": spec_of(GT)},
             {"id": "r-user", "scope": "example", "source": _src("user"), "assert": spec_of(GT)}]
    obs = {"id": "o", "target": "sample", "scope": "example", "row": {"f": -1},
           "reached": True, "evidence_refs": ["e:1"]}
    return rule_assessment.assess({"schema": "business-rules/1", "target": "sample", "rules": rules},
                                  {"observations": [obs]})


def _tier_guard():
    report = _tier_report()
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["r-ddl"]["confirmation_tier"] == "technical"
    assert by_id["r-user"]["confirmation_tier"] == "business"
    assert by_id["r-ddl"]["status"] == "FAIL"
    assert report["confirmation_tiers"]["technical"]["rules"] == 1


def _unconfirmed_guard():
    result = assess_one(spec_of(GT), {"f": -1}, source=_src("user", confirmation="unconfirmed"))
    assert result["status"] == "UNKNOWN"


def test_mutation_technical_washed_to_business(monkeypatch):
    # M7：技术约束洗成业务规则 —— 分级认识论红线，守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        report = original(pack, observations)
        for r in report["results"]:
            if r["confirmation_tier"] == "technical":
                r["confirmation_tier"] = "business"
        return report
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, _tier_guard)


def test_mutation_tier_marker_dropped(monkeypatch):
    # M8：抹掉 tier 标记 —— 报告不可区分两个 tier，守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        report = original(pack, observations)
        for r in report["results"]:
            r["confirmation_tier"] = None
        return report
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, _tier_guard)


def test_mutation_gate_widened_to_any_kind(monkeypatch):
    # M9：门控放宽到任意 kind —— 未知来源洗成 confirmed，守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        for rule in pack["rules"]:
            if isinstance(rule.get("source"), dict):
                rule["source"]["kind"] = "user"
        return original(pack, observations)
    def unknown_kind_guard():
        result = assess_one(spec_of(GT), {"f": -1}, source=_src("blog"))
        assert result["status"] == "UNKNOWN"
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, unknown_kind_guard)


def test_mutation_unconfirmed_washed_to_confirmed(monkeypatch):
    # M10：未确认洗成已确认 —— 守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        report = original(pack, observations)
        for r in report["results"]:
            r["status"] = r["observed_status"]
            r["source_confirmation"] = "declared_confirmed"
        return report
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, _unconfirmed_guard)


# ---- W11 补洞：needs_confirmation 纪律 / 双向 / 无观测 / 门控收缩变异 ----

@pytest.mark.parametrize("kind", ["user", "document", "ddl", "annotation", "openapi", "inferred"])
def test_needs_confirmation_never_reaches_verdict(kind):
    # 坑 51：推断的业务意图候选是 needs_confirmation，任何 tier 都不得当 confirmed。
    # inferred 不在白名单，双重锁（kind 与 confirmation 各挡一次）。
    result = assess_one(spec_of(GT), {"f": -1}, source=_src(kind, confirmation="needs_confirmation"))
    assert result["source_confirmation"] == "unconfirmed"
    assert result["status"] == "UNKNOWN"


@pytest.mark.parametrize("kind", ["ddl", "annotation", "openapi"])
def test_technical_tier_pass_direction(kind):
    # 做法 16：合法值/违反值双向都测 —— 技术 tier 不只解锁 FAIL，也解锁 PASS
    result = assess_one(spec_of(GT), {"f": 1}, source=_src(kind))
    assert result["status"] == "PASS"
    assert result["confirmation_tier"] == "technical"


@pytest.mark.parametrize("kind", ["ddl", "annotation", "openapi"])
def test_technical_tier_no_observations_is_unknown(kind):
    rule = {"id": "r", "scope": "example", "source": _src(kind), "assert": spec_of(GT)}
    report = rule_assessment.assess({"schema": "business-rules/1", "target": "sample", "rules": [rule]},
                                    {"observations": []})
    result = report["results"][0]
    assert result["status"] == "UNKNOWN"
    assert result["observed_status"] == "UNKNOWN"
    assert result["gate_eligible"] is False


@pytest.mark.parametrize("kind", ["ddl", "annotation", "openapi"])
@pytest.mark.parametrize("kwargs", [{"reached": False}, {"refs": []}, {"obs_scope": "other"}])
def test_technical_tier_three_state_gates_not_relaxed(kind, kwargs):
    # 门控放开 ≠ 三态放宽：未到达 / 无证据 / scope 不匹配仍须 UNKNOWN
    result = assess_one(spec_of(GT), {"f": 1}, source=_src(kind), **kwargs)
    assert result["status"] == "UNKNOWN"


def test_mutation_gate_shrinks_to_business_only(monkeypatch):
    # M11：门控缩回只认业务 tier —— 复现本次修复前的阻塞（84% 永远 UNKNOWN），守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        report = original(pack, observations)
        for r in report["results"]:
            if r["confirmation_tier"] == "technical":
                r["status"] = "UNKNOWN"
                r["source_confirmation"] = "unconfirmed"
        return report
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, _tier_guard)


def test_mutation_technical_unknown_washed_to_pass(monkeypatch):
    # M12：技术 tier 的 UNKNOWN 洗成 PASS —— 三态铁律红线，守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        report = original(pack, observations)
        for r in report["results"]:
            if r["confirmation_tier"] == "technical" and r["status"] == "UNKNOWN":
                r["status"] = "PASS"
        return report
    def unknown_guard():
        assert assess_one(spec_of(GT), {}, source=_src("ddl"))["status"] == "UNKNOWN"
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, unknown_guard)


def test_mutation_needs_confirmation_washed_to_confirmed(monkeypatch):
    # M13：needs_confirmation 洗成 confirmed —— 坑 51 红线，守卫必须红
    original = rule_assessment.assess
    def mutated(pack, observations):
        for rule in pack["rules"]:
            src = rule.get("source")
            if isinstance(src, dict) and src.get("confirmation") == "needs_confirmation":
                src["confirmation"] = "confirmed"
        return original(pack, observations)
    def needs_confirmation_guard():
        result = assess_one(spec_of(GT), {"f": -1}, source=_src("document", confirmation="needs_confirmation"))
        assert result["status"] == "UNKNOWN"
    assert _caught(monkeypatch, rule_assessment, "assess", mutated, needs_confirmation_guard)


def test_mutation_tier_constants_drift_from_gate(monkeypatch):
    # M14：tier 常量单方面扩进重叠 kind —— 结构一致性守卫必须红（坑 11 漂移）
    monkeypatch.setattr(rule_assessment, "BUSINESS_KINDS", ("user", "document", "ddl"))
    with pytest.raises(AssertionError):
        test_gate_union_matches_tier_constants()


def test_mutation_gate_literal_drifts_from_tiers(monkeypatch):
    # M15：门控字面元组单方面缩 kind —— 结构一致性守卫必须红
    import inspect
    real = inspect.getsource(rule_assessment)
    drifted = real.replace('"ddl", "annotation", "openapi"', '"annotation", "openapi"')
    assert drifted != real, "变异必须真的改了源码形态"
    monkeypatch.setattr(inspect, "getsource", lambda obj: drifted if obj is rule_assessment else real)
    with pytest.raises(AssertionError):
        test_gate_union_matches_tier_constants()