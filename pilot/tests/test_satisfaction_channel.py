"""满足级 δ⁰ 通道契约守卫（W22，坑121）—— opt-in，全部合成夹具。

锁的正确行为：
  · 默认关：satisfaction=False 时任何输入不得产出 inconsistent（opt-in 门）；
  · 只有正面证据算满足：guard kind=unknown / 无 finding / 无 capability 绝不洗成满足；
  · 连接键 = (资源, 判据)：同资源不同判据的违反与满足不构成矛盾（设计推回的机械化）；
  · 双证据源：capability guard（可证明执行）与 oracle 连接的 verified positive finding；
  · 现有 consistent/unknown 裁决优先且不变；
  · 缺失输入显式 INCOMPLETE/disabled，不静默当成「无 guard」；
  · 反向：只有违反 / 只有满足 / 两者都无 / 未verified 不得误判 inconsistent；
  · 守恒：consistent + unknown + inconsistent == 候选数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import frame_check as fc


def _net_pair():
    return {"nodes": {"A#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                              "simple": "A", "method": "m"},
                      "B#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                              "simple": "B", "method": "m"}},
            "edges": []}


def _units():
    return {"units": [{"unit_key": "A#m"}, {"unit_key": "B#m"}]}


def _oracle(oid="O1", entity="X", field="amount", confirmation="confirmed", atype="field_cmp"):
    return {"id": oid, "target": {"entity": entity, "field": field},
            "assert": {"type": atype, "field": field, "op": ">=", "value": 0},
            "level": "L1_executable_constraint", "confirmation": confirmation,
            "source": {"kind": "ddl"}}


def _cap(guard_cell="B#m", oid="O1", kind="guard", field="amount", resolution="field_write"):
    return {"implemented_by": [
        {"oracle_id": oid, "unit_key": guard_cell, "relation": "implemented_by",
         "resolution": resolution,
         "enforcement": {"kind": kind, "field": field, "param": "amt",
                         "op": ">=", "value": 0, "reference": "X.java:10"}}]}


def _finding(fid, symbol, claim, rck, gt="verified", kind="defect", oracle_id=None, refs=()):
    f = {"id": fid, "ground_truth": gt, "claim": claim, "root_cause_key": rck,
         "kind": kind, "evidence_refs": list(refs),
         "location": {"symbol": symbol, "endpoint": ""}}
    if oracle_id is not None:
        f["oracle_id"] = oracle_id
    return f


def _run(findings, capability=None, oracles=None, satisfaction=True):
    return fc.run(_net_pair(), _units(), findings,
                  oracles if oracles is not None else [_oracle()],
                  capability=capability, satisfaction=satisfaction)


# ---------------------------------------------------------------- opt-in 门

def test_default_off_never_inconsistent():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(), satisfaction=False)
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["counts"]["candidates_delta0"]["inconsistent"] == 0
    assert r["coverage_gaps"]["satisfaction_channel"] == "disabled"


def test_opt_in_fires_on_tied_violation_vs_guard():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap())
    c = r["candidates"][0]
    assert c["delta0"] == "inconsistent"
    ev = c["resources"][0]["delta0_evidence"]
    assert ev["oracle_id"] == "O1"
    assert ev["violation_cell"] == "A#m" and ev["satisfaction_cell"] == "B#m"
    assert ev["satisfaction_source"] == "guard"
    assert ev["guard_reference"] == "X.java:10"
    assert ev["violation_findings"] == ["F1"]
    assert r["counts"]["satisfaction_fired_candidates"] == 1
    assert r["counts"]["satisfaction_fired_resources"] == 1


def test_symmetric_direction():
    f = _finding("F1", "B.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(guard_cell="A#m"))
    assert r["candidates"][0]["delta0"] == "inconsistent"
    assert r["candidates"][0]["resources"][0]["delta0_evidence"]["satisfaction_cell"] == "A#m"


def test_tie_via_evidence_refs():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", refs=["O1"])
    r = _run([f], capability=_cap())
    assert r["candidates"][0]["delta0"] == "inconsistent"


# ---------------------------------------------------------------- 正面 finding 证据源

def test_tied_positive_finding_is_satisfaction_source():
    fa = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    fb = _finding("P1", "B.m", "amount 恒非负", "amount.nonneg_proven",
                  kind="positive", oracle_id="O1")
    r = _run([fa, fb])  # 无 capability
    c = r["candidates"][0]
    assert c["delta0"] == "inconsistent"
    ev = c["resources"][0]["delta0_evidence"]
    assert ev["satisfaction_source"] == "positive_finding"
    assert ev["satisfaction_findings"] == ["P1"]
    assert ev["guard_reference"] is None


def test_untied_positive_finding_not_satisfaction():
    fa = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    fb = _finding("P1", "B.m", "amount 恒非负", "amount.nonneg_proven", kind="positive")
    r = _run([fa, fb])
    assert r["candidates"][0]["delta0"] == "unknown"


def test_tied_positive_on_violation_side_is_not_violation():
    # A 只有正面结论（tied），B 有 guard → 两侧都是满足，不得判 inconsistent。
    fa = _finding("P1", "A.m", "amount 恒非负", "amount.nonneg_proven",
                  kind="positive", oracle_id="O1")
    r = _run([fa], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


def test_guard_and_positive_both_count():
    fa = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    fb = _finding("P1", "B.m", "amount 恒非负", "amount.nonneg_proven",
                  kind="positive", oracle_id="O1")
    r = _run([fa, fb], capability=_cap())
    assert r["candidates"][0]["delta0"] == "inconsistent"


# ---------------------------------------------------------------- 连接键 = 资源×判据（坑121 推回）

def test_different_oracle_same_resource_stays_unknown():
    f = _finding("F1", "A.m", "amount 超上限", "amount.over_cap", oracle_id="O2")
    r = _run([f], capability=_cap(), oracles=[_oracle(), _oracle(oid="O2", atype="field_lte_const")])
    assert r["candidates"][0]["delta0"] == "unknown"


def test_untied_violation_stays_unknown():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative")
    r = _run([f], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


def test_observation_finding_is_not_violation():
    f = _finding("F1", "A.m", "amount 观测", "amount.observed",
                 kind="observation", oracle_id="O1")
    r = _run([f], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


def test_unverified_finding_is_not_violation():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative",
                 gt="unverified", oracle_id="O1")
    r = _run([f], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


# ---------------------------------------------------------------- 现有裁决优先

def test_equal_rck_consistent_takes_precedence_over_guard():
    fa = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    fb = _finding("F2", "B.m", "amount 负值", "amount.negative", oracle_id="O1")
    r = _run([fa, fb], capability=_cap())
    assert r["candidates"][0]["delta0"] == "consistent"


def test_different_rck_both_violating_stays_unknown_even_with_guard():
    fa = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    fb = _finding("F2", "B.m", "amount 缺校验", "amount.other", oracle_id="O1")
    r = _run([fa, fb], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


# ---------------------------------------------------------------- 三态红线

def test_no_finding_is_not_satisfaction():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability={"implemented_by": []})
    assert r["candidates"][0]["delta0"] == "unknown"


def test_unknown_enforcement_is_not_guard():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(kind="unknown"))
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 0


def test_guard_only_is_unknown():
    r = _run([], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"


def test_violation_only_is_unknown():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f])
    assert r["candidates"][0]["delta0"] == "unknown"


def test_neither_side_is_unknown():
    r = _run([])
    assert r["candidates"][0]["delta0"] == "unknown"


def test_both_guards_no_violation_is_unknown_not_consistent():
    cap = {"implemented_by": _cap()["implemented_by"] + _cap(guard_cell="A#m")["implemented_by"]}
    r = _run([], capability=cap)
    assert r["candidates"][0]["delta0"] == "unknown"


def test_both_positive_no_violation_is_unknown_not_consistent():
    fa = _finding("P1", "A.m", "amount 恒非负", "amount.nonneg_proven", kind="positive", oracle_id="O1")
    fb = _finding("P2", "B.m", "amount 恒非负", "amount.nonneg_proven", kind="positive", oracle_id="O1")
    r = _run([fa, fb])
    assert r["candidates"][0]["delta0"] == "unknown"


# ---------------------------------------------------------------- 输入降级可见

def test_enabled_without_capability_is_incomplete_not_silent():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=None)
    assert r["coverage_gaps"]["satisfaction_channel"] == "ok"
    assert r["coverage_gaps"]["satisfaction_guard_source"] == "INCOMPLETE"
    assert any("capability" in w and "INCOMPLETE" in w for w in r["input_warnings"])
    assert r["candidates"][0]["delta0"] == "unknown"


def test_enabled_no_guards_is_ok_zero():
    r = _run([], capability={"implemented_by": []})
    assert r["coverage_gaps"]["satisfaction_guard_source"] == "ok"
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 0
    assert not any("capability 未供给" in w for w in r["input_warnings"])


def test_unconfirmed_oracle_guard_excluded():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(), oracles=[_oracle(confirmation="unconfirmed")])
    st = r["coverage_gaps"]["satisfaction_guard_stats"]
    assert st["excluded_not_confirmed"] == 1 and st["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"


def test_oracle_absent_from_input_guard_excluded():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(), oracles=[])
    st = r["coverage_gaps"]["satisfaction_guard_stats"]
    assert st["excluded_oracle_absent"] == 1 and st["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"


def test_table_only_oracle_without_declarations_unresolved():
    o = _oracle()
    o["target"] = {"table": "t"}
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = fc.run(_net_pair(), _units(), [f], [o], capability=_cap(), satisfaction=True)
    st = r["coverage_gaps"]["satisfaction_guard_stats"]
    assert st["excluded_entity_unresolved"] == 1 and st["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"


def test_table_only_oracle_resolved_via_jpa_meta():
    o = _oracle()
    o["target"] = {"table": "t"}
    meta = {"X": {"table": "t", "columns": {}}}
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = fc.run(_net_pair(), _units(), [f], [o], entity_meta=meta, capability=_cap(), satisfaction=True)
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 1
    assert r["candidates"][0]["delta0"] == "inconsistent"


def test_guard_field_mismatch_excluded():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(field="other"))
    st = r["coverage_gaps"]["satisfaction_guard_stats"]
    assert st["excluded_field_mismatch"] == 1 and st["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"


def test_guard_field_substring_of_assert_field_is_mismatch():
    # V3（坑121 字段维复发）：guard 字段 amountCent vs assert.field amount —— substring 相关
    # 但不同性质。桶落在 guard 自己的字段 setAmountCent 上（ident 键匹配，夹具真到达判定面，
    # V20 教训：键不匹配会中和变异）。精确相等守卫必须挡掉 substring。
    o = _oracle(field="amount")
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmountCent"]},
                                  "simple": c, "method": "m"} for c in "AB"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "AB"]}
    f = _finding("F1", "A.m", "amountCent 可为负", "amountcent.negative", oracle_id="O1")
    r = fc.run(net, units, [f], [o], capability=_cap(field="amountCent"), satisfaction=True)
    st = r["coverage_gaps"]["satisfaction_guard_stats"]
    assert st["excluded_field_mismatch"] == 1 and st["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["counts"]["satisfaction_fired_candidates"] == 0


def test_evidence_refs_substring_is_not_oracle_tie():
    # V4：evidence_refs=["O10"] 不是 O1 的连接（精确相等）。计数面与判定面共用
    # _finding_tie_ids 单一实现 —— 两面都必须 0/unknown，不许一精一粗。
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", refs=["O10"])
    r = _run([f], capability=_cap())
    assert r["counts"]["satisfaction_tied_violation_findings"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["counts"]["satisfaction_fired_candidates"] == 0


def test_satisfaction_section_visible_even_when_channel_disabled():
    # 满足集通道接线守卫：_satisfaction_section_value 不是死代码 —— 产物可见面消费它，
    # 默认关时照样发布（可见≠裁决，裁决只消费 oracle 连接的满足证据）。
    fs = [_finding("P1", "A.m", "amount 恒非负", "amount.nonneg_proven", kind="positive")]
    r = fc.run(_net_pair(), _units(), fs, [_oracle()])
    res = r["candidates"][0]["resources"][0]
    assert res["satisfaction_section"]["A#m"] == ["amount.nonneg_proven"]
    assert res["satisfaction_section"]["B#m"] == []
    assert r["candidates"][0]["delta0"] == "unknown"


def test_non_field_write_resolution_not_consumed():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap(resolution="bean_validation_cascade"))
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 0
    assert r["candidates"][0]["delta0"] == "unknown"


# ---------------------------------------------------------------- 守恒 / 分解 / 结构事实

def test_delta0_counts_conservation():
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap())
    d = r["counts"]["candidates_delta0"]
    assert d["consistent"] + d["unknown"] + d["inconsistent"] == len(r["candidates"])


def test_zero_fire_decomposition_visible():
    # 0 触发必须能归因：guard 事实与 oracle 连接数并排在产物里（机制成立≠语料充足）。
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative")  # 无连接
    r = _run([f], capability=_cap())
    assert r["counts"]["satisfaction_fired_candidates"] == 0
    assert r["counts"]["satisfaction_tied_violation_findings"] == 0
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 1


def test_guard_bucket_cycle_uses_constraint_basis():
    # 结构事实1：真实 DDL 形状（field_cmp）可被 bridge 解释 → 同桶必有约束集截面 → constraint_set。
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = _run([f], capability=_cap())
    cyc = [c for c in r["cycles"] if c["resource"] == "e.X.setAmount"][0]
    assert cyc["basis"] == "constraint_set"
    assert r["counts"]["cycle_obstructions"] == 0


def test_p2_not_vb_blocks_all_edges_on_3cycle():
    # P2 挡边守卫（V20 第二轮改名：有环 ≠ 环上有已知边）。真 3-细胞环，B/C 混合 V+S，
    # P2(not vb/not va) 把三条边全挡成 unknown ⇒ edges_known==0，parity 无事可做。
    # 已知边上的 coboundary 由 pure 对照承担。A1（去 not-vb）下三边全 1 → 奇环 → 本守卫红。
    o = _oracle(atype="field_gte_const")  # bridge 拒收 → 强制二值退回
    del o["assert"]["op"]
    cap = {"implemented_by": _cap(guard_cell="B#m")["implemented_by"]
           + _cap(guard_cell="C#m")["implemented_by"]}
    fs = [_finding("FA", "A.m", "amount 可为负", "amount.negative", oracle_id="O1"),
          _finding("FB", "B.m", "amount 另一种违反", "amount.other", oracle_id="O1"),
          _finding("FC", "C.m", "amount 第三种违反", "amount.third", oracle_id="O1")]
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                                  "simple": c, "method": "m"} for c in "ABC"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "ABC"]}
    r = fc.run(net, units, fs, [o], capability=cap, satisfaction=True)
    cyc = [c for c in r["cycles"] if c["resource"] == "e.X.setAmount"][0]
    assert cyc["basis"] == "binary"
    assert len(cyc["cells"]) == 3
    # B/C 同时携带违反指纹与 guard ⇒ P2 把三条边全挡成 unknown（不是没环，是环上无已知边）
    assert cyc["edges_known"] == 0
    assert cyc["status"] == "unknown"
    assert r["counts"]["cycle_obstructions"] == 0


def test_channel_coboundary_on_real_3cycle_pure_cells():
    # 阳性对照：纯 V/S 细胞 → 三条已知边（1,1,0）真的跑 parity，2-着色相容 → consistent。
    # 证明上一个守卫的 unknown 不是「parity 没跑」，而是 P2 挡边；coboundary 在已知边上成立。
    o = _oracle(atype="field_gte_const")
    del o["assert"]["op"]
    fs = [_finding("FA", "A.m", "amount 可为负", "amount.negative", oracle_id="O1"),
          _finding("FC", "C.m", "amount 可为负", "amount.negative", oracle_id="O1")]
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                                  "simple": c, "method": "m"} for c in "ABC"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "ABC"]}
    r = fc.run(net, units, fs, [o], capability=_cap(guard_cell="B#m"), satisfaction=True)
    cyc = [c for c in r["cycles"] if c["resource"] == "e.X.setAmount"][0]
    assert cyc["basis"] == "binary"
    assert cyc["edges_known"] == 3
    assert cyc["status"] == "consistent"
    assert r["counts"]["cycle_obstructions"] == 0


def test_external_judgments_break_precondition_p3():
    # P3 边界反例（V20 API 反例机械化）：外部 judgments 注入 0-边 + 通道给 A-C=1 ⇒
    # 奇矛盾 ⇒ obstructions 1。生产不可达（CLI 无 --judgments、audit_entry 不传），
    # 但定理必须带 P3 前提 —— 本守卫锁「前提破坏时结论不再成立」。
    o = _oracle(atype="field_gte_const")
    del o["assert"]["op"]
    fs = [_finding("FA", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")]
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                                  "simple": c, "method": "m"} for c in "ABC"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "ABC"]}
    j = {("A#m", "B#m", "e.X.setAmount"): "consistent",
         ("B#m", "C#m", "e.X.setAmount"): "consistent"}
    r = fc.run(net, units, fs, [o], judgments=j, capability=_cap(guard_cell="C#m"), satisfaction=True)
    assert r["counts"]["cycle_obstructions"] == 1


def test_satisfaction_facts_counter_replaces_hardcoded_zero():
    r = _run([], capability=_cap())
    assert r["coverage_gaps"]["resource_level_satisfaction_judgments"] == 1
    r0 = _run([], capability=_cap(), satisfaction=False)
    assert r0["coverage_gaps"]["resource_level_satisfaction_judgments"] == 0


# ---------------------------------------------------------------- 默认值 / 逐字节（V20 A4/A8/A-cap）

def test_guard_evidence_does_not_leak_across_cells():
    # N3 守卫（V20 第二轮查出）：(细胞, ident) 键的细胞维必须有自己的守卫。
    # guard 只属 C#m，B#m 无 guard ⇒ A|B=unknown（不得继承 C 的 guard），A|C=inconsistent。
    fs = [_finding("FA", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")]
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                                  "simple": c, "method": "m"} for c in "ABC"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "ABC"]}
    r = fc.run(net, units, fs, [_oracle()], capability=_cap(guard_cell="C#m"), satisfaction=True)
    d = {tuple(c["cells"]): c["delta0"] for c in r["candidates"]}
    assert d[("A#m", "B#m")] == "unknown"
    assert d[("A#m", "C#m")] == "inconsistent"
    assert d[("B#m", "C#m")] == "unknown"


def test_run_default_satisfaction_is_off():
    # A4 守卫：不传 satisfaction 参数，锁默认值本身（不是显式传 False 的关掉行为）。
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    r = fc.run(_net_pair(), _units(), [f], [_oracle()], capability=_cap())
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["counts"]["candidates_delta0"]["inconsistent"] == 0
    assert r["coverage_gaps"]["satisfaction_channel"] == "disabled"


def test_capability_without_flag_is_byte_identical():
    # A-cap 守卫：供 capability 不开开关 ⇒ 整份产物与完全不供 capability 逐字节相同。
    # 统计计算与发布必须同门（V20：原实现 run 无条件算/发布，三键漂移 + stdout 自相矛盾）。
    import json
    f = _finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")
    a = fc.run(_net_pair(), _units(), [f], [_oracle()], capability=_cap())
    b = fc.run(_net_pair(), _units(), [f], [_oracle()], capability=None)
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_cli_default_off_and_on(tmp_path):
    # A8 守卫：真 CLI subprocess，无 --satisfaction → disabled；有 → ok 且触发。
    import json, os, subprocess
    engine = Path(__file__).resolve().parents[1] / "engine" / "frame_check.py"
    net = _net_pair()
    units = _units()
    findings = {"findings": [_finding("F1", "A.m", "amount 可为负", "amount.negative", oracle_id="O1")]}
    oracles = {"oracles": [_oracle()]}
    for name, doc in (("net", net), ("units", units), ("findings", findings),
                      ("oracles", oracles), ("cap", _cap())):
        (tmp_path / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    base = [sys.executable, str(engine), "--net", str(tmp_path / "net.json"), "--units", str(tmp_path / "units.json"),
            "--findings", str(tmp_path / "findings.json"), "--oracles", str(tmp_path / "oracles.json")]
    # PATH 挡板自包含（做法49/94）：tmp 里造失败桩，不依赖任何外部固定目录，换机不退化。
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("javac", "java", "mvn", "docker", "psql", "gradle"):
        stub = bindir / tool
        stub.write_text("#!/bin/sh\nexit 97\n")
        stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:" + os.environ["PATH"])
    out_off = tmp_path / "off.json"
    p = subprocess.run([*base, "--out", str(out_off)], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    r = json.loads(out_off.read_text(encoding="utf-8"))
    assert r["coverage_gaps"]["satisfaction_channel"] == "disabled"
    assert r["counts"]["candidates_delta0"]["inconsistent"] == 0
    assert r["coverage_gaps"]["satisfaction_guard_stats"]["facts_used"] == 0
    assert "未测量" in p.stdout
    out_on = tmp_path / "on.json"
    p = subprocess.run([*base, "--satisfaction", "--capability", str(tmp_path / "cap.json"),
                        "--out", str(out_on)], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    r = json.loads(out_on.read_text(encoding="utf-8"))
    assert r["coverage_gaps"]["satisfaction_channel"] == "ok"
    assert r["counts"]["candidates_delta0"]["inconsistent"] == 1