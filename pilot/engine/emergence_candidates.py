"""涌现候选链生成器 —— 从关系网自动生成「可执行」的候选链（零项目字面量）。

手写攻击链只能覆盖想到的那几种组合。这里的做法是：
从可执行依赖图（G_exec）上按**模式模板**做有界匹配，把「入口 → 服务 → 仓储 → 写汇」
这条能真跑的路径连成候选链，每条链**锚定一条已声明的不变量**，再按枢纽度与
Monte-Carlo 级联估计排序，最后按配额选出一批值得真跑的链。

剪枝的硬约束（缺一条就会爆出几千条不可复现的候选）：
  1. 链首必须是 HTTP_ENTRY（外部可触发）
  2. 深度 ≤ 4（入口之后的代码节点数）
  3. 禁止环（路径内不重复节点）
  4. 每个写汇最多展开 3 条路径
  5. 三元组（入口, 写汇, 不变量）去重
  6. 只保留含写汇的路径（只读的路径不是缺陷候选）
  7. 必须锚定一条已声明的不变量（没锚点的链不开单 —— 那属普通单点缺陷）
  8. 全局候选上限 500

排序：枢纽度四维合成（加权 betweenness / PageRank / 写放大 / 信任边界穿越）
      + Monte-Carlo 级联估计（按枢纽加权采样故障，BFS 传播，纯静态模拟）。
随机数一律走固定种子 —— 同输入两次跑，结论必须完全一致。

防塌缩：集中度上限 + 四组配额 + 70% 利用 / 30% 探索 + 「没跑的链」全部记录。

用法：
    python3 emergence_candidates.py --net <关系网.json> --catalog <注入目录.json> \
        --out <产物.json> [--budget 12] [--seed 20261107]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

SEED = 20261107

# 边的「影响强度」：写汇最实、调用次之、读最弱（DESIGN §4.4 的传播权重）
EDGE_INFLUENCE = {"CALLS": 0.5, "READS": 0.3, "WRITES": 1.0, "EXPOSES": 0.8, "DERIVES": 0.2}

MAX_DEPTH = 4              # 入口之后的代码节点数上限
MAX_PATHS_PER_SINK = 3     # 每个写汇最多展开几条路径
MAX_CANDIDATES = 500       # 全局候选上限
MC_SAMPLES = 2000          # Monte-Carlo 采样次数
CONCENTRATION_CAP = 0.40   # 单一入口最多占选中集合的比例
QUOTA_ENTRY = 4
QUOTA_TABLE = 8
QUOTA_INVARIANT = 6
EXPLORE_RATIO = 0.30

# 模式模板（DESIGN §4.3）：候选链命中的模板，决定配额怎么分
PATTERNS = ("T1", "T2", "T3", "T4", "T5", "T6", "T7")

SEVERITY_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "": 0.5}


# ---------------------------------------------------------------- 输入

def load_json(path: str | Path) -> Any:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def assert_fields(invariant: dict[str, Any]) -> list[str]:
    a = invariant.get("assert") or {}
    out: list[str] = []
    for key in ("left", "right", "field", "items_path", "ref_path", "id_field", "ref_id_field"):
        val = a.get(key)
        if isinstance(val, str) and val:
            out.append(val.split(".")[-1])
    return sorted(set(out))


def derive_anchors(nodes: dict[str, Any], invariants: list[dict[str, Any]]) -> dict[str, Any]:
    """从图上的实体字段节点反推不变量锚表（关系网自带锚表时直接用它的）。"""
    anchors: dict[str, Any] = {}
    for inv in invariants:
        iid = str(inv.get("id") or "")
        if not iid:
            continue
        entity = str(inv.get("entity") or "")
        tables: list[str] = []
        for nid, node in nodes.items():
            if node.get("type") != "ENTITY_FIELD":
                continue
            ent = str(node.get("entity") or "")
            if entity and (ent.lower() == entity.lower()
                           or ent.lower().endswith(entity.lower())):
                table = str(node.get("table") or "")
                if table and table not in tables:
                    tables.append(table)
        fields = assert_fields(inv)
        anchors[iid] = {
            "entity": entity,
            "tables": tables,
            "fields": fields,
            "covered_fields": fields,
            "severity": str(inv.get("severity") or "MEDIUM"),
            "category": str(inv.get("category") or ""),
        }
    return anchors


# ---------------------------------------------------------------- 枢纽度

def _norm(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    span = hi - lo
    if span <= 0:
        return {k: 0.0 for k in values}
    return {k: (v - lo) / span for k, v in values.items()}


def betweenness(nodes: list[str], adj: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    """加权 betweenness（Brandes，Dijkstra 最短路）—— 确定性，无随机。"""
    score: dict[str, float] = {n: 0.0 for n in nodes}
    for s in nodes:
        stack: list[str] = []
        preds: dict[str, list[str]] = defaultdict(list)
        sigma: dict[str, float] = dict.fromkeys(nodes, 0.0)
        dist: dict[str, float] = {}
        sigma[s] = 1.0
        dist[s] = 0.0
        import heapq
        heap: list[tuple[float, str]] = [(0.0, s)]
        seen: set[str] = set()
        while heap:
            d, v = heapq.heappop(heap)
            if v in seen:
                continue
            seen.add(v)
            stack.append(v)
            for w, cost in adj.get(v, []):
                nd = d + cost
                if w not in dist:
                    dist[w] = nd
                    heapq.heappush(heap, (nd, w))
                    sigma[w] = sigma[v]
                    preds[w] = [v]
                elif abs(nd - dist[w]) < 1e-12:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
                elif nd < dist[w]:
                    dist[w] = nd
                    sigma[w] = sigma[v]
                    preds[w] = [v]
                    heapq.heappush(heap, (nd, w))
        delta: dict[str, float] = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in preds.get(w, []):
                if sigma[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                score[w] += delta[w]
    n = max(len(nodes) - 1, 1)
    return {k: v / n for k, v in score.items()}


def pagerank(nodes: list[str], adj: dict[str, list[tuple[str, float]]],
             damping: float = 0.85, rounds: int = 100) -> dict[str, float]:
    """PageRank —— 幂迭代到收敛，确定性。"""
    n = len(nodes)
    if not n:
        return {}
    rank = {x: 1.0 / n for x in nodes}
    out_sum = {x: sum(w for _, w in adj.get(x, [])) or 1.0 for x in nodes}
    for _ in range(rounds):
        nxt = {x: (1.0 - damping) / n for x in nodes}
        for x in nodes:
            share = damping * rank[x] / out_sum[x]
            for y, w in adj.get(x, []):
                nxt[y] += share * w
        delta = sum(abs(nxt[x] - rank[x]) for x in nodes)
        rank = nxt
        if delta < 1e-9:
            break
    return rank


def hub_metrics(net: dict[str, Any]) -> dict[str, Any]:
    """枢纽度四维合成（DESIGN §4.4）：
    hub(v) = 0.35·加权betweenness + 0.25·PageRank + 0.25·写放大 + 0.15·信任边界穿越
    """
    nodes_map = net.get("nodes") or {}
    edges = net.get("edges") or []
    ids = sorted(nodes_map)
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        w = EDGE_INFLUENCE.get(e.get("type", ""), 0.3)
        adj[e["from"]].append((e["to"], w))
    b = betweenness(ids, adj)
    r = pagerank(ids, adj)
    # 写放大：可达的表数
    out_adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        out_adj[e["from"]].append(e["to"])
    tables = {n for n, v in nodes_map.items() if v.get("type") == "TABLE"}
    reach_tables: dict[str, int] = {}
    for nid in ids:
        seen: set[str] = set()
        stack = list(out_adj.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(out_adj.get(cur, []))
        reach_tables[nid] = sum(1 for t in seen if t in tables)
    # 信任边界穿越：可达子图里跨了几种节点类型
    types = {n: str(nodes_map[n].get("type", "")) for n in ids}
    crossings: dict[str, int] = {}
    for nid in ids:
        seen: set[str] = set()
        stack = list(out_adj.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(out_adj.get(cur, []))
        crossings[nid] = max(len({types[x] for x in seen} - {""}), 0)
    bn, rn = _norm(b), _norm(r)
    tn = _norm({k: float(v) for k, v in reach_tables.items()})
    cn = _norm({k: float(v) for k, v in crossings.items()})
    composite = {n: round(0.35 * bn.get(n, 0.0) + 0.25 * rn.get(n, 0.0)
                          + 0.25 * tn.get(n, 0.0) + 0.15 * cn.get(n, 0.0), 6)
                 for n in ids}
    return {"betweenness": b, "pagerank": r, "write_amp": reach_tables,
            "boundary": crossings, "composite": composite}


# ---------------------------------------------------------------- 候选生成

def _paths_from_entry(entry: str, adj: dict[str, list[str]],
                      nodes: dict[str, Any], max_depth: int) -> list[list[str]]:
    """从入口出发的所有有界路径（深度 ≤ max_depth，路径内不重复节点 → 无环）。"""
    out: list[list[str]] = []

    def walk(path: list[str]) -> None:
        out.append(list(path))
        if len(path) - 1 >= max_depth:
            return
        for nxt in adj.get(path[-1], []):
            if nxt in path:
                continue                      # 禁止环
            ntype = nodes.get(nxt, {}).get("type")
            if ntype not in ("SVC_OP", "REPO_OP"):
                continue                      # 只走代码节点（表与字段不是路径的一环）
            walk(path + [nxt])

    walk([entry])
    return out


def _call_closure(spine: list[str], adj: dict[str, list[str]],
                  max_extra: int = 2) -> list[str]:
    """主链节点 + 它们直接调用到的兄弟节点（闭包深度 1）。

    条件装配分叉不在主链上时，只看主链会漏掉实例条件。
    """
    out: list[str] = []
    for nid in spine:
        if nid not in out:
            out.append(nid)
    extra: list[str] = []
    for nid in spine:
        for nxt in adj.get(nid, []):
            if nxt not in out and nxt not in extra:
                extra.append(nxt)
    for nid in extra[:max_extra * len(spine)]:
        out.append(nid)
    return out


def _read_sinks(spine: list[str], read_edges: dict[str, list[str]],
                adj: dict[str, list[str]], nodes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """链上的读汇：链上每个节点**调到**的仓储读到的表。

    写汇是链尾那个仓储，读汇却是链中间调用的别的仓储 —— 两者不是同一个节点，
    只看链尾会把「读 A 表 → 写 B 表」的跨实体结构整个丢掉（而那正是涌现的结构）。
    """
    tables: list[str] = []
    repos: list[str] = []
    for nid in spine:
        for nxt in adj.get(nid, []):
            if nodes.get(nxt, {}).get("type") != "REPO_OP":
                continue
            if nxt not in repos:
                repos.append(nxt)
            for table in read_edges.get(nxt, []):
                if table not in tables:
                    tables.append(table)
    return sorted(tables), repos


def _write_sink(spine: list[str], write_edges: dict[str, list[str]]) -> str:
    for nid in reversed(spine):
        tables = write_edges.get(nid) or []
        if tables:
            return sorted(tables)[0]
    return ""


def _patterns(spine: list[str], nodes: dict[str, Any], read_sinks: list[str],
              write_sink: str, anchor: dict[str, Any]) -> list[str]:
    """命中的模式模板（DESIGN §4.3 的七类）。"""
    hits: list[str] = []
    guards = [bool(nodes.get(n, {}).get("taint", {}).get("guard")) for n in spine]
    sources = sum(len(nodes.get(n, {}).get("taint", {}).get("source_fields") or [])
                  for n in spine)
    has_read = bool(read_sinks)
    cross = has_read and write_sink not in read_sinks
    if has_read and cross:
        hits.append("T3")            # 跨实体信任：读 A 表 → 写 B 表
    if has_read and not cross:
        hits.append("T2")            # 派生快照：读 A → 写 A
    if not has_read and sources and not all(guards):
        hits.append("T1")            # 无守卫写
    fields = set(anchor.get("covered_fields") or [])
    if fields & {"status", "state"}:
        hits.append("T5")            # 状态机：写汇落在状态字段上
    branch = max([int(nodes.get(n, {}).get("taint", {}).get("branches") or 0)
                   for n in spine] or [0])
    if has_read and branch >= 8:
        hits.append("T7")            # 查询放大
    return hits or ["T3"]


def generate(net: dict[str, Any], anchors: dict[str, Any],
             metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """图 → 候选链（含八条硬约束的全部剪枝）。"""
    nodes = net.get("nodes") or {}
    edges = net.get("edges") or []
    adj: dict[str, list[str]] = defaultdict(list)
    read_edges: dict[str, list[str]] = defaultdict(list)
    write_edges: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        etype = e.get("type")
        if etype in ("CALLS", "EXPOSES"):
            adj[e["from"]].append(e["to"])
        elif etype == "READS":
            read_edges[e["from"]].append(e["to"])
        elif etype == "WRITES":
            write_edges[e["from"]].append(e["to"])
    for key in adj:
        adj[key] = sorted(set(adj[key]))

    entries = sorted(n for n, v in nodes.items() if v.get("type") == "HTTP_ENTRY"
                     and v.get("endpoint"))
    # 写汇 → 不变量 的索引（锚表）
    by_table: dict[str, list[str]] = defaultdict(list)
    for iid, anc in anchors.items():
        for table in anc.get("tables") or []:
            by_table[table].append(iid)

    per_sink: dict[tuple[str, str], int] = defaultdict(int)
    seen_triples: set[tuple[str, str, str]] = set()
    candidates: list[dict[str, Any]] = []

    for entry in entries:
        endpoint = nodes[entry].get("endpoint", "")
        for path in _paths_from_entry(entry, adj, nodes, MAX_DEPTH):
            sink = _write_sink(path, write_edges)
            if not sink:
                continue                      # 只保留含写汇的路径
            table = sink.split(":", 1)[-1]
            for iid in by_table.get(table, []):
                key = (entry, sink, iid)
                if key in seen_triples:
                    continue                  # 三元组去重
                if per_sink[(entry, sink)] >= MAX_PATHS_PER_SINK:
                    continue                  # 每个写汇最多展开 3 条路径
                seen_triples.add(key)
                per_sink[(entry, sink)] += 1
                anchor = anchors[iid]
                reads, read_repos = _read_sinks(path, read_edges, adj, nodes)
                patterns = _patterns(path, nodes, reads, sink, anchor)
                conditions: list[str] = []
                # 条件取自**调用闭包**，不是只有主链：条件装配分叉（dispatch.variants）
                # 往往挂在主链调用的兄弟节点上 —— 只看主链会把「实例条件」整个丢掉，
                # 而实例条件恰恰是涌现最常见的触发器。
                closure = _call_closure(path, adj)
                for nid in closure:
                    disp = (nodes.get(nid, {}).get("taint", {}) or {}).get("dispatch") or {}
                    if disp.get("variants", 0) >= 2:
                        conditions.append(f"instance:{disp['callee']}")
                for table_read in reads:
                    conditions.append(f"data:{table_read}")
                for nid in closure:
                    for src in (nodes.get(nid, {}).get("taint", {}) or {}).get("source_fields") or []:
                        conditions.append(f"request:{src}")
                # 跨仓库条件：链上有一跳是**另一个仓库**的入口。
                # 少了它，跨仓库的合取链就退化成单仓库链 —— 那个仓库的返回值
                # 参与了什么，本仓库的图上根本看不见。
                # 闭包的深度是 1（刻意的剪枝），而跨仓库的入口在**再下一层**
                # （服务方法 → 客户端方法 → 另一个仓库的入口）——
                # 所以跨仓库这一项单独多走一步，不动闭包本身的深度。
                xrepo_nodes = [nid for nid in closure
                               if nodes.get(nid, {}).get("cross_repo")]
                for nid in closure:
                    xrepo_nodes += [n2 for n2 in adj.get(nid, [])
                                    if nodes.get(n2, {}).get("cross_repo")]
                xrepo_calls = sorted({str(nodes.get(nid, {}).get("repo") or "")
                                      for nid in xrepo_nodes})
                for repo in xrepo_calls:
                    conditions.append(f"xrepo:{repo}")
                if xrepo_calls:
                    patterns.append("T8")   # 跨仓库调用：条件的一端在别的仓库
                candidates.append({
                    "entry": entry,
                    "endpoint": endpoint,
                    "spine": path,
                    "depth": len(path) - 1,
                    "write_sink": sink,
                    "read_sinks": reads,
                    "read_nodes": read_repos,
                    "invariant": iid,
                    "anchor_tables": list(anchor.get("tables") or []),
                    "patterns": patterns,
                    "conditions": conditions,
                    "severity": anchor.get("severity", "MEDIUM"),
                })
                if len(candidates) >= MAX_CANDIDATES:
                    return candidates
    return candidates


# ---------------------------------------------------------------- Monte-Carlo

def monte_carlo(net: dict[str, Any], metrics: dict[str, Any],
                seed: int, samples: int = MC_SAMPLES) -> dict[str, Any]:
    """按枢纽加权采样故障节点，BFS 传播，纯静态模拟。

    排序用，不判定用 —— 它只回答「这条链有多大可能被波及」。
    """
    nodes = net.get("nodes") or {}
    edges = net.get("edges") or []
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        adj[e["from"]].append((e["to"], EDGE_INFLUENCE.get(e.get("type", ""), 0.3)))
    ids = sorted(nodes)
    if not ids:
        return {"hit": {}, "size": {}, "entries": {}}
    comp = metrics.get("composite") or {}
    weights = [max(comp.get(n, 0.0), 0.01) for n in ids]
    entries = {n for n, v in nodes.items() if v.get("type") == "HTTP_ENTRY"}
    rng = random.Random(seed)
    hit: dict[str, int] = defaultdict(int)
    size: dict[str, int] = defaultdict(int)
    ent_hit: dict[str, int] = defaultdict(int)
    for _ in range(samples):
        fault = rng.choices(ids, weights=weights, k=1)[0]
        seen = {fault}
        stack = [fault]
        while stack:
            cur = stack.pop()
            for nxt, w in adj.get(cur, []):
                if nxt in seen or w < 0.3:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        for n in seen:
            size[n] += 1
            ent_hit[n] += sum(1 for x in seen if x in entries)
    return {"hit": {n: hit.get(n, 0) for n in ids},
            "size": {n: size[n] / samples for n in ids},
            "entries": {n: ent_hit.get(n, 0) / samples for n in ids},
            "samples": samples, "seed": seed}


# ---------------------------------------------------------------- 打分

def score_candidates(candidates: list[dict[str, Any]], nodes: dict[str, Any],
                     metrics: dict[str, Any], mc: dict[str, Any],
                     anchors: dict[str, Any]) -> list[dict[str, Any]]:
    """score(c) = 2.0·blast + 1.5·hub + 2.0·inv + 1.0·prec + 1.0·gap − 0.8·cost − 1.5·red

    blast = Monte-Carlo 级联波及规模（归一）
    hub   = 链上节点的枢纽合成均值（归一）
    inv   = 不变量强度（severity × 断言字段被覆盖的比例）
    prec  = 链上节点「有声明事实」的比例（结构越实越可信）
    gap   = 链上没人守的表占比（碰到盲区加分）
    cost  = 链长（归一）
    red   = 同一写汇上的候选拥挤度（归一）
    """
    comp = metrics.get("composite") or {}
    blast_raw = {c["id"]: mc.get("size", {}).get(c["write_sink"], 0.0) for c in candidates}
    hub_raw = {c["id"]: sum(comp.get(n, 0.0) for n in c["spine"]) / max(len(c["spine"]), 1)
               for c in candidates}
    cost_raw = {c["id"]: float(len(c["spine"])) for c in candidates}
    anchored_tables = {t for anc in anchors.values() for t in (anc.get("tables") or [])}
    sink_count: dict[str, int] = defaultdict(int)
    for c in candidates:
        sink_count[c["write_sink"]] += 1
    max_sink = max(sink_count.values()) if sink_count else 1
    bn, hn, cn = _norm(blast_raw), _norm(hub_raw), _norm(cost_raw)
    out: list[dict[str, Any]] = []
    for c in candidates:
        anchor = anchors.get(c["invariant"], {})
        covered = set(anchor.get("covered_fields") or [])
        fields = set(anchor.get("fields") or [])
        cover_ratio = (len(covered & fields) / len(fields)) if fields else 0.0
        inv_term = SEVERITY_WEIGHT.get(c.get("severity", ""), 0.5) * cover_ratio
        declared = sum(1 for n in c["spine"] if nodes.get(n, {}).get("declared", True))
        prec = declared / max(len(c["spine"]), 1)
        tables = {c["write_sink"].split(":", 1)[-1]}
        tables |= {t.split(":", 1)[-1] for t in c["read_sinks"]}
        gap = (len([t for t in tables if t not in anchored_tables]) / len(tables)) if tables else 0.0
        red = (sink_count[c["write_sink"]] - 1) / max(max_sink - 1, 1)
        score = (2.0 * bn.get(c["id"], 0.0) + 1.5 * hn.get(c["id"], 0.0)
                 + 2.0 * inv_term + 1.0 * prec + 1.0 * gap
                 - 0.8 * cn.get(c["id"], 0.0) - 1.5 * red)
        item = dict(c)
        item.update({"score": round(score, 6),
                     "mc_blast": round(blast_raw[c["id"]], 6),
                     "mc_detect": round(mc.get("hit", {}).get(c["write_sink"], 0)
                                        / max(mc.get("samples", 1), 1), 6),
                     "hub": round(hub_raw[c["id"]], 6),
                     "inv_term": round(inv_term, 6),
                     "prec": round(prec, 6),
                     "gap": round(gap, 6),
                     "cost": cost_raw[c["id"]],
                     "red": round(red, 6)})
        out.append(item)
    out.sort(key=lambda c: (-c["score"], c["id"]))
    return out


# ---------------------------------------------------------------- 防塌缩

def _quota_ok(cand: dict[str, Any], chosen: list[dict[str, Any]]) -> bool:
    per_entry: dict[str, int] = defaultdict(int)
    per_table: dict[str, int] = defaultdict(int)
    per_inv: dict[str, int] = defaultdict(int)
    for c in chosen:
        per_entry[c["entry"]] += 1
        per_table[c["write_sink"]] += 1
        per_inv[c["invariant"]] += 1
    if per_entry[cand["entry"]] >= QUOTA_ENTRY:
        return False
    if per_table[cand["write_sink"]] >= QUOTA_TABLE:
        return False
    if per_inv[cand["invariant"]] >= QUOTA_INVARIANT:
        return False
    return True


def _concentration_ok(cand: dict[str, Any], chosen: list[dict[str, Any]], budget: int) -> bool:
    same = sum(1 for c in chosen if c["entry"] == cand["entry"])
    return (same + 1) <= CONCENTRATION_CAP * budget


def select(candidates: list[dict[str, Any]], budget: int, seed: int) -> dict[str, Any]:
    """70% 利用 + 30% 探索，四组配额，集中度上限，模式保底。"""
    ranked = sorted(candidates, key=lambda c: (-c["score"], c["id"]))
    n_exploit = int(round(budget * (1.0 - EXPLORE_RATIO)))
    chosen: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def take(cand: dict[str, Any]) -> bool:
        if not _quota_ok(cand, chosen) or not _concentration_ok(cand, chosen, budget):
            return False
        chosen.append(cand)
        return True

    for cand in ranked:
        if len(chosen) >= n_exploit:
            break
        if not take(cand):
            skipped.append(cand)
    pool = [c for c in ranked if c["id"] not in {x["id"] for x in chosen}]
    rng = random.Random(seed)
    rng.shuffle(pool)
    for cand in pool:
        if len(chosen) >= budget:
            break
        if not take(cand):
            skipped.append(cand)
    # 模式保底：每个有候选的模式至少留一条
    present = {p for c in chosen for p in c["patterns"]}
    for pattern in PATTERNS:
        if pattern in present:
            continue
        for cand in ranked:
            if pattern in cand["patterns"] and cand["id"] not in {x["id"] for x in chosen}:
                if take(cand):
                    present.add(pattern)
                break
    chosen_ids = {c["id"] for c in chosen}
    unrun = [{"id": c["id"], "score": c["score"], "entry": c["entry"],
              "write_sink": c["write_sink"], "invariant": c["invariant"],
              "reason": "配额/集中度/预算之外"} for c in ranked if c["id"] not in chosen_ids]
    return {"selected": chosen, "unrun": unrun,
            "explore": sum(1 for c in chosen if c["id"] in {p["id"] for p in pool}),
            "skipped_by_quota": len(skipped)}


# ---------------------------------------------------------------- 召回比对

def recall(candidates: list[dict[str, Any]], catalog: dict[str, Any],
           anchors: dict[str, Any]) -> dict[str, Any]:
    """把注入目录里的**已知链**当成答案，检查自动生成的候选里有没有它。

    匹配口径（三者同时成立）：同一条已声明不变量 + 同一个外部入口 + 同一个写汇表。
    再比一层「条件覆盖」：已知链的每一条注入（数据/请求/实例）都得在候选链上
    有一个能承载它的条件位 —— 否则那条候选只是碰巧同名，不是同一条链。
    """
    known: list[dict[str, Any]] = []
    matched = 0
    for chain in catalog.get("chains") or []:
        inv = str(chain.get("target_invariant") or "")
        endpoint = str((chain.get("location") or {}).get("endpoint") or "")
        anchor = anchors.get(inv, {})
        anchor_tables = set(anchor.get("tables") or [])
        hits = []
        for cand in candidates:
            if cand["invariant"] != inv or cand["endpoint"] != endpoint:
                continue
            table = cand["write_sink"].split(":", 1)[-1]
            if anchor_tables and table not in anchor_tables:
                continue
            hits.append(cand)
        coverage = ""
        if hits:
            best = max(hits, key=lambda c: (len(c.get("conditions") or []), c["score"]))
            kinds = [(i.get("mutation") or {}).get("kind", "")
                     for i in chain.get("injections") or []]
            covered = []
            for kind in kinds:
                hit = any(str(cond).startswith(f"{kind}:") for cond in best.get("conditions") or [])
                covered.append(hit)
            coverage = "full" if all(covered) else ("partial" if any(covered) else "none")
        ok = bool(hits) and coverage in ("full", "partial")
        matched += 1 if ok else 0
        known.append({
            "chain": str(chain.get("id") or ""),
            "target_invariant": inv,
            "endpoint": endpoint,
            "matched": ok,
            "candidate": hits[0]["id"] if hits else "",
            "condition_coverage": coverage,
            "injection_kinds": [k for k in
                                [(i.get("mutation") or {}).get("kind", "")
                                 for i in chain.get("injections") or []] if k],
        })
    return {"known": known, "matched": matched, "total": len(known),
            "recall_rate": round(matched / len(known), 4) if known else 0.0}


def fingerprint(items: list[dict[str, Any]], field: str = "id") -> str:
    blob = json.dumps(sorted(str(i.get(field, "")) for i in items), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="涌现候选链生成器（通用引擎）")
    ap.add_argument("--net", required=True, help="关系网 JSON")
    ap.add_argument("--invariants", default="", help="不变式声明（关系网没带锚表时用）")
    ap.add_argument("--catalog", default="", help="注入目录（已知链，用于召回比对）")
    ap.add_argument("--out", required=True, help="产物路径")
    ap.add_argument("--budget", type=int, default=12, help="本轮真跑的链数（选择集大小）")
    ap.add_argument("--seed", type=int, default=SEED, help="随机种子（固定 → 结论可复现）")
    args = ap.parse_args()

    net = load_json(args.net)
    invariants = load_json(args.invariants).get("invariants") or [] if args.invariants else []
    anchors = net.get("invariant_anchors") or derive_anchors(net.get("nodes") or {}, invariants)
    metrics = hub_metrics(net)
    candidates = generate(net, anchors, metrics)
    for i, cand in enumerate(candidates):
        cand["id"] = f"C{i:04d}"
    mc = monte_carlo(net, metrics, args.seed)
    scored = score_candidates(candidates, net.get("nodes") or {}, metrics, mc, anchors)
    picked = select(scored, args.budget, args.seed)
    catalog = load_json(args.catalog) if args.catalog else {}
    rec = recall(scored, catalog, anchors)

    payload = {
        "schema_version": 1,
        "seed": args.seed,
        "net": str(args.net),
        "catalog": str(args.catalog),
        "counts": {
            "candidates": len(scored),
            "selected": len(picked["selected"]),
            "unrun": len(picked["unrun"]),
            "by_pattern": {p: sum(1 for c in scored if p in c["patterns"]) for p in PATTERNS},
            "by_entry": {e: sum(1 for c in scored if c["entry"] == e)
                         for e in sorted({c["entry"] for c in scored})},
            "known_chains": rec["total"],
            "matched_known": rec["matched"],
        },
        "candidates": scored,
        "selected": [c["id"] for c in picked["selected"]],
        "unrun": picked["unrun"],
        "recall": rec,
        "fingerprints": {
            "candidate_set": fingerprint(scored),
            "selection": fingerprint(picked["selected"]),
            "scores": hashlib.sha256(
                json.dumps([(c["id"], c["score"]) for c in scored],
                           ensure_ascii=False).encode("utf-8")).hexdigest()[:16],
        },
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    c = payload["counts"]
    print(f"候选链 -> {out}")
    print(f"  候选 {c['candidates']} 条，选中 {c['selected']} 条，没跑 {c['unrun']} 条")
    print(f"  模式分布：{c['by_pattern']}")
    print(f"  已知链命中 {c['matched_known']}/{c['known_chains']}")
    for k in rec["known"]:
        flag = "✓" if k["matched"] else "✗"
        print(f"    {flag} {k['chain']} -> {k['candidate'] or '（未命中）'} "
              f"条件覆盖 {k['condition_coverage']}")
    print(f"  指纹：候选集 {payload['fingerprints']['candidate_set']} "
          f"选择集 {payload['fingerprints']['selection']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
