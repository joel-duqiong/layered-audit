"""relation_net 表访问三态的回归守卫。

背景：子串匹配曾把 findByIdAndIsDeletedFalse 这类派生 SELECT 里的
Deleted 当成 delete 写动词，26 条 WRITES 里 11 条是只读误标。
全部合成数据，不碰真实台账、不跑流水线、不写数据库。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import frame_check as fc
import relation_net as rn

REPO = "com.x.repo.ThingRepository"
SVC = "com.x.service.ThingService"
TABLE = "things"


def _fn(cls, method, modifiers="public", calls=(), effects=(), query=None):
    rec = {"class": cls, "method": method, "modifiers": modifiers,
           "internal_calls": list(calls), "side_effects": list(effects),
           "external_calls": [], "params": [], "branch_count": 0,
           "is_pure": not effects}
    if query is not None:
        rec["query"] = query
    return rec


def _class_decl():
    return {"jpa": {"com.x.entity.Thing": {"table": TABLE, "columns": []}},
            "ddl": {"tables": {TABLE: {"columns": {"id": {}}}}}}


def _net(repo_method, declared=True, modifiers="public abstract", query=None,
         body_calls=(), svc=True):
    funcs = []
    if declared:
        funcs.append(_fn(REPO, repo_method, modifiers=modifiers,
                         calls=body_calls, query=query))
    if svc:
        callee = f"{REPO}.{repo_method}"
        funcs.append(_fn(SVC, "op", calls=[callee], effects=[callee]))
    return rn.build({"functions": funcs}, _class_decl(), {}, [])


def _table_edges(net):
    return [e for e in net["edges"] if e["to"] == f"table:{TABLE}"]


def _repo_node(net, method):
    return net["nodes"][f"{REPO}#{method}"]


# ---- 派生 SELECT 不得标 WRITES（11 条误标的全部形态）----

DERIVED_READS = [
    "findByIdAndIsDeletedFalse",
    "findAllByIsDeletedFalseOrderBySortOrderAscIdAsc",
    "existsByCategoryIdAndIsDeletedFalse",
    "findByNameAndIsDeletedFalse",
    "existsByBanquetTypeAndIsDeletedFalse",
    "existsNonDeletedPackageReferenceByProductId",
    "findByName",
    "findMaxSortOrder",
    "existsByOrderNo",
    "countByStatus",
    "readByName",
    "getByName",
    "queryByName",
    "searchByName",
    "streamAll",
    "findAll",
    "findFirst10ByStatus",
]


def test_derived_select_is_read_not_write():
    for method in DERIVED_READS:
        net = _net(method)
        edges = _table_edges(net)
        assert edges, method
        for e in edges:
            assert e["type"] == "READS", (method, e)
            assert e["access"] == "read", (method, e)
        assert _repo_node(net, method)["access"] == "read", method


def test_deleted_property_path_is_not_delete_verb():
    net = _net("findByIdAndIsDeletedFalse")
    assert not [e for e in net["edges"] if e["type"] == "WRITES"]
    assert _repo_node(net, "findByIdAndIsDeletedFalse")["access_evidence"] == "derived-query"


def test_write_hint_substring_inside_name_is_not_evidence():
    for method in ("savedSnapshot", "deletionLog", "finder", "updateStatus"):
        net = _net(method)
        node = _repo_node(net, method)
        assert node["access"] == "unknown", (method, node)
        edges = _table_edges(net)
        assert edges and all(e["access"] == "unknown" for e in edges), method


# ---- 真写方法仍标 WRITES ----

def test_crud_contract_writes():
    for method in ("save", "saveAll", "deleteById", "deleteAll", "persist", "merge"):
        net = _net(method, declared=False)
        edges = _table_edges(net)
        assert edges, method
        for e in edges:
            assert e["type"] == "WRITES", (method, e)
            assert e["access"] == "write", (method, e)
            assert e["evidence"] == "crud-contract", (method, e)


def test_derived_delete_is_write():
    for method in ("deleteByExpiredTrue", "removeByStatus"):
        net = _net(method)
        edges = _table_edges(net)
        assert edges and all(e["type"] == "WRITES" and e["access"] == "write"
                             for e in edges), method


def test_body_write_sink_is_write():
    net = _net("archive", modifiers="public",
               body_calls=[f"{REPO}.save"])
    node = _repo_node(net, "archive")
    assert node["access"] == "write"
    assert node["access_evidence"] == "body-write-sink"
    assert all(e["type"] == "WRITES" for e in _table_edges(net))


def test_body_read_only_is_read():
    net = _net("lookup", modifiers="public",
               body_calls=[f"{REPO}.findByName"])
    node = _repo_node(net, "lookup")
    assert node["access"] == "read"
    assert node["access_evidence"] == "body-read-only"


# ---- 语句文本优先于名字 ----

def test_query_statement_overrides_name():
    net = _net("deleteOld", query="select coalesce(max(sortOrder), 0) from Thing")
    assert _repo_node(net, "deleteOld")["access"] == "read"
    assert all(e["type"] == "READS" for e in _table_edges(net))

    net = _net("findReport", query="update Thing set status = 1")
    assert _repo_node(net, "findReport")["access"] == "write"
    assert all(e["type"] == "WRITES" and e["access"] == "write"
               for e in _table_edges(net))

    net = _net("snapshot", query="insert into Thing (id) values (1)")
    assert _repo_node(net, "snapshot")["access"] == "write"


def test_unrecognized_statement_is_unknown():
    net = _net("mystery", query="with cte as (select 1) select * from cte")
    assert _repo_node(net, "mystery")["access"] == "unknown"


# ---- 证据不足 → unknown，且不得被下游当成不相交/合规 ----

def test_no_evidence_is_unknown_and_kept_as_may_write():
    net = _net("recalculateTotals")
    node = _repo_node(net, "recalculateTotals")
    assert node["access"] == "unknown"
    assert node["access_evidence"] == "no-evidence"
    edges = _table_edges(net)
    assert edges, "unknown 足迹不得被丢弃"
    for e in edges:
        assert e["type"] == "WRITES" and e["access"] == "unknown", e
        assert e["evidence"] == "no-evidence"


def test_unknown_footprint_not_disjoint_downstream():
    net = _net("recalculateTotals")
    del net["nodes"][f"{SVC}#op"]
    net["edges"] = [e for e in net["edges"] if e["from"] != f"{SVC}#op"]
    other = f"{SVC}#writer"
    net["nodes"][other] = {"id": other, "type": "SVC_OP", "cls": SVC,
                           "method": "writer", "taint": {"source_fields": [], "sinks": []}}
    net["edges"].append({"from": other, "to": f"table:{TABLE}", "type": "WRITES",
                         "access": "write"})
    fps, _ = fc.build_footprints(net, {"units": []})
    unknown_fp = fps[f"{REPO}#recalculateTotals"]
    assert f"table:{TABLE}" in unknown_fp.writes, "unknown 必须保留写足迹（may-write）"
    candidates, stats = fc.find_candidates(fps, {}, [])
    assert stats["pairs_disjoint_empty"] == 0
    assert any(f"{REPO}#recalculateTotals" in c["cells"] for c in candidates)


def test_read_read_overlap_is_not_candidate_downstream():
    net = _net("findByIdAndIsDeletedFalse")
    other = f"{REPO}#findByName"
    net["nodes"][other] = {"id": other, "type": "REPO_OP", "cls": REPO,
                           "method": "findByName", "access": "read",
                           "taint": {"source_fields": [], "sinks": []}}
    net["edges"].append({"from": other, "to": f"table:{TABLE}", "type": "READS",
                         "access": "read"})
    fps, _ = fc.build_footprints(net, {"units": []})
    candidates, stats = fc.find_candidates(fps, {}, [])
    assert candidates == []
    assert stats["pairs_disjoint_readonly"] >= 1


# ---- taint.persist 同样不许子串误判 ----

def test_taint_persist_evidence_based():
    net = _net("findByIdAndIsDeletedFalse")
    assert net["nodes"][f"{SVC}#op"]["taint"]["persist"] is False
    net = _net("save", declared=False)
    assert net["nodes"][f"{SVC}#op"]["taint"]["persist"] is True


# ---- 空输入与统计 ----

def test_empty_inputs_do_not_crash():
    net = rn.build({}, {}, {}, [])
    assert net["counts"]["nodes"] == 0
    assert net["counts"]["edges"] == 0
    assert net["counts"]["edges_by_access"] == {"write": 0, "read": 0, "unknown": 0}


def test_every_table_edge_carries_tri_state():
    net = _net("save", declared=False)
    for e in _table_edges(net):
        assert e["access"] in ("write", "read", "unknown")
        assert e["evidence"]
    assert net["counts"]["edges_by_access"]["write"] >= 1