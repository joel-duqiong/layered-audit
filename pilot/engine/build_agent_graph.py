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
from agent_schema import agent_id, validate_agent, validate_edge

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


def _build_instances(roles: dict[str, dict[str, Any]], units: dict[str, list[dict[str, Any]]],
                     templates: dict[str, str]) -> list[dict[str, Any]]:
    cases, findings_by_unit = _evidence_index(units)
    instances: list[dict[str, Any]] = []

    for layer, layer_units in units.items():
        layer_roles = [r for r in roles.values() if r["layer"] == layer]
        if not layer_roles:
            continue
        for unit in layer_units:
            key = unit["unit_key"]
            uid = agent_id(unit["unit_kind"], key)
            unit_cases = cases.get(key, [])
            unit_findings = findings_by_unit.get(key, []) or findings_by_unit.get(unit["symbol"], [])

            obligations = []
            for role in layer_roles:
                violated = [f for f in unit_findings if f.get("category") == role["role_id"]]
                positive = [f for f in unit_findings
                            if f.get("category") == role["role_id"]
                            and f.get("kind") == "positive"]
                if violated:
                    status, evidence = "violated", violated
                elif positive:
                    status, evidence = "satisfied", positive
                else:
                    status, evidence = "unknown", []
                obligations.append({
                    "obligation_id": f"OB-{role['role_id'].replace('.', '-')}-{key}",
                    "statement": templates.get(role["role_id"], role["category"]),
                    "kind": "invariant",
                    "oracle": role.get("category"),
                    "scope": "unit",
                    "status": status,
                    "source": "declared" if status != "unknown" else "declared",
                    "gate_eligible": status != "unknown",
                    "evidence_refs": [f["uid"] for f in evidence][:3],
                    "confidence": max([f.get("confidence", 0) for f in evidence], default=0.0),
                })

            status = "PASS"
            if any(o["status"] == "violated" for o in obligations):
                status = "FAIL"
            elif any(o["status"] == "unknown" for o in obligations):
                status = "UNKNOWN"

            satisfied = sum(1 for o in obligations if o["status"] == "satisfied")
            violated_n = sum(1 for o in obligations if o["status"] == "violated")
            unknown = sum(1 for o in obligations if o["status"] == "unknown")
            total = len(obligations) or 1

            instances.append({
                "agent_id": uid,
                "role_id": "",
                "layer": layer,
                "unit": {
                    "unit_kind": unit["unit_kind"],
                    "unit_key": key,
                    "file": unit.get("file", ""),
                    "symbol": unit["symbol"],
                    "line": 0,
                },
                "mission": {
                    "statement": f"{unit['symbol']} 需要满足 {layer} 层的判定要求",
                    "source": "derived",
                    "confidence": 0.5,
                    "gate_eligible": True,
                    "obligations": obligations,
                },
                "static_life": {
                    "carrier_id": "",
                    "facts": unit["facts"],
                    "contract_fingerprint": _sha(unit["facts"]),
                    "blind_to": [],
                },
                "dynamic_life": {
                    "fidelity": "unknown",
                    "exercised": bool(unit_cases),
                    "cases": [{"case_id": c["id"], "verdict": c["verdict"],
                               "invariant": c.get("invariant", "")}
                              for c in unit_cases[:20]],
                    "behavior_fingerprint": _sha([c["verdict"] for c in unit_cases]),
                },
                "relations": {"in_edges": [], "out_edges": []},
                "conclusions": {
                    "agent_status": status,
                    "fulfillment": {"satisfied": satisfied, "violated": violated_n,
                                    "unknown": unknown, "total": total,
                                    "ratio": round(satisfied / total, 3)},
                    "coverage": {"exercised": len(unit_cases), "total": total,
                                 "ratio": round(len(unit_cases) / total, 3)},
                    "finding_ids": [f["uid"] for f in unit_findings][:10],
                    "root_cause_keys": sorted({f.get("root_cause_key", "") for f in unit_findings}),
                    "owner_layer": layer,
                },
                "state": {"status": "fresh", "stale_reason": None,
                          "baseline_key": f"{uid}@{_sha(unit['facts'])}"},
            })
    return instances


def main() -> None:
    target = findings_schema.load_target()
    categories = findings_schema.load_layer_categories()
    templates = target.get("obligation_templates", {})
    roles = _roles(categories)
    units = _units()
    edges = _edges(units)
    instances = _build_instances(roles, units, templates)

    # 给每个 role 各实例化一份（role × unit）
    expanded: list[dict[str, Any]] = []
    for inst in instances:
        for role in roles.values():
            if role["layer"] != inst["layer"]:
                continue
            clone = json.loads(json.dumps(inst, ensure_ascii=False))
            clone["role_id"] = role["role_id"]
            clone["mission"]["obligations"] = [
                o for o in inst["mission"]["obligations"]
                if o["obligation_id"].startswith(f"OB-{role['role_id'].replace('.', '-')}-")
            ]
            clone["static_life"]["carrier_id"] = role["carrier"]
            clone["dynamic_life"]["fidelity"] = role["fidelity"]
            expanded.append(clone)

    # 关系回填
    by_id = {i["agent_id"] + "|" + i["role_id"]: i for i in expanded}
    for e in edges:
        for inst in expanded:
            if inst["agent_id"] == e["from_agent_id"]:
                inst["relations"]["out_edges"].append(e["edge_id"])
            if inst["agent_id"] == e["to_agent_id"]:
                inst["relations"]["in_edges"].append(e["edge_id"])

    problems: list[str] = []
    for inst in expanded:
        for p in validate_agent(inst):
            problems.append(f"{inst['agent_id']}|{inst['role_id']}: {p}")
    for e in edges:
        for p in validate_edge(e):
            problems.append(f"{e['edge_id']}: {p}")

    payload = {
        "schema_version": 1,
        "repo_id": target.get("target", ""),
        "roles": list(roles.values()),
        "counts": {
            "units": len(units.get("function", [])) + len(units.get("file", []))
                     + len(units.get("class", [])) + len(units.get("service", []))
                     + len(units.get("integration", [])),
            "roles": len(roles),
            "agents": len(expanded),
            "edges": len(edges),
            "edges_unresolved": sum(1 for e in edges if e["resolution"] == "unknown"),
        },
        "agents": expanded,
    }
    (PROJECT_DIR / "agents.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT_DIR / "edges.json").write_text(
        json.dumps({"schema_version": 1, "edges": edges}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    c = payload["counts"]
    print(f"Agent 群 -> {PROJECT_DIR / 'agents.json'}")
    print(f"  单元 {c['units']} / 角色 {c['roles']} / Agent 实例 {c['agents']} / 边 {c['edges']}"
          f"（未解析边 {c['edges_unresolved']}）")
    if problems:
        print(f"  契约违规 {len(problems)} 条：")
        for p in problems[:10]:
            print(f"    {p}")
    else:
        print("  契约违规 0 条")


if __name__ == "__main__":
    main()
