"""高阶通道（全局奇偶裁定 + 神经复形容量统计）的契约测试：自证、双向变异、三锁、单因子隔离、语料重放。

全部合成数据 + 只读语料重放，不跑流水线、不写数据库、不碰真实产物。

变异锚点矩阵（坑123 入库可重跑；坑78 双向：缺陷态必杀 + 合法态不误红）：
  M1 真跨桶聚合障碍（三 2-细胞桶三角形，judgments 全 inconsistent）
     → 新通道 inconsistent/信息增量1；旧通道 0 障碍全 consistent
  M2 合法偶环（同形状标签 1,1,0 与 0,0,0）→ 新通道 consistent 零障碍（反向假阳性守卫）
  M3 桶内奇环（单 3-细胞桶）→ 旧通道已判红；新通道见证全边覆盖，信息增量=0
  M4 三态红线（跨资源三角一条 unknown 边）→ unknown 零障碍，不洗成 consistent/inconsistent
  M5 默认关字节一致（run 不传参 ≡ False；报告无 higher_order 键/计数器）
  M6 真 CLI 默认关（无旗标 ≡ API 默认产物；stdout 报「未测量」不发布 dim H¹；带旗标才发布）
  M7 Helly 分工（monkeypatch 细胞相关截面，成对交非空全桶交空）→ 旧通道全桶交集判红，
     新通道成对标签不重复开单，旧裁决一字不变
  M8 空输入/零已知边 → unknown 不崩不洗绿
  M9 多资源对聚合（同对跨桶 consistent+inconsistent → inconsistent）→ 跨桶障碍可达
  M10 语料重放（钉快照哈希，坑126）→ dim H¹=0、status=unknown、旧裁决 14 桶 5/9/0 不变；
     **关闭态产物与改动前引擎冻结基线逐字节一致**（做法68：不是同代码显式传 False）
  CE1 纯约束跨资源三角（三不同 ident 各挂 in[1,2]+in[3,4] ⇒ 截面空集）→ 旧 3 桶全判红，
     新通道见证**不一致边全被旧红桶覆盖 ⇒ 信息增量=0**（边级对账守卫）
  CE2 跨桶标签聚合（r1=[A,B,C] 全 consistent + r2=[A,B] inconsistent）→ 旧 0 障碍，
     新见证 ⊆ r1（scope=within_bucket）但信息增量=1，且 dim H¹=0（裁定≠H¹ 类检测）
  V3 冗余包含方向反转锚点（4-细胞桶奇三角，见证 ⊊ 旧 inconsistent 桶）→ 信息增量=0
  CE5 单不一致边形态（X.f 截面空旧红，Y.g/Z.h 截面{1,2}旧一致）→ 一致边不参与冗余判定，
     唯一不一致边旧已判红 ⇒ 信息增量=0（V21 第二轮：all(covered) 要求一致边覆盖是高估）
  CE6 旧桶因别的原因判红（r1 因 A-B-D 奇环红、conflict=B-D；r2/r3/r4 绿桶 inconsistent）
     → 「两端点⊆旧红桶」≠「该边被判红」，逐对粒度对账 ⇒ 信息增量=1（V21 第二轮：低估）
  CE7 混合覆盖形态（4-环 covered=[T,F,F,T]）→ 信息增量=1；W1(all→any) 指名红
     （全覆盖/零覆盖夹具上 all/any 恒等，混合态才分得开 —— V21 第二轮元教训）
  X2 条件C 第三态（r1 旧 unknown 不算红）→ 增量 1；X2(==inc→!=con) 指名红恰 1
  X3 条件B 量词第三态（(A,B) 跨红绿两桶，any 取红）→ 增量 0；X3(any→all) 指名红恰 1
  X4 条件B 标签第三态（红桶 r1 的 (A,C) 逐对 unknown 不算覆盖）→ 增量 1；X4(==inc→!=con) 指名红恰 1
     （V21 第三轮坑140：每个合取条件各自踩遍三态；X1 一致边空真已证等价不动）
  口径守卫：status 语义限定（无奇环≠无障碍）与裁定来源（全局奇偶；H¹ 不参与裁定）
     必须在产物 notes 与 CLI stdout 同时在场，删限定即红；前瞻限定（约束标签只在 full 桶
     产生，DTO 投影放开部分截面须重推）同锁
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from itertools import combinations, permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import frame_check as fc

PILOT = Path(__file__).resolve().parents[1]
ENGINE = PILOT / "engine"


def _net(nodes=None, edges=None):
    return {"nodes": nodes or {}, "edges": edges or []}


def _units(*keys):
    return {"units": [{"unit_key": k} for k in keys]}


def _sink_net(cell_resources: dict[str, list[str]]):
    nodes = {c: {"taint": {"source_fields": [], "sinks": list(rs)}} for c, rs in cell_resources.items()}
    return _net(nodes)


def _judgments(labels: dict[tuple[str, str], str], index_resources: dict[str, list[str]]):
    """把成对标签铺到每个共享桶的 (a,b,r) 键上（两序都铺，免疫桶内排序）。"""
    out = {}
    for r, cs in index_resources.items():
        for a, b in combinations(sorted(cs), 2):
            lab = labels.get((a, b)) or labels.get((b, a))
            if lab is not None:
                for x, y in permutations((a, b)):
                    out[(x, y, r)] = lab
    return out


def _triangle_net():
    return _sink_net({"A#m": ["r1", "r3"], "B#m": ["r1", "r2"], "C#m": ["r2", "r3"]})


TRI_BUCKETS = {"r1": ["A#m", "B#m"], "r2": ["B#m", "C#m"], "r3": ["A#m", "C#m"]}


# ---------------------------------------------------------------- 参考实现（独立于被测方，坑3/25）

def _ref_gf2_rank_sets(rows):
    """独立的 GF(2) 秩：frozenset 行消元（与生产 bitset 实现不同表示、不同代码路径）。"""
    basis = []
    for r in rows:
        cur = set(r)
        for b in basis:
            if max(b) in cur:
                cur ^= b
        if cur:
            basis.append(frozenset(cur))
    return len(basis)


def _ref_gf2_solve(rows, rhs):
    """增广消元可解性（测试参考实现）。"""
    basis = {}
    for r, b in zip(rows, rhs):
        cur = (r << 1) | b
        while cur:
            p = cur.bit_length() - 1
            if p == 0:
                return False
            if p in basis:
                cur ^= basis[p]
            else:
                basis[p] = cur
                break
    return True


def _ref_brute_section(verts, known_edges, labels):
    """暴力枚举 2^V 全局截面（着色），满足全部已知边则 True。"""
    vi = {c: i for i, c in enumerate(verts)}
    cons = [(a, b, 0 if labels[(a, b)] == "consistent" else 1) for a, b in known_edges]
    for mask in range(1 << len(verts)):
        if all(((mask >> vi[a]) & 1) ^ ((mask >> vi[b]) & 1) == rel for a, b, rel in cons):
            return True
    return False


def _ref_components(verts, edges):
    parent = {v: v for v in verts}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return len({find(v) for v in verts}) if verts else 0


# ---------------------------------------------------------------- 自证（坑25：参考实现先对账暴力）

def test_selfcheck_rank_vs_brute_force_seeded():
    rng = random.Random(20261115)
    for t in range(300):
        nv = rng.randint(2, 8)
        verts = [f"c{i}#m" for i in range(nv)]
        cell_resources = {c: [] for c in verts}
        buckets = {}
        for k in range(rng.randint(1, 5)):
            cs = sorted(rng.sample(verts, rng.randint(2, nv)))
            r = f"r{k}"
            buckets[r] = cs
            for c in cs:
                cell_resources[c].append(r)
        net = _sink_net(cell_resources)
        units = _units(*verts)
        labels = {}
        for a, b in combinations(verts, 2):
            if any(a in cs and b in cs for cs in buckets.values()):
                labels[(a, b)] = rng.choice(["consistent", "inconsistent", "unknown"])
        judgments = _judgments({p: l for p, l in labels.items() if l != "unknown"}, buckets)
        r = fc.run(net, units, judgments=judgments, higher_order=True)
        ho = r["higher_order"]
        v2, edges, tris, _ = fc._nerve_complex(fc.build_index(fc.build_footprints(net, units)[0]))
        known = [e for e in edges if labels.get(e, "unknown") != "unknown"]
        brute = _ref_brute_section(v2, known, labels) if known else True
        # 三态逐态对账：inconsistent ⇔ 已知边子系统不可满足；否则有 unknown ⇒ unknown
        if known and not brute:
            assert ho["status"] == "inconsistent", f"trial {t}: 暴力无截面但通道未判红"
            assert ho["obstructions"], f"trial {t}: 判红必须有见证"
            # 见证有效性直接断言（坑78：锁住真正声称的不变式，不是相邻性质）
            for ob in ho["obstructions"]:
                cyc = ob["cycle"]
                assert len(set(cyc)) >= 3, f"trial {t}: 退化假环 {cyc}"
                assert len(set(cyc)) == len(cyc), f"trial {t}: 环顶点重复"
                es = [tuple(e) for e in ob["edges"]]
                assert len(es) == len(cyc), f"trial {t}: 边数≠顶点数"
                assert all(tuple(sorted(e)) in set(map(tuple, map(sorted, known))) for e in es), f"trial {t}: 见证用了 unknown 边"
                closed = all(tuple(sorted((cyc[i], cyc[(i + 1) % len(cyc)]))) in {tuple(sorted(e)) for e in es} for i in range(len(cyc)))
                assert closed, f"trial {t}: 见证边不构成闭环"
                parity = 0
                for e in es:
                    lab = labels.get(tuple(sorted(e)))
                    parity ^= 0 if lab == "consistent" else 1
                assert parity == 1, f"trial {t}: 见证环奇偶≠1"
        elif len(known) < len(edges) or not known:
            assert ho["status"] == "unknown", f"trial {t}: unknown 边被洗成 {ho['status']}"
        else:
            assert ho["status"] == "consistent", f"trial {t}: 暴力有截面但通道未判一致"
        # 独立秩实现交叉验证 dim H¹（不同表示、不同代码路径）
        vi = {c: i for i, c in enumerate(v2)}
        ei = {e: i for i, e in enumerate(edges)}
        d0_sets = [{vi[a], vi[b]} for a, b in edges]
        d1_sets = [{ei[(a, b)], ei[(b, c)], ei[(a, c)]} for a, b, c in tris]
        r0_ref = _ref_gf2_rank_sets(d0_sets)
        r1_ref = _ref_gf2_rank_sets(d1_sets)
        assert ho["cochain"]["rank_delta0"] == r0_ref, f"trial {t}: rank δ0 分歧"
        assert ho["cochain"]["rank_delta1"] == r1_ref, f"trial {t}: rank δ1 分歧"
        cycle_dim = len(edges) - len(v2) + _ref_components(v2, edges)
        assert ho["cochain"]["h1_dimension"] == cycle_dim - r1_ref == len(edges) - r0_ref - r1_ref, f"trial {t}: H¹ 公式分歧"
        # 秩法可解性与暴力对账（仅已知边）
        if known:
            rows = [(1 << vi[a]) | (1 << vi[b]) for a, b in known]
            rhs = [0 if labels[e] == "consistent" else 1 for e in known]
            assert _ref_gf2_solve(rows, rhs) == brute, f"trial {t}: 秩法 vs 暴力分歧"


def test_h1_hand_computed_fixtures():
    # 未填充三角形（三个 2-细胞桶）：h1=1
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), higher_order=True)
    assert r["higher_order"]["cochain"]["h1_dimension"] == 1
    assert r["higher_order"]["nerve"] == {"vertices": 3, "edges": 3, "triangles": 0}
    # 填充三角形（单 3-细胞桶）：h1=0
    net = _sink_net({"A#m": ["r"], "B#m": ["r"], "C#m": ["r"]})
    r = fc.run(net, _units("A#m", "B#m", "C#m"), higher_order=True)
    assert r["higher_order"]["cochain"]["h1_dimension"] == 0
    assert r["higher_order"]["nerve"]["triangles"] == 1
    # 未填充 4-环：h1=1
    net = _sink_net({"A#m": ["r1", "r4"], "B#m": ["r1", "r2"], "C#m": ["r2", "r3"], "D#m": ["r3", "r4"]})
    r = fc.run(net, _units("A#m", "B#m", "C#m", "D#m"), higher_order=True)
    assert r["higher_order"]["cochain"]["h1_dimension"] == 1
    # 两桶共享一条边（钻石）：全部环被填充，h1=0
    net = _sink_net({"A#m": ["r1", "r2"], "B#m": ["r1", "r2"], "C#m": ["r1"], "D#m": ["r2"]})
    r = fc.run(net, _units("A#m", "B#m", "C#m", "D#m"), higher_order=True)
    assert r["higher_order"]["cochain"]["h1_dimension"] == 0


# ---------------------------------------------------------------- 双向变异（坑78）

def test_m1_true_cross_resource_obstruction_new_detects_old_misses():
    labels = {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent", ("A#m", "C#m"): "inconsistent"}
    j = _judgments(labels, TRI_BUCKETS)
    net, units = _triangle_net(), _units("A#m", "B#m", "C#m")
    old = fc.run(net, units, judgments=j)
    # 旧通道必须检不出：三个 2-细胞桶各一条边，无环可判
    assert old["counts"]["cycle_obstructions"] == 0
    assert all(c["status"] == "consistent" for c in old["cycles"])
    new = fc.run(net, units, judgments=j, higher_order=True)
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    assert len(ho["obstructions"]) == 1
    assert ho["obstructions_incremental"] == 1
    ob = ho["obstructions"][0]
    assert ob["scope"] == "cross_resource"
    assert ob["containing_buckets"] == []
    assert set(ob["cycle"]) == {"A#m", "B#m", "C#m"}
    assert not ob["redundant_with_skeleton"]
    # 旧裁决逐项不变（单因子隔离）
    assert new["cycles"] == old["cycles"]
    assert new["counts"]["cycle_obstructions"] == 0


def test_m2_legitimate_even_cycle_no_false_positive():
    net, units = _triangle_net(), _units("A#m", "B#m", "C#m")
    for labels in (
        {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent", ("A#m", "C#m"): "consistent"},
        {("A#m", "B#m"): "consistent", ("B#m", "C#m"): "consistent", ("A#m", "C#m"): "consistent"},
    ):
        j = _judgments(labels, TRI_BUCKETS)
        r = fc.run(net, units, judgments=j, higher_order=True)
        ho = r["higher_order"]
        assert ho["status"] == "consistent", f"合法偶环被误报: {labels}"
        assert ho["obstructions"] == []
        assert ho["obstructions_incremental"] == 0


def test_m3_within_bucket_odd_cycle_redundant_not_incremental():
    net = _sink_net({"A#m": ["r"], "B#m": ["r"], "C#m": ["r"]})
    units = _units("A#m", "B#m", "C#m")
    labels = {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent", ("A#m", "C#m"): "inconsistent"}
    j = _judgments(labels, {"r": ["A#m", "B#m", "C#m"]})
    old = fc.run(net, units, judgments=j)
    assert old["counts"]["cycle_obstructions"] == 1  # 旧通道自己检得出
    new = fc.run(net, units, judgments=j, higher_order=True)
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    assert len(ho["obstructions"]) == 1
    assert ho["obstructions"][0]["scope"] == "within_bucket"
    assert ho["obstructions"][0]["redundant_with_skeleton"] is True
    assert ho["obstructions"][0]["edges_covered_by_skeleton"] == [True, True, True]
    assert ho["obstructions_incremental"] == 0  # 检测力不重复计
    assert new["cycles"] == old["cycles"]


def test_m4_unknown_edge_keeps_three_state():
    labels = {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent"}  # A-C 无标签
    j = _judgments(labels, TRI_BUCKETS)
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), judgments=j, higher_order=True)
    ho = r["higher_order"]
    assert ho["status"] == "unknown"
    assert ho["obstructions"] == []
    assert ho["edges_unknown"] == 1


def test_m8_empty_and_zero_known_edges():
    r = fc.run(_net(), _units(), higher_order=True)
    ho = r["higher_order"]
    assert ho["status"] == "unknown"
    assert ho["cochain"]["h1_dimension"] == 0
    assert ho["obstructions"] == []
    # 有边但零已知边（无 judgments/findings）⇒ unknown，不是 consistent
    r = fc.run(_triangle_net(), _units("A#m", "B#m", "C#m"), higher_order=True)
    assert r["higher_order"]["status"] == "unknown"
    assert r["higher_order"]["edges_known"] == 0


def test_m9_multi_resource_pair_aggregation():
    # A-B 共享 r1（约束 consistent）与 r4（judgment inconsistent）⇒ 聚合 inconsistent
    net = _sink_net({"A#m": ["field:X.f", "r4", "r3"], "B#m": ["field:X.f", "r4", "r2"], "C#m": ["r2", "r3"]})
    units = _units("A#m", "B#m", "C#m")
    oracles = [{"id": "O1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"}]
    labels = {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent", ("A#m", "C#m"): "inconsistent"}
    buckets = {"field:X.f": ["A#m", "B#m"], "r4": ["A#m", "B#m"], "r2": ["B#m", "C#m"], "r3": ["A#m", "C#m"]}
    j = _judgments(labels, buckets)
    old = fc.run(net, units, judgments=j, oracles=oracles)
    assert old["counts"]["cycle_obstructions"] == 0
    new = fc.run(net, units, judgments=j, oracles=oracles, higher_order=True)
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    assert ho["obstructions_incremental"] == 1
    assert new["cycles"] == old["cycles"]


# ---------------------------------------------------------------- V21 反例入库（CE1/CE2/V3）

def test_ce1_pure_constraint_cross_resource_zero_information_increment():
    # 三个不同 ident 资源各挂 in[1,2] 与 in[3,4] ⇒ 各桶截面空集；三角桶。
    # 旧通道 3 桶全判红；新通道见证三边各被旧桶覆盖 ⇒ 信息增量必 0（边级对账）。
    net = _sink_net({"A#m": ["field:X.f", "field:Z.h"], "B#m": ["field:X.f", "field:Y.g"],
                     "C#m": ["field:Y.g", "field:Z.h"]})
    units = _units("A#m", "B#m", "C#m")
    oracles = []
    for ent, fld in (("X", "f"), ("Y", "g"), ("Z", "h")):
        for i, vals in enumerate(([1, 2], [3, 4])):
            oracles.append({"id": f"{ent}{i}", "target": {"entity": ent, "field": fld},
                            "assert": {"type": "in", "field": fld, "value": vals}, "confirmation": "confirmed"})
    # 逐桶核实细胞无关截面为空集（不是 None）
    fps, _ = fc.build_footprints(net, units)
    for r in ("field:X.f", "field:Y.g", "field:Z.h"):
        assert all(fc._cell_section(fps[c], r, oracles) == ("set", frozenset()) for c in ("A#m", "B#m", "C#m"))
    old = fc.run(net, units, oracles=oracles)
    assert old["counts"]["cycle_obstructions"] == 3
    new = fc.run(net, units, oracles=oracles, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    assert len(ho["obstructions"]) == 1
    ob = ho["obstructions"][0]
    assert ob["scope"] == "cross_resource"
    assert ob["edges_covered_by_skeleton"] == [True, True, True]
    assert ob["redundant_with_skeleton"] is True
    assert ho["obstructions_incremental"] == 0  # 信息增量为零（不是「字段为零」的旧论断）


def test_ce2_cross_bucket_aggregation_within_bucket_witness():
    # r1=[A,B,C] 全 consistent、r2=[A,B] inconsistent ⇒ 旧 2 桶全绿；
    # 新见证 ⊆ r1（within_bucket）但标签来自 r2 ⇒ 信息增量=1；dim H¹=0 仍有障碍。
    net = _sink_net({"A#m": ["r1", "r2"], "B#m": ["r1", "r2"], "C#m": ["r1"]})
    units = _units("A#m", "B#m", "C#m")
    labels = {("A#m", "B#m"): "consistent", ("B#m", "C#m"): "consistent", ("A#m", "C#m"): "consistent"}
    j = _judgments(labels, {"r1": ["A#m", "B#m", "C#m"]})
    j[("A#m", "B#m", "r2")] = "inconsistent"
    j[("B#m", "A#m", "r2")] = "inconsistent"
    old = fc.run(net, units, judgments=j)
    assert old["counts"]["cycle_obstructions"] == 0
    assert all(c["status"] == "consistent" for c in old["cycles"])
    new = fc.run(net, units, judgments=j, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    assert ho["cochain"]["h1_dimension"] == 0  # 三角形被 r1 填充，H¹=0
    assert len(ho["obstructions"]) == 1
    ob = ho["obstructions"][0]
    assert set(ob["cycle"]) == {"A#m", "B#m", "C#m"}
    assert ob["scope"] == "within_bucket"  # 顶点集在单桶内
    assert ob["containing_buckets"] == ["r1"]
    assert ob["redundant_with_skeleton"] is False
    assert ho["obstructions_incremental"] == 1  # 真盲区=跨桶标签聚合，与 scope 无关


def test_v3_proper_superset_bucket_redundancy_direction():
    # 4-细胞桶：ABC 奇三角 + D 全 consistent ⇒ 旧桶判红，见证 {A,B,C} ⊊ 桶。
    # 包含方向反转（obs⊆vset）在此夹具上可达：反转会判增量=1，本守卫指名杀。
    net = _sink_net({"A#m": ["r"], "B#m": ["r"], "C#m": ["r"], "D#m": ["r"]})
    units = _units("A#m", "B#m", "C#m", "D#m")
    labels = {("A#m", "B#m"): "inconsistent", ("B#m", "C#m"): "inconsistent", ("A#m", "C#m"): "inconsistent",
              ("A#m", "D#m"): "consistent", ("B#m", "D#m"): "consistent", ("C#m", "D#m"): "consistent"}
    j = _judgments(labels, {"r": ["A#m", "B#m", "C#m", "D#m"]})
    old = fc.run(net, units, judgments=j)
    assert old["counts"]["cycle_obstructions"] == 1
    new = fc.run(net, units, judgments=j, higher_order=True)
    ho = new["higher_order"]
    assert len(ho["obstructions"]) >= 1
    ob = ho["obstructions"][0]
    assert set(ob["cycle"]) == {"A#m", "B#m", "C#m"}  # 见证真包含于旧桶
    assert ob["edges_covered_by_skeleton"] == [True, True, True]
    assert ob["redundant_with_skeleton"] is True
    assert ho["obstructions_incremental"] == 0


def test_ce5_single_inconsistent_edge_consistent_edges_vacuous():
    # 纯约束:X.f=[A,B] 截面空(旧红);Y.g=[B,C]/Z.h=[A,C] 截面{1,2}(旧一致)。
    # 奇环只 1 条不一致边 (A,B),旧已判红 ⇒ 信息增量=0;一致边不参与冗余判定。
    net = _sink_net({"A#m": ["field:X.f", "field:Z.h"], "B#m": ["field:X.f", "field:Y.g"],
                     "C#m": ["field:Y.g", "field:Z.h"]})
    units = _units("A#m", "B#m", "C#m")
    oracles = [{"id": "X1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"},
               {"id": "X2", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [3, 4]}, "confirmation": "confirmed"},
               {"id": "Y1", "target": {"entity": "Y", "field": "g"},
                "assert": {"type": "in", "field": "g", "value": [1, 2]}, "confirmation": "confirmed"},
               {"id": "Z1", "target": {"entity": "Z", "field": "h"},
                "assert": {"type": "in", "field": "h", "value": [1, 2]}, "confirmation": "confirmed"}]
    fps, _ = fc.build_footprints(net, units)
    assert fc._cell_section(fps["A#m"], "field:X.f", oracles) == ("set", frozenset())
    assert fc._cell_section(fps["B#m"], "field:Y.g", oracles) == ("set", frozenset({1, 2}))
    old = fc.run(net, units, oracles=oracles)
    assert old["counts"]["cycle_obstructions"] == 1
    new = fc.run(net, units, oracles=oracles, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    ob = ho["obstructions"][0]
    # 见证恰 1 条不一致边(锁定单不一致边形态),且被旧红桶覆盖;一致边空真覆盖
    labels, _ = fc._higher_order_labels(fps, fc.build_index(fps), {}, None, oracles, None, None, frozenset(), False)
    inc = [e for e in ob["edges"] if labels.get(tuple(sorted(e))) == "inconsistent"]
    assert len(inc) == 1
    assert ob["edges_covered_by_skeleton"] == [True, True, True]
    assert ob["redundant_with_skeleton"] is True
    assert ho["obstructions_incremental"] == 0


def test_ce6_bucket_red_for_other_reason_not_edge_coverage():
    # r1=[A,B,C,D] 因 A-B-D 奇环判红(conflict=B-D,r1 内 (A,B)=consistent);
    # r2/r3/r4 各 2 细胞边 inconsistent,旧全绿。见证三角的不一致边来自绿桶 ⇒ 信息增量=1。
    # 「两端点⊆旧红桶」会误判全覆盖(坑136 原版病根)。
    net = _sink_net({"A#m": ["r1", "r2", "r4"], "B#m": ["r1", "r2", "r3"],
                     "C#m": ["r1", "r3", "r4"], "D#m": ["r1"]})
    units = _units("A#m", "B#m", "C#m", "D#m")
    lab1 = {("A#m", "B#m"): "consistent", ("A#m", "D#m"): "consistent", ("B#m", "D#m"): "inconsistent",
            ("A#m", "C#m"): "consistent", ("B#m", "C#m"): "consistent", ("C#m", "D#m"): "consistent"}
    j = _judgments(lab1, {"r1": ["A#m", "B#m", "C#m", "D#m"]})
    for r, (a, b) in (("r2", ("A#m", "B#m")), ("r3", ("B#m", "C#m")), ("r4", ("A#m", "C#m"))):
        j[(a, b, r)] = "inconsistent"
        j[(b, a, r)] = "inconsistent"
    old = fc.run(net, units, judgments=j)
    r1 = next(c for c in old["cycles"] if c["resource"] == "r1")
    assert r1["status"] == "inconsistent" and r1["conflict"] == ["B#m", "D#m"]
    assert old["counts"]["cycle_obstructions"] == 1  # r2/r3/r4 全绿
    new = fc.run(net, units, judgments=j, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    # 两个奇环见证:B-A-C(三边全来自绿桶)与 C-A-D(A-C 来自绿桶 r4)⇒ 都是真增量
    assert len(ho["obstructions"]) == 2
    tri = next(o for o in ho["obstructions"] if set(o["cycle"]) == {"A#m", "B#m", "C#m"})
    assert tri["edges_covered_by_skeleton"] == [False, False, False]
    assert tri["redundant_with_skeleton"] is False
    assert ho["obstructions_incremental"] == 2


def test_ce7_mixed_coverage_kills_all_any_and_containment_mutants():
    # 混合态:4-环 A-B-C-D,不一致边 A-B 被旧红约束桶覆盖,B-C/C-D 来自旧绿 2-细胞桶未覆盖,
    # A-D consistent。covered=[T,F,F,T] ⇒ redundant=False、增量=1。
    # W1(all→any)在此形态上 any=True≠all=False ⇒ 指名红;全覆盖/零覆盖夹具上两者恒等(病根)。
    net = _sink_net({"A#m": ["field:X.f", "r4"], "B#m": ["field:X.f", "r2"],
                     "C#m": ["r2", "r3"], "D#m": ["r3", "r4"]})
    units = _units("A#m", "B#m", "C#m", "D#m")
    oracles = [{"id": "X1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"},
               {"id": "X2", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [3, 4]}, "confirmation": "confirmed"}]
    j = _judgments({("B#m", "C#m"): "inconsistent", ("C#m", "D#m"): "inconsistent", ("A#m", "D#m"): "consistent"},
                   {"r2": ["B#m", "C#m"], "r3": ["C#m", "D#m"], "r4": ["A#m", "D#m"]})
    old = fc.run(net, units, judgments=j, oracles=oracles)
    assert old["counts"]["cycle_obstructions"] == 1  # 仅 field:X.f 红
    new = fc.run(net, units, judgments=j, oracles=oracles, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    ob = ho["obstructions"][0]
    assert len(ob["cycle"]) == 4
    cov = ob["edges_covered_by_skeleton"]
    assert sorted(cov) == [False, False, True, True]  # 部分覆盖:1 不一致边覆盖 + 2 未覆盖 + 1 空真
    assert ob["redundant_with_skeleton"] is False
    assert ho["obstructions_incremental"] == 1


def test_x2_unknown_old_bucket_not_counted_as_red():
    # 条件C 第三态:r1=[A,B,C] 只给 (A,B)=inc、其余对无 finding ⇒ 旧 unknown;
    # X.f=[B,C]/Y.g=[A,C] 截面空 ⇒ 旧红。(A,B) 只来自 r1 ⇒ 未覆盖 ⇒ 增量 1。
    # X2 变异(==inc → !=con,unknown 桶算红)会把增量翻成 0 ⇒ 本守卫指名红。
    net = _sink_net({"A#m": ["r1", "field:Y.g"], "B#m": ["r1", "field:X.f"], "C#m": ["r1", "field:X.f", "field:Y.g"]})
    units = _units("A#m", "B#m", "C#m")
    oracles = []
    for ent, fld in (("X", "f"), ("Y", "g")):
        for i, vals in enumerate(([1, 2], [3, 4])):
            oracles.append({"id": f"{ent}{i}", "target": {"entity": ent, "field": fld},
                            "assert": {"type": "in", "field": fld, "value": vals}, "confirmation": "confirmed"})
    j = {("A#m", "B#m", "r1"): "inconsistent", ("B#m", "A#m", "r1"): "inconsistent"}
    old = fc.run(net, units, judgments=j, oracles=oracles)
    r1 = next(c for c in old["cycles"] if c["resource"] == "r1")
    assert r1["status"] == "unknown"  # 第三态:旧桶 unknown
    assert old["counts"]["cycle_obstructions"] == 2  # X.f/Y.g 红
    new = fc.run(net, units, judgments=j, oracles=oracles, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    ob = ho["obstructions"][0]
    ab = next(i for i, e in enumerate(ob["edges"]) if set(e) == {"A#m", "B#m"})
    assert ob["edges_covered_by_skeleton"][ab] is False  # unknown 桶不算红
    assert ho["obstructions_incremental"] == 1


def test_x3_pair_in_red_and_green_bucket_any_not_all():
    # 条件B 量词第三态:(A,B) 同属 X.f(红+inc) 与 r2(绿+inc);另两边来自红桶。
    # any ⇒ (A,B) 经 X.f 覆盖 ⇒ 增量 0;X3 变异(any→all)要求全桶都红 ⇒ 增量 1 ⇒ 指名红。
    net = _sink_net({"A#m": ["field:X.f", "r2", "field:Z.h"], "B#m": ["field:X.f", "r2", "field:Y.g"],
                     "D#m": ["field:Y.g", "field:Z.h"]})
    units = _units("A#m", "B#m", "D#m")
    oracles = []
    for ent, fld in (("X", "f"), ("Y", "g"), ("Z", "h")):
        for i, vals in enumerate(([1, 2], [3, 4])):
            oracles.append({"id": f"{ent}{i}", "target": {"entity": ent, "field": fld},
                            "assert": {"type": "in", "field": fld, "value": vals}, "confirmation": "confirmed"})
    j = {("A#m", "B#m", "r2"): "inconsistent", ("B#m", "A#m", "r2"): "inconsistent"}
    old = fc.run(net, units, judgments=j, oracles=oracles)
    assert old["counts"]["cycle_obstructions"] == 3  # 三约束桶红,r2 绿
    new = fc.run(net, units, judgments=j, oracles=oracles, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    ob = ho["obstructions"][0]
    ab = next(i for i, e in enumerate(ob["edges"]) if set(e) == {"A#m", "B#m"})
    assert ob["edges_covered_by_skeleton"][ab] is True  # 一红一绿,any 取红
    assert ob["redundant_with_skeleton"] is True
    assert ho["obstructions_incremental"] == 0


def test_x4_unknown_per_pair_label_in_red_bucket_not_coverage():
    # 条件B 标签第三态:r1=[A,B,C,D] 因 B-D 奇环判红,但 (A,C) 无 judgment ⇒ 逐对 unknown;
    # (A,C) 的 inc 来自绿桶 r4 ⇒ 未覆盖 ⇒ 增量 1。X4 变异(==inc → !=con,红桶 unknown 算覆盖)
    # 会把增量翻成 0 ⇒ 本守卫指名红。
    net = _sink_net({"A#m": ["r1", "r4"], "B#m": ["r1"], "C#m": ["r1", "r4"], "D#m": ["r1"]})
    units = _units("A#m", "B#m", "C#m", "D#m")
    j = _judgments({("A#m", "B#m"): "consistent", ("A#m", "D#m"): "consistent",
                    ("B#m", "D#m"): "inconsistent", ("C#m", "D#m"): "consistent"},
                   {"r1": ["A#m", "B#m", "C#m", "D#m"]})
    j[("A#m", "C#m", "r4")] = "inconsistent"
    j[("C#m", "A#m", "r4")] = "inconsistent"
    old = fc.run(net, units, judgments=j)
    r1 = next(c for c in old["cycles"] if c["resource"] == "r1")
    assert r1["status"] == "inconsistent" and r1["conflict"] == ["B#m", "D#m"]
    assert old["counts"]["cycle_obstructions"] == 1  # r4 绿
    new = fc.run(net, units, judgments=j, higher_order=True)
    assert new["cycles"] == old["cycles"]
    ho = new["higher_order"]
    assert ho["status"] == "inconsistent"
    # 见证 C-A-D:(A,C) 不一致、逐对在红桶 r1 里是 unknown ⇒ 不算覆盖
    w = next(o for o in ho["obstructions"] if set(o["cycle"]) == {"A#m", "C#m", "D#m"})
    ac = next(i for i, e in enumerate(w["edges"]) if set(e) == {"A#m", "C#m"})
    assert w["edges_covered_by_skeleton"][ac] is False
    assert w["redundant_with_skeleton"] is False
    assert ho["obstructions_incremental"] == 1


def test_status_semantics_and_adjudication_source_qualifiers(tmp_path):
    # 必修5/6：status 口径限定与裁定来源必须在 notes 与 stdout 同时在场（删限定即红）。
    # 构造「cycles obstructions>0 且 高阶 status=consistent」并立面：2-细胞约束桶截面空集。
    net = _sink_net({"A#m": ["field:X.f"], "B#m": ["field:X.f"]})
    units = _units("A#m", "B#m")
    oracles = [{"id": "O1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"},
               {"id": "O2", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [3, 4]}, "confirmation": "confirmed"}]
    r = fc.run(net, units, oracles=oracles, higher_order=True)
    assert r["counts"]["cycle_obstructions"] == 1
    ho = r["higher_order"]
    assert ho["status"] == "consistent"  # 单边无环 ⇒ 无奇环，但桶级障碍存在
    notes = " ".join(ho["notes"])
    assert "不是「无障碍」" in notes
    assert "不参与裁定" in notes
    assert "全局 1-骨架 GF(2) 奇偶" in notes
    assert "DTO 投影放开部分截面时须重推" in notes  # 前瞻限定(V21 第三轮)
    nd, ud, od = tmp_path / "net.json", tmp_path / "units.json", tmp_path / "oracles.json"
    nd.write_text(json.dumps(net)); ud.write_text(json.dumps(units))
    od.write_text(json.dumps({"oracles": oracles}))
    proc, _ = _cli(["--net", str(nd), "--units", str(ud), "--oracles", str(od), "--higher-order"], tmp_path)
    assert proc.returncode == 0
    assert "语义=已知边无奇环,不是无障碍" in proc.stdout
    assert "不参与裁定" in proc.stdout


# ---------------------------------------------------------------- Helly 分工（M7）

def test_m7_helly_within_bucket_old_covers_new_not_double_count(monkeypatch):
    net = _sink_net({"A#m": ["field:X.f"], "B#m": ["field:X.f"], "C#m": ["field:X.f"]})
    units = _units("A#m", "B#m", "C#m")
    secs = {"A#m": ("set", frozenset({1, 2})), "B#m": ("set", frozenset({2, 3})), "C#m": ("set", frozenset({1, 3}))}
    monkeypatch.setattr(fc, "_cell_section", lambda fp, r, o, em=None: secs[fp.cell])
    old = fc.run(net, units)
    assert old["counts"]["cycle_obstructions"] == 1  # 全桶交集空 ⇒ 旧通道判红
    assert old["cycles"][0]["global_intersection_empty"] is True
    new = fc.run(net, units, higher_order=True)
    assert new["cycles"] == old["cycles"]  # 旧裁决一字不变
    ho = new["higher_order"]
    # 成对交全非空 ⇒ 新通道成对标签不重复开单；桶级 Helly 归旧通道
    assert ho["obstructions"] == []
    assert ho["status"] == "consistent"


def test_same_ident_sections_equal_across_spellings():
    # 同一 ident 的不同拼写（field: 与 setter）截面恒等（截面性质守卫；
    # 通道级「约束信息增量为零」由 CE1 守卫锁定，真依据是旧通道已枚举桶内全部成对交，
    # 与细胞无关性无关 —— V21 证伪后修正，坑133）
    net = _sink_net({"A#m": ["field:X.f"], "B#m": ["X.setF"]})
    fps, _ = fc.build_footprints(net, _units("A#m", "B#m"))
    oracles = [{"id": "O1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"}]
    s1 = fc._cell_section(fps["A#m"], "field:X.f", oracles)
    s2 = fc._cell_section(fps["B#m"], "X.setF", oracles)
    assert s1 == s2 == ("set", frozenset({1, 2}))


# ---------------------------------------------------------------- 三锁（做法66）

def _rich_fixture():
    net = _sink_net({"A#m": ["field:X.f", "r1"], "B#m": ["field:X.f", "r1", "r2"],
                     "C#m": ["r2"], "D#m": ["table:t"]})
    units = _units("A#m", "B#m", "C#m", "D#m")
    oracles = [{"id": "O1", "target": {"entity": "X", "field": "f"},
                "assert": {"type": "in", "field": "f", "value": [1, 2]}, "confirmation": "confirmed"}]
    return net, units, oracles


def test_m5_default_off_report_byte_identical():
    net, units, oracles = _rich_fixture()
    kw = dict(oracles=oracles, business_levels={"L0_business_rule"})
    base = fc.run(net, units, **kw)
    explicit_off = fc.run(net, units, higher_order=False, **kw)
    assert json.dumps(base, ensure_ascii=False, sort_keys=True) == json.dumps(explicit_off, ensure_ascii=False, sort_keys=True)
    assert "higher_order" not in base
    assert not any(k.startswith("higher_order_") for k in base["counts"])
    on = fc.run(net, units, higher_order=True, **kw)
    # 开启只增不改：原有键值逐项不变（counts 单列子检查，允许新增三个计数器）
    for k, v in base.items():
        if k == "counts":
            continue
        assert on[k] == v, f"开启高阶通道改变了原有键 {k}"
    for k, v in base["counts"].items():
        assert on["counts"][k] == v, f"开启高阶通道改变了计数 {k}"
    assert set(on["counts"]) - set(base["counts"]) == {
        "higher_order_obstructions", "higher_order_obstructions_incremental", "higher_order_h1_dimension"}


def _cli(args, tmp_path, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    out = tmp_path / "report.json"
    proc = subprocess.run([sys.executable, str(ENGINE / "frame_check.py"), *args, "--out", str(out)],
                          capture_output=True, text=True, env=env, timeout=120)
    return proc, (json.loads(out.read_text()) if out.exists() else None)


def test_m6_cli_default_off_and_publication_gate(tmp_path):
    net, units, oracles = _rich_fixture()
    nd, ud, od = tmp_path / "net.json", tmp_path / "units.json", tmp_path / "oracles.json"
    nd.write_text(json.dumps(net)); ud.write_text(json.dumps(units))
    od.write_text(json.dumps({"oracles": oracles}))
    base_args = ["--net", str(nd), "--units", str(ud), "--oracles", str(od)]
    proc_off, rep_off = _cli(base_args, tmp_path)
    assert proc_off.returncode == 0
    api_off = fc.run(net, units, oracles=oracles, business_levels={"L0_business_rule"})
    assert rep_off == api_off  # 真 CLI 无旗标 ≡ API 默认
    assert "higher_order" not in rep_off
    assert "高阶通道: disabled（未测量" in proc_off.stdout
    assert "dim H¹=" not in proc_off.stdout  # 自述面与判定面同门：关闭不发布数字
    proc_on, rep_on = _cli(base_args + ["--higher-order"], tmp_path)
    assert proc_on.returncode == 0
    assert rep_on["higher_order"]["channel"] == "ok"
    assert "dim H¹=" in proc_on.stdout
    # 既有 stdout 行不回归（新行只增不改）
    for line in proc_off.stdout.splitlines():
        if not line.startswith("高阶通道:"):
            assert line in proc_on.stdout.splitlines()


# ---------------------------------------------------------------- 语料重放（M10，坑123/126）

CORPUS_HASHES = {
    "relation_net.json": "b128afafe3e349f5937f7d8f0dd6fdf122309b56fd2d299ca6a5041ae2d1e3e0",
    "units.json": "9a8b5176a73ee614079669065ccb1c255ba12419a60e613e9b58bc23a061a95e",
    "oracles_harvested.json": "82fa1e91293af45cd90980d3a5978ad494e918c169da4c36c708068fead367f7",
}


def test_m10_corpus_replay_h1_zero_and_verdicts_unchanged():
    docs = {}
    for name, want in CORPUS_HASHES.items():
        data = (PILOT / name).read_bytes()
        got = hashlib.sha256(data).hexdigest()
        assert got == want, f"语料快照漂移: {name} {got[:16]} ≠ 钉值 {want[:16]} —— 须重裁定本守卫（坑126）"
        docs[name] = json.loads(data)
    net, units = docs["relation_net.json"], docs["units.json"]
    oracles = docs["oracles_harvested.json"].get("oracles", [])
    kw = dict(oracles=oracles, plumbing_types={"REPO_OP"}, business_levels={"L0_business_rule"})
    off = fc.run(net, units, **kw)
    on = fc.run(net, units, higher_order=True, **kw)
    # 做法68：字节一致基线必须是**改动前引擎**的冻结产物，不是同代码显式传 False。
    # 夹具由高阶通道引入前的引擎在钉值语料上生成（main 同序列化参数）。
    fixture = (PILOT / "tests" / "fixtures" / "frame_check_pre_higher_order.json").read_text(encoding="utf-8")
    assert json.dumps(off, ensure_ascii=False, indent=1) == fixture, "关闭态产物偏离改动前引擎基线"
    # 旧裁决冻结快照（2026-11-15 实测）
    assert off["counts"]["cycle_buckets"] == 14
    assert off["counts"]["cycle_status"] == {"consistent": 5, "inconsistent": 0, "unknown": 9}
    assert off["counts"]["cycle_obstructions"] == 0
    assert off["counts"]["constraint_section_buckets"] == 5
    assert off["counts"]["buckets_firing_capable"] == 1
    assert off["counts"]["buckets_co_singleton_only"] == 4
    # 单因子隔离：开启后逐项不变
    assert on["cycles"] == off["cycles"]
    assert on["cycle_obstructions"] == off["cycle_obstructions"]
    assert on["candidates"] == off["candidates"]
    for k, v in off["counts"].items():
        assert on["counts"][k] == v
    # 语料 H¹ 实测：拓扑容量 0（27 个环空间维度全被同桶三角形填充），47 条 unknown 边 ⇒ unknown
    ho = on["higher_order"]
    assert ho["nerve"] == {"vertices": 41, "edges": 55, "triangles": 44}
    assert ho["cochain"] == {"rank_delta0": 28, "rank_delta1": 27, "h1_dimension": 0}
    assert ho["status"] == "unknown"
    assert ho["edges_known"] == 8 and ho["edges_unknown"] == 47
    assert ho["obstructions"] == [] and ho["obstructions_incremental"] == 0
    assert ho["known_edge_cycle_space_dimension"] == 2