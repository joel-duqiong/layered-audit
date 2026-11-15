"""截面取值极性守卫（W22，坑120）：verified ≠ 违反，kind 才是极性轴。

锁的正确行为：
  · positive/observation 的 root_cause_key 绝不进违反级指纹（共享正面根因不得判 consistent）；
  · positive 不被丢弃 —— 满足级通道单独取值，且在产物里可见；
  · 反向：只有 positive 的合法输入不得被误判为违反/障碍；
  · kind 缺失按遗留违反处理（兼容旧数据）；
  · 缺陷通道不受影响。
全部合成夹具，不碰真实台账。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import frame_check as fc


def _net():
    return {"nodes": {"A#m": {"taint": {"source_fields": [], "sinks": ["e.X.setStatus"]},
                              "simple": "A", "method": "m"},
                      "B#m": {"taint": {"source_fields": [], "sinks": ["e.X.setStatus"]},
                              "simple": "B", "method": "m"}},
            "edges": []}


def _units():
    return {"units": [{"unit_key": "A#m"}, {"unit_key": "B#m"}]}


def _f(fid, symbol, rck, kind=None, gt="verified"):
    f = {"id": fid, "ground_truth": gt, "claim": "status 判定", "root_cause_key": rck,
         "evidence_refs": [], "location": {"symbol": symbol, "endpoint": ""}}
    if kind is not None:
        f["kind"] = kind
    return f


IDENT = ("field", "X", "status")


# ---------------------------------------------------------------- 极性隔离（正向）

def test_positive_shared_rck_is_not_consistent():
    # 修前：两细胞共享正面根因 → 假 consistent。修后：违反集双空 → unknown。
    fs = [_f("P1", "A.m", "status.proven", kind="positive"),
          _f("P2", "B.m", "status.proven", kind="positive")]
    r = fc.run(_net(), _units(), fs)
    assert r["candidates"][0]["delta0"] == "unknown"
    assert r["counts"]["candidates_delta0"]["consistent"] == 0


def test_observation_shared_rck_is_not_consistent():
    fs = [_f("O1", "A.m", "status.observed", kind="observation"),
          _f("O2", "B.m", "status.observed", kind="observation")]
    r = fc.run(_net(), _units(), fs)
    assert r["candidates"][0]["delta0"] == "unknown"


def test_section_value_excludes_positive_and_observation_direct():
    fs = [_f("D", "A.m", "status.bad", kind="defect"),
          _f("P", "A.m", "status.proven", kind="positive"),
          _f("O", "A.m", "status.observed", kind="observation")]
    assert fc._section_value(fs, IDENT) == frozenset({"status.bad"})


def test_satisfaction_value_takes_positive_only():
    fs = [_f("D", "A.m", "status.bad", kind="defect"),
          _f("P", "A.m", "status.proven", kind="positive"),
          _f("O", "A.m", "status.observed", kind="observation")]
    assert fc._satisfaction_section_value(fs, IDENT) == frozenset({"status.proven"})


def test_unverified_positive_is_not_satisfaction():
    fs = [_f("P", "A.m", "status.proven", kind="positive", gt="unverified")]
    assert fc._satisfaction_section_value(fs, IDENT) == frozenset()
    assert fc._section_value(fs, IDENT) == frozenset()


# ---------------------------------------------------------------- 反向（合法输入不得误红）

def test_positive_only_input_no_violation_no_obstruction():
    fs = [_f("P1", "A.m", "status.proven", kind="positive"),
          _f("P2", "B.m", "status.proven", kind="positive")]
    r = fc.run(_net(), _units(), fs)
    c = r["candidates"][0]
    assert c["delta0"] == "unknown"
    assert c["frame_violation"]  # 结构事实仍在（写写重叠），但判定不是违反
    assert all(cy["status"] != "inconsistent" for cy in r["cycles"])
    assert r["counts"]["cycle_obstructions"] == 0


def test_positive_not_discarded_visible_in_artifact():
    fs = [_f("P1", "A.m", "status.proven", kind="positive")]
    r = fc.run(_net(), _units(), fs)
    res = r["candidates"][0]["resources"][0]
    assert res["findings_positive"]["A#m"] == ["P1"]
    assert res["findings_positive"]["B#m"] == []
    assert res["satisfaction_section"]["A#m"] == ["status.proven"]
    assert fc._satisfaction_section_value(fs, IDENT) == frozenset({"status.proven"})


def test_defect_channel_unaffected_by_fix():
    fs = [_f("D1", "A.m", "status.bad", kind="defect"),
          _f("D2", "B.m", "status.bad", kind="defect")]
    r = fc.run(_net(), _units(), fs)
    assert r["candidates"][0]["delta0"] == "consistent"


def test_mixed_positive_does_not_leak_into_violation_set():
    fa = [_f("P", "A.m", "status.proven", kind="positive"),
          _f("D1", "A.m", "status.bad", kind="defect")]
    fb = [_f("D2", "B.m", "status.bad", kind="defect")]
    r = fc.run(_net(), _units(), fa + fb)
    assert r["candidates"][0]["delta0"] == "consistent"
    assert fc._section_value(fa, IDENT) == frozenset({"status.bad"})


def test_missing_kind_legacy_is_violation():
    # 兼容钉：旧数据无 kind 字段，按违反处理（现有 111 条契约的前提）。
    fs = [_f("D1", "A.m", "status.bad"), _f("D2", "B.m", "status.bad")]
    r = fc.run(_net(), _units(), fs)
    assert r["candidates"][0]["delta0"] == "consistent"


def test_positive_only_cycle_not_inconsistent():
    fs = [_f("P1", "A.m", "status.proven", kind="positive"),
          _f("P2", "B.m", "status.proven", kind="positive")]
    r = fc.run(_net(), _units(), fs)
    cyc = [c for c in r["cycles"] if c["resource"] == "e.X.setStatus"][0]
    assert cyc["status"] in ("unknown", "consistent")
    assert cyc["status"] != "inconsistent"