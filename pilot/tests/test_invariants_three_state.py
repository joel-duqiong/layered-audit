"""不变式求值器的三值判定 —— 「不知道」永远不许变成「成立」。

这组测试锁住五个曾经存在的洞（每一个都实测复现过）：

  1. field_eq_field 两侧都缺失 -> None == None -> 假绿
  2. jsonb_subset_of 两列都取不到 -> 两个空集 -> 假绿
  3. jsonb_sum_eq_field 列取不到 -> 0 != None -> 假红
  4. 零适用行 -> ok = not failures -> 假绿
  5. when 字段名写错 -> 静默全部跳过 -> 落到第 4 条
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.invariants import FAIL, PASS, UNKNOWN, evaluate, evaluate_all  # noqa: E402


EQ = {"id": "T-EQ", "assert": {"type": "field_eq_field", "left": "a", "right": "b"}}
SUBSET = {"id": "T-SUB", "assert": {
    "type": "jsonb_subset_of", "items_path": "items", "ref_path": "ref",
    "id_field": "productId", "ref_id_field": "productId"}}
SUM = {"id": "T-SUM", "assert": {
    "type": "jsonb_sum_eq_field", "items_path": "items",
    "value_field": "price", "qty_field": "qty", "equals": "total"}}


# ---- 洞 1：field_eq_field 两侧都缺失 ----
def test_eq_field_both_missing_is_unknown_not_pass():
    verdict, detail = evaluate({"id": 1}, EQ)
    assert verdict == UNKNOWN
    assert detail["ok"] is None          # 旧实现这里是 True


def test_eq_field_one_missing_is_unknown():
    assert evaluate({"a": 1, "b": None}, EQ)[0] == UNKNOWN


def test_eq_field_real_values_still_work():
    assert evaluate({"a": 5, "b": 5}, EQ)[0] == PASS
    assert evaluate({"a": 5, "b": 6}, EQ)[0] == FAIL


# ---- 洞 2：jsonb_subset_of 取不到列 ----
def test_subset_both_columns_missing_is_unknown():
    assert evaluate({"id": 1}, SUBSET)[0] == UNKNOWN


def test_subset_id_field_name_wrong_is_unknown():
    # 元素里没有 productId 这个键 —— 字段名对不上，不能当成「空集所以成立」
    row = {"id": 1, "items": [{"sku": 1}], "ref": [{"sku": 1}]}
    assert evaluate(row, SUBSET)[0] == UNKNOWN


def test_subset_real_violation_still_detected():
    row = {"id": 1, "items": [{"productId": 1}, {"productId": 99}],
           "ref": [{"productId": 1}]}
    assert evaluate(row, SUBSET)[0] == FAIL


def test_subset_all_inside_is_pass():
    row = {"id": 1, "items": [{"productId": 1}], "ref": [{"productId": 1}, {"productId": 2}]}
    assert evaluate(row, SUBSET)[0] == PASS


# ---- 洞 3：jsonb_sum_eq_field 取不到列 ----
def test_sum_missing_column_is_unknown_not_fail():
    verdict, detail = evaluate({"id": 1}, SUM)
    assert verdict == UNKNOWN            # 旧实现是 FAIL（0 != None）→ 假红
    assert detail["ok"] is None


def test_sum_real_mismatch_still_fails():
    row = {"id": 1, "items": [{"price": 10, "qty": 2}], "total": 999}
    assert evaluate(row, SUM)[0] == FAIL


# ---- 洞 4：零适用行 ----
def test_zero_applicable_rows_is_unknown_not_pass():
    result = evaluate_all([], [EQ])[0]
    assert result["status"] == UNKNOWN
    assert result["ok"] is None          # 旧实现是 True
    assert result["applicable"] is False


# ---- 洞 5：when 字段名写错 ----
def test_when_field_missing_marks_undecidable_not_skipped():
    inv = dict(EQ, when={"field": "not_there", "op": "eq", "value": 1})
    result = evaluate_all([{"a": 1, "b": 1}], [inv])[0]
    assert result["undecidable_rows"] == 1   # 旧实现会算成 skipped
    assert result["status"] == UNKNOWN
    assert result["ok"] is None


def test_when_condition_false_is_ordinary_skip():
    inv = dict(EQ, when={"field": "kind", "op": "eq", "value": "A"})
    result = evaluate_all([{"a": 1, "b": 2, "kind": "B"}], [inv])[0]
    assert result["skipped"] == 1
    assert result["undecidable_rows"] == 0
    assert result["status"] == UNKNOWN       # 一行都没判定 -> 未知


# ---- 汇总三态 ----
def test_summary_pass_fail_unknown():
    rows = [{"id": 1, "a": 1, "b": 1}]
    assert evaluate_all(rows, [EQ])[0]["status"] == PASS
    rows_bad = [{"id": 1, "a": 1, "b": 2}]
    assert evaluate_all(rows_bad, [EQ])[0]["status"] == FAIL
    assert evaluate_all([], [EQ])[0]["status"] == UNKNOWN


def test_mixed_unknown_rows_do_not_turn_fail_into_pass():
    # 一行无法判定 + 一行真的违反 -> 必须仍然是 fail
    inv = dict(EQ, when={"field": "kind", "op": "eq", "value": "A"})
    rows = [{"id": 1, "a": 1, "b": 1, "kind": "A"},
            {"id": 2, "a": 1, "b": 2, "kind": "A"},
            {"id": 3, "a": 1, "b": 1}]        # kind 缺失 -> undecidable
    result = evaluate_all(rows, [inv])[0]
    assert result["status"] == FAIL
    assert result["violations"] == 1
    assert result["undecidable_rows"] == 1
