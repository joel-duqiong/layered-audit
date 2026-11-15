"""frame_check 的契约测试：旁条件、三态、Čech δ⁰、空输入。

全部用合成数据，不碰真实台账、不跑流水线、不写数据库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import frame_check as fc


def _net(nodes=None, edges=None):
    return {"nodes": nodes or {}, "edges": edges or []}


def _units(*keys):
    return {"units": [{"unit_key": k} for k in keys]}


def _node(taint=None, endpoint=None, simple=None, method=None, node_type=None):
    n = {}
    if taint is not None:
        n["taint"] = taint
    if endpoint:
        n["endpoint"] = endpoint
    if simple:
        n["simple"] = simple
    if method:
        n["method"] = method
    if node_type:
        n["type"] = node_type
    return n


def _taint(source_fields=(), sinks=()):
    return {"source_fields": list(source_fields), "sinks": list(sinks)}


def _finding(fid, symbol, claim, gt="verified", endpoint="", rck=""):
    return {"id": fid, "ground_truth": gt, "claim": claim, "root_cause_key": rck,
            "evidence_refs": [], "location": {"symbol": symbol, "endpoint": endpoint}}


# ---------------------------------------------------------------- 旁条件

def test_disjoint_footprints_no_candidate():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"])),
                "B#m": _node(_taint(sinks=["e.Y.setB"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_candidate"] == 0
    assert r["counts"]["pairs_disjoint_empty"] == 1
    assert r["candidates"] == []


def test_write_write_overlap_is_candidate():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setStatus"])),
                "B#m": _node(_taint(sinks=["e.X.setStatus"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_candidate"] == 1
    c = r["candidates"][0]
    assert c["overlap_written"] == ["e.X.setStatus"]
    assert c["resources"][0]["a_writes"] and c["resources"][0]["b_writes"]
    assert c["frame_violation"] == ["mod(A) ∩ fp(B) ≠ ∅", "mod(B) ∩ fp(A) ≠ ∅"]
    assert c["delta0"] == "unknown"


def test_write_read_overlap_is_candidate():
    net = _net({"A#m": _node(), "B#m": _node()},
               [{"from": "A#m", "to": "table:t", "type": "WRITES"},
                {"from": "B#m", "to": "table:t", "type": "READS"}])
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_candidate"] == 1
    res = r["candidates"][0]["resources"][0]
    assert res["a_writes"] and res["b_reads"] and not res["b_writes"]
    assert res["provenance"]["A#m"][0]["source"] == "WRITES"
    assert res["provenance"]["B#m"][0]["source"] == "READS"


def test_read_read_overlap_not_candidate():
    # 双方都不写 ⇒ mod(C) ∩ fv(R) = ∅ 仍成立 ⇒ 局部只读推理可组合，不是涌现。
    net = _net({"A#m": _node(_taint(source_fields=["d.Req.q"])),
                "B#m": _node(_taint(source_fields=["d.Req.q"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_candidate"] == 0
    assert r["counts"]["pairs_disjoint_readonly"] == 1


def test_table_write_write_via_edges():
    net = _net({"A#m": _node(), "B#m": _node()},
               [{"from": "A#m", "to": "table:t", "type": "WRITES"},
                {"from": "B#m", "to": "table:t", "type": "WRITES"}])
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_candidate"] == 1
    assert r["candidates"][0]["overlap_written"] == ["table:t"]


# ---------------------------------------------------------------- 三态

def test_missing_footprint_is_unknown_not_disjoint():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["cells_footprint_unknown"] == 1
    assert r["counts"]["pairs_unknown"] == 1
    assert r["counts"]["pairs_candidate"] == 0
    assert r["counts"]["pairs_disjoint_empty"] == 0
    assert r["ratios"]["unknown_pairs_over_total"] == 1.0


def test_unknown_never_reported_as_pass_or_disjoint():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"]))})
    r = fc.run(net, _units("A#m", "B#m", "C#m"))
    g = r["coverage_gaps"]
    assert g["unknown_cells_count"] == 2
    assert "B#m" in g["unknown_cells_sample"]
    assert r["counts"]["pairs_unknown"] == 3


def test_known_empty_footprint_disjoint_against_unknown():
    # 空足迹与任何足迹不交：known-empty × unknown 可判不相交，不算 unknown。
    net = _net({"A#m": _node(_taint())})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["pairs_disjoint_unknown_side"] == 1
    assert r["counts"]["pairs_unknown"] == 0


def test_taint_present_empty_is_known_not_unknown():
    net = _net({"A#m": _node(_taint())})
    r = fc.run(net, _units("A#m"))
    assert r["counts"]["cells_footprint_known"] == 1
    assert r["counts"]["cells_footprint_unknown"] == 0


# ---------------------------------------------------------------- Čech δ⁰

def test_delta0_consistent_when_both_sides_violate_same_resource():
    nodes = {"A#m": _node(_taint(sinks=["e.X.setStatus"]), simple="A", method="m"),
             "B#m": _node(_taint(sinks=["e.X.setStatus"]), simple="B", method="m")}
    findings = [_finding("F1", "A.m", "status 越界", rck="status.invalid"),
                _finding("F2", "B.m", "status 缺失", rck="status.invalid")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    c = r["candidates"][0]
    assert c["delta0"] == "consistent"
    assert c["resources"][0]["findings"] == {"A#m": ["F1"], "B#m": ["F2"]}
    assert r["counts"]["candidates_delta0"] == {"consistent": 1, "unknown": 0, "inconsistent": 0}


def test_delta0_unknown_when_only_one_side_has_judgment():
    nodes = {"A#m": _node(_taint(sinks=["e.X.setStatus"]), simple="A", method="m"),
             "B#m": _node(_taint(sinks=["e.X.setStatus"]), simple="B", method="m")}
    findings = [_finding("F1", "A.m", "status 越界")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["delta0"] == "unknown"


def test_delta0_unknown_without_findings_never_pass():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"])),
                "B#m": _node(_taint(sinks=["e.X.setA"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["candidates"][0]["delta0"] == "unknown"
    assert "pass" not in r["counts"]["candidates_delta0"]


def test_unverified_finding_does_not_decide_delta0():
    nodes = {"A#m": _node(_taint(sinks=["e.X.setStatus"]), simple="A", method="m"),
             "B#m": _node(_taint(sinks=["e.X.setStatus"]), simple="B", method="m")}
    findings = [_finding("F1", "A.m", "status x", gt="unverified"),
                _finding("F2", "B.m", "status y", gt="unverified")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["delta0"] == "unknown"


def test_finding_substring_does_not_attribute():
    # token 匹配：字段 name 不许命中 filename / namespace 这类子串。
    nodes = {"A#m": _node(_taint(sinks=["e.X.setName"]), simple="A", method="m"),
             "B#m": _node(_taint(sinks=["e.X.setName"]), simple="B", method="m")}
    findings = [_finding("F1", "A.m", "filename 与 namespace 处理"),
                _finding("F2", "B.m", "rename 逻辑")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["resources"][0]["findings"] == {"A#m": [], "B#m": []}
    assert r["candidates"][0]["delta0"] == "unknown"


def test_oracle_attached_by_normalized_identity():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setStatus"])),
                "B#m": _node(_taint(sinks=["e.X.setStatus"]))})
    oracles = [{"id": "O1", "target": {"entity": "X", "field": "status"}},
               {"id": "O2", "target": {"entity": "Y", "field": "status"}}]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["candidates"][0]["resources"][0]["oracles"] == ["O1"]


def test_table_oracle_attached():
    net = _net({"A#m": _node(), "B#m": _node()},
               [{"from": "A#m", "to": "table:t", "type": "WRITES"},
                {"from": "B#m", "to": "table:t", "type": "READS"}])
    oracles = [{"id": "O1", "target": {"table": "t", "field": "c"}},
               {"id": "O2", "target": {"table": "other"}}]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["candidates"][0]["resources"][0]["oracles"] == ["O1"]


def test_finding_attributed_via_endpoint():
    nodes = {"A#m": _node(_taint(sinks=["e.X.setStatus"]), endpoint="POST /x"),
             "B#m": _node(_taint(sinks=["e.X.setStatus"]), endpoint="POST /y")}
    findings = [_finding("F1", "", "status 违反", endpoint="POST /x", rck="status.invalid"),
                _finding("F2", "", "status 违反", endpoint="POST /y", rck="status.invalid")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    assert r["coverage_gaps"]["findings_attributed"] == 2
    assert r["candidates"][0]["delta0"] == "consistent"


# ---------------------------------------------------------------- 归一化 / 空输入 / 缺口

def test_normalize_setter_to_field():
    assert fc.normalize_resource("a.b.X.setStatus") == ("field", "X", "status")
    assert fc.normalize_resource("a.b.dto.Req.name") == ("field", "Req", "name")
    assert fc.normalize_resource("table:t") == ("table", "t", None)
    assert fc.normalize_resource("field:X.name") == ("field", "X", "name")
    assert fc.normalize_resource("bare") is None


def test_empty_input_no_crash_zero_over_zero():
    r = fc.run(_net(), _units())
    c = r["counts"]
    assert c["cells_total"] == 0
    assert c["pairs_total"] == 0
    assert c["pairs_candidate"] == 0
    assert c["max_overlap_size"] == 0
    assert r["ratios"]["candidate_over_decidable"] == 0.0
    assert r["ratios"]["unknown_pairs_over_total"] == 0.0
    assert r["candidates"] == []


def test_graph_only_cells_included_and_reported():
    net = _net({"A#m": _node(), "R#save": _node()},
               [{"from": "R#save", "to": "table:t", "type": "WRITES"},
                {"from": "A#m", "to": "table:t", "type": "WRITES"}])
    r = fc.run(net, _units("A#m"))
    assert r["counts"]["cells_total"] == 2
    assert r["coverage_gaps"]["graph_only_cells"] == ["R#save"]
    assert r["counts"]["pairs_candidate"] == 1


def test_duplicate_unit_keys_deduped_and_reported():
    r = fc.run(_net({"A#m": _node(_taint())}), _units("A#m", "A#m"))
    assert r["counts"]["cells_total"] == 1
    assert r["coverage_gaps"]["duplicate_unit_keys"] == ["A#m"]
    assert r["coverage_gaps"]["unit_records"] == 2


def test_max_overlap_reported():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA", "e.X.setB"])),
                "B#m": _node(_taint(sinks=["e.X.setA", "e.X.setB", "e.X.setC"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["counts"]["max_overlap_size"] == 2
    assert r["counts"]["max_overlap_candidates"] == ["FC-0001"]


def test_suspect_write_label_flagged_not_dropped():
    # find* 方法被上游标成 WRITES：候选照报，但标记证据可疑，不许伪装成干净证据。
    net = _net({"R#findById": _node(), "R#save": _node()},
               [{"from": "R#findById", "to": "table:t", "type": "WRITES"},
                {"from": "R#save", "to": "table:t", "type": "WRITES"}])
    r = fc.run(net, _units("R#findById", "R#save"))
    assert r["counts"]["pairs_candidate"] == 1
    assert r["candidates"][0]["suspect_write_only"] is False
    assert r["candidates"][0]["resources"][0]["a_write_suspect"] is True
    assert r["coverage_gaps"]["suspect_write_edges"] == 1


def test_suspect_write_only_candidate():
    net = _net({"R#findById": _node(), "R#findByName": _node()},
               [{"from": "R#findById", "to": "table:t", "type": "WRITES"},
                {"from": "R#findByName", "to": "table:t", "type": "WRITES"}])
    r = fc.run(net, _units("R#findById", "R#findByName"))
    assert r["candidates"][0]["suspect_write_only"] is True
    assert r["counts"]["candidates_suspect_write_only"] == 1


def test_sink_write_not_suspect():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"])),
                "B#m": _node(_taint(sinks=["e.X.setA"]))})
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["candidates"][0]["suspect_write_only"] is False
    assert r["coverage_gaps"]["suspect_write_edges"] == 0


# ---------------------------------------------------------------- 归约 1：倒排索引

def test_index_only_compares_within_buckets():
    # A,B 共享资源；C 足迹不相交。桶内只比较 (A,B) 一次，C 根本不进桶。
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA"])),
                "B#m": _node(_taint(sinks=["e.X.setA"])),
                "C#m": _node(_taint(sinks=["e.Y.setZ"]))})
    r = fc.run(net, _units("A#m", "B#m", "C#m"))
    assert r["counts"]["bucket_comparisons"] == 1
    assert r["reduction"]["brute_force_pairs"] == 3
    assert r["counts"]["pairs_disjoint_empty"] == 2


def test_index_equivalent_to_brute_force():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setA", "e.X.setB"])),
                "B#m": _node(_taint(sinks=["e.X.setA"])),
                "C#m": _node(_taint(source_fields=["d.R.q"])),
                "D#m": _node(_taint(source_fields=["d.R.q"]))})
    units = _units("A#m", "B#m", "C#m", "D#m")
    fps, _ = fc.build_footprints(net, units)
    indexed, _ = fc.find_candidates(fps, {}, [], fc.build_index(fps))
    # 暴力参考：枚举所有 known 对
    from itertools import combinations
    brute = []
    known = sorted(c for c, f in fps.items() if f.known)
    for a, b in combinations(known, 2):
        ov = fps[a].all & fps[b].all
        if ov and (ov & (fps[a].writes | fps[b].writes)):
            brute.append((a, b))
    assert sorted(tuple(c["cells"]) for c in indexed) == sorted(brute)


def test_disjoint_pairs_never_enumerated():
    net = _net({f"C{i}#m": _node(_taint(sinks=[f"e.X.set{i}"])) for i in range(6)})
    r = fc.run(net, _units(*[f"C{i}#m" for i in range(6)]))
    assert r["counts"]["bucket_comparisons"] == 0
    assert r["counts"]["pairs_candidate"] == 0
    assert r["reduction"]["disjoint_pairs_never_enumerated"] == 15


# ---------------------------------------------------------------- 归约 2：环一致性

def _triangle_net():
    return _net({c: _node(_taint(sinks=["e.X.setStatus"])) for c in ("A#m", "B#m", "C#m")})


def test_cycle_inconsistent_is_obstruction():
    # 三元环上三条边都判不一致 → GF(2) 奇偶矛盾 → 非零障碍 → 候选。
    j = {("A#m", "B#m", "e.X.setStatus"): "inconsistent",
         ("A#m", "C#m", "e.X.setStatus"): "inconsistent",
         ("B#m", "C#m", "e.X.setStatus"): "inconsistent"}
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), judgments=j)
    assert r["counts"]["cycle_obstructions"] == 1
    ob = r["cycle_obstructions"][0]
    assert ob["status"] == "inconsistent"
    assert ob["conflict"] is not None
    assert r["counts"]["cycle_status"]["inconsistent"] == 1


def test_cycle_consistent_not_reported():
    j = {("A#m", "B#m", "e.X.setStatus"): "consistent",
         ("A#m", "C#m", "e.X.setStatus"): "consistent",
         ("B#m", "C#m", "e.X.setStatus"): "consistent"}
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), judgments=j)
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["counts"]["cycle_status"]["consistent"] == 1
    assert r["cycle_obstructions"] == []


def test_cycle_unknown_without_judgments_never_pass():
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"))
    assert r["counts"]["cycle_status"] == {"consistent": 0, "inconsistent": 0, "unknown": 1}
    assert r["counts"]["cycle_obstructions"] == 0
    assert "pass" not in r["counts"]["cycle_status"]


def test_cycle_mixed_unknown_is_unknown_not_consistent():
    # 一条已知一致 + 一条 unknown → 不能确认整环一致 → unknown。
    j = {("A#m", "B#m", "e.X.setStatus"): "consistent"}
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), judgments=j)
    assert r["counts"]["cycle_status"]["unknown"] == 1
    assert r["counts"]["cycle_obstructions"] == 0


def test_pairwise_consistent_triangle_can_still_be_obstruction():
    # 成对 δ⁰ 各自看都「一致」，但环上奇偶矛盾 → 高阶障碍，成对比较看不见。
    # AB 一致(0), BC 一致(0) ⇒ A=C；AC 不一致(1) ⇒ 矛盾。
    j = {("A#m", "B#m", "e.X.setStatus"): "consistent",
         ("B#m", "C#m", "e.X.setStatus"): "consistent",
         ("A#m", "C#m", "e.X.setStatus"): "inconsistent"}
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), judgments=j)
    assert r["counts"]["cycle_obstructions"] == 1


def test_higher_order_limitation_reported():
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"))
    notes = " ".join(r["coverage_gaps"]["notes"])
    assert "Čech H¹" in notes and "高阶" in notes
    assert "cycle_space_dimension" in r["overlap_graph"]
    assert r["overlap_graph"]["vertices"] == 3
    assert r["overlap_graph"]["edges"] == 3
    assert r["overlap_graph"]["cycle_space_dimension"] == 1


def test_binary_vertex_derived_edges_degenerate():
    # 只有 findings（二值顶点）导出的边恒为 coboundary：双方都违反 → 一致，检不出障碍。
    nodes = {c: _node(_taint(sinks=["e.X.setStatus"]), simple=c[0], method="m") for c in ("A#m", "B#m", "C#m")}
    findings = [_finding(f"F{c[0]}", f"{c[0]}.m", "status 违反", rck="status.invalid") for c in ("A#m", "B#m", "C#m")]
    r = fc.run(_net(nodes), _units("A#m", "B#m", "C#m"), findings)
    assert r["counts"]["cycle_status"]["consistent"] == 1
    assert r["counts"]["cycle_obstructions"] == 0


# ---------------------------------------------------------------- 归约 3：CRUD 噪音过滤 + 业务级要求

def _repo_table_net():
    return _net({"R#findById": _node(node_type="REPO_OP"), "R#save": _node(node_type="REPO_OP")},
                [{"from": "R#findById", "to": "table:t", "type": "READS"},
                 {"from": "R#save", "to": "table:t", "type": "WRITES"}])


def test_crud_filter_drops_repo_repo_table():
    r = fc.run(_repo_table_net(), _units("R#findById", "R#save"),
               plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    assert r["counts"]["candidates_before_filter"] == 1
    assert r["counts"]["dropped_crud_noise"] == 1
    assert r["counts"]["pairs_candidate"] == 0
    assert r["candidates_all"][0]["crud_noise"] is True
    assert r["candidates_all"][0]["survives_filter"] is False


def test_crud_filter_backward_compat_no_plumbing():
    # 不传 plumbing_types → 不过滤，候选保留（向后兼容）。
    r = fc.run(_repo_table_net(), _units("R#findById", "R#save"))
    assert r["counts"]["pairs_candidate"] == 1
    assert r["candidates"][0]["crud_noise"] is False


def test_crud_filter_keeps_service_field_pair():
    net = _net({"S#create": _node(_taint(sinks=["e.X.setFlag"]), node_type="SVC_OP"),
                "S#delete": _node(_taint(sinks=["e.X.setFlag"]), node_type="SVC_OP")})
    r = fc.run(net, _units("S#create", "S#delete"),
               plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    assert r["counts"]["pairs_candidate"] == 1
    assert r["counts"]["dropped_crud_noise"] == 0
    assert r["candidates"][0]["business_meaningful"] is True


def test_business_oracle_bare_field_anchor():
    # L0 业务判据只写裸 column 名（无 entity），按字段名 join 到 sink。
    net = _net({"S#apply": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP"),
                "S#updateStatus": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP")})
    oracles = [{"id": "doc:status", "level": "L0_business_rule", "target": {"field": "status"}}]
    r = fc.run(net, _units("S#apply", "S#updateStatus"), oracles=oracles,
               plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    res = r["candidates"][0]["resources"][0]
    assert res["business_oracles"] == ["doc:status"]
    assert res["oracles"] == ["doc:status"]


def test_repo_repo_field_with_business_oracle_survives():
    # 非表重叠（字段）不算 CRUD 噪音；携带业务判据 → 业务有意义 → 保留。
    net = _net({"R#a": _node(_taint(sinks=["e.X.setStatus"]), node_type="REPO_OP"),
                "R#b": _node(_taint(sinks=["e.X.setStatus"]), node_type="REPO_OP")})
    oracles = [{"id": "doc:status", "level": "L0_business_rule", "target": {"field": "status"}}]
    r = fc.run(net, _units("R#a", "R#b"), oracles=oracles,
               plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    assert r["counts"]["dropped_crud_noise"] == 0
    assert r["counts"]["pairs_candidate"] == 1


def test_repo_repo_field_no_business_dropped():
    net = _net({"R#a": _node(_taint(sinks=["e.X.setFlag"]), node_type="REPO_OP"),
                "R#b": _node(_taint(sinks=["e.X.setFlag"]), node_type="REPO_OP")})
    r = fc.run(net, _units("R#a", "R#b"), plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    assert r["counts"]["dropped_crud_noise"] == 0
    assert r["counts"]["dropped_no_business"] == 1
    assert r["counts"]["pairs_candidate"] == 0


def test_variety_deficit_quantified():
    # finding 粒度（class_method）与候选资源粒度（field）对不上 → 两侧缺口显式量化。
    net = _net({"S#a": _node(_taint(sinks=["e.X.setFlag"]), node_type="SVC_OP", simple="S", method="a"),
                "S#b": _node(_taint(sinks=["e.X.setFlag"]), node_type="SVC_OP", simple="S", method="b")})
    findings = [_finding("F1", "S.a", "某个无关的业务 claim")]
    r = fc.run(net, _units("S#a", "S#b"), findings, plumbing_types={"REPO_OP"})
    d = r["variety_deficit"]
    assert d["candidate_resources"] == 1
    assert d["resources_with_finding_coverage"] == 0
    assert d["resources_without_finding_coverage"] == 1
    assert d["findings_covering_any_candidate_resource"] == 0
    assert d["findings_covering_no_candidate_resource"] == 1
    assert d["finding_symbol_granularity"]["class_method"] == 1


def test_variety_deficit_covered_when_finding_mentions_resource():
    net = _net({"S#a": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP", simple="S", method="a"),
                "S#b": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP", simple="S", method="b")})
    findings = [_finding("F1", "S.a", "status 越界", rck="status.invalid"),
                _finding("F2", "S.b", "status 缺失", rck="status.invalid")]
    r = fc.run(net, _units("S#a", "S#b"), findings, plumbing_types={"REPO_OP"})
    d = r["variety_deficit"]
    assert d["resources_with_finding_coverage"] == 1
    assert d["findings_covering_any_candidate_resource"] == 2
    assert r["candidates"][0]["delta0"] == "consistent"


# ---------------------------------------------------------------- V3 缺陷 1：δ⁰ 语义（取值相等，非「双方都有」）

def _status_pair_nodes():
    return {"A#m": _node(_taint(sinks=["e.X.setStatus"]), simple="A", method="m"),
            "B#m": _node(_taint(sinks=["e.X.setStatus"]), simple="B", method="m")}


def test_delta0_different_root_causes_is_unknown():
    # 双方都有 verified finding 且都提及 status，但判定指纹不同 → 截面取值不等 → unknown，绝不 consistent。
    findings = [_finding("F1", "A.m", "status 越界", rck="status.enum"),
                _finding("F2", "B.m", "status 缺失", rck="status.null")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    c = r["candidates"][0]
    assert c["resources"][0]["findings"] == {"A#m": ["F1"], "B#m": ["F2"]}
    assert c["delta0"] == "unknown"


def test_delta0_empty_root_cause_is_unknown():
    # 双方都有 finding 但无判定指纹（空 root_cause_key）→ 拿不到可比较的值 → unknown。
    findings = [_finding("F1", "A.m", "status 越界"), _finding("F2", "B.m", "status 缺失")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["delta0"] == "unknown"


def test_delta0_same_root_cause_is_consistent():
    findings = [_finding("F1", "A.m", "status 越界", rck="status.invalid"),
                _finding("F2", "B.m", "status 另一种表述", rck="status.invalid")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["delta0"] == "consistent"


def test_cycle_label_uses_section_value_not_presence():
    # 环一致性的成对标签同样按取值相等：不同指纹 → unknown，不能因双方都有 finding 判 consistent。
    findings = [_finding("F1", "A.m", "status x", rck="status.enum"),
                _finding("F2", "B.m", "status y", rck="status.null")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    assert r["counts"]["cycle_status"]["unknown"] == 1
    assert r["counts"]["cycle_status"]["consistent"] == 0


# ---------------------------------------------------------------- V3 缺陷 2：token 大小写归一化（不退化为子串）

def test_token_case_insensitive_match():
    # status 只以大写出现（rck 不含 status token），隔离大小写变量。
    findings = [_finding("F1", "A.m", "STATUS 越界", rck="enum.violation"),
                _finding("F2", "B.m", "Status 缺失", rck="enum.violation")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    res = r["candidates"][0]["resources"][0]
    assert res["findings"] == {"A#m": ["F1"], "B#m": ["F2"]}
    assert r["candidates"][0]["delta0"] == "consistent"


def test_token_uppercase_id_matches():
    # ID 里的大写 STATUS token 也应归一化匹配（真实数据 FILE-VALIDATION-STATUS-SPLIT 的情形）。
    findings = [_finding("FILE-VALIDATION-STATUS-SPLIT", "A.m", "状态码分裂", rck="validation.status_code_split")]
    r = fc.run(_net(_status_pair_nodes()), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["resources"][0]["findings"]["A#m"] == ["FILE-VALIDATION-STATUS-SPLIT"]


def test_token_case_insensitive_substring_still_no_match():
    # 大小写归一化后仍是精确 token：FILENAME/NAMESPACE 不得命中字段 name。
    nodes = {"A#m": _node(_taint(sinks=["e.X.setName"]), simple="A", method="m"),
             "B#m": _node(_taint(sinks=["e.X.setName"]), simple="B", method="m")}
    findings = [_finding("F1", "A.m", "FILENAME 与 NAMESPACE 处理", rck="filename.issue"),
                _finding("F2", "B.m", "RENAME 逻辑", rck="rename.logic")]
    r = fc.run(_net(nodes), _units("A#m", "B#m"), findings)
    assert r["candidates"][0]["resources"][0]["findings"] == {"A#m": [], "B#m": []}


# ---------------------------------------------------------------- V7 红线：unknown may-write 不得洗成确定写

def test_unknown_may_write_not_definite_violation():
    # V7 实证场景：1 条 access=unknown 的 WRITES 边 × 一个确定读者 → 候选必须 may_write_unknown。
    net = _net({"R#find": _node(node_type="SVC_OP"), "S#apply": _node(node_type="SVC_OP")},
               [{"from": "R#find", "to": "table:t", "type": "READS", "access": "read"},
                {"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(net, _units("R#find", "S#apply"))
    assert r["counts"]["pairs_candidate"] == 1
    c = r["candidates"][0]
    assert c["evidence_strength"] == "may_write_unknown"
    assert c["frame_violation"] == []
    assert c["overlap_written"] == []
    assert c["overlap_may_written"] == ["table:t"]
    res = c["resources"][0]
    assert res["b_writes"] is False and res["b_may_write"] is True
    assert res["write_strength"] == "may_write_unknown"
    assert r["counts"]["candidates_evidence_strength"] == {"definite": 0, "may_write_unknown": 1}
    assert r["counts"]["may_write_edges"] == 1


def test_unknown_may_write_access_marker_visible():
    net = _net({"R#find": _node(), "S#apply": _node()},
               [{"from": "R#find", "to": "table:t", "type": "READS", "access": "read"},
                {"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(net, _units("R#find", "S#apply"))
    prov = r["candidates"][0]["resources"][0]["provenance"]["S#apply"]
    assert prov[0]["access"] == "unknown"


def test_unknown_may_write_vs_definite_write_still_unknown():
    # 一方确定写、另一方仅 may-write 触碰：重叠本身不确定 → 不得标确定违反（FC-0012 情形）。
    net = _net({"R#save": _node(node_type="SVC_OP"), "S#apply": _node(node_type="SVC_OP")},
               [{"from": "R#save", "to": "table:t", "type": "WRITES", "access": "write"},
                {"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(net, _units("R#save", "S#apply"))
    c = r["candidates"][0]
    assert c["evidence_strength"] == "may_write_unknown"
    assert c["frame_violation"] == []
    assert c["resources"][0]["a_writes"] is True  # save 确定写
    assert c["resources"][0]["write_strength"] == "may_write_unknown"  # 但重叠不确定


def test_definite_write_access_write_is_definite():
    net = _net({"R#save": _node(node_type="SVC_OP"), "R#find": _node(node_type="SVC_OP")},
               [{"from": "R#save", "to": "table:t", "type": "WRITES", "access": "write"},
                {"from": "R#find", "to": "table:t", "type": "READS", "access": "read"}])
    r = fc.run(net, _units("R#save", "R#find"))
    c = r["candidates"][0]
    assert c["evidence_strength"] == "definite"
    assert c["frame_violation"] == ["mod(B) ∩ fp(A) ≠ ∅"]  # 排序后 R#find=A(读), R#save=B(写)
    assert c["overlap_written"] == ["table:t"]


def test_legacy_writes_without_access_is_definite():
    # 向后兼容：旧 WRITES 边无 access 字段 → 确定写。
    net = _net({"A#m": _node(), "B#m": _node()},
               [{"from": "A#m", "to": "table:t", "type": "WRITES"},
                {"from": "B#m", "to": "table:t", "type": "READS"}])
    r = fc.run(net, _units("A#m", "B#m"))
    assert r["candidates"][0]["evidence_strength"] == "definite"


def test_may_write_alone_no_candidate():
    # 只有 may-write、无人触碰该资源 → 无重叠 → 无候选。
    net = _net({"S#apply": _node(), "R#other": _node(_taint(sinks=["e.Y.setZ"]))},
               [{"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(net, _units("S#apply", "R#other"))
    assert r["counts"]["pairs_candidate"] == 0


def test_injected_unknown_edge_adds_no_definite_violation():
    # V7 全量场景：基线全确定；注入 1 条 unknown may-write 边后，确定违反数不得增加。
    base_net = _net({"R#save": _node(node_type="SVC_OP"), "R#find": _node(node_type="SVC_OP"),
                     "S#apply": _node(node_type="SVC_OP")},
                    [{"from": "R#save", "to": "table:t", "type": "WRITES", "access": "write"},
                     {"from": "R#find", "to": "table:t", "type": "READS", "access": "read"}])
    units = _units("R#save", "R#find", "S#apply")
    base = fc.run(base_net, units)
    inj = _net(base_net["nodes"], base_net["edges"] + [
        {"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(inj, units)
    assert base["counts"]["candidates_evidence_strength"]["definite"] == 1
    assert r["counts"]["candidates_evidence_strength"]["definite"] == 1  # 确定违反不增加
    assert r["counts"]["candidates_evidence_strength"]["may_write_unknown"] == 2  # save×apply, find×apply
    new_def = [c for c in r["candidates"] if c["evidence_strength"] == "definite"]
    assert all("S#apply" not in c["cells"] for c in new_def)


# ---------------------------------------------------------------- 多值截面：约束集障碍路径

def _oracle(oid, target, assertion, level="L1_executable_constraint"):
    return {"id": oid, "level": level, "target": target, "assert": assertion}


def _quantity_pair_net():
    return _net({"A#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP"),
                 "B#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP")})


def test_constraint_disjoint_is_inconsistent():
    # 约束集 [5,∞) 与 (-∞,3] 不相交 → 非零障碍 → inconsistent（firing 证明）。
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 5}),
               _oracle("o2", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": "<=", "value": 3})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 1
    assert r["counts"]["cycle_status"]["inconsistent"] == 1
    ob = r["cycle_obstructions"][0]
    assert ob["basis"] == "constraint_set"
    assert ob["conflict"] is not None


def test_constraint_compatible_is_consistent():
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1}),
               _oracle("o2", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">", "value": 0})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_status"]["consistent"] == 1
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["cycles"][0]["basis"] == "constraint_set"


def test_constraint_missing_is_unknown_not_consistent():
    # 无约束判据 → 拿不到截面 → unknown，绝不默认 consistent。
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"))
    assert r["counts"]["cycle_status"]["unknown"] == 1
    assert r["counts"]["cycle_status"]["consistent"] == 0
    assert r["counts"]["cycle_obstructions"] == 0


def test_constraint_not_null_only_is_consistent():
    # not_null 是余单例域 V\{null}（可补）：双方都要求非空 → 交集非空 → consistent，覆盖上升。
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_status"]["consistent"] == 1
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["counts"]["constraint_section_cells"] == 2


def test_not_null_never_causes_inconsistent():
    # 无封闭原语强制 null → 余单例域单独永不致不相交（理论守卫）。
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"}),
               _oracle("o2", {"entity": "X", "field": "quantity"}, {"type": "not_blank", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["counts"]["cycle_status"]["consistent"] == 1


def test_not_null_intersect_interval_consistent():
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"}),
               _oracle("o2", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 0})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_status"]["consistent"] == 1
    assert r["counts"]["cycle_obstructions"] == 0


def test_not_blank_filters_empty_string():
    # notblank ∩ {"","A"} = {"A"}（非空）；与 {""} 交 → 空 → inconsistent。
    net = _net({"A#m": _node(_taint(sinks=["e.X.setName"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.X.setName"]), node_type="SVC_OP")})
    oracles = [_oracle("o1", {"entity": "X", "field": "name"}, {"type": "not_blank", "field": "name"}),
               _oracle("o2", {"entity": "X", "field": "name"}, {"type": "in", "field": "name", "value": [""]})]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 1  # notblank 排除 ""，与 {""} 不相交


def test_unique_oracle_is_unknown_not_constraint():
    # unique 是跨行约束，非单值域 → 桥不可映射 → 截面 None → unknown（理论局限，不猜）。
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "unique", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_status"]["unknown"] == 1
    assert r["counts"]["cycle_obstructions"] == 0


# ---------------------------------------------------------------- V10 缺陷1：反向红线（确定写不得被 may-write 降级）

def test_definite_write_not_downgraded_by_may_write():
    # 同一细胞同一资源既有确定写又有 may-write → 确定写优先，不得降级。
    net = _net({"R#save": _node(node_type="SVC_OP"), "R#find": _node(node_type="SVC_OP")},
               [{"from": "R#save", "to": "table:t", "type": "WRITES", "access": "write"},
                {"from": "R#save", "to": "table:t", "type": "WRITES", "access": "unknown"},
                {"from": "R#find", "to": "table:t", "type": "READS", "access": "read"}])
    fps, _ = fc.build_footprints(net, _units("R#save", "R#find"))
    fp = fps["R#save"]
    assert "table:t" in fp.writes          # 足迹保留
    assert "table:t" in fp.may_writes      # may-write 标记
    assert "table:t" in fp.definite_writes  # 确定写优先，不被降级
    r = fc.run(net, _units("R#save", "R#find"))
    c = r["candidates"][0]
    assert c["evidence_strength"] == "definite"
    assert c["frame_violation"] != []


def test_definite_count_stable_under_may_write_injection():
    # 注入 may-write 到已有确定写的细胞，确定违反数不得下降（反向红线端到端）。
    base_net = _net({"R#save": _node(node_type="SVC_OP"), "R#find": _node(node_type="SVC_OP")},
                    [{"from": "R#save", "to": "table:t", "type": "WRITES", "access": "write"},
                     {"from": "R#find", "to": "table:t", "type": "READS", "access": "read"}])
    units = _units("R#save", "R#find")
    base = fc.run(base_net, units)
    inj = _net(base_net["nodes"], base_net["edges"] + [
        {"from": "R#save", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    r = fc.run(inj, units)
    assert r["counts"]["candidates_evidence_strength"]["definite"] == base["counts"]["candidates_evidence_strength"]["definite"]


# ---------------------------------------------------------------- V10 缺陷2：跨实体守卫按资源实体（非细胞足迹）

def test_ddl_table_oracle_not_attach_to_other_entity_field():
    # products 表的 DDL 判据不得挂到 Category.name 字段资源（跨实体泄漏）。
    ddl = _oracle("ddl:products.name.x", {"table": "products", "field": "name"},
                  {"type": "in", "field": "name", "value": ["X"]})
    assert fc._matching_oracles([ddl], ("field", "Category", "name")) == []


def test_cross_entity_disjoint_ddl_no_false_obstruction():
    # 两细胞共享 Category.name；注入 products 表的不相交 DDL 约束 → 不得造假障碍。
    net = _net({"A#m": _node(_taint(sinks=["e.Category.setName"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.Category.setName"]), node_type="SVC_OP")})
    oracles = [_oracle("d1", {"table": "products", "field": "name"}, {"type": "in", "field": "name", "value": ["X"]}),
               _oracle("d2", {"table": "products", "field": "name"}, {"type": "in", "field": "name", "value": ["Y"]})]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["counts"]["cycle_status"]["unknown"] == 1


def test_schema_oracle_resource_level_guard():
    # schema=CreateOrderRequest 的判据不得挂到 BanquetOrder.status 资源（资源实体不符）。
    net = _net({"A#m": _node(_taint(sinks=["e.BanquetOrder.setStatus"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.BanquetOrder.setStatus"]), node_type="SVC_OP")})
    oracles = [_oracle("api", {"schema": "CreateOrderRequest", "field": "status"},
                       {"type": "in", "field": "status", "value": ["PENDING"]})]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["constraint_section_cells"] == 0  # 资源级守卫排除异实体 schema
    assert r["counts"]["cycle_obstructions"] == 0


def test_resource_guard_not_cell_footprint():
    # 隔离「资源级 vs 细胞足迹」：细胞 A 足迹含 CreateOrderRequest（读 DTO），但共享资源是
    # BanquetOrder.status。schema 判据按资源级须排除；按细胞足迹会误挂到 A（假不对称）。
    net = _net({"A#m": _node(_taint(source_fields=["d.CreateOrderRequest.status"], sinks=["e.BanquetOrder.setStatus"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.BanquetOrder.setStatus"]), node_type="SVC_OP")})
    oracles = [_oracle("api", {"schema": "CreateOrderRequest", "field": "status"},
                       {"type": "in", "field": "status", "value": ["PENDING"]})]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["constraint_section_cells"] == 0  # 资源级：CreateOrderRequest≠BanquetOrder → 双方都排除


# ---------------------------------------------------------------- V10 缺陷3：null 漏洞（坑45）+ 混合类型安全

def test_nonnull_intersect_null_set_is_empty():
    assert fc._is_empty(fc._intersect(("nonnull",), ("set", frozenset({None}))))


def test_notblank_intersect_null_set_is_empty():
    assert fc._is_empty(fc._intersect(("notblank",), ("set", frozenset({None}))))


def test_notblank_filters_null_and_empty():
    res = fc._intersect(("notblank",), ("set", frozenset({None, "", "A"})))
    assert res == ("set", frozenset({"A"}))


def test_nonnull_preserves_non_null_values():
    res = fc._intersect(("nonnull",), ("set", frozenset({None, 1, 2})))
    assert res == ("set", frozenset({1, 2}))


def test_mixed_type_interval_set_no_crash():
    # 字符串区间 × 数值有限域：不可比 → None（unknown），不抛 TypeError。
    assert fc._intersect(("iv", "a", True, "z", True), ("set", frozenset({1, 2}))) is None
    assert fc._intersect(("iv", 1, True, 5, True), ("iv", "a", True, "z", True)) is None


def test_constraint_polarity_strict_gt_disjoint():
    # >0 → (0,∞)；<=0 → (-∞,0]；交为空（0 被严格排除）→ inconsistent。锁桥的极性不放宽。
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">", "value": 0}),
               _oracle("o2", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": "<=", "value": 0})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 1


def test_constraint_finite_set_disjoint():
    oracles = [_oracle("o1", {"entity": "X", "field": "status"}, {"type": "in", "field": "status", "value": ["A", "B"]}),
               _oracle("o2", {"entity": "X", "field": "status"}, {"type": "in", "field": "status", "value": ["C", "D"]})]
    net = _net({"A#m": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP")})
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 1


def test_table_level_no_false_obstruction():
    # 表级无字段粒度：不同列约束（status 枚举 ∩ amount 区间）不得聚合成假障碍 → unknown。
    net = _net({"A#m": _node(node_type="SVC_OP"), "B#m": _node(node_type="SVC_OP")},
               [{"from": "A#m", "to": "table:t", "type": "WRITES", "access": "write"},
                {"from": "B#m", "to": "table:t", "type": "READS", "access": "read"}])
    oracles = [_oracle("o1", {"table": "t", "field": "status"}, {"type": "in", "field": "status", "value": ["A", "B"]}),
               _oracle("o2", {"table": "t", "field": "amount"}, {"type": "field_cmp", "field": "amount", "op": ">=", "value": 5})]
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["cycle_obstructions"] == 0
    assert r["counts"]["cycle_status"]["unknown"] == 1


def test_constraint_section_coverage_reported():
    oracles = [_oracle("o1", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["constraint_section_cells"] == 2
    assert r["counts"]["constraint_section_buckets"] == 1


def test_intersect_helly_pairwise_nonempty_triple_empty():
    # 理论守卫：有限域成对交非空但三元交空（Helly 失败）——区间无此性质，有限域有。
    a, b, c = ("set", frozenset({1, 2})), ("set", frozenset({2, 3})), ("set", frozenset({1, 3}))
    assert not fc._is_empty(fc._intersect(a, b))
    assert not fc._is_empty(fc._intersect(b, c))
    assert not fc._is_empty(fc._intersect(a, c))
    assert fc._is_empty(fc._intersect(fc._intersect(a, b), c))


def test_may_write_redline_not_regressed_by_constraints():
    # 多值截面接入后，may-write 三态红线不得回归。
    net = _net({"R#find": _node(node_type="SVC_OP"), "S#apply": _node(node_type="SVC_OP")},
               [{"from": "R#find", "to": "table:t", "type": "READS", "access": "read"},
                {"from": "S#apply", "to": "table:t", "type": "WRITES", "access": "unknown"}])
    oracles = [_oracle("o1", {"table": "t", "field": "q"}, {"type": "field_cmp", "field": "q", "op": ">=", "value": 5})]
    r = fc.run(net, _units("R#find", "S#apply"), oracles=oracles)
    c = r["candidates"][0]
    assert c["evidence_strength"] == "may_write_unknown"
    assert c["frame_violation"] == []


# ---------------------------------------------------------------- 坑25：CLI 默认空集使证据通道静默报 0

def _status_oracle_net():
    net = _net({"A#m": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.X.setStatus"]), node_type="SVC_OP")})
    oracles = [_oracle("doc:status", {"field": "status"},
                       {"type": "in", "field": "status", "value": ["PENDING", "COMPLETED"]},
                       level="L0_business_rule")]
    return net, oracles


def test_business_levels_empty_reports_incomplete_not_zero():
    net, oracles = _status_oracle_net()
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles)  # 不传 business_levels
    assert r["variety_deficit"]["business_oracle_channel"] == "INCOMPLETE"
    assert any("business_levels" in w for w in r["input_warnings"])


def test_business_levels_supplied_channel_ok():
    net, oracles = _status_oracle_net()
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles, business_levels={"L0_business_rule"})
    assert r["variety_deficit"]["business_oracle_channel"] == "ok"
    assert r["variety_deficit"]["resources_with_business_oracle"] == 1
    assert not any("business_levels" in w for w in r["input_warnings"])


def test_plumbing_types_empty_warns():
    net, oracles = _status_oracle_net()
    r = fc.run(net, _units("A#m", "B#m"), oracles=oracles, business_levels={"L0_business_rule"})
    assert any("plumbing_types" in w for w in r["input_warnings"])


def test_cli_default_business_levels_not_incomplete(tmp_path, capsys, monkeypatch):
    # 端到端：不传 --business-levels 时 CLI 默认含 L0，业务通道不得报 INCOMPLETE/0。
    net, oracles = _status_oracle_net()
    units = _units("A#m", "B#m")
    np, up, op = tmp_path / "net.json", tmp_path / "units.json", tmp_path / "oracles.json"
    np.write_text(json.dumps(net), encoding="utf-8")
    up.write_text(json.dumps(units), encoding="utf-8")
    op.write_text(json.dumps({"oracles": oracles}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["frame_check.py", "--net", str(np), "--units", str(up),
                                      "--oracles", str(op), "--plumbing-types", "REPO_OP"])
    fc.main()
    out = capsys.readouterr().out
    assert "business-oracle INCOMPLETE" not in out
    assert "business_levels 未供给" not in out
    assert "business-oracle 1" in out


def test_cli_explicit_empty_business_levels_incomplete(tmp_path, capsys, monkeypatch):
    net, oracles = _status_oracle_net()
    units = _units("A#m", "B#m")
    np, up, op = tmp_path / "net.json", tmp_path / "units.json", tmp_path / "oracles.json"
    np.write_text(json.dumps(net), encoding="utf-8")
    up.write_text(json.dumps(units), encoding="utf-8")
    op.write_text(json.dumps({"oracles": oracles}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["frame_check.py", "--net", str(np), "--units", str(up),
                                      "--oracles", str(op), "--plumbing-types", "REPO_OP",
                                      "--business-levels", ""])
    fc.main()
    out = capsys.readouterr().out
    assert "business-oracle INCOMPLETE" in out
    assert "business_levels 未供给" in out


# ---------------------------------------------------------------- V10 最终：跨实体守卫取 table（@Table 映射）+ 裸字段 DDL 拦截

def _entity_meta():
    return {"Category": {"table": "categories", "columns": {"isDeleted": "is_deleted"}},
            "PackageType": {"table": "package_types", "columns": {"isDeleted": "is_deleted"}},
            "Product": {"table": "products", "columns": {"isDeleted": "is_deleted"}},
            "BanquetPackage": {"table": "packages", "columns": {"isDeleted": "is_deleted"}}}


def _isdeleted_pair_net(entity):
    return _net({"A#m": _node(_taint(sinks=[f"e.{entity}.setIsDeleted"]), node_type="SVC_OP"),
                 "B#m": _node(_taint(sinks=[f"e.{entity}.setIsDeleted"]), node_type="SVC_OP")})


def test_v10_table_ddl_injection_no_false_obstruction():
    # V10 阳性对照：products.is_deleted∈{true} + packages.is_deleted∈{false} 不得在 Category/PackageType 造假障碍。
    oracles = [_oracle("injP", {"table": "products", "field": "is_deleted"}, {"type": "in", "field": "is_deleted", "value": [True]}, level="L1_executable_constraint"),
               _oracle("injK", {"table": "packages", "field": "is_deleted"}, {"type": "in", "field": "is_deleted", "value": [False]}, level="L1_executable_constraint")]
    for ent in ("Category", "PackageType"):
        r = fc.run(_isdeleted_pair_net(ent), _units("A#m", "B#m"), oracles=oracles, entity_meta=_entity_meta())
        assert r["counts"]["cycle_obstructions"] == 0, ent


def test_v10_bare_field_ddl_injection_no_false_obstruction():
    # 裸字段格式但 source.kind=ddl 的表级约束：不得借裸字段 join 挂到无关实体。
    oracles = [{"id": "inj1", "source": {"kind": "ddl"}, "target": {"field": "is_deleted"}, "assert": {"type": "in", "field": "is_deleted", "value": [True]}},
               {"id": "inj2", "source": {"kind": "ddl"}, "target": {"field": "is_deleted"}, "assert": {"type": "in", "field": "is_deleted", "value": [False]}}]
    r = fc.run(_isdeleted_pair_net("Category"), _units("A#m", "B#m"), oracles=oracles, entity_meta=_entity_meta())
    assert r["counts"]["cycle_obstructions"] == 0


def test_ddl_maps_to_correct_entity_via_table():
    # products.is_deleted 经 @Table 挂到 Product.isDeleted，不挂 Category.isDeleted。
    oracles = [_oracle("injP", {"table": "products", "field": "is_deleted"}, {"type": "in", "field": "is_deleted", "value": [True]}, level="L1_executable_constraint")]
    meta = _entity_meta()
    assert any(x["id"] == "injP" for x in fc._matching_oracles(oracles, ("field", "Product", "isDeleted"), meta))
    assert not any(x["id"] == "injP" for x in fc._matching_oracles(oracles, ("field", "Category", "isDeleted"), meta))


def test_ddl_correct_entity_can_obstruct():
    # 阳性对照：同一实体（Product）的两条不相交 products DDL → 真障碍（机制仍能 firing）。
    oracles = [_oracle("p1", {"table": "products", "field": "is_deleted"}, {"type": "in", "field": "is_deleted", "value": [True]}, level="L1_executable_constraint"),
               _oracle("p2", {"table": "products", "field": "is_deleted"}, {"type": "in", "field": "is_deleted", "value": [False]}, level="L1_executable_constraint")]
    r = fc.run(_isdeleted_pair_net("Product"), _units("A#m", "B#m"), oracles=oracles, entity_meta=_entity_meta())
    assert r["counts"]["cycle_obstructions"] == 1


def test_bare_field_document_still_global():
    # 非 DDL 源（document）的裸字段判据仍是全局规则，可挂（不被新限制误伤）。
    oracles = [{"id": "doc", "source": {"kind": "document"}, "target": {"field": "is_deleted"}, "assert": {"type": "in", "field": "is_deleted", "value": [True, False]}}]
    r = fc.run(_isdeleted_pair_net("Category"), _units("A#m", "B#m"), oracles=oracles, entity_meta=_entity_meta())
    assert r["counts"]["constraint_section_cells"] == 2
    assert r["counts"]["cycle_status"]["consistent"] == 1


def test_coverage_two_calibers():
    # 两口径：assoc（细胞×资源关联）vs unique（去重细胞）。同一细胞跨多资源时 assoc>unique。
    oracles = [_oracle("d1", {"table": "categories", "field": "is_deleted"}, {"type": "not_null", "field": "is_deleted"}, level="L1_executable_constraint")]
    net = _net({"A#m": _node(_taint(sinks=["e.Category.setIsDeleted", "e.Category.setName"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.Category.setIsDeleted", "e.Category.setName"]), node_type="SVC_OP")})
    meta = {"Category": {"table": "categories", "columns": {"isDeleted": "is_deleted", "name": "name"}}}
    o2 = oracles + [_oracle("d2", {"table": "categories", "field": "name"}, {"type": "not_null", "field": "name"}, level="L1_executable_constraint")]
    r = fc.run(net, _units("A#m", "B#m"), oracles=o2, entity_meta=meta)
    assert r["counts"]["constraint_section_cells"] == 4   # 2 细胞 × 2 资源
    assert r["counts"]["constraint_section_unique_cells"] == 2  # 去重 2 细胞


# ---------------------------------------------------------------- V13 坑64：检测力诚实性（覆盖≠检测力）

def test_firing_capable_iv_and_set():
    # 区间(iv)与有限域(set)可不相交 → firing_capable。
    net = _net({"A#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP"),
                "C#m": _node(_taint(sinks=["e.Y.setStatus"]), node_type="SVC_OP"),
                "D#m": _node(_taint(sinks=["e.Y.setStatus"]), node_type="SVC_OP")})
    oracles = [_oracle("q", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1}),
               _oracle("s", {"entity": "Y", "field": "status"}, {"type": "in", "field": "status", "value": ["A", "B"]})]
    r = fc.run(net, _units("A#m", "B#m", "C#m", "D#m"), oracles=oracles)
    assert r["counts"]["buckets_firing_capable"] == 2
    assert r["counts"]["buckets_co_singleton_only"] == 0


def test_co_singleton_only_not_firing_capable():
    # 余单例域(nonnull)数学上永不 inconsistent → co_singleton_only，不得算进 firing_capable。
    oracles = [_oracle("n", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["counts"]["buckets_co_singleton_only"] == 1
    assert r["counts"]["buckets_firing_capable"] == 0  # 变异把余单例域算进 firing → 此断言红
    assert r["counts"]["cycle_status"]["consistent"] == 1  # 仍 consistent（可满足），但零检测力


def test_coverage_and_detection_power_both_reported():
    # 覆盖 8 桶 ≠ 检测力 2 桶：两数字必须都在产物里（不得只报覆盖）。
    oracles = [_oracle("n", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert "buckets_firing_capable" in r["counts"]
    assert "buckets_co_singleton_only" in r["counts"]
    assert r["counts"]["constraint_section_buckets"] == 1
    assert r["counts"]["buckets_firing_capable"] == 0


def test_ddl_table_only_no_field_counter():
    # target={table} 无 field 的 DDL CHECK 判据：显式计数（静默不可挂载通道），不静默。
    oracles = [_oracle("chk", {"table": "t"}, {"type": "in", "field": "x", "value": [1]}, level="L1_executable_constraint")]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    assert r["coverage_gaps"]["ddl_table_only_no_field_oracles"] == 1


def test_detection_power_conservation():
    # 守恒（坑3 同族）：约束桶总数 == firing + co_singleton + other，不得静默丢桶。
    net = _net({"A#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP"),
                "B#m": _node(_taint(sinks=["e.X.setQuantity"]), node_type="SVC_OP"),
                "C#m": _node(_taint(sinks=["e.Y.setStatus"]), node_type="SVC_OP"),
                "D#m": _node(_taint(sinks=["e.Y.setStatus"]), node_type="SVC_OP"),
                "E#m": _node(_taint(sinks=["e.Z.setFlag"]), node_type="SVC_OP"),
                "F#m": _node(_taint(sinks=["e.Z.setFlag"]), node_type="SVC_OP")})
    oracles = [_oracle("q", {"entity": "X", "field": "quantity"}, {"type": "field_cmp", "field": "quantity", "op": ">=", "value": 1}),
               _oracle("s", {"entity": "Y", "field": "status"}, {"type": "in", "field": "status", "value": ["A"]}),
               _oracle("n", {"entity": "Z", "field": "flag"}, {"type": "not_null", "field": "flag"})]
    r = fc.run(net, _units("A#m", "B#m", "C#m", "D#m", "E#m", "F#m"), oracles=oracles)
    c = r["counts"]
    assert c["buckets_firing_capable"] + c["buckets_co_singleton_only"] + c["buckets_other_domain"] == c["constraint_section_buckets"]
    assert c["buckets_firing_capable"] == 2   # quantity iv + status set
    assert c["buckets_co_singleton_only"] == 1  # flag nonnull
    assert c["buckets_other_domain"] == 0


def test_domain_type_is_valid_kind_not_cell_char():
    # 回归 have[0][0] 旧 bug：domain_type 必须是域类型(iv/set/nonnull/notblank)，不是细胞名首字符 'c'。
    oracles = [_oracle("n", {"entity": "X", "field": "quantity"}, {"type": "not_null", "field": "quantity"})]
    r = fc.run(_quantity_pair_net(), _units("A#m", "B#m"), oracles=oracles)
    for cyc in r["cycles"]:
        if cyc["basis"] == "constraint_set":
            assert cyc["domain_type"] in {"iv", "set", "nonnull", "notblank"}, cyc["domain_type"]
    # 旧 bug 下 domain_type='c' → co_singleton_only=0；正确分类必须计入
    assert r["counts"]["buckets_co_singleton_only"] == 1


# ---------------------------------------------------------------- V13 坑61：M9 不对称截面防御守卫（可达性前提扩张时仍有牙）

def test_m9_partial_sections_is_unknown(monkeypatch):
    # 造 0<have<len(cells)（A 有截面、B 无）：必须 unknown/binary，不得洗成 consistent。
    # M9 变异（elif len(have)==len(cells) → >=1）会把此例判 consistent → 红。
    net = _quantity_pair_net()
    fps, _ = fc.build_footprints(net, _units("A#m", "B#m"))
    idx = fc.build_index(fps)

    def fake_section(fp, resource, oracles, entity_meta=None):
        return ("iv", 0, True, None, True) if fp.cell == "A#m" else None

    monkeypatch.setattr(fc, "_cell_section", fake_section)
    reports = fc.cycle_consistency(fps, idx, {}, oracles=[])
    bucket = next(r for r in reports if r["resource"] == "e.X.setQuantity")
    assert bucket["sections_present"] == 1
    assert bucket["status"] == "unknown"
    assert bucket["basis"] == "binary"


def test_cell_section_is_cell_independent_structural():
    # 可达性前提机械化：_cell_section 当前不读 fp（截面只由资源决定）→ M9 结构上不可达。
    # 一旦开始读 fp（DTO 投影），此守卫红，提醒不对称守卫必须生效。
    import inspect
    src = inspect.getsource(fc._cell_section)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    import re
    assert not re.search(r"\bfp\b", body), "_cell_section 开始读 fp：截面变细胞相关，M9 升级为真洞，须核不对称守卫"