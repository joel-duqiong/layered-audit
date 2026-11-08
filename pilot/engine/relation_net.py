"""可执行依赖图 G_exec —— 通用构建器（零项目字面量）。

涌现候选链生成器的底座：把「入口 → 服务 → 仓储 → 表」这条**可执行**路径连成一张图，
并带上字段级污点、装配变体（同一接口多实现）、入口绑定与不变量锚表。

输入（都在项目绑定的产物目录里，缺哪个就退化成空图，不猜）：
  fn_declaration.json     函数级声明（调用图、分支数、副作用）
  class_declaration.json  类级声明（DDL 表 + JPA 实体↔表映射）
  openapi.json            HTTP 契约（入口绑定）
  invariants.json         业务不变式声明（锚表 + 断言字段）

输出：relation_net.json —— 节点 / 边 / 污点 / 入口绑定 / 不变量锚。

**零项目字面量**：表名一律从 JPA 映射推，入口一律从 openapi 推，
实体↔表、入口↔控制器全靠结构规则，换项目只改绑定，脚本一行不改。

**为什么表名必须从 JPA 推而不是从类名 snake_case**：项目里的实体类名与表名
没有稳定的命名关系（类名带前缀、表名带复数），猜错一次整条链的写汇就落到
不存在的表上 —— 而候选链的价值恰恰在写汇。

用法：
    python3 relation_net.py                       # 用项目绑定的默认路径
    python3 relation_net.py --out <path>          # 换输出位置
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema

SEED = 20261107

NODE_TYPES = ("HTTP_ENTRY", "SVC_OP", "REPO_OP", "TABLE", "ENTITY_FIELD")
EDGE_TYPES = ("EXPOSES", "CALLS", "READS", "WRITES", "DERIVES")

# 仓储方法的读写语义：按方法名的稳定词根判断（find/save 这类动词前缀是约定俗成的）
READ_HINTS = ("find", "exists", "read", "get", "query", "list", "select", "load", "count")
WRITE_HINTS = ("save", "delete", "write", "insert", "update", "persist", "remove", "merge")

# 请求/响应 DTO 的类型特征：客户端可控输入的载体
DTO_SUFFIXES = ("Request", "Response", "Dto", "DTO", "Command", "Query", "Form",
                "Payload", "View", "Body")
DTO_PACKAGES = ("dto", "vo", "bo", "model", "payload", "web.dto")

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")


def _simple(fqn: str) -> str:
    return fqn.rsplit(".", 1)[-1]


def _package(fqn: str) -> str:
    return fqn.rsplit(".", 1)[0] if "." in fqn else ""


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------- 表名解析

def entity_tables(class_decl: dict[str, Any]) -> dict[str, str]:
    """JPA 实体类名 → 表名。"""
    out: dict[str, str] = {}
    for entity, rec in (class_decl.get("jpa") or {}).items():
        table = (rec or {}).get("table")
        if table:
            out[_simple(entity)] = table
    return out


def resolve_table(key: str, ent_map: dict[str, str]) -> str:
    """实体键 → 表名。

    精确命中 > 唯一后缀命中（类名带前缀、表名不带的情况）> snake_case 兜底。
    多个候选时**不猜** —— 返回 snake_case，让它落不到任何真实表上（会被剪枝掉）。
    """
    if key in ent_map:
        return ent_map[key]
    hits = {t for e, t in ent_map.items() if e.lower().endswith(key.lower())}
    if len(hits) == 1:
        return next(iter(hits))
    return _snake(key)


def repo_entity(repo_simple: str) -> str:
    """仓储类名 → 它操作的实体键（去掉 Repository 后缀）。"""
    if repo_simple.endswith("Repository"):
        return repo_simple[:-len("Repository")]
    return repo_simple


# ---------------------------------------------------------------- 不变量锚

def load_invariants(path: Path) -> list[dict[str, Any]]:
    """读业务不变式声明。

    支持两种声明形状（都是项目配置允许的）：
      · 单实体：entity 声明在文件级，每条不变式不重复声明 -> 这里继承下来
      · 多实体：entities 列表，每条不变式自带 entity
    """
    data = _load(path)
    invariants = list(data.get("invariants") or [])
    single = data.get("entity")
    default_entity = str(single.get("name") or "") if isinstance(single, dict) else ""
    if not default_entity:
        return invariants
    # 不改原对象：造新 dict
    return [{**inv, "entity": inv["entity"]} if inv.get("entity")
            else {**inv, "entity": default_entity}
            for inv in invariants]


def assert_fields(invariant: dict[str, Any]) -> list[str]:
    """断言里出现的字段/列名（字段相等类断言取两端）。"""
    a = invariant.get("assert") or {}
    out: list[str] = []
    for key in ("left", "right", "field", "items_path", "ref_path"):
        val = a.get(key)
        if isinstance(val, str) and val:
            out.append(val.split(".")[-1])
    for key in ("id_field", "ref_id_field"):
        val = a.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    return sorted(set(out))


def anchor_tables(invariant: dict[str, Any], ent_map: dict[str, str]) -> list[str]:
    """不变式 → 它成立/被违反时所在的表。

    优先用声明里的 entity 键；匹配不到时退化成 snake_case 表名。
    """
    entity = str(invariant.get("entity") or "")
    if not entity:
        return []
    tables: list[str] = []
    for name, table in ent_map.items():
        if name.lower() == entity.lower() or name.lower().endswith(entity.lower()):
            tables.append(table)
    if not tables:
        tables.append(resolve_table(entity, ent_map))
    seen: list[str] = []
    for t in tables:
        if t not in seen:
            seen.append(t)
    return seen


# ---------------------------------------------------------------- 图构建

def _classify(simple: str) -> str | None:
    if simple.endswith("Controller"):
        return "HTTP_ENTRY"
    if simple.endswith("Service"):
        return "SVC_OP"
    if simple.endswith("Repository"):
        return "REPO_OP"
    return None


def _is_write_effect(effect: str) -> bool:
    method = effect.rpartition(".")[2].lower()
    return any(h in method for h in WRITE_HINTS)


def _is_read_effect(effect: str) -> bool:
    method = effect.rpartition(".")[2].lower()
    return any(h in method for h in READ_HINTS) and not _is_write_effect(effect)


def _dispatch_of(calls: list[str], by_cm: dict[tuple[str, str], dict[str, Any]],
                 abstract: set[tuple[str, str]]) -> dict[str, Any]:
    """同一接口多实现 → 装配变体（部署维度）。

    字节码里接口方法带 abstract 标记；同包、同签名的具体实现就是候选实现。
    变体数 ≥2 意味着「同一段代码在不同部署下行为不同」—— 这是**实例维度**
    的条件来源，与数据维度的条件正交，涌现常常就出在两者的交叉上。
    """
    for callee in calls:
        owner, _, method = callee.rpartition(".")
        if not owner or (owner, method) not in abstract:
            continue
        want = list(by_cm[(owner, method)].get("params") or [])
        impls: list[str] = []
        for (cls, meth), f in by_cm.items():
            if meth != method or cls == owner or (cls, meth) in abstract:
                continue
            if _package(cls) != _package(owner):
                continue
            if list(f.get("params") or []) != want:
                continue
            impls.append(cls)
        if impls:
            return {"callee": callee, "implementations": sorted(impls),
                    "variants": len(impls)}
    return {}


def build(fn_decl: dict[str, Any], class_decl: dict[str, Any],
          openapi: dict[str, Any], invariants: list[dict[str, Any]]) -> dict[str, Any]:
    funcs = list(fn_decl.get("functions") or [])
    jpa = class_decl.get("jpa") or {}
    ddl = (class_decl.get("ddl") or {}).get("tables") or {}
    ent_map = entity_tables(class_decl)

    by_cm: dict[tuple[str, str], dict[str, Any]] = {}
    for f in funcs:
        cls, method = f.get("class", ""), f.get("method", "")
        if cls and method:
            by_cm[(cls, method)] = f
    abstract = {cm for cm, f in by_cm.items() if "abstract" in (f.get("modifiers") or "")}
    entity_classes = set(ent_map)

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(nid: str, ntype: str, **kw: Any) -> None:
        nodes.setdefault(nid, {"id": nid, "type": ntype, **kw})

    def add_edge(src: str, dst: str, etype: str, **kw: Any) -> None:
        edges.append({"from": src, "to": dst, "type": etype, **kw})

    # ---- 节点 ----
    for (cls, method), f in by_cm.items():
        simple = _simple(cls)
        ntype = _classify(simple)
        if not ntype:
            continue
        add_node(f"{cls}#{method}", ntype, cls=cls, method=method, simple=simple,
                 file=f.get("file", ""), modifiers=f.get("modifiers", ""), declared=True)
    for table, rec in ddl.items():
        cols = rec.get("columns") or {}
        add_node(f"table:{table}", "TABLE", table=table, columns=sorted(cols))
    for entity, rec in jpa.items():
        for col in rec.get("columns") or []:
            field = col.get("field")
            if field:
                add_node(f"field:{_simple(entity)}.{field}", "ENTITY_FIELD",
                         entity=_simple(entity), field=field,
                         column=col.get("column"), table=rec.get("table"))

    # ---- 只被调用、没被声明的仓储方法 ----
    # Spring Data 的仓储接口通常不声明方法（继承自 JpaRepository），
    # 但调用点照样打在它身上 —— 写汇就藏在这些调用点上，
    # 只认「声明」的话整条链的写汇会凭空消失。
    for f in funcs:
        for callee in list(f.get("internal_calls") or []) + list(f.get("side_effects") or []):
            owner, _, cmethod = callee.rpartition(".")
            if not owner or not _simple(owner).endswith("Repository"):
                continue
            nid = f"{owner}#{cmethod}"
            if nid not in nodes:
                add_node(nid, "REPO_OP", cls=owner, method=cmethod, simple=_simple(owner),
                         file="", declared=False)

    # ---- 污点 ----
    for nid, node in nodes.items():
        if node["type"] not in ("HTTP_ENTRY", "SVC_OP", "REPO_OP"):
            continue
        f = by_cm.get((node["cls"], node["method"]), {})
        calls = list(f.get("internal_calls") or [])
        effects = list(f.get("side_effects") or [])
        sources: list[str] = []
        sinks: list[str] = []
        for callee in calls:
            owner, _, method = callee.rpartition(".")
            if not owner:
                continue
            simple = _simple(owner)
            if simple in entity_classes:
                if method.lower().startswith("set"):
                    sinks.append(callee)
            elif simple.endswith(DTO_SUFFIXES) or _package(owner).split(".")[-1] in DTO_PACKAGES:
                sources.append(callee)
        guard = bool(int(f.get("branch_count") or 0) > 0
                     and any(("valueOf" in c or ".equals(" in c) for c in calls))
        taint = {
            "source_fields": sources,
            "sinks": sinks,
            "guard": guard,
            "persist": any(_is_write_effect(e) for e in effects),
            "branches": int(f.get("branch_count") or 0),
            "side_effects": effects,
            "is_pure": bool(f.get("is_pure", False)),
            "dispatch": _dispatch_of(calls, by_cm, abstract),
        }
        node["taint"] = taint

    # ---- 边 ----
    for (cls, method), f in by_cm.items():
        src = f"{cls}#{method}"
        if src not in nodes:
            continue
        for callee in list(f.get("internal_calls") or []):
            owner, _, cmethod = callee.rpartition(".")
            if not owner:
                continue
            dst = f"{owner}#{cmethod}"
            if dst in nodes:
                add_edge(src, dst, "CALLS")
                if nodes[dst]["type"] == "SVC_OP" and nodes[src]["type"] == "HTTP_ENTRY":
                    add_edge(src, dst, "EXPOSES")
            elif _simple(owner) in entity_classes and cmethod.lower().startswith("set"):
                field = cmethod[3:]
                field = field[:1].lower() + field[1:]
                prefix = f"field:{_simple(owner)}."
                hit = next((n for n in nodes
                            if n.startswith(prefix)
                            and n.split(".")[-1].lower() == field.lower()), "")
                if hit:
                    add_edge(src, hit, "DERIVES", callee=callee)
        for eff in list(f.get("side_effects") or []):
            owner, _, emethod = eff.rpartition(".")
            if not owner or not _simple(owner).endswith("Repository"):
                continue
            repo_node = f"{owner}#{emethod}"
            if repo_node not in nodes:
                continue
            table = resolve_table(repo_entity(_simple(owner)), ent_map)
            tnode = f"table:{table}"
            if tnode not in nodes:
                continue
            if _is_write_effect(eff):
                add_edge(repo_node, tnode, "WRITES", effect=eff)
            elif _is_read_effect(eff):
                add_edge(repo_node, tnode, "READS", effect=eff)

    # ---- 入口绑定 ----
    bindings, unbound = bind_entries(openapi, nodes)
    for nid, node in nodes.items():
        if node["type"] == "HTTP_ENTRY":
            node["endpoint"] = next((k for k, v in bindings.items() if v == nid), "")

    # ---- 不变量锚 ----
    anchors: dict[str, Any] = {}
    for inv in invariants:
        iid = str(inv.get("id") or "")
        if not iid:
            continue
        tables = anchor_tables(inv, ent_map)
        fields = assert_fields(inv)
        anchors[iid] = {
            "entity": str(inv.get("entity") or ""),
            "tables": tables,
            "fields": fields,
            "covered_fields": sorted(f for f in fields if f in _covered_columns(ddl, tables)),
            "severity": str(inv.get("severity") or "MEDIUM"),
            "category": str(inv.get("category") or ""),
        }

    return {
        "schema_version": 1,
        "seed": SEED,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": {t: sum(1 for n in nodes.values() if n["type"] == t)
                        for t in NODE_TYPES},
            "edges_by_type": {t: sum(1 for e in edges if e["type"] == t)
                              for t in EDGE_TYPES},
            "bindings": len(bindings),
            "unbound": len(unbound),
            "invariants": len(anchors),
        },
        "bindings": bindings,
        "unbound": unbound,
        "invariant_anchors": anchors,
        "nodes": nodes,
        "edges": edges,
        "centrality": _centrality(nodes, edges),
    }


def _covered_columns(ddl: dict[str, Any], tables: list[str]) -> set[str]:
    cols: set[str] = set()
    for t in tables:
        cols |= set((ddl.get(t, {}).get("columns") or {}))
    return cols


def bind_entries(openapi: dict[str, Any], nodes: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """openapi 操作 → 控制器方法。

    先用 tag（`order-controller` → OrderController）定位控制器类，
    再用 operationId 前缀定位方法；定位不到就**记为未绑定**，不猜。
    """
    by_class: dict[str, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        if n["type"] == "HTTP_ENTRY":
            by_class[n["cls"]].append(nid)
    bindings: dict[str, str] = {}
    unbound: list[str] = []
    for path, item in (openapi.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for verb, op in item.items():
            if verb.lower() not in HTTP_VERBS or not isinstance(op, dict):
                continue
            key = f"{verb.upper()} {path}"
            opid = str(op.get("operationId") or "")
            base = opid.split("_")[0]
            wanted = _controller_keys(op, path)
            cands: list[str] = []
            for cls in sorted(by_class):
                simple = _simple(cls)
                stem = simple[:-len("Controller")] if simple.endswith("Controller") else simple
                if not wanted or any(w.lower() == stem.lower() for w in wanted):
                    cands.append(cls)
            hit = ""
            for cls in cands:
                ids = by_class[cls]
                exact = [i for i in ids if i.split("#")[1] == base]
                if exact:
                    hit = exact[0]
                    break
                loose = [i for i in ids if i.split("#")[1].startswith(base)]
                if len(loose) == 1:
                    hit = loose[0]
                    break
            if hit:
                bindings[key] = hit
            else:
                unbound.append(key)
    return bindings, unbound


def _controller_keys(op: dict[str, Any], path: str) -> list[str]:
    """从 tag / 路径推出控制器名关键词（`order-controller` → order）。"""
    keys: list[str] = []
    for tag in op.get("tags") or []:
        stem = str(tag).lower().replace("-controller", "").replace("_controller", "")
        stem = stem.replace("controller", "").strip("-_ ")
        if stem:
            keys.append(stem)
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) >= 2 and segs[0] in ("api", "v1", "v2"):
        segs = segs[1:]
    if segs:
        resource = segs[0].lower()
        keys.append(resource[:-1] if resource.endswith("s") else resource)
    return keys


def _centrality(nodes: dict[str, Any], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """三个可解释的度指标（枢纽合成的四个维度在候选生成器里算）。"""
    degree: dict[str, int] = defaultdict(int)
    out_adj: dict[str, list[str]] = defaultdict(list)
    in_adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
        out_adj[e["from"]].append(e["to"])
        in_adj[e["to"]].append(e["from"])
    tables = {n for n, v in nodes.items() if v["type"] == "TABLE"}
    entries = {n for n, v in nodes.items() if v["type"] == "HTTP_ENTRY"}
    reach: dict[str, int] = {}
    entry_reach: dict[str, int] = {}
    for nid in nodes:
        seen: set[str] = set()
        stack = list(out_adj.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(out_adj.get(cur, []))
        reach[nid] = sum(1 for t in seen if t in tables)
        seen2: set[str] = set()
        stack2 = list(in_adj.get(nid, []))
        while stack2:
            cur = stack2.pop()
            if cur in seen2:
                continue
            seen2.add(cur)
            stack2.extend(in_adj.get(cur, []))
        entry_reach[nid] = sum(1 for x in seen2 if x in entries)
    return {"degree": dict(degree), "reachable_tables": reach,
            "entry_reachable": entry_reach}


# ---------------------------------------------------------------- 入口

def artifacts_dir() -> Path:
    target = findings_schema.load_target()
    raw = (target.get("paths") or {}).get("artifacts_dir")
    base = Path(raw) if raw else findings_schema.target_path().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


def project_dir() -> Path:
    return findings_schema.target_path().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="可执行依赖图构建器（通用引擎）")
    ap.add_argument("--out", default="", help="输出路径（默认 <产物目录>/relation_net.json）")
    args = ap.parse_args()

    base = artifacts_dir()
    net = build(_load(base / "fn_declaration.json"),
                _load(base / "class_declaration.json"),
                _load(base / "openapi.json"),
                load_invariants(project_dir() / "invariants.json"))
    out = Path(args.out) if args.out else base / "relation_net.json"
    out.write_text(json.dumps(net, ensure_ascii=False, indent=2), encoding="utf-8")
    c = net["counts"]
    print(f"可执行依赖图 -> {out}")
    print(f"  节点 {c['nodes']} / 边 {c['edges']}：{c['by_type']}")
    print(f"  边类型：{c['edges_by_type']}")
    print(f"  入口绑定 {c['bindings']} 条，未绑定 {c['unbound']} 条")
    for u in net["unbound"][:8]:
        print(f"    未绑定 {u}")
    print(f"  不变量锚 {c['invariants']} 条：")
    for iid, anc in net["invariant_anchors"].items():
        print(f"    {iid} -> {anc['tables']} 字段 {anc['covered_fields']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
