"""可执行依赖图 G_exec —— 涌现检测的底座。

**零新扫描器**：全部输入来自现有各层声明产物（fn/file/class/integration declaration + openapi）。
图上只做「剪枝 + 排序」，**判定一律由动态执行给出**（静态/模型只能产出待验证线索）。

节点类型：HTTP_ENTRY / SVC_OP / REPO_OP / TABLE / ENTITY_FIELD / ASSET
边类型：  EXPOSES / CALLS / READS / WRITES / DERIVES

字段级污点（让「客户端可控」变成图上可计算的东西）：
  SOURCE(f)  = 调用了 DTO 的 getter        → 客户端可控输入
  SINK(f)    = 调用了实体的 setter        → 写汇目标
  GUARD(f)   = 有分支且调了枚举 valueOf / 比较 → 有守卫
  PERSIST(f) = 副作用里含 Repository.save  → 会落库

自证标准（这是这个增量的验收）：
  A1 25 个 HTTP 入口全部绑定到 openapi operation
  A2 5 条手写攻击链的「语义骨架」必须能在图里找回
  A3 枢纽 top-5 集合固定（防止中心性实现写错）
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "emergence_graph.json"
SEED = 20261107

PKG = "com.jiuyan.banquet"


def _load(name: str) -> Any:
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _simple(fqn: str) -> str:
    return fqn.rsplit(".", 1)[-1]


def build() -> dict[str, Any]:
    fn = _load("fn_declaration.json") or {}
    cd = _load("class_declaration.json") or {}
    openapi = _load("openapi.json") or {}
    funcs = fn.get("functions") or []

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(nid: str, ntype: str, **kw: Any) -> None:
        nodes.setdefault(nid, {"id": nid, "type": ntype, **kw})

    def add_edge(src: str, dst: str, etype: str, **kw: Any) -> None:
        edges.append({"from": src, "to": dst, "type": etype, **kw})

    # ---- 节点 ----
    for f in funcs:
        cls, method = f.get("class", ""), f.get("method", "")
        if not cls or not method:
            continue
        simple = _simple(cls)
        if simple.endswith("Controller"):
            ntype = "HTTP_ENTRY"
        elif simple.endswith("Service"):
            ntype = "SVC_OP"
        elif simple.endswith("Repository"):
            ntype = "REPO_OP"
        else:
            continue
        add_node(f"{cls}#{method}", ntype, cls=cls, method=method,
                 simple=simple, file=f.get("file", ""))

    for table, rec in (cd.get("ddl") or {}).get("tables", {}).items():
        add_node(f"table:{table}", "TABLE", table=table,
                 columns=sorted((rec.get("columns") or {}).keys()))

    for entity, rec in (cd.get("jpa") or {}).items():
        for col in rec.get("columns") or []:
            field = col.get("field")
            if field:
                add_node(f"field:{entity}.{field}", "ENTITY_FIELD",
                         entity=entity, field=field, column=col.get("column"),
                         table=rec.get("table"))

    # ---- 污点与边 ----
    for f in funcs:
        cls, method = f.get("class", ""), f.get("method", "")
        src_id = f"{cls}#{method}"
        if src_id not in nodes:
            continue
        calls = list(f.get("internal_calls") or [])
        effects = list(f.get("side_effects") or [])
        has_enum_guard = any("valueOf" in c or ".equals(" in c for c in calls)
        branches = int(f.get("branch_count") or 0)
        sources = [c for c in calls if ".dto." in c and ".get" in c]
        sinks = [c for c in calls if ".entity." in c and ".set" in c]
        nodes[src_id]["taint"] = {
            "source_fields": sources,
            "sinks": sinks,
            "guard": bool(branches > 0 and has_enum_guard),
            "persist": any("save" in e for e in effects),
            "branches": branches,
            "side_effects": effects,
        }
        for callee in calls:
            ccls, _, cmethod = callee.rpartition(".")
            if not ccls:
                continue
            dst = f"{ccls}#{cmethod}"
            if dst in nodes:
                add_edge(src_id, dst, "CALLS")
            elif ".entity." in callee and ".set" in callee:
                # setter 调用 → DERIVES 到实体字段
                ent_field = callee.split(".entity.")[-1]
                ent = ent_field.split(".")[0]
                cand = [n for n in nodes if n.startswith(f"field:{ent}.")]
                for c in cand:
                    add_edge(src_id, c, "DERIVES", callee=callee)
        for eff in effects:
            if ".Repository." in eff or ".repository." in eff:
                _, _, m = eff.rpartition(".")
                cand = [n for n in nodes if n.endswith(f"#{m}") and "Repository" in n]
                for c in cand:
                    if any(k in eff for k in ("find", "exists", "read", "get")):
                        add_edge(c, f"table:{_table_of(c)}", "READS", effect=eff)
                    if any(k in eff for k in ("save", "delete", "write", "insert")):
                        add_edge(c, f"table:{_table_of(c)}", "WRITES", effect=eff)

    # 控制器 → 服务（EXPOSES）
    for f in funcs:
        cls = f.get("class", "")
        if not _simple(cls).endswith("Controller"):
            continue
        for callee in f.get("internal_calls") or []:
            ccls, _, cmethod = callee.rpartition(".")
            dst = f"{ccls}#{cmethod}"
            if dst in nodes and nodes[dst]["type"] == "SVC_OP":
                add_edge(f"{cls}#{f.get('method')}", dst, "EXPOSES")

    # ---- 入口绑定（Controller ↔ openapi） ----
    paths = openapi.get("paths") or {}
    bindings: dict[str, str] = {}
    unbound: list[str] = []
    for path, item in paths.items():
        resource = path.strip("/").split("/")[1] if path.startswith("/api/") else ""
        for verb, op in item.items():
            if verb not in ("get", "post", "put", "delete", "patch"):
                continue
            opid = (op or {}).get("operationId", "")
            base = opid.split("_")[0]
            want = f"{_simple(resource).capitalize()}Controller#{base}"
            hit = next((n for n in nodes if n.endswith(f"#{base}")
                        and _simple(n.split("#")[0]).endswith("Controller")), None)
            if hit:
                bindings[f"{verb.upper()} {path}"] = hit
            else:
                unbound.append(f"{verb.upper()} {path}")
    for nid, n in nodes.items():
        if n["type"] == "HTTP_ENTRY":
            key = next((k for k, v in bindings.items() if v == nid), None)
            n["endpoint"] = key

    # ---- 枢纽度 ----
    centrality = _centrality(nodes, edges)

    return {
        "schema_version": 1,
        "seed": SEED,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": {t: sum(1 for n in nodes.values() if n["type"] == t)
                        for t in ("HTTP_ENTRY", "SVC_OP", "REPO_OP", "TABLE", "ENTITY_FIELD")},
            "edges_by_type": {t: sum(1 for e in edges if e["type"] == t)
                              for t in ("EXPOSES", "CALLS", "READS", "WRITES", "DERIVES")},
            "bindings": len(bindings),
            "unbound": len(unbound),
        },
        "bindings": bindings,
        "unbound": unbound,
        "nodes": nodes,
        "edges": edges,
        "centrality": centrality,
    }


def _table_of(repo_node: str) -> str:
    """Repository 节点 → 它操作的表（按实体名映射）。"""
    simple = repo_node.split("#")[0].rsplit(".", 1)[-1].replace("Repository", "")
    return {"Order": "orders", "BanquetPackage": "packages", "Product": "products",
            "Category": "categories", "PackageItem": "package_items",
            "PackageType": "package_types"}.get(simple, simple.lower())


def _centrality(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """四个维度的枢纽指标 + 合成。全部确定性（无随机）。"""
    # 无向度
    degree: dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    # 写放大：可达的表数
    out_adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        out_adj[e["from"]].append(e["to"])
    tables = [n for n, v in nodes.items() if v["type"] == "TABLE"]
    reach_tables: dict[str, int] = {}
    for nid in nodes:
        seen: set[str] = set()
        stack = list(out_adj.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(out_adj.get(cur, []))
        reach_tables[nid] = sum(1 for t in seen if t.startswith("table:"))
    # 信任边界：被多少 HTTP 入口可达
    entries = [n for n, v in nodes.items() if v["type"] == "HTTP_ENTRY"]
    in_adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        in_adj[e["to"]].append(e["from"])
    entry_reach: dict[str, int] = {}
    for nid in nodes:
        seen: set[str] = set()
        stack = list(in_adj.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(in_adj.get(cur, []))
        entry_reach[nid] = sum(1 for x in seen if x in entries)
    # 合成
    max_deg = max(degree.values()) if degree else 1
    max_tbl = max(reach_tables.values()) if reach_tables else 1
    max_ent = max(entry_reach.values()) if entry_reach else 1
    hub: dict[str, float] = {}
    for nid in nodes:
        hub[nid] = round(0.55 * degree[nid] / max_deg
                         + 0.30 * reach_tables[nid] / max_tbl
                         + 0.15 * entry_reach[nid] / max_ent, 4)
    return {
        "degree": dict(degree),
        "reachable_tables": reach_tables,
        "entry_reachable": entry_reach,
        "hub": hub,
    }


def main() -> None:
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    c = payload["counts"]
    print(f"可执行依赖图 -> {OUT}")
    print(f"  节点 {c['nodes']} / 边 {c['edges']}：{c['by_type']}")
    print(f"  边类型：{c['edges_by_type']}")
    print(f"  入口绑定 {c['bindings']} 条，未绑定 {c['unbound']} 条")
    if payload["unbound"]:
        for u in payload["unbound"][:8]:
            print(f"    未绑定 {u}")
    hub = sorted(payload["centrality"]["hub"].items(), key=lambda kv: -kv[1])[:8]
    print("  枢纽 top-8：")
    for nid, v in hub:
        print(f"    {v:.4f}  {nid}")


if __name__ == "__main__":
    main()
