"""足迹重叠 → frame rule 旁条件违反 → 涌现候选（通用引擎，零项目字面量）。

理论（同一现象的两种形式化）：
  1. 分离逻辑 frame rule（Reynolds/O'Hearn）：{P} C {Q} 只有在旁条件
     mod(C) ∩ fv(R) = ∅ 成立时才能组合成 {P*R} C {Q*R}。
     两细胞足迹相交且至少一方写 ⇒ 旁条件被违反 ⇒ 局部判定不可组合 ⇒ 涌现候选。
  2. 层上同调（Čech）：细胞 = 局部截面，共享足迹 = 重叠，细胞判定 = 截面值。
     重叠上判定不一致 ⇒ δ⁰ ≠ 0 ⇒ 粘合障碍。拿不到判定 ⇒ unknown，绝不 pass。

三态：足迹未知 → unknown（不许当成「不相交」）；读-读重叠 → 旁条件仍成立，不报候选。
足迹定义（按任务契约）：taint.source_fields ∪ taint.sinks ∪ WRITES/READS 边目标。
DERIVES 边不在契约内，未纳入 —— 这是已声明的覆盖缺口，不是遗漏。

两个算法归约（成本极低，算法优先于算力）：
  1. 倒排索引 resource → 细胞桶：只有同桶细胞才可能重叠，配对成本 O(n²) → O(Σ bucket²)，
     不相交的对根本不枚举（旁条件对它们平凡成立）。
  2. Čech 障碍用重叠图 1-骨架的环一致性（GF(2) 并查集奇偶），不算上同调群。
     成对精确；三元及以上环由奇偶矛盾检测；高阶 H¹ 类算不了，显式报为局限。

用法：
    python3 frame_check.py --net <关系网.json> --units <细胞.json> \
        [--findings <发现.json>] [--oracles <判据.json>] [--out <产物.json>]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import oracle_bridge

SCHEMA_VERSION = 1

_SETTER = re.compile(r"^(?:.*\.)?([A-Za-z_$][\w$]*)\.set([A-Z_$][\w$]*)$")
_TOKEN = re.compile(r"[A-Za-z_][\w]*")
_READ_LIKE = re.compile(r"#(find|exists|count|get)[A-Z_a-z$]*$")


def normalize_resource(raw: str) -> tuple[str, str, str | None] | None:
    """把足迹资源映射成 (kind, owner, field)，仅用于挂判据/发现，不参与相交判定。"""
    if raw.startswith("table:"):
        return ("table", raw[len("table:"):], None)
    if raw.startswith("field:"):
        rest = raw[len("field:"):]
        if "." in rest:
            owner, fld = rest.split(".", 1)
            return ("field", owner, fld)
        return None
    m = _SETTER.match(raw)
    if m:
        prop = m.group(2)
        return ("field", m.group(1), prop[0].lower() + prop[1:])
    parts = raw.rsplit(".", 2)
    if len(parts) == 3 and parts[1] and parts[2]:
        return ("field", parts[1], parts[2])
    return None


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass
class Footprint:
    cell: str
    known: bool
    node_type: str = ""
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    may_writes: set[str] = field(default_factory=set)
    definite_writes: set[str] = field(default_factory=set)
    provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def all(self) -> set[str]:
        return self.reads | self.writes | self.may_writes


def build_footprints(net: dict[str, Any], units: dict[str, Any]) -> tuple[dict[str, Footprint], dict[str, Any]]:
    nodes = net.get("nodes") or {}
    out_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in net.get("edges") or []:
        out_edges[e.get("from", "")].append(e)

    records = [u for u in (units.get("units") or []) if isinstance(u, dict) and u.get("unit_key")]
    unit_keys = [u["unit_key"] for u in records]
    method_nodes = {k for k in nodes if ":" not in k}
    cells = sorted(set(unit_keys) | method_nodes)
    graph_only = sorted(method_nodes - set(unit_keys))

    fps: dict[str, Footprint] = {}
    for c in cells:
        node = nodes.get(c)
        fp = Footprint(cell=c, known=False, node_type=(node or {}).get("type", ""))
        if node is not None:
            taint = node.get("taint")
            if isinstance(taint, dict):
                fp.known = True
                for f in taint.get("source_fields") or []:
                    fp.reads.add(f)
                    fp.provenance.setdefault(f, []).append({"source": "taint.source_fields"})
                for s in taint.get("sinks") or []:
                    fp.writes.add(s)
                    fp.definite_writes.add(s)
                    fp.provenance.setdefault(s, []).append({"source": "taint.sinks"})
            for e in out_edges.get(c, []):
                access = e.get("access")
                if e.get("type") == "WRITES":
                    fp.known = True
                    # 写足迹一律保留（不得丢弃）。三态两个方向都守：
                    # access=unknown → may-write（不得洗成确定写，V7）；
                    # 确定写证据独立记入 definite_writes，may-write 不得降级已有确定证据（V10 反向红线）。
                    fp.writes.add(e["to"])
                    if access == "unknown":
                        fp.may_writes.add(e["to"])
                    else:
                        fp.definite_writes.add(e["to"])
                    fp.provenance.setdefault(e["to"], []).append(
                        {"source": "WRITES", "to": e["to"], "access": access or "write"})
                elif e.get("type") == "READS":
                    fp.known = True
                    fp.reads.add(e["to"])
                    fp.provenance.setdefault(e["to"], []).append(
                        {"source": "READS", "to": e["to"], "access": access or "read"})
        fps[c] = fp

    quirks = {
        "unit_records": len(records),
        "unit_keys_unique": len(set(unit_keys)),
        "duplicate_unit_keys": sorted({k for k in unit_keys if unit_keys.count(k) > 1}),
        "graph_only_cells": graph_only,
        "cells_total": len(cells),
    }
    return fps, quirks


def cell_domain(net: dict[str, Any], cells: list[str]) -> tuple[set[str], bool]:
    """按 net 声明的建模域分出**域外**细胞。返回 (域外集合, 是否声明了域)。

    为什么需要它：net 是方法层可执行图（只给 *Controller/*Service/*Repository 建点），
    而 units 含类层/文件层/dto/entity 等细胞。把它们一律算成「未知足迹」会虚增待查队列，
    并掩盖真正的域内缺口。域外是 not_applicable（本图不建模），域内没证据才是 unknown。

    两条保守纪律：
    · **未声明域 → 不敢分类**，全部当域内（unknown 不许被洗成 not_applicable）；
    · net 里**真有节点**的细胞一律域内（图确实建模了它，声明漂移也不能把它推出去）；
      键形态+后缀只用来判「该被建模却缺节点」的细胞 —— 那是域内 unknown，不是域外。
    """
    dom = net.get("modeling_domain") or {}
    suffixes = dom.get("class_suffixes") or []
    if not suffixes or dom.get("cell_key_form") != "FQCN#method":
        return set(), False
    nodes = net.get("nodes") or {}
    out: set[str] = set()
    for c in cells:
        if c in nodes:
            continue
        if "#" in c and ":" not in c:
            simple = c.split("#")[0].rsplit(".", 1)[-1]
            if any(simple.endswith(s) for s in suffixes):
                continue
        out.add(c)
    return out, True


def attribute_findings(findings: list[dict[str, Any]], nodes: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    by_simple: dict[str, list[str]] = defaultdict(list)
    by_endpoint: dict[str, list[str]] = defaultdict(list)
    for k, v in nodes.items():
        if v.get("simple") and v.get("method"):
            by_simple[f"{v['simple']}.{v['method']}"].append(k)
        if v.get("endpoint"):
            by_endpoint[v["endpoint"]].append(k)

    by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attributed = 0
    for f in findings:
        loc = f.get("location") or {}
        sym = (loc.get("symbol") or "").split(" / ")[0].strip()
        cell = None
        for key, table in ((sym, by_simple), (loc.get("endpoint") or "", by_endpoint), (sym, by_endpoint)):
            hits = table.get(key) or []
            if len(hits) == 1:
                cell = hits[0]
                break
        if cell is not None:
            by_cell[cell].append(f)
            attributed += 1
    return by_cell, attributed


def _finding_mentions(f: dict[str, Any], ident: tuple[str, str, str | None] | None) -> bool:
    if ident is None:
        return False
    kind, owner, fld = ident
    text = " ".join(str(f.get(k) or "") for k in ("claim", "root_cause_key", "id"))
    text += " " + " ".join(f.get("evidence_refs") or [])
    # 大小写归一化，但仍是精确 token 匹配（词边界），不退化为子串（V2 的 13/42 误报教训）。
    tokens = {t.lower() for t in _TOKEN.findall(text)}
    if kind == "table":
        return owner.lower() in tokens
    if not fld:
        return False
    return fld.lower() in tokens or _snake(fld).lower() in tokens


def _matching_oracles(oracles: list[dict[str, Any]], ident: tuple[str, str, str | None] | None,
                      entity_meta: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if ident is None:
        return []
    kind, owner, fld = ident
    hits = []
    for o in oracles:
        t = o.get("target") or {}
        if kind == "table" and t.get("table") == owner and (fld is None or t.get("field") in (None, fld)):
            hits.append(o)
        elif kind == "field":
            if t.get("entity") == owner and (fld is None or t.get("field") == fld):
                hits.append(o)
            elif t.get("table") and entity_meta:
                # 表级判据经 @Table 权威映射挂到**正确**实体字段（V10：守卫取 table 作实体来源）。
                meta = entity_meta.get(owner) or {}
                res_table = meta.get("table")
                res_col = (meta.get("columns") or {}).get(fld, _snake(fld))
                if res_table and t.get("table") == res_table and t.get("field") in (res_col, fld, _snake(fld)):
                    hits.append(o)
            elif not t.get("entity") and not t.get("table") and fld and t.get("field") in (fld, _snake(fld)):
                # 真正全局裸字段判据（如 L0 PRD）才按字段名 join；DDL 源必须带 table 经映射挂载，
                # 不得借裸字段把表级约束挂到无关实体（V10 跨实体假障碍）。
                if (o.get("source") or {}).get("kind") != "ddl":
                    hits.append(o)
    return hits


def _section_value(findings: list[dict[str, Any]], ident: tuple[str, str, str | None] | None) -> frozenset[str]:
    """细胞在重叠资源上的**违反级**局部截面值 = verified 缺陷 finding 的非空 root_cause_key。
    kind=positive/observation 不是违反（verified 只证观测可信，不证方向；正面结论混入
    会让两细胞因共享正面根因被判「对违反的看法一致」——坑120）。kind 缺失按遗留违反处理。
    没有可比较的判定值（空集）→ 后续 δ⁰ 判 unknown，绝不因「两边都有 finding」就判一致。"""
    return frozenset(f["root_cause_key"] for f in findings
                     if f.get("ground_truth") == "verified" and f.get("root_cause_key")
                     and f.get("kind") not in ("positive", "observation")
                     and _finding_mentions(f, ident))


def _satisfaction_section_value(findings: list[dict[str, Any]], ident: tuple[str, str, str | None] | None) -> frozenset[str]:
    """**满足级**局部截面值（正面结论通道）= verified positive finding 的非空 root_cause_key。
    与违反集分开取值、不丢弃；默认 δ⁰ 不消费它（opt-in 满足级通道才消费，绝不洗成 consistent）。
    observation 既非违反也非满足，两通道都不进。"""
    return frozenset(f["root_cause_key"] for f in findings
                     if f.get("ground_truth") == "verified" and f.get("root_cause_key")
                     and f.get("kind") == "positive" and _finding_mentions(f, ident))


def _guard_facts(capability: dict[str, Any] | None, oracles: list[dict[str, Any]],
                 entity_meta: dict[str, dict[str, Any]] | None) -> tuple[dict[tuple[str, tuple], list[dict[str, Any]]], dict[str, int]]:
    """从 capability_map 产物提取**满足级**证据：(细胞, 资源ident) → guard 证明列表。

    只认 enforcement.kind=guard（参数级绑定+throw确认+界蕴含，可证明的执行）。
    unknown 绝不进入（没找到 ≠ 未实现，更 ≠ 满足）。每条 guard 必须：
      · 对应判据在供给的 oracles 里且 confirmation=confirmed（oracle 门禁纪律）；
      · 能解析出实体作用域（target.entity/schema，或 table 经 @Table 权威映射）——
        解析不出 → 计入 unresolved，不猜；
      · guard 字段与判据 assert.field 对得上（同一性质，不是同字段就算）。
    输入缺失 → 空 facts + 调用方报 INCOMPLETE，绝不静默当成「无 guard」。"""
    stats = {"guard_edges_seen": 0, "facts_used": 0, "excluded_not_guard": 0,
             "excluded_oracle_absent": 0, "excluded_not_confirmed": 0,
             "excluded_entity_unresolved": 0, "excluded_field_mismatch": 0}
    facts: dict[tuple[str, tuple], list[dict[str, Any]]] = {}
    if not capability:
        return facts, stats
    by_id = {o.get("id"): o for o in oracles}
    for e in capability.get("implemented_by") or []:
        enf = e.get("enforcement") or {}
        if enf.get("kind") != "guard" or e.get("resolution") != "field_write":
            stats["excluded_not_guard"] += 1
            continue
        stats["guard_edges_seen"] += 1
        o = by_id.get(e.get("oracle_id"))
        if o is None:
            stats["excluded_oracle_absent"] += 1
            continue
        if o.get("confirmation") != "confirmed":
            stats["excluded_not_confirmed"] += 1
            continue
        t = o.get("target") or {}
        entity = t.get("entity") or t.get("schema")
        if not entity and t.get("table") and entity_meta:
            hits = [ent for ent, m in entity_meta.items() if m.get("table") == t["table"]]
            if len(hits) == 1:
                entity = hits[0]
        if not entity:
            stats["excluded_entity_unresolved"] += 1
            continue
        fld = enf.get("field")
        afld = (o.get("assert") or {}).get("field")
        if not fld or not afld or (_snake(fld) != afld and fld != afld):
            stats["excluded_field_mismatch"] += 1
            continue
        ident = ("field", entity, fld)
        facts.setdefault((e.get("unit_key"), ident), []).append(
            {"oracle_id": e["oracle_id"], "op": enf.get("op"), "value": enf.get("value"),
             "param": enf.get("param"), "reference": enf.get("reference")})
        stats["facts_used"] += 1
    return facts, stats


def _satisfaction_value(guard_facts: dict[tuple[str, tuple], list[dict[str, Any]]] | None,
                        cell: str | None, ident: tuple[str, str, str | None] | None) -> frozenset[str]:
    """满足级截面值（guard 源）= 该细胞在该资源上**可证明执行**的判据 id 集合。只有正面证据。"""
    if not guard_facts or cell is None or ident is None:
        return frozenset()
    return frozenset(g["oracle_id"] for g in guard_facts.get((cell, ident), []))


def _finding_tie_ids(f: dict[str, Any], oracle_ids: frozenset[str]) -> frozenset[str]:
    """finding→判据连接的**唯一实现**：oracle_id 字段或 evidence_refs 元素与判据 id **精确相等**。
    计数面与判定面共用（V20 第三轮 V4：两处实现同一语义会一精一粗漂移，假计数）。"""
    ties = f.get("oracle_id") or []
    if isinstance(ties, str):
        ties = [ties]
    return frozenset(t for t in list(ties) + list(f.get("evidence_refs") or []) if t in oracle_ids)


def _finding_ties(f: dict[str, Any], oid: str, oracle_ids: frozenset[str]) -> bool:
    return oid in _finding_tie_ids(f, oracle_ids)


def _tied_oracle_ids(findings: list[dict[str, Any]], ident: tuple[str, str, str | None] | None,
                     oracle_ids: frozenset[str], kinds: tuple[str, ...] | None) -> frozenset[str]:
    """可归因到判据的 finding 集合：verified + 非空 rck + 提及资源 + kind 过滤 +
    **显式判据连接**（finding.oracle_id 或 evidence_refs 恰含判据 id）。
    kinds=None 收 defect 及遗留无 kind；kinds=("positive",) 只收正面结论。
    没有连接 → 空集：同资源的违反与满足未必关于同一性质，拿不准保 unknown（坑121）。"""
    out: set[str] = set()
    for f in findings:
        if f.get("ground_truth") != "verified" or not f.get("root_cause_key"):
            continue
        if kinds is None:
            if f.get("kind") in ("positive", "observation"):
                continue
        elif f.get("kind") not in kinds:
            continue
        if not _finding_mentions(f, ident):
            continue
        out.update(_finding_tie_ids(f, oracle_ids))
    return frozenset(out)


def _violated_oracle_ids(findings: list[dict[str, Any]], ident: tuple[str, str, str | None] | None,
                         oracle_ids: frozenset[str]) -> frozenset[str]:
    return _tied_oracle_ids(findings, ident, oracle_ids, None)


def _satisfied_oracle_ids(findings: list[dict[str, Any]], ident: tuple[str, str, str | None] | None,
                          oracle_ids: frozenset[str]) -> frozenset[str]:
    return _tied_oracle_ids(findings, ident, oracle_ids, ("positive",))


def _delta0(a_findings: list[dict[str, Any]], b_findings: list[dict[str, Any]],
            ident: tuple[str, str, str | None] | None = None,
            guard_facts: dict[tuple[str, tuple], list[dict[str, Any]]] | None = None,
            a_cell: str | None = None, b_cell: str | None = None,
            oracle_ids: frozenset[str] = frozenset(), satisfaction: bool = False) -> str:
    # Čech 粘合要求局部截面在重叠上取值相同/相容，不是「两边都有东西」。
    # 违反级通道（默认）：两侧指纹非空且相等 → consistent。
    # 满足级通道（opt-in，satisfaction=True）：一侧有可归因到判据 O 的 verified 违反、
    # 另一侧对同一资源可证明执行 O（guard）或有 O 的 verified 正面结论 → inconsistent。
    # 连接键是 (资源, 判据)，不是裸资源：同字段不同性质的违反与满足可同时为真（坑121）。
    va = _section_value(a_findings, ident)
    vb = _section_value(b_findings, ident)
    if va and vb and va == vb:
        return "consistent"
    if satisfaction:
        oa = _violated_oracle_ids(a_findings, ident, oracle_ids)
        ob = _violated_oracle_ids(b_findings, ident, oracle_ids)
        sb = _satisfaction_value(guard_facts, b_cell, ident) | _satisfied_oracle_ids(b_findings, ident, oracle_ids)
        sa = _satisfaction_value(guard_facts, a_cell, ident) | _satisfied_oracle_ids(a_findings, ident, oracle_ids)
        if oa and not vb and (oa & sb):
            return "inconsistent"
        if ob and not va and (ob & sa):
            return "inconsistent"
    return "unknown"


def build_index(fps: dict[str, Footprint]) -> dict[str, list[str]]:
    """倒排索引 resource → 触碰它的细胞。只有同桶细胞才可能重叠，不相交的对根本不枚举。"""
    index: dict[str, list[str]] = defaultdict(list)
    for c, fp in fps.items():
        if fp.known:
            for r in fp.all:
                index[r].append(c)
    return {r: sorted(cs) for r, cs in index.items()}


def _write_suspect(fp: Footprint, resource: str) -> bool:
    # 上游把 find*/exists* 查询标成 WRITES 的边：证据可疑，标记但不丢弃（不猜业务意图）。
    if not _READ_LIKE.search(fp.cell):
        return False
    return any(p.get("source") == "WRITES" for p in fp.provenance.get(resource, []))


def find_candidates(
    fps: dict[str, Footprint],
    findings_by_cell: dict[str, list[dict[str, Any]]],
    oracles: list[dict[str, Any]],
    index: dict[str, list[str]] | None = None,
    plumbing_types: set[str] | None = None,
    business_levels: set[str] | None = None,
    entity_meta: dict[str, dict[str, Any]] | None = None,
    guard_facts: dict[tuple[str, tuple], list[dict[str, Any]]] | None = None,
    oracle_ids: frozenset[str] = frozenset(),
    satisfaction: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if index is None:
        index = build_index(fps)
    plumbing_types = plumbing_types or set()
    business_levels = business_levels or set()
    known = sorted(c for c, f in fps.items() if f.known)
    pair_resources: dict[tuple[str, str], set[str]] = defaultdict(set)
    comparisons = 0
    for r, cells in index.items():
        for a, b in combinations(cells, 2):
            comparisons += 1
            pair_resources[(a, b)].add(r)

    candidates: list[dict[str, Any]] = []
    readonly = 0
    for (a, b), overlap in sorted(pair_resources.items()):
        fa, fb = fps[a], fps[b]
        definite_written: set[str] = set()
        may_written: set[str] = set()
        for r in overlap:
            a_dw, b_dw = r in fa.definite_writes, r in fb.definite_writes
            a_dt, b_dt = a_dw or r in fa.reads, b_dw or r in fb.reads
            a_mw, b_mw = r in fa.may_writes, r in fb.may_writes
            # 确定违反 = 一方确定写 × 另一方确定触碰。may-write 触碰不确定 → 不得算确定违反。
            if (a_dw and b_dt) or (b_dw and a_dt):
                definite_written.add(r)
            elif ((a_dw or a_mw) and (b_dt or b_mw)) or ((b_dw or b_mw) and (a_dt or a_mw)):
                may_written.add(r)
        if not definite_written and not may_written:
            readonly += 1
            continue
        resources = []
        for r in sorted(definite_written | may_written):
            ident = normalize_resource(r)
            fa_f = [f for f in findings_by_cell.get(a, []) if _finding_mentions(f, ident)]
            fb_f = [f for f in findings_by_cell.get(b, []) if _finding_mentions(f, ident)]
            matched = _matching_oracles(oracles, ident, entity_meta)
            a_w, b_w = r in fa.definite_writes, r in fb.definite_writes
            resources.append({
                "resource": r,
                "identity": list(ident) if ident else None,
                "a_writes": a_w, "b_writes": b_w,
                # may-write 标志保留证据可见性（有 unknown 边即 True）；确定写优先体现在
                # write_strength 与 definite_writes：有确定证据则 write_strength=definite，不降级。
                "a_may_write": r in fa.may_writes, "b_may_write": r in fb.may_writes,
                "a_reads": r in fa.reads, "b_reads": r in fb.reads,
                "write_strength": "definite" if r in definite_written else "may_write_unknown",
                "a_write_suspect": _write_suspect(fa, r), "b_write_suspect": _write_suspect(fb, r),
                "provenance": {a: fa.provenance.get(r, []), b: fb.provenance.get(r, [])},
                "oracles": [o.get("id", "") for o in matched],
                "business_oracles": [o.get("id", "") for o in matched if o.get("level") in business_levels],
                "findings": {a: [f.get("id") for f in fa_f], b: [f.get("id") for f in fb_f]},
                "findings_positive": {
                    a: [f.get("id") for f in fa_f if f.get("kind") == "positive" and f.get("ground_truth") == "verified"],
                    b: [f.get("id") for f in fb_f if f.get("kind") == "positive" and f.get("ground_truth") == "verified"]},
                # 满足级取值通道（与违反集 _section_value 对称）：正面结论指纹，可见但不驱动默认裁决；
                # opt-in 矛盾判定只消费 oracle 连接的满足证据（_satisfied_oracle_ids/guard）。
                "satisfaction_section": {a: sorted(_satisfaction_section_value(fa_f, ident)),
                                         b: sorted(_satisfaction_section_value(fb_f, ident))},
                "delta0": _delta0(fa_f, fb_f, ident, guard_facts, a, b, oracle_ids, satisfaction),
            })
            if resources[-1]["delta0"] == "inconsistent":
                oa = _violated_oracle_ids(fa_f, ident, oracle_ids)
                sb = _satisfaction_value(guard_facts, b, ident) | _satisfied_oracle_ids(fb_f, ident, oracle_ids)
                if oa & sb:
                    vcell, gcell, vf, sf, tied = a, b, fa_f, fb_f, oa & sb
                else:
                    ob = _violated_oracle_ids(fb_f, ident, oracle_ids)
                    sa = _satisfaction_value(guard_facts, a, ident) | _satisfied_oracle_ids(fa_f, ident, oracle_ids)
                    vcell, gcell, vf, sf, tied = b, a, fb_f, fa_f, ob & sa
                oid = sorted(tied)[0]
                gproof = next((g for g in (guard_facts or {}).get((gcell, ident), []) if g["oracle_id"] == oid), None)
                resources[-1]["delta0_evidence"] = {
                    "oracle_id": oid, "violation_cell": vcell, "satisfaction_cell": gcell,
                    "satisfaction_source": "guard" if gproof else "positive_finding",
                    "guard_reference": gproof["reference"] if gproof else None,
                    "violation_findings": [f.get("id") for f in vf if _finding_ties(f, oid, oracle_ids)],
                    "satisfaction_findings": [f.get("id") for f in sf if f.get("kind") == "positive" and _finding_ties(f, oid, oracle_ids)],
                }
        if any(r["delta0"] == "inconsistent" for r in resources):
            delta0 = "inconsistent"
        elif all(r["delta0"] == "consistent" for r in resources):
            delta0 = "consistent"
        else:
            delta0 = "unknown"
        suspect = all((not r["a_writes"] or r["a_write_suspect"]) and (not r["b_writes"] or r["b_write_suspect"])
                      for r in resources)
        # frame_violation 只从确定写×确定触碰得出；may-write 不得标确定违反（三态红线）。
        violations = []
        if any(r["a_writes"] and r["write_strength"] == "definite" for r in resources):
            violations.append("mod(A) ∩ fp(B) ≠ ∅")
        if any(r["b_writes"] and r["write_strength"] == "definite" for r in resources):
            violations.append("mod(B) ∩ fp(A) ≠ ∅")
        # 归约 3：同表 CRUD 噪音 + 业务级要求。repository plumbing 不是关于业务不变式的独立局部推理。
        kinds = {normalize_resource(r)[0] for r in overlap if normalize_resource(r)}
        crud_noise = (fa.node_type in plumbing_types and fb.node_type in plumbing_types and kinds == {"table"})
        has_finding = any(r["findings"][a] or r["findings"][b] for r in resources)
        has_business_oracle = any(r["business_oracles"] for r in resources)
        business_cell = fa.node_type not in plumbing_types or fb.node_type not in plumbing_types
        candidates.append({
            "id": f"FC-{len(candidates) + 1:04d}",
            "cells": [a, b],
            "cell_types": [fa.node_type, fb.node_type],
            "overlap_all": sorted(overlap),
            "overlap_written": sorted(definite_written),
            "overlap_may_written": sorted(may_written),
            "overlap_size": len(overlap),
            "resources": resources,
            "delta0": delta0,
            "suspect_write_only": suspect,
            "evidence_strength": "definite" if definite_written else "may_write_unknown",
            "frame_violation": violations,
            "crud_noise": crud_noise,
            "business_meaningful": bool(business_cell or has_business_oracle or has_finding),
        })
    for c in candidates:
        c["survives_filter"] = (not c["crud_noise"]) and c["business_meaningful"]
    disjoint = len(known) * (len(known) - 1) // 2 - len(pair_resources)
    dropped_crud = sum(1 for c in candidates if c["crud_noise"])
    dropped_no_business = sum(1 for c in candidates if not c["crud_noise"] and not c["business_meaningful"])
    stats = {"pairs_disjoint_empty": disjoint, "pairs_disjoint_readonly": readonly,
             "bucket_comparisons": comparisons, "overlapping_pairs": len(pair_resources),
             "candidates_before_filter": len(candidates),
             "dropped_crud_noise": dropped_crud, "dropped_no_business": dropped_no_business,
             "candidates_after_filter": sum(1 for c in candidates if c["survives_filter"])}
    return candidates, stats


def _pair_label_from_findings(a: str, b: str, r: str, findings_by_cell: dict[str, list[dict[str, Any]]],
                              guard_facts: dict[tuple[str, tuple], list[dict[str, Any]]] | None = None,
                              oracle_ids: frozenset[str] = frozenset(), satisfaction: bool = False) -> str:
    ident = normalize_resource(r)
    return _delta0(findings_by_cell.get(a, []), findings_by_cell.get(b, []), ident,
                   guard_facts, a, b, oracle_ids, satisfaction)


# ---------------------------------------------------------------- 多值截面：约束集代数
# 约束集 = 区间 ('iv', lo, lo_inc, hi, hi_inc)（None=无界）、有限域 ('set', frozenset)、
# 或余单例域 ('nonnull',)=V\{null} / ('notblank',)=V\{null,""}（not_null/not_blank）。
# 空集统一表示 ('set', frozenset())。两约束集不相交 = 重叠上不一致 = 非零 Čech 障碍。
# 约束来自已采集判据（PRD/DDL/注解），经桥映射到封闭原语后解释为域 —— 不需外部测量。
# 余单例域可补（抽象域里单例的补仍是合法域元素），但无封闭原语强制 null/""，故它单独永不致不相交。
# unique(跨行)/default(条件)/references(动态键集)/field_*_field(二元关系) 不是单值域约束 → 见局限。

_CO_RANK = {"nonnull": 1, "notblank": 2}


def _prim_constraint(prim: dict[str, Any]) -> tuple | None:
    t = prim.get("type")
    if t == "field_gte_const":
        return ("iv", prim["value"], True, None, True)
    if t == "field_gt_const":
        return ("iv", prim["value"], False, None, True)
    if t == "field_lte_const":
        return ("iv", None, True, prim["value"], True)
    if t == "field_in_set":
        return ("set", frozenset(prim["value"]))
    if t == "field_not_null":
        return ("nonnull",)
    if t == "field_not_blank":
        return ("notblank",)
    return None


def _oracle_constraint(assertion: dict[str, Any]) -> tuple | None:
    prims, reason = oracle_bridge.map_assertion(assertion)
    if reason or not prims:
        return None
    g = None
    for p in prims:
        c = _prim_constraint(p)
        if c is None:
            return None  # 含不可解释原语（跨行/关系比较）→ 整体 unknown，不猜
        g = _intersect(g, c)
    return g


def _filter_set_by_co(s: tuple, co: str) -> tuple:
    # 余单例域定义：nonnull=V\{null}，notblank=V\{null,""}。两者都必须排 null（坑45）。
    vals = {v for v in s[1] if v is not None}
    if co == "notblank":
        vals = {v for v in vals if v != ""}
    return ("set", frozenset(vals))


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _intersect(c1: tuple | None, c2: tuple | None) -> tuple | None:
    if c1 is None:
        return c2
    if c2 is None:
        return c1
    k1, k2 = c1[0], c2[0]
    if (k1 == "set" and not c1[1]) or (k2 == "set" and not c2[1]):
        return ("set", frozenset())
    if k1 in _CO_RANK and k2 in _CO_RANK:
        return ("notblank",) if _CO_RANK[k1] >= 2 or _CO_RANK[k2] >= 2 else ("nonnull",)
    if k1 in _CO_RANK and k2 == "iv":
        return c2  # 数值区间的值非 null/非空串
    if k2 in _CO_RANK and k1 == "iv":
        return c1
    if k1 in _CO_RANK and k2 == "set":
        return _filter_set_by_co(c2, k1)
    if k2 in _CO_RANK and k1 == "set":
        return _filter_set_by_co(c1, k2)
    if k1 == "set" and k2 == "set":
        return ("set", c1[1] & c2[1])
    if k1 == "iv" and k2 == "iv":
        bounds = [b for b in (c1[1], c1[3], c2[1], c2[3]) if b is not None]
        if any(not _is_num(b) for b in bounds):
            return None  # 非数值区间（如字符串界）不可比 → unknown，不崩不猜（坑45）
        if c1[1] is None:
            lo, li = c2[1], c2[2]
        elif c2[1] is None:
            lo, li = c1[1], c1[2]
        elif c1[1] > c2[1]:
            lo, li = c1[1], c1[2]
        elif c2[1] > c1[1]:
            lo, li = c2[1], c2[2]
        else:
            lo, li = c1[1], c1[2] and c2[2]
        if c1[3] is None:
            hi, hj = c2[3], c2[4]
        elif c2[3] is None:
            hi, hj = c1[3], c1[4]
        elif c1[3] < c2[3]:
            hi, hj = c1[3], c1[4]
        elif c2[3] < c1[3]:
            hi, hj = c2[3], c2[4]
        else:
            hi, hj = c1[3], c1[4] and c2[4]
        if lo is not None and hi is not None and (lo > hi or (lo == hi and not (li and hj))):
            return ("set", frozenset())
        return ("iv", lo, li, hi, hj)
    s, iv = (c1, c2) if k1 == "set" else (c2, c1)
    lo, hi = iv[1], iv[3]
    if (lo is not None and not _is_num(lo)) or (hi is not None and not _is_num(hi)):
        return None  # 非数值区间 × 有限域不可比 → unknown（坑45）
    out = set()
    for v in s[1]:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue  # 字符串/布尔值不在数值区间内
        if (lo is None or v > lo or (v == lo and iv[2])) and (hi is None or v < hi or (v == hi and iv[4])):
            out.add(v)
    return ("set", frozenset(out))


def _is_empty(c: tuple | None) -> bool:
    return c is not None and c[0] == "set" and not c[1]


def _cell_section(fp: Footprint, resource: str, oracles: list[dict[str, Any]],
                  entity_meta: dict[str, dict[str, Any]] | None = None) -> tuple | None:
    """细胞在重叠资源上的局部截面 = 它对该资源施加的约束集。
    仅字段级资源可算（表级无字段粒度，聚合不同列约束会造假障碍 → unknown）。
    跨实体守卫按**资源的实体**判：entity/schema 须等于资源实体；表级判据经 @Table 映射
    只挂到对应实体（entity_meta）；真正全局裸字段判据双方都挂。不猜 DTO→实体投影（V10）。"""
    ident = normalize_resource(resource)
    if ident is None or ident[0] != "field":
        return None
    resource_entity = ident[1]
    g = None
    for x in _matching_oracles(oracles, ident, entity_meta):
        t = x.get("target") or {}
        owner = t.get("entity") or t.get("schema")
        if owner is not None and owner != resource_entity:
            continue
        c = _oracle_constraint(x.get("assert") or {})
        if c is not None:
            g = _intersect(g, c)
    return g


def _parity_check(cells: list[str], edges: dict[tuple[str, str], str]) -> tuple[str, tuple[str, str] | None, int, int]:
    """GF(2) 环一致性：consistent=同色(0)，inconsistent=异色(1)。
    并查集带奇偶；已知边推出矛盾 = 环上判定无法同时满足 = 非零障碍。O(m·α(m))。"""
    parent = {c: c for c in cells}
    par = {c: 0 for c in cells}

    def find(x: str) -> tuple[str, int]:
        if parent[x] == x:
            return x, 0
        root, p = find(parent[x])
        return root, p ^ par[x]

    def union(a: str, b: str, rel: int) -> bool:
        ra, pa = find(a)
        rb, pb = find(b)
        if ra == rb:
            return (pa ^ pb) == rel
        parent[rb] = ra
        par[rb] = pa ^ pb ^ rel
        return True

    known = unknown = 0
    conflict = None
    for (a, b), label in edges.items():
        if label == "unknown":
            unknown += 1
            continue
        known += 1
        if not union(a, b, 0 if label == "consistent" else 1):
            conflict = (a, b)
            break
    if conflict is not None:
        status = "inconsistent"
    elif known == 0 or unknown > 0:
        status = "unknown"
    else:
        status = "consistent"
    return status, conflict, known, unknown


def cycle_consistency(
    fps: dict[str, Footprint],
    index: dict[str, list[str]],
    findings_by_cell: dict[str, list[dict[str, Any]]],
    judgments: dict[tuple[str, str, str], str] | None = None,
    oracles: list[dict[str, Any]] | None = None,
    entity_meta: dict[str, dict[str, Any]] | None = None,
    guard_facts: dict[tuple[str, tuple], list[dict[str, Any]]] | None = None,
    oracle_ids: frozenset[str] = frozenset(),
    satisfaction: bool = False,
) -> list[dict[str, Any]]:
    """每个资源桶是一个 clique（共享该资源的细胞两两重叠）。
    多值截面优先：细胞截面=约束集，全桶交集为空→inconsistent（非零障碍），非空→consistent，
    任一方缺约束集→unknown（绝不默认 consistent）。约束集不可用时退回 judgments/二值 parity。
    成对不相交即障碍；有限域可现 Helly 型失败（成对交非空但全桶交空）。
    注意：对称挂载下两侧约束集构造上相同，consistent 检的是「资源全局约束集可满足」，
    不是「两细胞独立取值一致」（信息量限定，见做法18）。"""
    judgments = judgments or {}
    oracles = oracles or []
    reports = []
    for r, cells in sorted(index.items()):
        if len(cells) < 2:
            continue
        sections = {c: _cell_section(fps[c], r, oracles, entity_meta) for c in cells}
        have = [c for c in cells if sections[c] is not None]
        conflict = None
        global_empty = False
        if len(have) >= 2:
            for a, b in combinations(have, 2):
                if _is_empty(_intersect(sections[a], sections[b])):
                    conflict = (a, b)
                    break
        if conflict is not None:
            status, basis, known, unknown = "inconsistent", "constraint_set", 0, 0
        elif len(have) == len(cells):
            g = None
            for c in cells:
                g = _intersect(g, sections[c])
            global_empty = _is_empty(g)
            status = "inconsistent" if global_empty else "consistent"
            basis, known, unknown = "constraint_set", 0, 0
        else:
            edges = {}
            for a, b in combinations(cells, 2):
                key = (a, b, r)
                edges[(a, b)] = judgments[key] if key in judgments else _pair_label_from_findings(a, b, r, findings_by_cell, guard_facts, oracle_ids, satisfaction)
            status, conflict, known, unknown = _parity_check(cells, edges)
            basis = "judgments" if judgments else "binary"
        reports.append({
            "resource": r, "cells": cells, "status": status, "basis": basis,
            "conflict": list(conflict) if conflict else None,
            "global_intersection_empty": global_empty,
            "sections_present": len(have), "cells_with_sections": have,
            "domain_type": sections[have[0]][0] if have else None,
            "edges_known": known, "edges_unknown": unknown,
        })
    return reports


def overlap_graph_stats(index: dict[str, list[str]]) -> dict[str, int]:
    verts: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for cells in index.values():
        verts.update(cells)
        for a, b in combinations(cells, 2):
            pairs.add((a, b))
    parent = {v: v for v in verts}

    def find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    comps = len({find(v) for v in verts}) if verts else 0
    v, e = len(verts), len(pairs)
    return {"vertices": v, "edges": e, "components": comps, "cycle_space_dimension": e - v + comps}


def _variety_deficit(candidates: list[dict[str, Any]], findings: list[dict[str, Any]],
                     findings_by_cell: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Ashby 判据多样性赤字：finding 通道的粒度与候选资源通道的粒度对不上。
    显式量化两侧缺口，不因优化而隐藏。"""
    resources = sorted({r["resource"] for c in candidates for r in c["resources"]})
    idents = {r: normalize_resource(r) for r in resources}
    res_with_finding = [r for r in resources if any(_finding_mentions(f, idents[r]) for f in findings)]
    res_with_business = [r["resource"] for c in candidates for r in c["resources"] if r["business_oracles"]]
    cand_cells = {x for c in candidates for x in c["cells"]}
    findings_covering = {f.get("id") for f in findings
                         if any(_finding_mentions(f, idents[r]) for r in resources)}
    findings_on_cells = {f.get("id") for cell in cand_cells for f in findings_by_cell.get(cell, [])}
    forms: dict[str, int] = defaultdict(int)
    for f in findings:
        sym = (f.get("location") or {}).get("symbol") or ""
        if not sym:
            forms["empty"] += 1
        elif sym.startswith(("GET", "POST", "PUT", "PATCH", "DELETE")):
            forms["endpoint"] += 1
        elif "." in sym.split(" / ")[0]:
            forms["class_method"] += 1
        else:
            forms["other"] += 1
    return {
        "candidate_resources": len(resources),
        "resources_with_finding_coverage": len(res_with_finding),
        "resources_without_finding_coverage": len(resources) - len(res_with_finding),
        "resources_with_business_oracle": len(set(res_with_business)),
        "findings_total": len(findings),
        "findings_covering_any_candidate_resource": len(findings_covering),
        "findings_covering_no_candidate_resource": len(findings) - len(findings_covering),
        "findings_attributed_to_candidate_cells": len(findings_on_cells),
        "finding_symbol_granularity": dict(forms),
        "covered_resources": res_with_finding,
    }


def run(net: dict[str, Any], units: dict[str, Any], findings: list[dict[str, Any]] | None = None,
        oracles: list[dict[str, Any]] | None = None,
        judgments: dict[tuple[str, str, str], str] | None = None,
        plumbing_types: set[str] | None = None,
        business_levels: set[str] | None = None,
        entity_meta: dict[str, dict[str, Any]] | None = None,
        capability: dict[str, Any] | None = None,
        satisfaction: bool = False) -> dict[str, Any]:
    findings = findings or []
    oracles = oracles or []
    fps, quirks = build_footprints(net, units)
    findings_by_cell, attributed = attribute_findings(findings, net.get("nodes") or {})
    index = build_index(fps)
    # 统计计算与发布同门（V20：供 capability 不开开关时产物必须逐字节不变）。
    guard_facts, guard_stats = _guard_facts(capability if satisfaction else None, oracles, entity_meta)
    oracle_ids = frozenset(o.get("id") for o in oracles if o.get("id"))
    gf = guard_facts
    all_candidates, pair_stats = find_candidates(fps, findings_by_cell, oracles, index, plumbing_types, business_levels, entity_meta, gf, oracle_ids, satisfaction)
    candidates = [c for c in all_candidates if c["survives_filter"]]
    cycles = cycle_consistency(fps, index, findings_by_cell, judgments, oracles, entity_meta, gf, oracle_ids, satisfaction)
    graph = overlap_graph_stats(index)
    obstructions = [c for c in cycles if c["status"] == "inconsistent"]
    section_cells = sum(c["sections_present"] for c in cycles)  # 口径A：细胞×资源关联数
    section_unique_cells = len({cell for c in cycles for cell in c["cells_with_sections"]})  # 口径B：去重细胞数
    section_buckets = sum(1 for c in cycles if c["basis"] == "constraint_set")
    # 检测力（面向用户诚实性，坑64）：覆盖≠检测力。区间(iv)/有限域(set)可不相交→可 firing；
    # 余单例域(nonnull/notblank)无封闭原语强制 null/""→数学上永不 inconsistent→零检测力。
    _FIRING_DOMAINS = {"iv", "set"}
    _CO_SINGLETON_DOMAINS = {"nonnull", "notblank"}
    constraint_cycles = [c for c in cycles if c["basis"] == "constraint_set"]
    buckets_firing_capable = sum(1 for c in constraint_cycles if c["domain_type"] in _FIRING_DOMAINS)
    buckets_co_singleton_only = sum(1 for c in constraint_cycles if c["domain_type"] in _CO_SINGLETON_DOMAINS)
    # 守恒（坑3 同族）：约束桶必须被分类到 firing/co_singleton/other 之一，不得静默丢桶。
    buckets_other_domain = sum(1 for c in constraint_cycles
                               if c["domain_type"] not in _FIRING_DOMAINS | _CO_SINGLETON_DOMAINS)
    # 信息量限定：对称挂载下两侧约束集构造上相同，consistent 检的是「资源全局约束集可满足」，
    # 不是「两细胞独立取值一致」。统计 consistent 桶中两侧约束集逐一相等的比例。
    consistent_buckets = [c for c in cycles if c["status"] == "consistent" and c["basis"] == "constraint_set"]
    symmetric_consistent = 0
    for c in consistent_buckets:
        secs = [_cell_section(fps[cell], c["resource"], oracles, entity_meta) for cell in c["cells_with_sections"]]
        if len(secs) >= 2 and all(s == secs[0] for s in secs):
            symmetric_consistent += 1

    cells = list(fps)
    known = [c for c in cells if fps[c].known]
    out_domain, domain_declared = cell_domain(net, cells)
    # unknown 只指**域内**没拿到证据的细胞；域外单列 not_applicable，两者不许混为一谈。
    unknown = [c for c in cells if not fps[c].known and c not in out_domain]
    out_cells = [c for c in cells if c in out_domain]
    nonempty = [c for c in known if fps[c].all]
    empty_known = len(known) - len(nonempty)
    n = len(cells)
    pairs_total = n * (n - 1) // 2
    pairs_decidable = len(known) * (len(known) - 1) // 2
    pairs_unknown = len(nonempty) * len(unknown) + len(unknown) * (len(unknown) - 1) // 2
    pairs_disjoint_unknown_side = empty_known * len(unknown)
    # 守恒：四个桶恰好铺满 pairs_total。域外对单列，既不静默丢弃也不算成可判。
    pairs_not_applicable = (pairs_total - pairs_decidable - pairs_unknown
                            - pairs_disjoint_unknown_side)

    delta0_counts = {"consistent": 0, "unknown": 0, "inconsistent": 0}
    for c in candidates:
        delta0_counts[c["delta0"]] += 1
    # 满足级通道分解（坑50/111）：0 触发必须能归因 —— guard 事实数 / oracle 连接 finding 数并排报。
    tied_viol = sum(1 for fs in findings_by_cell.values() for f in fs
                    if f.get("ground_truth") == "verified" and f.get("root_cause_key")
                    and f.get("kind") not in ("positive", "observation")
                    and any(_finding_ties(f, oid, oracle_ids) for oid in oracle_ids))
    tied_pos = sum(1 for fs in findings_by_cell.values() for f in fs
                   if f.get("ground_truth") == "verified" and f.get("root_cause_key")
                   and f.get("kind") == "positive" and any(_finding_ties(f, oid, oracle_ids) for oid in oracle_ids))
    fired_resources = sum(1 for c in all_candidates for r in c["resources"] if r["delta0"] == "inconsistent")
    strength_counts = {"definite": 0, "may_write_unknown": 0}
    for c in candidates:
        strength_counts[c["evidence_strength"]] += 1
    may_write_edges = sum(1 for e in net.get("edges") or []
                          if e.get("type") == "WRITES" and e.get("access") == "unknown")
    suspect_candidates = sum(1 for c in candidates if c["suspect_write_only"])
    suspect_edges = sum(1 for e in net.get("edges") or []
                        if e.get("type") == "WRITES" and _READ_LIKE.search(e.get("from", "")))
    max_ov = max((c["overlap_size"] for c in candidates), default=0)
    max_pairs = [c["id"] for c in candidates if c["overlap_size"] == max_ov]
    cycle_status = {"consistent": 0, "inconsistent": 0, "unknown": 0}
    for c in cycles:
        cycle_status[c["status"]] += 1
    deficit = _variety_deficit(candidates, findings, findings_by_cell)
    # 缺口必须可见：参数缺失使证据通道静默报 0 是误导（坑25）。未供给 → INCOMPLETE，不是「无」。
    input_warnings: list[str] = []
    if not business_levels:
        deficit["business_oracle_channel"] = "INCOMPLETE"
        input_warnings.append("business_levels 未供给：business-oracle 通道 INCOMPLETE，报出的 0 是「未测量」不是「无业务判据」")
    else:
        deficit["business_oracle_channel"] = "ok"
    if not plumbing_types:
        input_warnings.append("plumbing_types 未供给：CRUD 噪音过滤未启用，候选含 repository×repository 同表对")
    if not domain_declared:
        input_warnings.append("net 未声明 modeling_domain：无法区分「域外不建模」与「域内未测」，"
                              "全部保守按 unknown 计（不许把 unknown 洗成 not_applicable）")
    if satisfaction and capability is None:
        input_warnings.append("满足级通道已启用但 capability 未供给：guard 证据源 INCOMPLETE"
                              "（oracle 连接的正面 finding 源仍可用）；inconsistent=0 是「未测量」不是「无矛盾」")

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "frame_check",
        "input_warnings": input_warnings,
        "counts": {
            "cells_total": n,
            "cells_footprint_known": len(known),
            "cells_footprint_nonempty": len(nonempty),
            "cells_footprint_unknown": len(unknown),
            # 三口径分报（域内已测 / 域内 unknown / 域外 not_applicable），三者之和 == cells_total
            "domain_declared": domain_declared,
            "cells_in_domain": n - len(out_cells),
            "cells_in_domain_measured": len(known),
            "cells_in_domain_unknown": len(unknown),
            "cells_out_of_domain": len(out_cells),
            "pairs_total": pairs_total,
            "pairs_decidable": pairs_decidable,
            "pairs_not_applicable": pairs_not_applicable,
            "pairs_candidate": len(candidates),
            "pairs_disjoint_empty": pair_stats["pairs_disjoint_empty"],
            "pairs_disjoint_readonly": pair_stats["pairs_disjoint_readonly"],
            "pairs_disjoint_unknown_side": pairs_disjoint_unknown_side,
            "pairs_unknown": pairs_unknown,
            "candidates_delta0": delta0_counts,
            "satisfaction_fired_candidates": delta0_counts["inconsistent"],
            "satisfaction_fired_resources": fired_resources,
            "satisfaction_tied_violation_findings": tied_viol,
            "satisfaction_tied_positive_findings": tied_pos,
            "candidates_evidence_strength": strength_counts,
            "may_write_edges": may_write_edges,
            "candidates_suspect_write_only": suspect_candidates,
            "max_overlap_size": max_ov,
            "max_overlap_candidates": max_pairs,
            "bucket_comparisons": pair_stats["bucket_comparisons"],
            "overlapping_pairs": pair_stats["overlapping_pairs"],
            "cycle_buckets": len(cycles),
            "cycle_status": cycle_status,
            "cycle_obstructions": len(obstructions),
            "constraint_section_cells": section_cells,
            "constraint_section_unique_cells": section_unique_cells,
            "constraint_section_buckets": section_buckets,
            "buckets_firing_capable": buckets_firing_capable,
            "buckets_co_singleton_only": buckets_co_singleton_only,
            "buckets_other_domain": buckets_other_domain,
            "consistent_buckets_symmetric": symmetric_consistent,
            "consistent_buckets_total": len(consistent_buckets),
            "candidates_before_filter": pair_stats["candidates_before_filter"],
            "dropped_crud_noise": pair_stats["dropped_crud_noise"],
            "dropped_no_business": pair_stats["dropped_no_business"],
        },
        "reduction": {
            "brute_force_pairs": pairs_decidable,
            "index_comparisons": pair_stats["bucket_comparisons"],
            "disjoint_pairs_never_enumerated": pair_stats["pairs_disjoint_empty"],
            "comparison_reduction_factor": round(pairs_decidable / pair_stats["bucket_comparisons"], 2) if pair_stats["bucket_comparisons"] else None,
            "crud_filter": {"before": pair_stats["candidates_before_filter"],
                            "dropped_crud_noise": pair_stats["dropped_crud_noise"],
                            "dropped_no_business": pair_stats["dropped_no_business"],
                            "after": len(candidates)},
        },
        "overlap_graph": graph,
        "ratios": {
            "candidate_over_decidable": round(len(candidates) / pairs_decidable, 6) if pairs_decidable else 0.0,
            "unknown_pairs_over_total": round(pairs_unknown / pairs_total, 6) if pairs_total else 0.0,
            "not_applicable_pairs_over_total": round(pairs_not_applicable / pairs_total, 6) if pairs_total else 0.0,
            "out_of_domain_cells_over_total": round(len(out_cells) / n, 6) if n else 0.0,
            "in_domain_measured_over_in_domain": round(len(known) / (n - len(out_cells)), 6) if n - len(out_cells) else 0.0,
            "known_cells_over_total": round(len(known) / n, 6) if n else 0.0,
            "delta0_unknown_over_candidates": round(delta0_counts["unknown"] / len(candidates), 6) if candidates else 0.0,
            "findings_attributed_over_total": round(attributed / len(findings), 6) if findings else 0.0,
        },
        "candidates": candidates,
        "candidates_all": all_candidates,
        "variety_deficit": deficit,
        "cycle_obstructions": obstructions,
        "cycles": cycles,
        "coverage_gaps": {
            "unit_records": quirks["unit_records"],
            "unit_keys_unique": quirks["unit_keys_unique"],
            "duplicate_unit_keys": quirks["duplicate_unit_keys"],
            "graph_only_cells": quirks["graph_only_cells"],
            "unknown_cells_count": len(unknown),
            "unknown_cells_sample": unknown[:20],
            "out_of_domain_cells_count": len(out_cells),
            "out_of_domain_cells_sample": sorted(out_cells)[:20],
            "modeling_domain": net.get("modeling_domain"),
            "findings_total": len(findings),
            "findings_attributed": attributed,
            "oracles_total": len(oracles),
            "resource_level_satisfaction_judgments": guard_stats["facts_used"],
            "satisfaction_channel": "ok" if satisfaction else "disabled",
            "satisfaction_guard_source": ("ok" if capability is not None else "INCOMPLETE") if satisfaction else "disabled",
            "satisfaction_guard_stats": guard_stats,
            "suspect_write_edges": suspect_edges,
            "derives_edges_excluded": sum(1 for e in net.get("edges") or [] if e.get("type") == "DERIVES"),
            "ddl_table_only_no_field_oracles": sum(1 for x in oracles
                                                    if (x.get("target") or {}).get("table")
                                                    and not (x.get("target") or {}).get("field")
                                                    and not (x.get("target") or {}).get("entity")),
            "notes": [
                "足迹未知 → unknown，不计入不相交；known-empty × unknown 可判不相交（空足迹与任何足迹不交）",
                "读-读重叠不报候选：双方都不写，mod(C) ∩ fv(R) = ∅ 仍成立",
                "δ⁰ 三态：consistent/unknown/inconsistent。违反级通道证相等→consistent（positive/observation 不进违反指纹，坑120）；"
                "满足级通道 opt-in（--satisfaction，默认关）：同一判据上一侧 verified 违反、另一侧可证明执行（capability guard）"
                "或 verified 正面结论（oracle 连接）→inconsistent。连接键=资源×判据：同资源不同性质的违反与满足可同时为真，不构成矛盾（坑121）；"
                "guard 的 unknown 绝不读成满足；未启用时 inconsistent 恒 0，是「未测量」不是「无矛盾」",
                "满足级通道 0 触发须并读分解：satisfaction_fired_* / guard 事实数(satisfaction_guard_stats) / oracle 连接 finding 数"
                "(satisfaction_tied_*)。机制成立≠语料充足：违反与 guard 同资源共现、且可归因到同一判据，才有判定力",
                "DERIVES 边未纳入足迹（任务契约外），entity 字段写重叠可能漏检",
                "graph_only_cells 是关系网有、units.json 缺失的细胞；不并入则表级足迹全部不可见",
                "suspect_write_edges：WRITES 边来自 find*/exists* 只读方法名，上游标注可疑；标记不丢弃",
                "表级足迹无字段粒度，判据按整表挂载，可能过挂（写未必触及表中每个字段）",
                "环一致性只算重叠图 1-骨架（每资源 clique 的 GF(2) 奇偶），不算全 cover 的 Čech H¹；跨资源环与需三重以上交集才显现的高阶障碍类算不了，显式报为局限",
                "环一致性已接入多值截面（判据约束集求交）：不相交→inconsistent，相交→consistent，缺约束集→unknown；二值/judgments 路径保留为退回",
                "约束集仅按字段粒度算：表级资源无字段粒度，聚合不同列约束（status 枚举 ∩ deposit_cent 区间）会造假障碍 → 表级一律 unknown",
                "coboundary = 边标签形如 c(a)⊕c(b) 的顶点着色导出，parity 恒一致；「端点值的任意函数」意义的顶点导出"
                "不一定是 coboundary（三互异指纹三角形在「不同⇒inconsistent」标签下即矛盾）。满足级通道的已知边在三个前提下"
                "恰为 coboundary，四个前提：**P1 单向** oa≠∅ ⇒ va≠∅（_tied_oracle_ids 的谓词 = _section_value 的谓词"
                "+ 判据连接，故 tied 违反 ⊆ 违反指纹；**反向 va⇒oa 不成立**，无连接的违反不在 oa 里）、"
                "P2 firing 要求满足侧无违反指纹(not vb/not va)、P3 无外部 judgments、"
                "P4 unknown 边被 _parity_check 排除（label==unknown → continue，不参与 union）。"
                "此时 1-边只跨 V/S 切割、0-边只在 V 内，c(v)=1[v∈S] 是全局 2-着色，已知边 parity 恒一致。"
                "任一前提破坏环障碍即可达（P1/P2/P4 破坏→1，P3 破坏→0→1，守卫 tests/test_satisfaction_premises.py 逐个坐实），"
                "不得声称「结构上不可能」（坑121/125/127，V20 两轮证伪：全称版→双条件版）。"
                "**P1 双条件与本通道正确性互斥**：要 va⇒oa 成立就得拆掉判据连接键，那正是 R9 推回的资源级缺陷"
                "（同资源不同性质误判矛盾）—— 后人不得朝「补全双条件」方向修",
                "满足级通道当前对已发布裁定零影响（双重未接线）：audit_entry.pillar_emergence 的 RED 只消费 cycle_obstructions，"
                "不读候选 δ⁰=inconsistent；且 audit_entry 不传 --findings/--capability/--satisfaction。候选级 inconsistent 是"
                "待接线的机器证明矛盾，不是已生效裁定；环级检测力受上述四前提限定，判定力落点在候选级 δ⁰",
                "归约3：repository×repository 同表 CRUD 对是数据访问 plumbing，不是关于业务不变式的独立局部推理，排除；候选须至少一方为业务细胞或携带业务级判据/finding",
                "variety_deficit 是 Ashby 判据多样性赤字：finding 通道粒度（endpoint/class_method/empty）与候选资源粒度（field/table）对不上，两侧缺口显式量化，不因优化隐藏",
                "三态红线：WRITES 边 access=unknown 是 may-write（可靠过近似），计入 may_writes 而非 writes；其重叠候选标 evidence_strength=may_write_unknown，frame_violation 为空，绝不标确定违反",
                "上游依赖（不在本文件权限内）：抽取器 javap 缺 -v 导致 MethodHandle(::) 自调用被跳过、@Query 未抽（V7 实证）；relation_net 的 access 编码由 W5 维护，本引擎只消费",
                "检测力诚实性（坑64）：覆盖≠检测力。buckets_firing_capable（区间/有限域，可不相交）与 buckets_co_singleton_only（余单例域，数学上永不 inconsistent）分报；consistent 头条须并读检测力，不得读成「N 处两细胞取值一致已验证」",
                "ddl_table_only_no_field_oracles：target={table} 无 field 的 DDL CHECK 判据，字段级资源挂不上（_matching_oracles 读 target.field）；当前检测力后果 0（唯一受影响资源是单细胞桶），记为无计数器的静默不可挂载通道，不夸大成回归",
            ],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="frame rule 旁条件违反 → 涌现候选")
    ap.add_argument("--net", required=True, type=Path)
    ap.add_argument("--units", required=True, type=Path)
    ap.add_argument("--findings", type=Path)
    ap.add_argument("--oracles", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--plumbing-types", default="", help="逗号分隔的数据访问 plumbing 节点类型（如 REPO_OP），用于排除同表 CRUD 噪音")
    ap.add_argument("--business-levels", default="L0_business_rule", help="逗号分隔的业务级判据 level（默认 L0_business_rule；传空串则业务通道报 INCOMPLETE）")
    ap.add_argument("--declarations", type=Path, help="class_declaration.json（含 jpa @Table 实体→表映射）；供给后表级判据经映射挂到正确实体字段")
    ap.add_argument("--capability", type=Path, help="capability_map.json；满足级通道的 guard 证据源（需同时 --satisfaction）")
    ap.add_argument("--satisfaction", action="store_true", help="启用满足级 δ⁰ 通道（opt-in，默认关）；缺失 capability 时 guard 源报 INCOMPLETE，保守降为 unknown")
    args = ap.parse_args()

    net = json.loads(args.net.read_text(encoding="utf-8"))
    units = json.loads(args.units.read_text(encoding="utf-8"))
    findings = json.loads(args.findings.read_text(encoding="utf-8")).get("findings", []) if args.findings else []
    oracles = json.loads(args.oracles.read_text(encoding="utf-8")).get("oracles", []) if args.oracles else []
    capability = json.loads(args.capability.read_text(encoding="utf-8")) if args.capability else None
    plumbing = {s for s in args.plumbing_types.split(",") if s}
    business = {s for s in args.business_levels.split(",") if s}
    entity_meta = None
    if args.declarations:
        jpa = json.loads(args.declarations.read_text(encoding="utf-8")).get("jpa", {})
        entity_meta = {e: {"table": v.get("table"),
                           "columns": {c["field"]: c["column"] for c in v.get("columns", []) if "field" in c and "column" in c}}
                       for e, v in jpa.items()}

    report = run(net, units, findings, oracles, plumbing_types=plumbing, business_levels=business, entity_meta=entity_meta, capability=capability, satisfaction=args.satisfaction)
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    c = report["counts"]
    for w in report["input_warnings"]:
        print(f"输入告警: {w}")
    print(f"cells {c['cells_total']} (known {c['cells_footprint_known']} / nonempty {c['cells_footprint_nonempty']} / unknown {c['cells_footprint_unknown']})")
    print(f"pairs {c['pairs_total']} = decidable {c['pairs_decidable']} + unknown-side-disjoint {c['pairs_disjoint_unknown_side']} + unknown {c['pairs_unknown']}")
    cf = report["reduction"]["crud_filter"]
    print(f"candidates {cf['before']} -> {cf['after']} (dropped crud-noise {cf['dropped_crud_noise']}, no-business {cf['dropped_no_business']})")
    print(f"delta0 {c['candidates_delta0']}  strength {c['candidates_evidence_strength']}  may-write-edges {c['may_write_edges']}  suspect-write-only {c['candidates_suspect_write_only']}")
    gs = report["coverage_gaps"]["satisfaction_guard_stats"]
    gline = (f"guard事实 {gs['facts_used']}/{gs['guard_edges_seen']}" if report["coverage_gaps"]["satisfaction_channel"] == "ok"
             else "guard事实未测量(通道未启用)")
    print(f"满足级通道: {report['coverage_gaps']['satisfaction_channel']}（guard源 {report['coverage_gaps']['satisfaction_guard_source']}）"
          f"  {gline}"
          f"  oracle连接finding 违反{c['satisfaction_tied_violation_findings']}/正面{c['satisfaction_tied_positive_findings']}"
          f"  触发 候选{c['satisfaction_fired_candidates']}/资源{c['satisfaction_fired_resources']}"
          f" —— 覆盖≠检测力：0 触发可能是语料不足（无共现/无判据连接），不是「无矛盾」；候选级 inconsistent 尚未被 audit_entry 裁定消费")
    rd = report["reduction"]
    print(f"reduction: brute {rd['brute_force_pairs']} pairs -> index {rd['index_comparisons']} comparisons (x{rd['comparison_reduction_factor']}), disjoint never enumerated {rd['disjoint_pairs_never_enumerated']}")
    print(f"cycles: buckets {c['cycle_buckets']} status {c['cycle_status']} obstructions {c['cycle_obstructions']}  constraint-sections assoc {c['constraint_section_cells']} / unique-cells {c['constraint_section_unique_cells']} / buckets {c['constraint_section_buckets']}  graph {report['overlap_graph']}")
    print(f"检测力: firing_capable {c['buckets_firing_capable']}（区间/有限域,可不相交） / co_singleton_only {c['buckets_co_singleton_only']}（余单例域,永不 inconsistent） / other {c['buckets_other_domain']} —— 守恒 {c['buckets_firing_capable']}+{c['buckets_co_singleton_only']}+{c['buckets_other_domain']}={c['constraint_section_buckets']} 约束桶; 覆盖≠检测力")
    print(f"consistent 信息量: {c['consistent_buckets_symmetric']}/{c['consistent_buckets_total']} 桶两侧约束集逐一相等（对称挂载→检的是资源全局约束可满足，非两细胞独立取值一致）")
    d = report["variety_deficit"]
    bo = d["resources_with_business_oracle"] if d["business_oracle_channel"] == "ok" else "INCOMPLETE"
    print(f"variety deficit: resources {d['candidate_resources']} (finding-covered {d['resources_with_finding_coverage']}, business-oracle {bo}); findings {d['findings_total']} (covering candidate resource {d['findings_covering_any_candidate_resource']}, on candidate cells {d['findings_attributed_to_candidate_cells']})")
    print(f"unknown pair ratio {report['ratios']['unknown_pairs_over_total']}  findings attributed {report['coverage_gaps']['findings_attributed']}/{report['coverage_gaps']['findings_total']}")


if __name__ == "__main__":
    main()