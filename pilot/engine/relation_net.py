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

**表访问三态**：仓储方法的读写按证据分级判定（语句文本 > 框架契约 > 方法体写汇），
拿不到证据 → unknown。每条表边带 access=write/read/unknown；unknown 编码为
may-write（WRITES 边 + access=unknown），因为下游只认 WRITES/READS，
丢弃未知足迹等于把未知当成「不相交/合规」—— 可靠过近似宁可多报不可漏报。

**getter 读通道（opt-in，默认关闭）**：`--getter-reads <field_access.json>` 开启后，
JPA 实体 get*/is* 调用的**字节码验证 getfield 目标**计入 taint.source_fields（读）。
资源键用 setter 等价形（`owner.setField`）——下游倒排索引按原始资源串建桶，
必须与 sinks 的 setter 串同键才能重叠；归一化身份由 frame_check.normalize_resource
现有 setter 分支完成，**读/写通道由 provenance 区分（source_fields vs sinks），不靠键形**。
只发射字节码验证的字段读：计算型 getter（无 getfield）、别名 getter（读他字段）
按真实目标发射或不发射，**名字推导一律不用**（实测 isActive() 读 isActive 而非 active，
ThreadLocal 访问器无字段访问 —— 名字推导会造假足迹）。不可验证 → 保分裂，不猜。
缺输入文件 / 形状不合格 → 拒绝出数（rc=1），不静默降级。关闭时产物逐字节不变。
field_access.json 由只读 javap 抽取器生产（getfield/putfield + 继承链），引擎不跑工具链。

**为什么表名必须从 JPA 推而不是从类名 snake_case**：项目里的实体类名与表名
没有稳定的命名关系（类名带前缀、表名带复数），猜错一次整条链的写汇就落到
不存在的表上 —— 而候选链的价值恰恰在写汇。

用法：
    python3 relation_net.py                       # 用项目绑定的默认路径
    python3 relation_net.py --out <path>          # 换输出位置
    python3 relation_net.py --artifacts-dir <dir> --project-dir <dir>   # 脱离项目绑定
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema
from surface import load_surfaces

SEED = 20261107

NODE_TYPES = ("HTTP_ENTRY", "SVC_OP", "REPO_OP", "TABLE", "ENTITY_FIELD")
EDGE_TYPES = ("EXPOSES", "CALLS", "READS", "WRITES", "DERIVES")

# 语句首词分类词表（用于解析输入里携带的 @Query 原文，不是内置的项目 SQL）
READ_HINTS = ("find", "exists", "read", "get", "query", "list", "select", "load", "count")
WRITE_HINTS = ("save", "delete", "write", "insert", "update", "persist", "remove", "merge")

# Spring Data 派生查询语法：方法名本身就是查询定义（框架契约，不是命名猜测）。
# 只认**首个 camelCase 动词**且带词边界 —— findByIdAndIsDeletedFalse 里的
# Deleted 是属性路径的一部分，子串匹配会把它误判成 delete 写操作。
_DERIVED_READ_VERBS = ("find", "read", "get", "query", "search", "stream", "count", "exists")
_DERIVED_WRITE_VERBS = ("delete", "remove")
_VERB_BOUNDARY = r"(?=[A-Z0-9_]|$)"
# Spring Data / JPA 契约里语义确定的写方法（精确匹配，绝不子串）
_CRUD_WRITE_METHODS = frozenset({
    "save", "saveall", "saveandflush", "persist", "merge", "flush",
    "delete", "deletebyid", "deleteall", "deleteallbyid",
    "deleteinbatch", "deleteallinbatch", "remove",
})

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


# 出站 HTTP 调用的类标记 —— **按行为判，不按名字判**：
# 名字叫 Client/Gateway/Invoker 的可能是别的东西，而**真的调了
# java.net.http 的**才是跨仓库客户端。跨仓库的链要靠它才接得起来。
_OUTBOUND_HTTP_HINTS = ("java.net.http.", "okhttp3.", "org.apache.http.", "feign.")


def _is_http_client(f: dict[str, Any]) -> bool:
    calls = list(f.get("external_calls") or []) + list(f.get("internal_calls") or [])
    return any(any(h in c for h in _OUTBOUND_HTTP_HINTS) for c in calls)


def repo_access(method: str, decl: dict[str, Any] | None) -> tuple[str, str]:
    """仓储方法 → (write|read|unknown, 证据)。证据分级，拿不到就 unknown，绝不猜。

    1. 声明携带的语句文本（@Query 原文）：以语句首词为准，名字说了不算。
    2. 框架契约：CRUD 写方法精确名；派生查询/派生删除的首动词。
    3. 具体方法体里真的调了写汇（仓储/EntityManager 的写方法）。
    """
    decl = decl or {}
    query = decl.get("query")
    if isinstance(query, str) and query.strip():
        head = re.match(r"[A-Za-z]+", query.strip())
        verb = head.group(0).lower() if head else ""
        if verb in WRITE_HINTS:
            return "write", "query-statement"
        if verb in READ_HINTS:
            return "read", "query-statement"
        return "unknown", "query-statement-unrecognized"
    if method.lower() in _CRUD_WRITE_METHODS:
        return "write", "crud-contract"
    for verb in _DERIVED_WRITE_VERBS:
        if re.match(verb + _VERB_BOUNDARY, method):
            return "write", "derived-delete"
    for verb in _DERIVED_READ_VERBS:
        if re.match(verb + _VERB_BOUNDARY, method):
            return "read", "derived-query"
    if "abstract" not in str(decl.get("modifiers") or ""):
        sinks: list[str] = []
        for callee in list(decl.get("internal_calls") or []) + list(decl.get("side_effects") or []):
            owner, _, sink = callee.rpartition(".")
            simple = _simple(owner)
            if simple.endswith("Repository") or simple == "EntityManager":
                sinks.append(repo_access(sink, None)[0])
        if "write" in sinks:
            return "write", "body-write-sink"
        if sinks and all(s == "read" for s in sinks):
            return "read", "body-read-only"
    return "unknown", "no-evidence"


def _effect_access(effect: str, by_cm: dict[tuple[str, str], dict[str, Any]]) -> str:
    owner, _, method = effect.rpartition(".")
    if not owner or not _simple(owner).endswith("Repository"):
        return "unknown"
    return repo_access(method, by_cm.get((owner, method)))[0]


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


def _resolve_field_access(classes: dict[str, Any], fq: str, method: str) -> list[str] | None:
    """字节码字段访问映射里解析方法的 getfield 目标（沿继承链）。

    返回读到的字段名列表；方法不存在 / 无 getfield（计算型）/ 形状不合格 → None（保分裂）。
    """
    seen: set[str] = set()
    while fq and fq not in seen:
        seen.add(fq)
        c = classes.get(fq)
        if not isinstance(c, dict):
            return None
        methods = c.get("methods")
        if isinstance(methods, dict) and method in methods:
            reads = (methods[method] or {}).get("reads")
            if not isinstance(reads, list):
                return None
            out = [r for r in reads if isinstance(r, str) and r]
            return out or None
        fq = c.get("super") or ""
    return None


def build(fn_decl: dict[str, Any], class_decl: dict[str, Any],
          openapi: dict[str, Any], invariants: list[dict[str, Any]],
          surfaces: dict[str, dict[str, Any]] | None = None,
          field_access: dict[str, Any] | None = None) -> dict[str, Any]:
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
        if not ntype and _is_http_client(f):
            ntype = "HTTP_CLIENT"
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

    # ---- 仓储方法读写三态（按证据，不按子串）----
    for node in nodes.values():
        if node["type"] != "REPO_OP":
            continue
        access, evidence = repo_access(node["method"], by_cm.get((node["cls"], node["method"])))
        node["access"] = access
        node["access_evidence"] = evidence

    # ---- 污点 ----
    getter_reads_emitted = 0
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
        if field_access is not None:
            # 验证读通道：只发射字节码 getfield 证实的字段读，名字推导一律不用。
            # setter 等价键是为了与 sinks 同桶（倒排索引按原始串建桶）；通道是读，
            # 下游 provenance 记 taint.source_fields，绝不进 sinks。
            for callee in calls:
                owner, _, method = callee.rpartition(".")
                if not owner or not method.startswith(("get", "is")):
                    continue
                if _simple(owner) not in entity_classes:
                    continue
                reads = _resolve_field_access(field_access, owner, method)
                if not reads:
                    continue
                for fld in reads:
                    sources.append(f"{owner}.set{fld[:1].upper()}{fld[1:]}")
                    getter_reads_emitted += 1
        guard = bool(int(f.get("branch_count") or 0) > 0
                     and any(("valueOf" in c or ".equals(" in c) for c in calls))
        taint = {
            "source_fields": sources,
            "sinks": sinks,
            "guard": guard,
            "persist": any(_effect_access(e, by_cm) == "write" for e in effects),
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
            rnode = nodes.get(repo_node)
            if rnode is None:
                continue
            table = resolve_table(repo_entity(_simple(owner)), ent_map)
            tnode = f"table:{table}"
            if tnode not in nodes:
                continue
            access = rnode.get("access", "unknown")
            evidence = rnode.get("access_evidence", "no-evidence")
            if access == "read":
                add_edge(repo_node, tnode, "READS", effect=eff, access="read", evidence=evidence)
            else:
                # unknown 编码为 may-write：下游只认 WRITES/READS 两种边类型，
                # 丢弃或标 READS 都会把未知当成「不相交/合规」。写足迹是可靠过近似。
                add_edge(repo_node, tnode, "WRITES", effect=eff, access=access, evidence=evidence)

    # ---- 跨仓库：把另一个仓库的入口并进图 ----
    # 跨仓库的合取链（入口在 A、条件在 B）要能自动生成，图上就得有这一跳。
    # 面的声明里给了调用点（call_site），才知道本仓库哪个方法发起了这次调用。
    xrepo_edges: list[dict[str, Any]] = []
    for repo_id, surface in (surfaces or {}).items():
        for call in surface.get("declared_outbound_calls", []):
            site = str(call.get("call_site") or "")
            if "#" not in site:
                continue
            cls, _, method = site.partition("#")
            local_id = f"{cls}#{method}"
            if local_id not in nodes:
                continue
            target = call.get("target_repo_hint") or ""
            path = call.get("path") or ""
            for other_id, other in (surfaces or {}).items():
                if other_id == repo_id:
                    continue
                if target and other_id != target:
                    continue
                provided = {f"{e.get('method', 'GET').upper()} {e.get('path', '')}"
                            for e in other.get("http_endpoints", [])}
                if f"{call.get('method', 'GET').upper()} {path}" not in provided:
                    continue
                nid = f"xrepo:{other_id}:{call.get('method', 'GET').upper()} {path}"
                add_node(nid, "XREPO_ENTRY", repo=other_id, endpoint=f"{path}",
                         method=call.get("method", "GET").upper(),
                         cross_repo=True)
                add_edge(local_id, nid, "CALLS", cross_repo=True, repo=other_id)
                xrepo_edges.append({"from": local_id, "to": nid, "repo": other_id,
                                    "method": call.get("method", "GET").upper(),
                                    "path": path})

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

    result = {
        "schema_version": 1,
        "seed": SEED,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": {t: sum(1 for n in nodes.values() if n["type"] == t)
                        for t in NODE_TYPES},
            "edges_by_type": {t: sum(1 for e in edges if e["type"] == t)
                              for t in EDGE_TYPES},
            "edges_by_access": {a: sum(1 for e in edges if e.get("access") == a)
                                for a in ("write", "read", "unknown")},
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
    if field_access is not None:
        result["counts"]["getter_read_resources"] = getter_reads_emitted
    return result


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


def generation_stamp(fn_decl: Any, class_decl: Any) -> dict[str, str]:
    """世代指纹 = 两个图构建器**共同**读的那两份声明的内容哈希。

    用内容不用 mtime：mtime 会被 cp / 还原改，内容才是决定性的。
    实测过的缺陷：relation_net.json 比 fn_declaration.json 旧 13 小时、少 47 条边，
    与新一代 units.json 一起喂给消费者 → 照常 exit 0 出「看似合理的错覆盖率」。
    **缺指纹 ≠ 同世代**，消费者必须判 UNKNOWN（三态）。
    """
    def h(obj: Any) -> str:
        return "sha256:" + hashlib.sha256(
            json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {"fn_declaration": h(fn_decl), "class_declaration": h(class_decl)}


def check_generation(net_path: Path, units_path: Path) -> int:
    """比对两份产物的世代指纹：0 同世代 / 1 错配 / 2 未知（缺指纹或读不到）。"""
    try:
        net = json.loads(net_path.read_text(encoding="utf-8"))
        units = json.loads(units_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"世代一致性 UNKNOWN：读不到产物（{exc}）")
        return 2
    a, b = net.get("generation"), units.get("generation")
    if not a or not b:
        print(f"世代一致性 UNKNOWN：缺指纹（net 有={bool(a)} / units 有={bool(b)}）"
              f"—— 缺指纹不是同世代，不许当成一致")
        return 2
    if a != b:
        diff = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        print(f"世代错配：{diff} 指纹不同 —— 一起消费会产出伪影覆盖率，拒绝当成同世代")
        print(f"  net   {a}")
        print(f"  units {b}")
        return 1
    print(f"世代一致：{a}")
    return 0


def modeling_domain_declaration(getter_reads_active: bool = False) -> dict[str, Any]:
    """建模域声明。开启验证读通道时必须显式声明通道（不得静默扩足迹语义）。"""
    dom: dict[str, Any] = {
        "cell_key_form": "FQCN#method",
        "node_types": list(NODE_TYPES),
        "method_node_types": ["HTTP_ENTRY", "SVC_OP", "REPO_OP"],
        "class_suffixes": ["Controller", "Service", "Repository"],
        "scope_note": "只建模可执行路径（入口→服务→仓储→表）上的方法细胞；"
                      "键形态不同或类名不匹配上述后缀的单元不在本图域内，"
                      "其足迹是 not_applicable（域外），不是 unknown（域内未测）",
    }
    if getter_reads_active:
        dom["footprint_channels"] = {
            "entity_getter_reads": {
                "source": "field_access.json（字节码 getfield 验证，沿继承链解析）",
                "scope": "JPA 实体 get*/is* 调用；不可验证（计算型/缺映射）保分裂不发射",
                "resource_form": "taint.source_fields 中的 setter 等价键（读通道，"
                                 "与 sinks 共享资源键以便同桶；读/写由 provenance 区分）",
                "detection_power_note": "firing-capable = 域可不相交，不是已检出违规；"
                                        "通道新增约束桶当前全是 consistent，obstructions 不变；"
                                        "覆盖≠检测力（坑64/66），桶状态须并读 frame_check 的 status",
            },
        }
    return dom


def validate_field_access(data: Any) -> dict[str, Any]:
    """校验 field_access 产物形状（边界输入，不合格即拒绝，不猜）。返回 classes 映射。"""
    classes = data.get("classes") if isinstance(data, dict) else None
    if not isinstance(classes, dict) or not classes:
        raise ValueError("field_access.json 缺非空 classes 容器")
    for fq, c in classes.items():
        if not isinstance(c, dict) or not isinstance(c.get("methods"), dict):
            raise ValueError(f"field_access 类记录不合格（缺 methods 容器）: {fq}")
    return classes


def main() -> int:
    ap = argparse.ArgumentParser(description="可执行依赖图构建器（通用引擎）")
    ap.add_argument("--out", default="", help="输出路径（默认 <产物目录>/relation_net.json）")
    ap.add_argument("--check-generation", nargs=2, type=Path, metavar=("NET", "UNITS"),
                    help="只比对两份已有产物的世代指纹，不建图（0 一致 / 1 错配 / 2 未知）")
    ap.add_argument("--artifacts-dir", type=Path,
                    help="声明产物目录（fn/class_declaration、openapi）；给了就不读项目绑定")
    ap.add_argument("--project-dir", type=Path,
                    help="项目配置目录（invariants.json、surfaces/）；默认取绑定目录")
    ap.add_argument("--getter-reads", type=Path, default=None, metavar="FIELD_ACCESS",
                    help="opt-in：字节码验证的 getter 读通道（field_access.json）；"
                         "缺省关闭，产物与关闭时逐字节不变；文件缺失/不合格拒绝出数")
    args = ap.parse_args()
    if args.check_generation:
        return check_generation(*args.check_generation)

    fa_classes = None
    fa_meta = None
    if args.getter_reads is not None:
        try:
            raw = args.getter_reads.read_bytes()
            fa_classes = validate_field_access(json.loads(raw.decode("utf-8")))
            fa_meta = {"path": str(args.getter_reads),
                       "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()[:16],
                       "classes": len(fa_classes)}
        except (OSError, ValueError) as exc:
            print(f"field_access 输入拒绝：{exc}（宁缺勿猜，不建读通道）")
            return 1

    # 显式覆盖必须绕开 artifacts_dir()：它会 load_target()，缺 target.json 直接抛。
    base = args.artifacts_dir if args.artifacts_dir else artifacts_dir()
    proj = args.project_dir if args.project_dir else project_dir()
    fn_decl = _load(base / "fn_declaration.json")
    class_decl = _load(base / "class_declaration.json")
    net = build(fn_decl, class_decl,
                _load(base / "openapi.json"),
                load_invariants(proj / "invariants.json"),
                surfaces=load_surfaces(proj / "surfaces"),
                field_access=fa_classes)
    net["generation"] = generation_stamp(fn_decl, class_decl)
    if fa_meta is not None:
        net["field_access_input"] = fa_meta
    # 显式声明本图的建模域：消费者据此把域外细胞标 not_applicable，而不是静默算成「未知足迹」。
    # 实测教训：net 是方法层的，units 含类层/文件层细胞，混在一个分母里会产出误导性覆盖率。
    net["modeling_domain"] = modeling_domain_declaration(getter_reads_active=fa_classes is not None)
    out = Path(args.out) if args.out else base / "relation_net.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 实测过陈旧产物：relation_net.json 比 fn_declaration.json 旧 → 少 47 条自调用边，
    # 而消费者读旧产物不会报错。重新生成时把「旧产物落后于哪些输入」显式打出来。
    if out.exists():
        older_than = out.stat().st_mtime
        newer = [p.name for p in (base / "fn_declaration.json", base / "class_declaration.json",
                                  base / "openapi.json", proj / "invariants.json")
                 if p.exists() and p.stat().st_mtime > older_than]
        if newer:
            print(f"  警告：现有产物落后于输入 {newer}（本次重新生成；读旧产物的消费者会少边）")
    out.write_text(json.dumps(net, ensure_ascii=False, indent=2), encoding="utf-8")
    c = net["counts"]
    print(f"可执行依赖图 -> {out}")
    print(f"  节点 {c['nodes']} / 边 {c['edges']}：{c['by_type']}")
    if fa_classes is not None:
        print(f"  getter 读通道: {c.get('getter_read_resources', 0)} 条验证读资源；"
              f"firing-capable ≠ 已检出违规（新增桶当前全是 consistent，桶状态见 frame_check）")
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
