"""类层规则派发层的 oracle 闸门（硬约束 5 的第二扇门）。

第一扇门是 `rule_oracle_buckets`（规则三层全缺）。但 `diff_rules` 里还有
另一批规则直接产出结论，`NO_STATE_MACHINE` 就是其中之一：

  报 HIGH + ground_truth=verified「状态机缺失：9/9 种迁移全部被接受」
  而项目权威文档第 64 行把「订单状态流转限制：当前允许任意状态直接切换」
  列在「当前未定义、实现也未校验的规则」标题下。

**「9/9 迁移都被接受」是客观观测；「所以这是缺陷」需要业务预期。**

闸门只对**声明了 oracle** 的规则生效：值相等缺失、封装泄漏、自调用绕过
代理这些是语言级客观事实，不需要业务预期，不能被误伤。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PILOT))

import class_rules  # noqa: E402

F = {"id": "X", "severity": "HIGH", "layer": "class",
     "title": "t", "detail": "d", "evidence": {}}


def _rule(oracle: dict | None) -> dict:
    params = {"finding_id": "X"}
    if oracle is not None:
        params["oracle"] = oracle
    return {"id": "some_rule", "params": params}


def test_refuted_rule_is_not_filed():
    kept, held = class_rules.oracle_gate(
        _rule({"confirmation": "refuted", "reference": "doc 第 64 行"}), [F])
    assert kept == []
    assert len(held) == 1
    assert held[0]["oracle"]["confirmation"] == "refuted"


def test_unconfirmed_rule_is_not_filed():
    kept, held = class_rules.oracle_gate(
        _rule({"confirmation": "unconfirmed"}), [F])
    assert kept == []
    assert len(held) == 1


def test_confirmed_rule_is_filed():
    kept, held = class_rules.oracle_gate(
        _rule({"confirmation": "confirmed", "reference": "doc P1"}), [F])
    assert kept == [F]
    assert held == []


def test_rule_without_oracle_is_objective_and_unchanged():
    """语言级客观事实不需要业务预期 —— 闸门不能误伤它们。"""
    kept, held = class_rules.oracle_gate(_rule(None), [F])
    assert kept == [F]
    assert held == []


def test_unknown_confirmation_is_conservative():
    kept, held = class_rules.oracle_gate(_rule({"confirmation": "也许吧"}), [F])
    assert kept == []
    assert len(held) == 1


def test_held_record_carries_the_original_finding():
    """留痕必须带上原结论，否则人工复核看不到被压下来的是什么。"""
    _, held = class_rules.oracle_gate(_rule({"confirmation": "refuted"}), [F])
    assert held[0]["finding"]["id"] == "X"
    assert held[0]["rule_id"] == "some_rule"


def test_empty_findings_stay_empty():
    kept, held = class_rules.oracle_gate(_rule({"confirmation": "refuted"}), [])
    assert kept == [] and held == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
