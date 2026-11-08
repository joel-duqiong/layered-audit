"""从已有的各层声明产物，构造 Agent 群（agents.json）与全局边表（edges.json）。

**不新增任何检测器** —— 全部输入来自现有脚本已经产出的 JSON。
这一版的目的是证明：「群」是现有能力的重新组织，而不是重写。

产出：
  agents.json  每个 (role, unit) 一个 Agent 实例，带静态命/动态命/义务/关系/结论/状态
  edges.json   一张全局边表，跨 repo 共用，edge_id 全局唯一

边的解析状态（resolution）决定失效强度：
  static    静态可确定（javap/AST 直接抽到）
  inferred  推断得到（如 DI 唯一候选实现）
  observed  运行时观测到（打桩代理采集 / 真实请求）
  unknown   解不出来 —— 一律按保守失效处理，宁可多标不可漏标
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema
from agent_schema import agent_id, validate_edge

PROJECT_DIR = findings_schema.target_path().parent


def _load(name: str) -> Any:
    path = PROJECT_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _roles(categories: dict[str, tuple[str, ...]]) -> dict[str, dict[str, Any]]:
    """角色 = (层, 类别)。来自层契约，角色数与代码规模无关。"""
    carriers = findings_schema.load_target().get("carriers", {})
    layer_carriers = findings_schema.load_target().get("layer_carriers", {})
    roles: dict[str, dict[str, Any]] = {}
    for layer, cats in categories.items():
        for cat in cats:
            role_id = f"{layer}.{cat}"
            carrier = layer_carriers.get(layer, "")
            roles[role_id] = {
                "role_id": role_id,
                "layer": layer,
                "category": cat,
                "carrier": carrier,
                "fidelity": carriers.get(carrier, {}).get("fidelity", "unknown"),
            }
    return roles


def _units() -> dict[str, list[dict[str, Any]]]:
    """单元索引：函数 / 文件 / 类 / 服务 / 端到端。"""
    units: dict[str, list[dict[str, Any]]] = defaultdict(list)

    fn = _load("fn_declaration.json") or {}
    for fn_rec in fn.get("functions") or []:
        cls = fn_rec.get("class") or ""
        method = fn_rec.get("method") or ""
        units["function"].append({
            "unit_kind": "method",
            "unit_key": f"{cls}#{method}",
            "class": cls,
            "symbol": f"{cls.split('.')[-1]}.{method}",
            "file": fn_rec.get("file", ""),
            "facts": fn_rec,
        })

    fd = _load("file_declaration.json") or {}
    for rec in fd.get("files") or []:
        cls = rec.get("class") or ""
        units["file"].append({
            "unit_kind": "file",
            "unit_key": cls,
            "class": cls,
            "symbol": cls.split(".")[-1],
            "file": rec.get("file", ""),
            "facts": rec,
        })

    cd = _load("class_declaration.json") or {}
    for cls, rec in (cd.get("jpa") or {}).items():
        units["class"].append({
            "unit_kind": "class",
            "unit_key": cls,
            "class": cls,
            "symbol": cls,
            "facts": rec,
        })

    # 函数单元的源码文件：fn_declaration 不带 file 字段，
    # 从文件单元（file_declaration 有 file）按类名反查补上 ——
    # 增量调度要按文件路径定位受影响单元，这条链路不能是死的。
    class_to_file: dict[str, str] = {}
    for u in units["file"]:
        if u.get("file"):
            class_to_file.setdefault(u["class"], u["file"])
    for u in units["function"]:
        if not u.get("file"):
            u["file"] = class_to_file.get(u["class"], "")

    # 服务与端到端各是一个整体单元
    units["service"].append({
        "unit_kind": "service",
        "unit_key": "service:api",
        "class": "",
        "symbol": "api",
        "facts": {"note": "对外 HTTP 契约边界"},
    })
    units["integration"].append({
        "unit_kind": "flow",
        "unit_key": "flow:end-to-end",
        "class": "",
        "symbol": "end-to-end",
        "facts": {"note": "API/DB/前端三方对账 + 攻击链"},
    })
    return units


def _callee_key(callee: str) -> str:
    """调用方名归一：'a.b.C.m' / 'a.b.C#m' -> 'a.b.C#m'。"""
    if "#" in callee:
        return callee
    cls, _, method = callee.rpartition(".")
    return f"{cls}#{method}" if cls else callee


def _edges(units: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """全局边表：函数级 call、类级 type_use。"""
    edges: list[dict[str, Any]] = []
    counter = 0

    by_key: dict[str, str] = {}
    for layer in ("function", "class", "file"):
        for u in units.get(layer, []):
            by_key[u["unit_key"]] = agent_id(u["unit_kind"], u["unit_key"])

    fn = _load("fn_declaration.json") or {}
    for fn_rec in fn.get("functions") or []:
        src = by_key.get(f"{fn_rec.get('class')}#{fn_rec.get('method')}")
        if not src:
            continue
        for callee in fn_rec.get("internal_calls") or []:
            dst = by_key.get(_callee_key(callee))
            counter += 1
            edges.append({
                "edge_id": f"E-{counter:06d}",
                "kind": "call",
                "from_agent_id": src,
                "to_agent_id": dst or f"fn:UNRESOLVED#{callee}",
                "resolution": "static" if dst else "unknown",
                "stability": "behavior",
                "invalidation": "reverify_caller",
                "call_count": 1,
            })
        for callee in fn_rec.get("external_calls") or []:
            counter += 1
            edges.append({
                "edge_id": f"E-{counter:06d}",
                "kind": "call",
                "from_agent_id": src,
                "to_agent_id": f"ext:{callee}",
                "resolution": "static",
                "stability": "semantic",
                "invalidation": "recheck_boundary",
                "call_count": 1,
            })

    fd = _load("file_declaration.json") or {}
    for rec in fd.get("cross_file_edges") or []:
        src = by_key.get(rec.get("from", ""))
        dst = by_key.get(rec.get("to", ""))
        if not src or not dst:
            continue
        counter += 1
        edges.append({
            "edge_id": f"E-{counter:06d}",
            "kind": "type_use",
            "from_agent_id": src,
            "to_agent_id": dst,
            "resolution": "static",
            "stability": "semantic",
            "invalidation": "recheck_consumer",
            "call_count": rec.get("calls", 1),
        })
    return edges


def _evidence_index(units: dict[str, list[dict[str, Any]]]
                    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """动态用例按目标函数索引；发现按 unit 索引。

    发现里的 location.symbol 常是「简单类名.方法名」（OrderService.apply），
    而 unit_key 是全限定名（com.example.svc.OrderService#apply）。
    不做简单名→全名的解析，文件层单元就会永远关联不上自己的 finding，
    在盲区地图里被误标成「完全没查」。
    """
    simple_to_fqn: dict[str, str] = {}
    for layer_units in units.values():
        for u in layer_units:
            key = u["unit_key"]
            simple = key.split("#")[0].rsplit(".", 1)[-1]
            simple_to_fqn.setdefault(simple, key.split("#")[0])

    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fo = _load("fn_observation.json") or {}
    for case in fo.get("cases") or []:
        target = case.get("target") or ""
        if "#" in target:
            cases[target].append(case)

    findings_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    af = _load("all_findings.json") or {}
    for rec in af.get("findings") or []:
        loc = rec.get("location") or {}
        sym = loc.get("symbol") or ""
        simple = sym.split(".")[0] if "." in sym else ""
        method = sym.split(".")[-1] if sym else ""
        fqn = simple_to_fqn.get(simple, "")
        keys = {k for k in (f"{fqn}#{method}" if fqn else "", f"{simple}#{method}" if simple else "",
                            sym, loc.get("file", "")) if k}
        for k in keys:
            findings_by_unit[k].append(rec)
    return cases, findings_by_unit


def _build_units(roles: dict[str, dict[str, Any]],
                 units: dict[str, list[dict[str, Any]]],
                 edges: list[dict[str, Any]],
                 templates: dict[str, str]) -> list[dict[str, Any]]:
    """单元索引 —— 每个可寻址的代码实体一条记录。

    **刻意不物化「Agent 实例」**：一个实例 = (单元 × 角色)，533×19 = 2071 条，
    但绝大部分既没有观测也没有结论，字段全是占位符 —— 那是纯成本。
    真正有意义的聚合是「这个单元该被哪些判定手段检验、现在验过没有、有没有结论」，
    这三样都能实时算，不需要落盘。

    「Agent 实例」这个概念保留在概念层：判定某单元时，(单元, 判定手段) 就是一次
    Agent 作业；它有观测或结论时才有内容。
    """
    cases, findings_by_unit = _evidence_index(units)
    centrality: dict[str, int] = defaultdict(int)
    for e in edges:
        centrality[e["to_agent_id"]] += int(e.get("call_count") or 1)

    # 逻辑密度：只看「这个单元有多少值得测的分支/副作用/守卫」
    logic: dict[str, dict[str, Any]] = {}
    fn = _load("fn_declaration.json") or {}
    for rec in fn.get("functions") or []:
        key = f"{rec.get('class')}#{rec.get('method')}"
        branches = int(rec.get("branch_count") or 0)
        effects = len(rec.get("side_effects") or [])
        guards = int(rec.get("null_guard_count") or 0)
        logic[key] = {
            "branches": branches, "side_effects": effects, "null_guards": guards,
            "is_pure": bool(rec.get("is_pure")),
            "score": branches + effects * 3 + guards + (0 if rec.get("is_pure") else 5),
        }
    member_logic: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"branches": 0, "side_effects": 0, "null_guards": 0,
                 "is_pure": True, "score": 0})
    for key, d in logic.items():
        agg = member_logic[key.split("#")[0]]
        for f in ("branches", "side_effects", "null_guards", "score"):
            agg[f] += d[f]
        agg["is_pure"] = agg["is_pure"] and d["is_pure"]

    out: list[dict[str, Any]] = []
    for layer, layer_units in units.items():
        layer_roles = sorted(r["role_id"] for r in roles.values() if r["layer"] == layer)
        for unit in layer_units:
            key = unit["unit_key"]
            unit_cases = cases.get(key, [])
            unit_findings = findings_by_unit.get(key, []) or findings_by_unit.get(unit["symbol"], [])
            violated = [f for f in unit_findings if f.get("kind") != "positive"]
            positive = [f for f in unit_findings if f.get("kind") == "positive"]
            # 单元状态要看**用例的判定结果**，不能只看「有没有用例」——
            # 否则一组全 PASS 的用例会被记成「有观测但义务未定」，
            # 覆盖率被系统性低估（实测 13 个单元受影响）。
            verdicts = {c.get("verdict") for c in unit_cases}
            failing = [c for c in unit_cases if c.get("verdict") == "FAIL"]
            passing = [c for c in unit_cases if c.get("verdict") == "PASS"]
            # 该单元「被判定为满足」与「被判定为违反」的义务类别。
            # 类别来自用例文件声明的分组映射（用例带 category）。
            # 义务是 (层, 类别) 对：函数层单元的义务只能是 function.* 类别。
            # 别的层的类别（比如类层的 inheritance_design）不能记到函数层单元头上 ——
            # 那是别的层的义务，这一层只是提供了一个观测。
            def _own(cat: str) -> bool:
                return bool(cat) and cat.partition(".")[0] == layer

            satisfied_categories = sorted({c["category"] for c in passing
                                           if c.get("category") and _own(c["category"])})
            violated_categories = sorted({c["category"] for c in failing
                                          if c.get("category") and _own(c["category"])}
                                         | {f.get("category") for f in violated
                                            if f.get("category")
                                            and f.get("category") != "UNMAPPED"
                                            and _own(f.get("category"))})
            if violated or failing:
                status = "FAIL"
            elif passing:
                status = "PASS"
            elif unit_cases:
                status = "OBSERVED"
            elif positive:
                status = "PASS"
            else:
                status = "UNKNOWN"
            lg = logic.get(key) or member_logic.get(key) or {
                "branches": 0, "side_effects": 0, "null_guards": 0,
                "is_pure": True, "score": 0}
            out.append({
                "unit_key": key,
                "kind": unit["unit_kind"],
                "layer": layer,
                "class": unit.get("class", ""),
                "symbol": unit["symbol"],
                "file": unit.get("file", ""),
                "role_count": len(layer_roles),
                "centrality": centrality.get(agent_id(unit["unit_kind"], key), 0),
                "logic": lg,
                "priority": centrality.get(agent_id(unit["unit_kind"], key), 0) * (1 + lg["score"]),
                "status": status,
                "observations": len(unit_cases),
                "satisfied_categories": satisfied_categories,
                "violated_categories": violated_categories,
                "case_ids": [c["id"] for c in unit_cases][:20],
                "finding_ids": [f["uid"] for f in unit_findings][:20],
                "root_cause_keys": sorted({f.get("root_cause_key", "") for f in unit_findings
                                           if f.get("root_cause_key")}),
                "obligation_template": templates.get(layer_roles[0], "") if layer_roles else "",
                "obligation_source_layer": layer,
            })
    return out


def main() -> None:
    target = findings_schema.load_target()
    categories = findings_schema.load_layer_categories()
    templates = target.get("obligation_templates", {})
    roles = _roles(categories)
    units = _units()
    edges = _edges(units)
    unit_records = _build_units(roles, units, edges, templates)

    problems: list[str] = []
    for u in unit_records:
        if not u["unit_key"] or not u["role_count"]:
            problems.append(f"{u.get('unit_key')}: 单元缺少 unit_key 或 roles")
    for e in edges:
        for p in validate_edge(e):
            problems.append(f"{e['edge_id']}: {p}")

    payload = {
        "schema_version": 2,
        "repo_id": target.get("target", ""),
        "roles": list(roles.values()),
        "layer_roles": {lay: sorted(r["role_id"] for r in roles.values() if r["layer"] == lay)
                        for lay in {r["layer"] for r in roles.values()}},
        "counts": {
            "units": len(unit_records),
            "roles": len(roles),
            "edges": len(edges),
            "edges_unresolved": sum(1 for e in edges if e["resolution"] == "unknown"),
            "units_with_observations": sum(1 for u in unit_records if u["observations"]),
            "units_with_findings": sum(1 for u in unit_records if u["finding_ids"]),
        },
        "units": unit_records,
    }
    (PROJECT_DIR / "units.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_DIR / "edges.json").write_text(
        json.dumps({"schema_version": 1, "edges": edges}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    c = payload["counts"]
    print(f"单元索引 -> {PROJECT_DIR / 'units.json'}")
    print(f"  单元 {c['units']} / 角色 {c['roles']} / 边 {c['edges']}"
          f"（未解析边 {c['edges_unresolved']}）")
    print(f"  有动态观测的单元 {c['units_with_observations']} / 有结论的 {c['units_with_findings']}")
    if problems:
        print(f"  契约违规 {len(problems)} 条：")
        for p in problems[:10]:
            print(f"    {p}")
    else:
        print("  契约违规 0 条")


if __name__ == "__main__":
    main()
