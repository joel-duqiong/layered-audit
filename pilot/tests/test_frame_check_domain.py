"""frame_check 建模域归类的契约守卫（W19）。

锁的正确行为：
  · 三口径（域内已测 / 域内 unknown / 域外 not_applicable）分开报且**守恒**；
  · 四个 pair 桶守恒，域外对单列，既不丢弃也不算成可判；
  · **域内没证据仍是 unknown**，绝不因重分类被洗成 pass；
  · 未声明域 → 保守全按 unknown + 显式告警（不许假装知道域）；
  · 重分类**只改覆盖率口径，不改检测力**（候选/环障碍/可判对数一字不变）；
  · audit_entry：域外 > 0 仍 INCOMPLETE，即使 unknown 比率归零；干净图仍 GREEN（反向）。

全部合成夹具，不碰真实台账、不跑流水线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PILOT / "engine"))

import audit_entry as ae  # noqa: E402
import frame_check as fc  # noqa: E402

DOMAIN = {"cell_key_form": "FQCN#method",
          "node_types": ["HTTP_ENTRY", "SVC_OP", "REPO_OP", "TABLE", "ENTITY_FIELD"],
          "method_node_types": ["HTTP_ENTRY", "SVC_OP", "REPO_OP"],
          "class_suffixes": ["Controller", "Service", "Repository"],
          "scope_note": "域外是 not_applicable，不是 unknown"}

SVC = "com.x.AService#m"
CTRL = "com.x.AController#e"
MISSING = "com.x.AService#missing"


def _net(with_domain: bool = True) -> dict:
    net = {
        "nodes": {
            SVC: {"type": "SVC_OP", "cls": "com.x.AService", "method": "m", "simple": "AService",
                  "taint": {"source_fields": ["orders.amount"], "sinks": ["table:orders"]}},
            CTRL: {"type": "HTTP_ENTRY", "cls": "com.x.AController", "method": "e",
                   "simple": "AController"},
        },
        "edges": [{"from": CTRL, "to": SVC, "type": "CALLS"},
                  {"from": SVC, "to": "table:orders", "type": "WRITES", "access": "write"}],
    }
    if with_domain:
        net["modeling_domain"] = DOMAIN
    return net


def _units() -> dict:
    return {"units": [{"unit_key": k, "obligation_source_layer": lay} for k, lay in (
        (SVC, "function"), (CTRL, "function"), (MISSING, "function"),
        ("BareEntity", "class"), ("com.x.Dto#get", "function"), ("service:api", "service"))]}


def _run(with_domain: bool = True) -> dict:
    return fc.run(_net(with_domain), _units(), [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)


# ---------------------------------------------------------------- 三口径 / 守恒

def test_three_cell_buckets_are_separate_and_conserve_total():
    c = _run()["counts"]
    assert c["domain_declared"] is True
    assert c["cells_in_domain_measured"] == 1, "只有带 taint 的 SVC 算已测"
    assert c["cells_in_domain_unknown"] == 2, "CTRL(有节点无足迹证据) + MISSING(该建模却缺节点)"
    assert c["cells_out_of_domain"] == 3, "裸类名 / dto 方法 / service:api 都在域外"
    assert (c["cells_in_domain_measured"] + c["cells_in_domain_unknown"]
            + c["cells_out_of_domain"]) == c["cells_total"] == 6


def test_four_pair_buckets_conserve_total():
    c = _run()["counts"]
    assert c["pairs_total"] == 15
    assert c["pairs_decidable"] == 0
    assert c["pairs_unknown"] == 3, "域内 unknown 参与的对：1×2 + C(2,2)"
    assert c["pairs_not_applicable"] == 12, "含至少一个域外细胞的对：15 - C(3,2)"
    assert (c["pairs_decidable"] + c["pairs_unknown"]
            + c["pairs_disjoint_unknown_side"] + c["pairs_not_applicable"]) == c["pairs_total"]


def test_out_of_domain_is_not_applicable_not_unknown():
    """域外细胞不许进 unknown 分子 —— 那会虚增待查队列。"""
    d = _run()
    c = d["counts"]
    assert c["cells_footprint_unknown"] == 2, "unknown 只指域内未测"
    gaps = d["coverage_gaps"]
    assert gaps["out_of_domain_cells_count"] == 3
    assert set(gaps["out_of_domain_cells_sample"]) == {"BareEntity", "com.x.Dto#get", "service:api"}
    assert "BareEntity" not in gaps["unknown_cells_sample"]


def test_in_domain_missing_footprint_stays_unknown():
    """反向红线：该被建模却没证据的细胞仍是 unknown，绝不因重分类被洗成 not_applicable/pass。"""
    c = _run()["counts"]
    assert MISSING in _run()["coverage_gaps"]["unknown_cells_sample"]
    assert c["cells_in_domain_unknown"] == 2
    assert c["pairs_unknown"] > 0, "域内 unknown 必须继续产生不可判对"


def test_node_presence_beats_suffix_drift():
    """net 里真有节点的细胞一律域内，即使类名不匹配声明后缀（声明漂移不能把它推出去）。"""
    net = _net()
    net["nodes"]["com.x.Odd#z"] = {"type": "SVC_OP", "simple": "Odd",
                                   "taint": {"source_fields": ["t.f"], "sinks": []}}
    units = _units()
    units["units"].append({"unit_key": "com.x.Odd#z", "obligation_source_layer": "function"})
    d = fc.run(net, units, [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)
    assert d["counts"]["cells_in_domain_measured"] == 2, "有节点+有 taint 的 Odd 必须算域内已测"
    assert "com.x.Odd#z" not in d["coverage_gaps"]["out_of_domain_cells_sample"]


def test_undeclared_domain_falls_back_to_unknown_and_warns():
    """未声明域 → 不敢分类：全部非 known 都算 unknown，并显式告警。"""
    d = _run(with_domain=False)
    c = d["counts"]
    assert c["domain_declared"] is False
    assert c["cells_out_of_domain"] == 0
    assert c["cells_footprint_unknown"] == 5, "6 个细胞只有 1 个已测，其余 5 个保守全算 unknown"
    assert c["pairs_not_applicable"] == 0
    assert any("modeling_domain" in w for w in d["input_warnings"])


def test_reclassification_does_not_change_detection_power():
    """重分类只改覆盖率口径：候选、环障碍、可判对数必须一字不变。"""
    a, b = _run(with_domain=True), _run(with_domain=False)
    assert a["counts"]["pairs_decidable"] == b["counts"]["pairs_decidable"]
    assert a["counts"]["cycle_obstructions"] == b["counts"]["cycle_obstructions"]
    assert len(a["candidates"]) == len(b["candidates"])
    assert [c["id"] for c in a["candidates"]] == [c["id"] for c in b["candidates"]]
    assert a["counts"]["candidates_delta0"] == b["counts"]["candidates_delta0"]


# ---------------------------------------------------------------- audit_entry 消费侧

def _fake(monkeypatch, counts, **extra):
    data = {"counts": counts, "candidates": [], **extra}

    def _run_engine(script, argv, out_path):
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data), encoding="utf-8")
        return 0, "", data

    monkeypatch.setattr(ae, "run_engine", _run_engine)


def _disc():
    return {"java_roots": [Path("src")], "test_roots": [], "migrations": [], "docs": [],
            "openapi": None, "packages": set()}


BASE = {"pairs_decidable": 8385, "pairs_total": 148785, "pairs_unknown": 0,
        "cells_footprint_unknown": 0, "cycle_obstructions": 0,
        "candidates_delta0": {"consistent": 0, "unknown": 0},
        "cells_in_domain": 130, "cells_in_domain_measured": 130,
        "cells_in_domain_unknown": 0, "cells_out_of_domain": 416,
        "pairs_not_applicable": 140400, "domain_declared": True}


def test_out_of_domain_keeps_incomplete_even_when_unknown_ratio_zero(tmp_path, monkeypatch):
    """核心红线：unknown 比率归零**不得**被读成绿灯 —— 416 个域外细胞根本没查。"""
    _fake(monkeypatch, dict(BASE))
    net = tmp_path / "net.json"
    net.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
    assert p["state"] == ae.INCOMPLETE, "域外 416 被洗成 GREEN"
    assert "not_applicable" in p["reason"] and "416" in p["reason"]
    assert p["cells_out_of_domain"] == 416 and p["pairs_not_applicable"] == 140400
    assert p["cells_in_domain_measured"] == 130 and p["cells_in_domain_unknown"] == 0


def test_clean_fully_in_domain_is_green(tmp_path, monkeypatch):
    """反向：全部域内且全已测、零候选零 unknown → 必须 GREEN，不许误红。"""
    clean = {**BASE, "pairs_decidable": 5, "pairs_total": 5, "cells_in_domain": 5,
             "cells_in_domain_measured": 5, "cells_out_of_domain": 0, "pairs_not_applicable": 0}
    _fake(monkeypatch, clean)
    net = tmp_path / "net.json"
    net.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
    assert p["state"] == ae.GREEN, p["reason"]


def test_in_domain_unknown_still_reported_as_gap(tmp_path, monkeypatch):
    """域内 unknown 必须继续单独报，不被域外口径吸收掉。"""
    counts = {**BASE, "cells_in_domain": 132, "cells_in_domain_measured": 130,
              "cells_in_domain_unknown": 2, "cells_footprint_unknown": 2, "pairs_unknown": 7}
    _fake(monkeypatch, counts)
    net = tmp_path / "net.json"
    net.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
    assert p["state"] == ae.INCOMPLETE
    assert "域内 unknown 2" in p["reason"]
    assert "7 对足迹 unknown 不可判" in p["reason"]