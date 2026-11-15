"""满足级通道环级条件命题的**前提可达性**守卫（W22，V20 第二轮，坑127）。

条件命题：P1(单向 oa≠∅⇒va≠∅) + P2(not vb/not va) + P3(无外部 judgments) +
P4(unknown 边被 parity 排除) ⇒ 已知边是 c(v)=1[v∈S] 的 coboundary ⇒ 无环障碍。

本文件把「任一前提破坏 ⇒ 环障碍可达」逐个机械化：每个探针在 tmp 里造一份变异引擎副本
（入库可重跑，坑123），断言破坏后 obstructions==1、同输入未破坏对照==0。
P1 单向性注：_tied_oracle_ids = _section_value 谓词 + 判据连接 ⇒ oa⊆va；反向不成立，
且补全双条件 = 拆判据连接键 = R9 推回的资源级缺陷（互斥，不得朝该方向修）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))

import frame_check as fc

FIRE = ('        if oa and not vb and (oa & sb):\n            return "inconsistent"\n'
        '        if ob and not va and (ob & sa):\n            return "inconsistent"')
P1_ANCHOR = '        if not _finding_mentions(f, ident):\n            continue'
P4_ANCHOR = '        if label == "unknown":\n            unknown += 1\n            continue'


def _mutated(tmp_path, name, reps):
    src = (ENGINE / "frame_check.py").read_text(encoding="utf-8")
    for old, new in reps:
        assert src.count(old) == 1, f"anchor not unique: {old[:40]}"
        src = src.replace(old, new)
    p = tmp_path / f"fc_{name}.py"
    p.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"fc_mut_{name}", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses 注解解析需要模块在 sys.modules（py3.14）
    spec.loader.exec_module(mod)
    return mod


def _oracle():
    # bridge 拒收 field_gte_const+field → 强制二值退回（环走 parity 而非约束集）
    return {"id": "O1", "target": {"entity": "X", "field": "amount"},
            "assert": {"type": "field_gte_const", "field": "amount", "value": 0},
            "level": "L1_executable_constraint", "confirmation": "confirmed",
            "source": {"kind": "ddl"}}


def _world(viol, guards, mention=True):
    """viol: {cell: rck} tied 违反；guards: 细胞列表。三细胞全写 e.X.setAmount。"""
    net = {"nodes": {f"{c}#m": {"taint": {"source_fields": [], "sinks": ["e.X.setAmount"]},
                                  "simple": c, "method": "m"} for c in "ABC"}, "edges": []}
    units = {"units": [{"unit_key": f"{c}#m"} for c in "ABC"]}
    cap = {"implemented_by": [
        {"oracle_id": "O1", "unit_key": f"{c}#m", "relation": "implemented_by",
         "resolution": "field_write",
         "enforcement": {"kind": "guard", "field": "amount", "param": "amt",
                         "op": ">=", "value": 0, "reference": "X.java:10"}} for c in guards]}
    fs = [{"id": f"F{c}", "ground_truth": "verified", "kind": "defect",
           "claim": "amount 可为负" if mention else "其他问题",
           "root_cause_key": rck, "oracle_id": "O1", "evidence_refs": [],
           "location": {"symbol": f"{c}.m", "endpoint": ""}} for c, rck in viol.items()]
    return net, units, fs, [_oracle()], cap


def _obstr(mod, world, judgments=None):
    net, units, fs, oracles, cap = world
    r = mod.run(net, units, fs, oracles, judgments=judgments, capability=cap, satisfaction=True)
    return r["counts"]["cycle_obstructions"]


MIXED = ({"A": "amount.negative", "B": "amount.other", "C": "amount.third"}, ["B", "C"])


def test_p0_intact_all_premises_no_obstruction():
    assert _obstr(fc, _world(*MIXED)) == 0


def test_p1_break_one_way_premise_reachable(tmp_path):
    # P1 破坏：去掉 _tied_oracle_ids 的资源提及谓词 ⇒ oa≠∅ 但 va=∅（单向前提塌）。
    # 不提及资源的 tied 违反 + 双 guard → 三边全 1 → 奇环。对照（未破坏）oa=∅ → 0。
    mod = _mutated(tmp_path, "p1", [(P1_ANCHOR, "")])
    world = _world({"A": "other.problem", "C": "other.problem2"}, ["B", "C"], mention=False)
    assert _obstr(fc, world) == 0
    assert _obstr(mod, world) == 1


def test_p2_break_not_vb_reachable(tmp_path):
    mod = _mutated(tmp_path, "p2", [(FIRE,
        '        if oa and (oa & sb):\n            return "inconsistent"\n'
        '        if ob and (ob & sa):\n            return "inconsistent"')])
    world = _world(*MIXED)
    assert _obstr(fc, world) == 0
    assert _obstr(mod, world) == 1


def test_p3_break_external_judgments_reachable():
    # P3 破坏：judgments 注入 0-边 + 通道 A-C=1。生产不可达（CLI 无 --judgments）。
    world = _world({"A": "amount.negative"}, ["C"])
    assert _obstr(fc, world) == 0
    j = {("A#m", "B#m", "e.X.setAmount"): "consistent",
         ("B#m", "C#m", "e.X.setAmount"): "consistent"}
    assert _obstr(fc, world, judgments=j) == 1


def test_p4_break_unknown_edges_in_parity_reachable(tmp_path):
    # P4 破坏：unknown 边参与 union（此处 rel=1；rel=0 朝假 consistent 方向同样塌）。
    # 混合夹具三边全 unknown → 全 1 → 奇环。对照：unknown 被排除 → 0。
    mod = _mutated(tmp_path, "p4", [(P4_ANCHOR,
        '        if label == "unknown":\n            label = "inconsistent"')])
    world = _world(*MIXED)
    assert _obstr(fc, world) == 0
    assert _obstr(mod, world) == 1