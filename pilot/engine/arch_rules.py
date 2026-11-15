"""架构禁止边检测 —— 细胞图上「哪些调用不允许发生」。

规则全部来自外部配置，引擎里没有项目字面量。三态：resolution 不是 static 的边
既不算合规也不算违规，单列 unknown。类别归属由配置声明、按层契约校验，装不下就
标 UNMAPPED 并给理由，不静默丢弃，也不在运行期造类别。

命中 0 条不等于绿灯：每条规则都报 in_scope_edges、scope_role_classes 与 verdict。
RULE_UNMATCHED 表示规则引用的角色在图里一个类都没匹配到（模式没配对，规则不可证伪），
NO_VIOLATION_IN_SCOPE 才是「域内确实没有违规边」。

两个命题分开记账，不混算：
- basis=resolved_target（默认）判「运行时目标」，resolution 非 static 一律 unknown。
- basis=declared_type 判「声明侧依赖」—— invoke 指令的声明接收者类型是编译期客观事实，
  接口/代理导致的 UNRESOLVED 不影响它。产出进 declared_type_findings 独立通道，
  每条带 runtime_resolution，绝不回填进 resolved_target 的 findings/unknown。
角色优先用外部机制事实（注解/接口继承等）判定，名字模式只作兜底，两者分歧显式上报。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import findings_schema

CONFIG_SCHEMA = "arch-rules/1"
REPORT_SCHEMA = "arch-rule-audit/1"
UNMAPPED = "UNMAPPED"

VIOLATION = "violation"
COMPLIANT = "compliant"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"

EXTERNAL = "external"
UNCLASSIFIED = "unclassified"
RESOLUTION_STATIC = "static"

RULE_TYPES = ("forbidden_call", "layer_skip", "visibility", "dependency_direction")
DEFAULT_EDGE_KINDS = {
    "forbidden_call": ("call",),
    "layer_skip": ("call",),
    "visibility": ("call",),
    "dependency_direction": ("call", "type_use"),
}
VISIBILITY_LEVELS = ("public", "protected", "private", "package_private")
BASES = ("resolved_target", "declared_type")
RESOLVED = "resolved_target"
DECLARED = "declared_type"
UNIT_PREFIX = {"method": "fn", "class": "cls", "file": "file", "service": "svc", "flow": "flow"}
UNRESOLVED_MARK = "UNRESOLVED#"

VERDICT_UNMATCHED = "RULE_UNMATCHED"
VERDICT_NO_EDGE = "NO_EDGE_IN_SCOPE"
VERDICT_VIOLATIONS = "VIOLATIONS_FOUND"
VERDICT_UNKNOWN_ONLY = "UNKNOWN_ONLY"
VERDICT_PARTIAL = "PARTIAL_UNKNOWN"
VERDICT_CLEAN = "NO_VIOLATION_IN_SCOPE"


def parse_endpoint(raw: Any) -> dict[str, Any]:
    text = str(raw or "")
    prefix, sep, body = text.partition(":")
    if not sep or not body:
        return {"reference": text, "class": "", "method": "", "scope": "unparsed"}
    if prefix == "ext":
        cls, _, method = body.rpartition(".")
        return {"reference": text, "class": cls or body, "method": method, "scope": EXTERNAL}
    if body.startswith(UNRESOLVED_MARK):
        ref = body[len(UNRESOLVED_MARK):]
        # 上游有两种编码：UNRESOLVED#fqcn.method 与 UNRESOLVED#fqcn#method。
        # 只按点号切会把类名切成包名，角色判不出来。
        if "#" in ref:
            cls, _, method = ref.partition("#")
        else:
            cls, _, method = ref.rpartition(".")
            cls = cls or ref
        return {"reference": text, "class": cls, "method": method, "scope": "unresolved"}
    cls, hash_sep, method = body.partition("#")
    return {"reference": text, "class": cls, "method": method if hash_sep else "", "scope": "project"}


def classify(endpoint: dict[str, Any], roles: list[dict[str, Any]],
             facts: dict[str, str] | None = None) -> str:
    if endpoint["scope"] == EXTERNAL:
        return EXTERNAL
    cls = endpoint["class"]
    if endpoint["scope"] == "unparsed" or not cls:
        return UNCLASSIFIED
    if facts and cls in facts:
        return facts[cls]
    for role in roles:
        if any(fnmatch.fnmatchcase(cls, pattern) for pattern in role["class_patterns"]):
            return role["id"]
    return UNCLASSIFIED


DEFAULT_MEMBER_SOURCE = {"records": "members", "class": "class", "method": "method", "modifiers": "modifiers"}
DEFAULT_FACT_SOURCE = {"records": "facts", "class": "class", "mechanism": "mechanism"}


def extract_records(data: Any, spec: dict[str, str], label: str) -> list[Any]:
    """按配置声明的形状从已有产物取记录，引擎里不写死任何产物的键名。"""
    if not isinstance(data, dict):
        raise ValueError(f"{label} 输入必须是对象")
    container = data.get(spec["records"])
    if isinstance(container, dict):
        return list(container.values())
    if isinstance(container, list):
        return container
    raise ValueError(f"{label} 输入里找不到记录容器 {spec['records']!r}")


def validate_record_source(spec: Any, fields: tuple[str, ...], defaults: dict[str, str], label: str) -> Any:
    """接受单个形状或候选形状列表；列表用于同一配置吃多种已有产物容器。"""
    if spec is None:
        return dict(defaults)
    candidates = spec if isinstance(spec, list) else [spec]
    if not candidates:
        raise ValueError(f"{label}_source 候选列表不得为空")
    out = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError(f"{label}_source 每项必须是对象")
        row = {}
        for field in ("records",) + fields:
            value = candidate.get(field, defaults[field])
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}_source.{field} 必须是非空字符串")
            row[field] = value
        out.append(row)
    return out[0] if isinstance(spec, dict) else out


def select_source(data: Any, specs: Any, label: str) -> dict[str, str]:
    candidates = specs if isinstance(specs, list) else [specs]
    tried = []
    for spec in candidates:
        container = data.get(spec["records"]) if isinstance(data, dict) else None
        if isinstance(container, (list, dict)):
            return spec
        tried.append(spec["records"])
    raise ValueError(f"{label} 输入里找不到任何已声明的记录容器 {tried}")


def load_role_facts(data: Any, role_mechanisms: dict[str, Any],
                    spec: Any = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """机制事实 -> 角色。同一个类被多种机制指向时不挑一个，记冲突并退回名字模式。"""
    spec = select_source(data, spec or DEFAULT_FACT_SOURCE, "role_facts")
    by_class: dict[str, set[str]] = {}
    for row in extract_records(data, spec, "role_facts"):
        if not isinstance(row, dict):
            raise ValueError("role_facts 记录必须是对象")
        cls, mechanism = row.get(spec["class"]), row.get(spec["mechanism"])
        if not isinstance(cls, str) or not cls or not isinstance(mechanism, str) or not mechanism:
            continue
        for role, mechanisms in role_mechanisms.items():
            if mechanism in mechanisms:
                by_class.setdefault(cls, set()).add(role)
    facts: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for cls, roles in sorted(by_class.items()):
        if len(roles) == 1:
            facts[cls] = next(iter(roles))
        else:
            conflicts.append({"class": cls, "roles": sorted(roles)})
    return facts, conflicts


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"规则配置必须是 {CONFIG_SCHEMA}")
    roles_raw, rules_raw = data.get("roles"), data.get("rules")
    if not isinstance(roles_raw, list) or not isinstance(rules_raw, list):
        raise ValueError("规则配置必须含 roles 与 rules 数组")

    roles: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for item in roles_raw:
        if not isinstance(item, dict):
            raise ValueError("role 必须是对象")
        rid, patterns = item.get("id"), item.get("class_patterns")
        if not isinstance(rid, str) or not rid.strip() or rid in seen_roles:
            raise ValueError(f"role id 必须唯一且非空: {rid!r}")
        if (not isinstance(patterns, list) or not patterns
                or not all(isinstance(p, str) and p.strip() for p in patterns)):
            raise ValueError(f"role {rid} 的 class_patterns 必须是非空字符串数组")
        seen_roles.add(rid)
        roles.append({"id": rid, "class_patterns": list(patterns)})

    rules: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for item in rules_raw:
        if not isinstance(item, dict):
            raise ValueError("rule 必须是对象")
        rid, rtype = item.get("id"), item.get("type")
        if not isinstance(rid, str) or not rid.strip() or rid in seen_rules:
            raise ValueError(f"rule id 必须唯一且非空: {rid!r}")
        if rtype not in RULE_TYPES:
            raise ValueError(f"规则 {rid} 类型非法: {rtype!r}，支持 {RULE_TYPES}")
        severity, claim = item.get("severity"), item.get("claim")
        if not isinstance(severity, str) or not severity.strip():
            raise ValueError(f"规则 {rid} 必须声明非空 severity")
        if not isinstance(claim, str) or not claim.strip():
            raise ValueError(f"规则 {rid} 必须声明非空 claim")
        kinds = item.get("edge_kinds")
        if kinds is None:
            kinds = list(DEFAULT_EDGE_KINDS[rtype])
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) and k for k in kinds):
            raise ValueError(f"规则 {rid} 的 edge_kinds 必须是非空字符串数组")
        source = item.get("source")
        basis = item.get("basis", RESOLVED)
        if basis not in BASES:
            raise ValueError(f"规则 {rid} 的 basis 必须是 {BASES}，收到 {basis!r}")
        if basis == DECLARED and rtype == "visibility":
            raise ValueError(f"规则 {rid}: visibility 判的是成员修饰符，声明侧类型给不出，不支持 declared_type")
        rule: dict[str, Any] = {
            "id": rid, "type": rtype, "severity": severity, "claim": claim,
            "edge_kinds": tuple(kinds), "basis": basis,
            "layer": item.get("layer"), "category": item.get("category"),
            "unmapped_reason": item.get("unmapped_reason"),
            "contract_proposal": item.get("contract_proposal") if isinstance(item.get("contract_proposal"), dict) else None,
            "source": source if isinstance(source, dict) else {},
        }
        if rtype in ("forbidden_call", "dependency_direction"):
            frm, to = item.get("from"), item.get("to")
            if frm not in seen_roles or to not in seen_roles:
                raise ValueError(f"规则 {rid} 的 from/to 必须是已声明角色: {frm!r} -> {to!r}")
            rule["from"], rule["to"] = frm, to
        elif rtype == "layer_skip":
            chain = item.get("chain")
            if (not isinstance(chain, list) or len(chain) < 2
                    or not all(isinstance(c, str) and c in seen_roles for c in chain)
                    or len(set(chain)) != len(chain)):
                raise ValueError(f"规则 {rid} 的 chain 必须是至少 2 个互不重复的已声明角色")
            rule["chain"] = list(chain)
        else:
            levels = item.get("levels")
            if (not isinstance(levels, list) or not levels
                    or not all(isinstance(l, str) and l in VISIBILITY_LEVELS for l in levels)):
                raise ValueError(f"规则 {rid} 的 levels 必须是 {VISIBILITY_LEVELS} 的非空子集")
            rule["levels"] = list(levels)
            rule["exempt_same_top_level_class"] = bool(item.get("exempt_same_top_level_class", False))
        seen_rules.add(rid)
        rules.append(rule)

    mechanisms = data.get("role_mechanisms") or {}
    if not isinstance(mechanisms, dict) or not all(
            isinstance(k, str) and k in seen_roles and isinstance(v, list) and v
            and all(isinstance(m, str) and m for m in v) for k, v in mechanisms.items()):
        raise ValueError("role_mechanisms 必须是 {已声明角色: 非空机制名数组}")
    opaque = data.get("opaque_dispatch_patterns") or []
    if not isinstance(opaque, list) or not all(isinstance(p, str) and p.strip() for p in opaque):
        raise ValueError("opaque_dispatch_patterns 必须是字符串数组")
    forms = data.get("unprojected_dispatch_patterns") or []
    if not isinstance(forms, list):
        raise ValueError("unprojected_dispatch_patterns 必须是数组")
    seen_forms: set[str] = set()
    for form in forms:
        if not isinstance(form, dict) or not isinstance(form.get("id"), str) or not form["id"].strip():
            raise ValueError("unprojected_dispatch_patterns 每项必须含非空 id")
        if form["id"] in seen_forms:
            raise ValueError(f"unprojected_dispatch_patterns id 重复: {form['id']}")
        if not isinstance(form.get("regex"), str) or not form["regex"]:
            raise ValueError(f"dispatch 形式 {form['id']} 必须含非空 regex")
        try:
            compiled = re.compile(form["regex"])
        except re.error as exc:
            raise ValueError(f"dispatch 形式 {form['id']} 的 regex 非法: {exc}") from exc
        if compiled.groups != 2:
            raise ValueError(f"dispatch 形式 {form['id']} 的 regex 必须恰好 2 个捕获组（类型提示, 成员提示）")
        seen_forms.add(form["id"])
    markers = data.get("unknown_markers") or []
    if not isinstance(markers, list):
        raise ValueError("unknown_markers 必须是数组")
    for marker in markers:
        if not isinstance(marker, dict) or not isinstance(marker.get("field"), str) or not marker["field"].strip():
            raise ValueError("unknown_markers 每项必须含非空 field")
        values = marker.get("values")
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise ValueError(f"unknown_markers.{marker['field']} 的 values 必须是非空字符串数组")
    induction = data.get("induction")
    if induction is not None:
        if not isinstance(induction, dict):
            raise ValueError("induction 必须是对象")
        support = induction.get("min_support", 5)
        if not isinstance(support, int) or isinstance(support, bool) or support < 1:
            raise ValueError("induction.min_support 必须是 >=1 的整数")
        basis = induction.get("basis", DECLARED)
        if basis not in BASES:
            raise ValueError(f"induction.basis 必须是 {BASES}")
        induction = {"min_support": support, "basis": basis}
    member_source = validate_record_source(data.get("member_source"), ("class", "method", "modifiers"),
                                           DEFAULT_MEMBER_SOURCE, "member")
    fact_source = validate_record_source(data.get("role_fact_source"), ("class", "mechanism"),
                                         DEFAULT_FACT_SOURCE, "role_fact")
    root_key = data.get("source_root_key")
    if root_key is not None and (not isinstance(root_key, str) or not root_key.strip()):
        raise ValueError("source_root_key 必须是非空字符串（target.json 里的点号键路径）")
    revision = data.get("contract_revision_proposal")
    if revision is not None and not isinstance(revision, dict):
        raise ValueError("contract_revision_proposal 必须是对象")
    member_inputs = data.get("member_inputs") or []
    if not isinstance(member_inputs, list) or not all(
            isinstance(m, dict) and isinstance(m.get("label"), str) and m["label"].strip()
            and isinstance(m.get("path"), str) and m["path"].strip() for m in member_inputs):
        raise ValueError("member_inputs 必须是 [{label, path, ...}] 数组")
    prerequisites = data.get("repro_prerequisites") or []
    if not isinstance(prerequisites, list) or not all(isinstance(p, str) and p.strip() for p in prerequisites):
        raise ValueError("repro_prerequisites 必须是非空字符串数组")
    return {"schema": CONFIG_SCHEMA, "roles": roles, "rules": rules,
            "role_mechanisms": mechanisms, "opaque_dispatch_patterns": list(opaque),
            "unprojected_dispatch_patterns": forms, "induction": induction,
            "unknown_markers": markers, "member_source": member_source,
            "role_fact_source": fact_source, "source_root_key": root_key,
            "contract_revision_proposal": revision, "member_inputs": member_inputs,
            "repro_prerequisites": prerequisites}


def resolve_mapping(rule: dict[str, Any], categories: dict[str, Any]) -> dict[str, Any]:
    layer, category = rule.get("layer"), rule.get("category")
    declared_reason = rule.get("unmapped_reason")

    def unmapped(why: str) -> dict[str, Any]:
        return {"layer": layer if isinstance(layer, str) and layer else None,
                "category": UNMAPPED, "category_fit": "unmapped",
                "unmapped_reason": declared_reason if isinstance(declared_reason, str) and declared_reason.strip() else why}

    if not isinstance(layer, str) or not layer or not isinstance(category, str) or not category:
        return unmapped("规则未声明 layer/category，层契约里没有它的归属")
    if layer not in categories:
        return unmapped(f"层契约没有层 {layer}")
    if category not in categories[layer]:
        return unmapped(f"层 {layer} 的类别列表不含 {category}")
    return {"layer": layer, "category": f"{layer}.{category}", "category_fit": "exact", "unmapped_reason": ""}


def visibility_level(modifiers: Any) -> str | None:
    if not isinstance(modifiers, str):
        return None
    tokens = modifiers.split()
    for level in ("public", "protected", "private"):
        if level in tokens:
            return level
    return "package_private"


def top_level_class(cls: str) -> str:
    return cls.split("$", 1)[0]


def member_row_ok(row: Any, spec: dict[str, str]) -> bool:
    return (isinstance(row, dict)
            and isinstance(row.get(spec["class"]), str) and bool(row[spec["class"]])
            and isinstance(row.get(spec["method"]), str) and bool(row[spec["method"]])
            and isinstance(row.get(spec["modifiers"]), str))


def member_record_stats(data: Any, spec: Any = None) -> dict[str, Any]:
    """区分「容器空」与「容器有记录但全不合格」——两者都不得出数，但原因必须分开报。"""
    chosen = select_source(data, spec or DEFAULT_MEMBER_SOURCE, "members")
    rows = extract_records(data, chosen, "members")
    valid = sum(1 for row in rows if member_row_ok(row, chosen))
    return {"container": chosen["records"], "records_seen": len(rows),
            "records_valid": valid, "records_skipped": len(rows) - valid}


def load_members(data: Any, spec: Any = None) -> dict[tuple[str, str], Any]:
    spec = select_source(data, spec or DEFAULT_MEMBER_SOURCE, "members")
    out: dict[tuple[str, str], Any] = {}
    for row in extract_records(data, spec, "members"):
        if not isinstance(row, dict):
            raise ValueError("members 记录必须是对象")
        if not member_row_ok(row, spec):
            # 异构容器（如关系网里混着表/字段节点）跳过；跳过不是静默的——
            # 它会变成 member_coverage.declaration_missing 与输入告警。
            continue
        cls, method, modifiers = row[spec["class"]], row[spec["method"]], row[spec["modifiers"]]
        key = (cls, method)
        if key in out:
            # 重载同名合并：修饰符不一致时不挑一个，记为不可判定。
            if out[key] != modifiers:
                out[key] = None
        else:
            out[key] = modifiers
    return out


def build_unit_index(units_doc: Any) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    rows = units_doc.get("units") if isinstance(units_doc, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        prefix = UNIT_PREFIX.get(row.get("kind"))
        key = row.get("unit_key")
        if prefix and isinstance(key, str) and key:
            index.setdefault(f"{prefix}:{key}", []).append(row)
    return index


def unit_location(index: dict[str, list[dict[str, Any]]], reference: str) -> tuple[str, str]:
    rows = index.get(reference) or []
    if not rows:
        return "", "no_unit"
    files = sorted({r["file"] for r in rows if isinstance(r.get("file"), str) and r["file"]})
    if len(files) > 1:
        return "", "ambiguous_unit"
    if not files:
        return "", "unit_without_file"
    return files[0], "ok"


def source_path(root: Path | None, rel: str) -> Path | None:
    if root is None or not rel or "\\" in rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return root / rel


def locate_call(root: Path | None, rel: str, method: str) -> dict[str, Any]:
    """行号不在边快照里，只能回源码文本定位；多命中/零命中都不给唯一行。"""
    out: dict[str, Any] = {"file": rel, "line": None, "symbol": method,
                           "line_status": "no_source_root", "call_text": "", "line_candidates": []}
    path = source_path(root, rel)
    if path is None:
        out["line_status"] = "no_source_root" if root is None or not rel else "unsafe_source_path"
        return out
    if not path.is_file():
        out["line_status"] = "source_missing"
        return out
    if not method:
        out["line_status"] = "no_callee_symbol"
        return out
    pattern = re.compile(r"\.\s*" + re.escape(method) + r"\s*\(")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [(i, lines[i - 1].strip()) for i in range(1, len(lines) + 1) if pattern.search(lines[i - 1])]
    out["line_candidates"] = [i for i, _ in hits]
    if len(hits) == 1:
        out["line"], out["call_text"], out["line_status"] = hits[0][0], hits[0][1], "resolved"
    elif hits:
        out["line_status"] = "ambiguous"
    else:
        out["line_status"] = "not_found"
    return out


def evaluate_visibility(rule: dict[str, Any], edge: dict[str, Any],
                        members: dict[tuple[str, str], Any] | None) -> tuple[str, str, bool]:
    frm, to = edge["from"], edge["to"]
    if frm["scope"] == "unparsed" or to["scope"] == "unparsed":
        return UNKNOWN, "endpoint_unparsed", False
    if to["scope"] == EXTERNAL:
        return NOT_APPLICABLE, "external_callee", False
    if not frm["class"]:
        return UNKNOWN, "caller_class_unknown", False
    if not to["class"] or not to["method"]:
        return NOT_APPLICABLE, "callee_not_a_member", False
    if frm["class"] == to["class"]:
        return NOT_APPLICABLE, "same_class", False
    if rule["exempt_same_top_level_class"] and top_level_class(frm["class"]) == top_level_class(to["class"]):
        return NOT_APPLICABLE, "nested_class_of_same_top_level", False
    if edge["resolution"] != RESOLUTION_STATIC:
        return UNKNOWN, f"edge_resolution_{edge['resolution'] or 'missing'}", True
    if members is None:
        return UNKNOWN, "member_declarations_not_supplied", True
    key = (to["class"], to["method"])
    if key not in members:
        return UNKNOWN, "member_declaration_missing", True
    level = visibility_level(members[key])
    if level is None:
        return UNKNOWN, "member_modifiers_ambiguous", True
    if level in rule["levels"]:
        return VIOLATION, f"callee_visibility:{level}", True
    return COMPLIANT, f"callee_visibility:{level}", True


def evaluate(rule: dict[str, Any], edge: dict[str, Any],
             members: dict[tuple[str, str], Any] | None) -> tuple[str, str, bool]:
    """返回 (status, reason, in_scope)。in_scope 表示规则的角色域真的配到了这条边。"""
    if edge["kind"] not in rule["edge_kinds"]:
        return NOT_APPLICABLE, "edge_kind_out_of_scope", False
    # 上游把 unknown 编码进 resolution 以外的字段时，同样不得判成合规或违规。
    if edge["uncommitted"]:
        return UNKNOWN, edge["uncommitted"], False
    if rule["type"] == "visibility":
        return evaluate_visibility(rule, edge, members)
    basis = rule["basis"]
    frm, to = edge["from"], edge["to"]
    if frm["scope"] == "unparsed" or to["scope"] == "unparsed":
        return UNKNOWN, "endpoint_unparsed", False
    # 外部端点先判：callee 在仓库外时，无论 caller 角色是否已知，角色规则都不可能命中。
    if frm["role"] == EXTERNAL or to["role"] == EXTERNAL:
        return NOT_APPLICABLE, "external_endpoint", False
    if frm["role"] == UNCLASSIFIED or to["role"] == UNCLASSIFIED:
        return UNKNOWN, "role_unclassified", False
    if rule["type"] == "layer_skip":
        chain = rule["chain"]
        if frm["role"] not in chain or to["role"] not in chain:
            return NOT_APPLICABLE, "role_not_in_chain", False
        i, j = chain.index(frm["role"]), chain.index(to["role"])
        if j <= i:
            return NOT_APPLICABLE, "not_forward_in_chain", False
        if basis == RESOLVED and edge["resolution"] != RESOLUTION_STATIC:
            return UNKNOWN, f"edge_resolution_{edge['resolution'] or 'missing'}", True
        if j > i + 1:
            return VIOLATION, "skipped_layers:" + ",".join(chain[i + 1:j]), True
        return COMPLIANT, "adjacent_layer", True
    if frm["role"] != rule["from"] or to["role"] != rule["to"]:
        return NOT_APPLICABLE, "role_pair_out_of_scope", False
    if basis == RESOLVED and edge["resolution"] != RESOLUTION_STATIC:
        return UNKNOWN, f"edge_resolution_{edge['resolution'] or 'missing'}", True
    return VIOLATION, "forbidden_edge_present", True


def scan_unprojected_dispatch(edges: list[dict[str, Any]], index: dict[str, list[dict[str, Any]]],
                              source_root: Path | None, forms: list[dict[str, Any]]) -> dict[str, Any]:
    """源码里存在、但边快照里找不到对应边的 dispatch 形式 —— 缺失的边不得被读成合规。

    按文件去重扫描：一处 dispatch 属于哪个方法体无法从文本确定，所以不归属到具体细胞，
    只给同文件的候选细胞。关联也只到文件级，三种结果分开：
    unprojected（整个文件的出边里都没有匹配 —— 确定没投影）、
    matched_in_file（文件里有匹配，但不能证明就是这一处 —— unknown）、
    not_associable（接收者是表达式，类型不可解析 —— unknown）。
    """
    out: dict[str, Any] = {"forms": [f["id"] for f in forms], "scanned_files": 0, "occurrences": [],
                           "files_with_unprojected": [], "cells_in_files_with_unprojected": [],
                           "channel_status": "OK"}
    if not forms:
        out["channel_status"] = "NO_PATTERN"
        return out
    if source_root is None:
        out["channel_status"] = "NO_SOURCE_ROOT"
        return out
    if not source_root.is_dir():
        out["channel_status"] = "SOURCE_ROOT_MISSING"
        return out
    compiled = [(f["id"], re.compile(f["regex"])) for f in forms]
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"]["reference"], []).append(edge)
    by_file: dict[str, list[str]] = {}
    for ref in sorted(index):
        file, status = unit_location(index, ref)
        if status == "ok":
            by_file.setdefault(file, []).append(ref)
    for file, refs in sorted(by_file.items()):
        path = source_path(source_root, file)
        if path is None or not path.is_file():
            continue
        out["scanned_files"] += 1
        pool = [e for ref in refs for e in outgoing.get(ref, [])]
        own_classes = {r.get("class") for ref in refs for r in index[ref] if isinstance(r.get("class"), str)}
        unprojected = False
        for number, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for form_id, pattern in compiled:
                for match in pattern.finditer(text):
                    type_hint, member_hint = match.group(1), match.group(2)
                    if type_hint in ("this", "super"):
                        targets = own_classes
                    elif type_hint[:1].isupper():
                        targets = {c for c in (e["to"]["class"] for e in pool) if c.rsplit(".", 1)[-1] == type_hint}
                    else:
                        targets = None
                    if targets is None:
                        status, matched = "not_associable", []
                    else:
                        matched = sorted({e["edge_id"] for e in pool
                                          if e["to"]["method"] == member_hint and e["to"]["class"] in targets})
                        status = "matched_in_file" if matched else "unprojected"
                    out["occurrences"].append({
                        "file": file, "line": number, "form": form_id, "text": match.group(0),
                        "type_hint": type_hint, "member_hint": member_hint,
                        "candidate_cells": refs, "enclosing_cell": None,
                        "matching_edge_ids": matched, "status": status})
                    unprojected = unprojected or status == "unprojected"
        if unprojected:
            out["files_with_unprojected"].append(file)
            out["cells_in_files_with_unprojected"].extend(refs)
    out["cells_in_files_with_unprojected"] = sorted(set(out["cells_in_files_with_unprojected"]))
    if out["scanned_files"] == 0:
        out["channel_status"] = "NO_SCANNABLE_FILE"
    return out


def induce_candidates(edges: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """从系统自己的接线归纳候选约束 —— 受体-配体特异性，不是从命名猜业务。

    判据：配体 B 在别处真实结合（有内部调用方），A 到 B 存在长度 >=2 的必经通路，
    但 A 与 B 之间没有直接边。这是「特异性缺失」而非「平凡缺席」。
    归纳产物是假设，status=candidate / unknown，confirmation=unconfirmed，
    绝不自动升级为规则，也不进 findings —— 确认前不得用于判定。
    """
    spec = cfg.get("induction")
    out: dict[str, Any] = {"enabled": spec is not None, "basis": spec["basis"] if spec else None,
                           "min_support": spec["min_support"] if spec else None,
                           "observed_role_pairs": {}, "candidates": []}
    if not spec:
        return out
    basis = spec["basis"]
    direct: dict[tuple[str, str], int] = {}
    unresolved: dict[tuple[str, str], int] = {}
    support: dict[str, int] = {}
    callers: dict[str, set[str]] = {}
    for edge in edges:
        a, b = edge["from"]["role"], edge["to"]["role"]
        if a in (EXTERNAL, UNCLASSIFIED) or b in (EXTERNAL, UNCLASSIFIED) or a == b:
            continue
        if edge["kind"] not in ("call", "type_use"):
            continue
        if basis == DECLARED or edge["resolution"] == RESOLUTION_STATIC:
            direct[(a, b)] = direct.get((a, b), 0) + 1
            support[a] = support.get(a, 0) + 1
            callers.setdefault(b, set()).add(a)
        else:
            unresolved[(a, b)] = unresolved.get((a, b), 0) + 1
    out["observed_role_pairs"] = {f"{a}->{b}": n for (a, b), n in sorted(direct.items())}

    adjacency: dict[str, set[str]] = {}
    for a, b in direct:
        adjacency.setdefault(a, set()).add(b)
    covered = {(r["type"], r.get("from"), r.get("to"), r["basis"]) for r in cfg["rules"]
               if r["type"] in ("forbidden_call", "dependency_direction")}
    for a in sorted(adjacency):
        if support.get(a, 0) < spec["min_support"]:
            continue
        distance: dict[str, int] = {a: 0}
        previous: dict[str, str] = {}
        queue = [a]
        while queue:
            current = queue.pop(0)
            for nxt in sorted(adjacency.get(current, ())):
                if nxt not in distance:
                    distance[nxt] = distance[current] + 1
                    previous[nxt] = current
                    queue.append(nxt)
        for b in sorted(distance):
            if b == a or distance[b] < 2:
                continue
            if any((t, a, b, basis) in covered for t in ("forbidden_call", "dependency_direction")):
                continue
            path = [b]
            while path[-1] != a:
                path.append(previous[path[-1]])
            path.reverse()
            blocked = unresolved.get((a, b), 0)
            out["candidates"].append({
                "id": f"IND-{a}-{b}", "type": "dependency_direction", "basis": basis,
                "from": a, "to": b,
                "status": UNKNOWN if blocked else "candidate",
                "confirmation": "unconfirmed",
                "reason": "absence_not_established_unresolved_edges" if blocked else "obligate_pathway_no_direct_binding",
                "shortest_observed_path": path, "path_length": distance[b],
                "support": {"from_role_internal_edges": support.get(a, 0),
                            "to_role_caller_roles": sorted(callers.get(b, ())),
                            "direct_edges": direct.get((a, b), 0),
                            "unresolved_direct_edges": blocked},
                "claim": f"{a} 经 {' -> '.join(path[1:-1])} 间接到达 {b}，但无直接边；候选特异性约束：{a} 不得直接依赖 {b}",
                "promotion_requires": "人工确认或架构文档依据；确认前不得用于判定，不得自动升级为规则",
            })
    return out


def relativize(path: str, source_root: Path | None) -> str:
    if source_root is not None:
        root = str(source_root).rstrip("/") + "/"
        if path.startswith(root):
            return path[len(root):]
    return path


def upstream_location(edge: dict[str, Any], source_root: Path | None, method: str) -> dict[str, Any] | None:
    """上游给的调用点证据优先于文本匹配；多处证据不一致时不给唯一行。"""
    evidence = edge.get("evidence")
    if not isinstance(evidence, list):
        return None
    pairs = sorted({(relativize(item["file"], source_root), item["line"]) for item in evidence
                    if isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"]
                    and isinstance(item.get("line"), int) and not isinstance(item.get("line"), bool)})
    if not pairs:
        return None
    files = {f for f, _ in pairs}
    lines = sorted({line for _, line in pairs})
    return {"file": pairs[0][0] if len(files) == 1 else "", "line": lines[0] if len(pairs) == 1 else None,
            "symbol": method, "line_status": "upstream_evidence" if len(pairs) == 1 else "upstream_evidence_ambiguous",
            "call_text": "", "line_candidates": lines,
            "file_status": "upstream_evidence" if len(files) == 1 else "upstream_evidence_conflict"}


def make_finding(rule: dict[str, Any], edge: dict[str, Any], mapping: dict[str, Any],
                 index: dict[str, list[dict[str, Any]]], source_root: Path | None) -> dict[str, Any]:
    frm, to = edge["from"], edge["to"]
    file, file_status = unit_location(index, frm["reference"])
    location = upstream_location(edge, source_root, to["method"])
    if location is None:
        location = locate_call(source_root, file, to["method"])
        location["file_status"] = file_status
    elif not location["file"]:
        location["file"] = file
    refs = [f"edge:{edge['edge_id']}"]
    if location["line"]:
        refs.append(f"{file}:{location['line']}")
    elif file:
        refs.append(f"file:{file}")
    return {
        "id": f"{rule['id']}:{edge['edge_id']}",
        "rule_id": rule["id"], "rule_type": rule["type"], "severity": rule["severity"],
        "status": VIOLATION, "claim": rule["claim"], "rule_source": rule["source"],
        "basis": rule["basis"], "runtime_resolution": edge["resolution"],
        **mapping,
        "from": {"reference": frm["reference"], "class": frm["class"], "method": frm["method"],
                 "role": frm["role"], "file": file},
        "to": {"reference": to["reference"], "class": to["class"], "method": to["method"],
               "role": to["role"], "file": unit_location(index, to["reference"])[0]},
        "edge": {"edge_id": edge["edge_id"], "kind": edge["kind"],
                 "resolution": edge["resolution"], "call_count": edge["call_count"],
                 "proxy_mode": edge["proxy_mode"], "uncommitted": edge["uncommitted"]},
        "location": location,
        "evidence_refs": refs,
        "ground_truth": "unverified",
    }


def audit(units_doc: Any, edges_doc: Any, config: Any, categories: dict[str, Any],
          members: dict[tuple[str, str], Any] | None = None,
          source_root: Path | None = None, role_facts: Any = None) -> dict[str, Any]:
    cfg = validate_config(config)
    visibility_rules = [r["id"] for r in cfg["rules"] if r["type"] == "visibility"]
    if visibility_rules and members is None:
        raise ValueError(f"规则 {visibility_rules} 属 visibility，必须供给成员声明输入；"
                         "缺失时拒绝出数，避免产生与完整输入不可比的 unknown 计数")
    if visibility_rules and not members:
        raise ValueError(f"规则 {visibility_rules} 属 visibility，但成员声明为空"
                         "（容器无记录，或记录全不合格）；与缺文件同等对待，拒绝出数")
    edges_raw = edges_doc.get("edges") if isinstance(edges_doc, dict) else None
    if not isinstance(edges_raw, list):
        raise ValueError("edges 输入必须含 edges 数组")
    index = build_unit_index(units_doc)
    facts, fact_conflicts = (load_role_facts(role_facts, cfg["role_mechanisms"], cfg["role_fact_source"])
                             if role_facts is not None else ({}, []))

    edges: list[dict[str, Any]] = []
    role_classes: dict[str, set[str]] = {r["id"]: set() for r in cfg["roles"]}
    observed_classes: set[str] = set()
    unclassified: dict[str, int] = {}
    inventory_unclassified: dict[str, int] = {}
    rows = units_doc.get("units") if isinstance(units_doc, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        cls = row.get("class")
        if not isinstance(cls, str) or not cls:
            continue
        observed_classes.add(cls)
        # 角色覆盖按细胞清单算，不只按边端点：某层没出现在边上不等于模式没配对。
        role = classify({"class": cls, "scope": "project"}, cfg["roles"], facts)
        if role == UNCLASSIFIED:
            inventory_unclassified[cls] = inventory_unclassified.get(cls, 0) + 1
        else:
            role_classes[role].add(cls)
    external_endpoints = unresolved_endpoints = 0
    proxy_census: dict[str, int] = {}
    for position, raw in enumerate(edges_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"边记录必须是对象（index {position}）")
        frm, to = parse_endpoint(raw.get("from_agent_id")), parse_endpoint(raw.get("to_agent_id"))
        for endpoint in (frm, to):
            endpoint["role"] = classify(endpoint, cfg["roles"], facts)
            if endpoint["scope"] == "unresolved":
                unresolved_endpoints += 1
            if endpoint["role"] == EXTERNAL:
                external_endpoints += 1
                continue
            if endpoint["class"]:
                observed_classes.add(endpoint["class"])
            if endpoint["role"] == UNCLASSIFIED:
                key = endpoint["class"] or endpoint["reference"] or "<empty>"
                unclassified[key] = unclassified.get(key, 0) + 1
            else:
                role_classes[endpoint["role"]].add(endpoint["class"])
        uncommitted = ""
        for marker in cfg["unknown_markers"]:
            value = raw.get(marker["field"])
            if isinstance(value, str) and value in marker["values"]:
                uncommitted = f"uncommitted_{marker['field']}_{value}"
                break
        proxy = raw.get("proxy_mode")
        if isinstance(proxy, str) and proxy:
            proxy_census[proxy] = proxy_census.get(proxy, 0) + 1
        edges.append({"edge_id": raw.get("edge_id") or f"index:{position}",
                      "kind": raw.get("kind") or "unknown",
                      "resolution": raw.get("resolution") or UNKNOWN,
                      "call_count": raw.get("call_count"), "from": frm, "to": to,
                      "uncommitted": uncommitted, "evidence": raw.get("evidence"),
                      "proxy_mode": proxy if isinstance(proxy, str) else ""})

    checked = sorted(observed_classes & set(facts))
    disagreements = [{"class": cls, "mechanism_role": facts[cls],
                      "name_pattern_role": classify({"class": cls, "scope": "project"}, cfg["roles"])}
                     for cls in checked if classify({"class": cls, "scope": "project"}, cfg["roles"]) != facts[cls]]
    role_validation = {
        "mechanism_facts_supplied": role_facts is not None,
        "precedence": "mechanism_over_name_pattern",
        "classes_with_facts": len(facts), "classes_checked": len(checked),
        "agree": len(checked) - len(disagreements), "disagree": len(disagreements),
        "disagreements": disagreements, "fact_conflicts": fact_conflicts,
    }

    opaque = cfg["opaque_dispatch_patterns"]
    opacity: dict[str, dict[str, Any]] = {}
    opaque_edges = 0
    for edge in edges:
        to = edge["to"]
        target = f"{to['class']}.{to['method']}" if to["method"] else to["class"]
        matched = [p for p in opaque if fnmatch.fnmatchcase(target, p) or fnmatch.fnmatchcase(to["reference"], p)]
        if not matched:
            continue
        opaque_edges += 1
        frm = edge["from"]
        rec = opacity.setdefault(frm["reference"], {"reference": frm["reference"], "class": frm["class"],
                                                    "method": frm["method"], "patterns": set(), "edge_ids": []})
        rec["patterns"].update(matched)
        rec["edge_ids"].append(edge["edge_id"])
    dispatch_opacity = {
        "patterns": opaque, "matched_edges": opaque_edges,
        "cells": [{"reference": k, "class": v["class"], "method": v["method"],
                   "patterns": sorted(v["patterns"]), "edge_ids": v["edge_ids"]}
                  for k, v in sorted(opacity.items())],
        "note": "这些细胞使用运行期解析的 dispatch，其出边集合在静态图里不保证完整；缺失的边不得当成合规",
    }

    unprojected = scan_unprojected_dispatch(edges, index, source_root, cfg["unprojected_dispatch_patterns"])
    induced = induce_candidates(edges, cfg)

    findings: list[dict[str, Any]] = []
    declared_findings: list[dict[str, Any]] = []
    unknown_rows: list[dict[str, Any]] = []
    declared_unknown: list[dict[str, Any]] = []
    rule_reports: list[dict[str, Any]] = []
    edge_scope: dict[str, list[str]] = {}
    declared_scope: dict[str, list[str]] = {}
    for rule in cfg["rules"]:
        mapping = resolve_mapping(rule, categories)
        declared = rule["basis"] == DECLARED
        counts = {VIOLATION: 0, COMPLIANT: 0, UNKNOWN: 0, NOT_APPLICABLE: 0}
        reasons: dict[str, int] = {}
        in_scope = 0
        for edge in edges:
            status, reason, scoped = evaluate(rule, edge, members)
            counts[status] += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            if scoped:
                in_scope += 1
                (declared_scope if declared else edge_scope).setdefault(edge["edge_id"], []).append(rule["id"])
            if status == VIOLATION:
                record = make_finding(rule, edge, mapping, index, source_root)
                (declared_findings if declared else findings).append(record)
            elif status == UNKNOWN:
                row = {"rule_id": rule["id"], "edge_id": edge["edge_id"], "basis": rule["basis"],
                       "reason": reason, "in_scope": scoped, "resolution": edge["resolution"],
                       "from": edge["from"]["reference"], "to": edge["to"]["reference"]}
                (declared_unknown if declared else unknown_rows).append(row)
        if rule["type"] in ("forbidden_call", "dependency_direction"):
            scope_roles = [rule["from"], rule["to"]]
        elif rule["type"] == "layer_skip":
            scope_roles = list(rule["chain"])
        else:
            scope_roles = []
        empty_roles = [r for r in scope_roles if not role_classes[r]]
        measured = counts[VIOLATION] + counts[COMPLIANT]
        if empty_roles:
            verdict = VERDICT_UNMATCHED
        elif counts[VIOLATION]:
            verdict = VERDICT_VIOLATIONS
        elif in_scope and counts[UNKNOWN] == in_scope:
            verdict = VERDICT_UNKNOWN_ONLY
        elif counts[UNKNOWN]:
            verdict = VERDICT_PARTIAL
        elif measured == 0:
            # 域内 0 条边 = 没产生任何判定，不是「测得合规」。两者必须分开报。
            verdict = VERDICT_NO_EDGE
        else:
            verdict = VERDICT_CLEAN
        rule_reports.append({"id": rule["id"], "type": rule["type"], "basis": rule["basis"],
                             "severity": rule["severity"], "claim": rule["claim"], "source": rule["source"],
                             "edge_kinds": list(rule["edge_kinds"]), **mapping,
                             "verdict": verdict, "in_scope_edges": in_scope,
                             "edges_examined": len(edges), "measured_edges": measured,
                             "scope_role_classes": {r: len(role_classes[r]) for r in scope_roles},
                             "unmatched_roles": empty_roles,
                             "counts": counts, "reasons": reasons})

    unresolved_edges = [
        {"edge_id": e["edge_id"], "kind": e["kind"], "resolution": e["resolution"],
         "from": e["from"]["reference"], "to": e["to"]["reference"],
         "declared_callee_class": e["to"]["class"],
         "resolved_rules_in_scope": edge_scope.get(e["edge_id"], []),
         "declared_rules_in_scope": declared_scope.get(e["edge_id"], [])}
        for e in edges
        if e["resolution"] != RESOLUTION_STATIC or e["from"]["scope"] == "unresolved" or e["to"]["scope"] == "unresolved"]

    input_warnings: list[str] = []
    if not cfg["unknown_markers"]:
        input_warnings.append("未声明 unknown_markers：上游若把 unknown 编码进 resolution 以外的字段，本引擎不会识别")
    if cfg["role_mechanisms"] and role_facts is None:
        input_warnings.append("配置声明了 role_mechanisms 但未供给机制事实：角色仅由名字模式判定")
    if source_root is None:
        input_warnings.append("未供给源码根：行号定位与未投影 dispatch 检测不可用")
    elif not source_root.is_dir():
        input_warnings.append(f"源码根不存在或不是目录：{source_root} —— 行号定位与未投影 dispatch 检测不可用，"
                              "该通道报 0 是「没测」不是「测得 0」")
    if unprojected["channel_status"] != "OK":
        input_warnings.append(f"未投影 dispatch 通道状态 {unprojected['channel_status']}：occurrences 为空不得读成「测得 0 处」")

    judged = missing = ambiguous = 0
    for rule in rule_reports:
        if rule["type"] != "visibility":
            continue
        for reason, count in rule["reasons"].items():
            if reason.startswith("callee_visibility:"):
                judged += count
            elif reason == "member_declaration_missing":
                missing += count
            elif reason == "member_modifiers_ambiguous":
                ambiguous += count
    if missing:
        input_warnings.append(f"成员声明覆盖不全：{missing} 条边找不到 callee 修饰符（unknown），不是合规")
    member_coverage = {"supplied": members is not None, "declarations": len(members) if members else 0,
                       "judged": judged, "declaration_missing": missing, "modifiers_ambiguous": ambiguous}

    contract_gaps = []
    for rule, report in zip(cfg["rules"], rule_reports):
        if report["category_fit"] != "unmapped":
            continue
        proposal = rule["contract_proposal"]
        contract_gaps.append({"rule_id": rule["id"], "rule_type": rule["type"], "basis": rule["basis"],
                              "claim": rule["claim"], "unmapped_reason": report["unmapped_reason"],
                              "proposal": proposal,
                              "status": "proposed" if proposal else "needs_decision_no_proposal"})
    unproposed = [g["rule_id"] for g in contract_gaps if not g["proposal"]]
    if unproposed:
        input_warnings.append(f"契约缺口无修订提案：{unproposed} —— 必须人工决策，不得静默堆着")

    return {
        "schema": REPORT_SCHEMA,
        "repo_id": units_doc.get("repo_id") if isinstance(units_doc, dict) else None,
        "input_warnings": input_warnings,
        "member_coverage": member_coverage,
        "contract_gaps": contract_gaps,
        "contract_revision_proposal": cfg["contract_revision_proposal"],
        "counts": {
            "edges": len(edges), "rules": len(cfg["rules"]), "evaluations": len(edges) * len(cfg["rules"]),
            "findings": len(findings),
            "unmapped_findings": sum(1 for f in findings if f["category_fit"] == "unmapped"),
            "unknown_evaluations": len(unknown_rows),
            "declared_type_findings": len(declared_findings),
            "declared_type_unmapped_findings": sum(1 for f in declared_findings if f["category_fit"] == "unmapped"),
            "declared_type_unknown_evaluations": len(declared_unknown),
            "unresolved_edges": len(unresolved_edges),
            "unprojected_dispatch_occurrences": sum(1 for o in unprojected["occurrences"] if o["status"] == "unprojected"),
            "dispatch_association_unknown": sum(1 for o in unprojected["occurrences"] if o["status"] != "unprojected"),
            "files_with_unprojected_dispatch": len(unprojected["files_with_unprojected"]),
            "cells_in_files_with_unprojected_dispatch": len(unprojected["cells_in_files_with_unprojected"]),
            "induced_candidates": sum(1 for c in induced["candidates"] if c["status"] == "candidate"),
            "induced_unknown": sum(1 for c in induced["candidates"] if c["status"] == UNKNOWN),
            "contract_gaps": len(contract_gaps),
            "contract_gaps_without_proposal": len(unproposed),
            "rules_unmatched": sum(1 for r in rule_reports if r["verdict"] == VERDICT_UNMATCHED),
            "rules_with_violations": sum(1 for r in rule_reports if r["verdict"] == VERDICT_VIOLATIONS),
            "rules_clean_in_scope": sum(1 for r in rule_reports if r["verdict"] == VERDICT_CLEAN),
            "rules_unknown_only": sum(1 for r in rule_reports if r["verdict"] == VERDICT_UNKNOWN_ONLY),
            "rules_no_edge_in_scope": sum(1 for r in rule_reports if r["verdict"] == VERDICT_NO_EDGE),
            "rules_measured": sum(1 for r in rule_reports if r["measured_edges"] > 0),
            "judgments_made": sum(r["measured_edges"] for r in rule_reports),
        },
        "role_coverage": {
            "roles": {rid: sorted(classes) for rid, classes in sorted(role_classes.items())},
            "unclassified_classes": [{"class": k, "endpoints": unclassified.get(k, 0),
                                      "units": inventory_unclassified.get(k, 0)}
                                     for k in sorted(set(unclassified) | set(inventory_unclassified))],
            "external_endpoints": external_endpoints,
            "unresolved_endpoints": unresolved_endpoints,
        },
        "role_validation": role_validation,
        "dispatch_opacity": dispatch_opacity,
        "proxy_mode_census": dict(sorted(proxy_census.items())),
        "unprojected_dispatch": unprojected,
        "induced_candidates": induced,
        "rules": rule_reports,
        "measurement": {
            "judgments_made": sum(r["measured_edges"] for r in rule_reports),
            "unknown_evaluations": len(unknown_rows) + len(declared_unknown),
            "rules_with_measurement": [r["id"] for r in rule_reports if r["measured_edges"] > 0],
            "rules_without_measurement": [r["id"] for r in rule_reports if r["measured_edges"] == 0],
            "note": "measured_edges=0 表示该规则扫了全部边但域内 0 条，未产生任何合规/违规判定，"
                    "不得读成「测得合规」；findings=0 也不得读成全局合规。合规证据只来自 measured_edges>0 的规则。",
        },
        "findings": findings,
        "unknown_evaluations": unknown_rows,
        "declared_type_findings": declared_findings,
        "declared_type_unknown_evaluations": declared_unknown,
        "unresolved_edges": unresolved_edges,
        "limitations": [
            "边快照不含调用点行号；行号由源码文本匹配推断，ambiguous/not_found 时不给唯一行",
            "resolved_target 通道：resolution 非 static 的边一律单列 unknown，不参与合规/违规判定",
            "declared_type 通道判的是编译期声明类型，不是运行时目标；每条带 runtime_resolution，两个通道不混算",
            "角色优先用外部机制事实，名字模式兜底；未匹配的项目类记 unclassified 并按 unknown 处理，不当合规",
            "成员可见性来自外部声明输入；重载同名合并，修饰符冲突记 unknown",
            "静态图不证明运行时可达；dispatch_opacity 列出出边集合不保证完整的细胞，未列出的也不等于完整",
            "unprojected_dispatch 只把「整个文件的出边里都找不到匹配」记为确定未投影；matched_in_file / not_associable 是 unknown，不当合规也不当缺失",
            "dispatch 出现处属于哪个方法体无法从文本确定，enclosing_cell 恒为 null，只给同文件候选细胞",
            "induced_candidates 是从接线归纳的假设，不是规则也不是缺陷；未经确认不得用于判定，不进 findings",
            "unknown_markers 命中的边在两个通道都记 unknown；未列入配置的未知字段不会被自动识别，需显式声明",
            "proxy_mode_census 只如实转述上游的代理绕过事实，本引擎不据此开单（需要独立的机制规则与契约类别）",
            "visibility 规则对 protected 成员会把合法继承调用判成违规，配置 levels 时需自行取舍",
            "source.kind 非 project_declared 的规则未经项目确认，开单前需人工确认",
            "UNMAPPED 表示层契约装不下，需走契约提交修订，不在运行期造类别",
            "measured_edges=0 的规则是「域内无边可判」，不是「测得合规」；两者用不同 verdict 分开报",
        ],
    }


def build_repro(argv: list[str], cwd: str, member_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """复现命令必须带解释器前缀且逐字可执行；每个 members 口径各给一条，输出文件互不覆盖。"""
    base = shlex.join(["python3"] + list(argv))
    prefix = f"cd {shlex.quote(cwd)} && "
    out: dict[str, Any] = {"repro_cwd": cwd, "repro_cmd": base,
                           "repro_cmd_anywhere": prefix + base, "repro_variants": []}
    for entry in member_inputs:
        replaced = list(argv)
        if "--members" in replaced:
            replaced[replaced.index("--members") + 1] = entry["path"]
        else:
            replaced += ["--members", entry["path"]]
        if "--out" in replaced:
            index = replaced.index("--out") + 1
            target = Path(replaced[index])
            replaced[index] = str(target.with_name(f"{target.stem}.{entry['label']}{target.suffix}"))
        cmd = shlex.join(["python3"] + replaced)
        out["repro_variants"].append({"label": entry["label"], "members_path": entry["path"],
                                      "tracked": entry.get("tracked"),
                                      "prerequisite": entry.get("prerequisite"),
                                      "cmd": cmd, "cmd_anywhere": prefix + cmd})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect forbidden architecture edges in a cell graph")
    parser.add_argument("--units", required=True, type=Path)
    parser.add_argument("--edges", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--layers", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--members", type=Path)
    parser.add_argument("--role-facts", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    named = {"units": args.units, "edges": args.edges, "rules": args.rules, "layers": args.layers}
    if args.members:
        named["members"] = args.members
    if args.role_facts:
        named["role_facts"] = args.role_facts
    if args.out.resolve() in {p.resolve() for p in named.values()}:
        parser.error("output must differ from inputs")
    categories = findings_schema.load_layer_categories(args.layers)
    raw_config = json.loads(args.rules.read_text(encoding="utf-8"))
    cfg = validate_config(raw_config)
    raw_members = json.loads(args.members.read_text(encoding="utf-8")) if args.members else None
    members = load_members(raw_members, cfg["member_source"]) if raw_members is not None else None
    member_stats = member_record_stats(raw_members, cfg["member_source"]) if raw_members is not None else None
    if member_stats and member_stats["records_valid"] == 0 and any(r["type"] == "visibility" for r in cfg["rules"]):
        detail = ("容器为空" if member_stats["records_seen"] == 0 else
                  f"容器有 {member_stats['records_seen']} 条记录但全不合格（跳过 {member_stats['records_skipped']} 条）")
        raise ValueError(f"成员声明输入不可用：{detail}；visibility 规则拒绝出数，不得产出与完整输入不可比的 unknown 计数")
    facts = json.loads(args.role_facts.read_text(encoding="utf-8")) if args.role_facts else None
    source_root = args.source_root
    if source_root is None and cfg["source_root_key"]:
        node: Any = findings_schema.load_target()
        for part in cfg["source_root_key"].split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, str) and node:
            source_root = Path(node)
    report = audit(json.loads(args.units.read_text(encoding="utf-8")),
                   json.loads(args.edges.read_text(encoding="utf-8")),
                   raw_config, categories, members, source_root, facts)
    report["inputs"] = {name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                        for name, path in named.items()}
    report.update(build_repro(sys.argv, os.getcwd(), cfg["member_inputs"]))
    report["repro_prerequisites"] = cfg["repro_prerequisites"]
    if raw_members is not None:
        report["member_coverage"]["source_records"] = select_source(raw_members, cfg["member_source"], "members")["records"]
        report["member_coverage"].update({k: member_stats[k] for k in ("records_seen", "records_valid", "records_skipped")})
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for warning in report["input_warnings"]:
        print(f"输入告警: {warning}")
    print(json.dumps(report["counts"], ensure_ascii=False))
    print("role_validation:", json.dumps({k: v for k, v in report["role_validation"].items()
                                          if k not in ("disagreements", "fact_conflicts")}, ensure_ascii=False))
    print("dispatch_opacity:", json.dumps({"matched_edges": report["dispatch_opacity"]["matched_edges"],
                                           "cells": len(report["dispatch_opacity"]["cells"])}, ensure_ascii=False))
    print("unprojected_dispatch:", json.dumps({"scanned_files": report["unprojected_dispatch"]["scanned_files"],
                                               "occurrences": len(report["unprojected_dispatch"]["occurrences"]),
                                               "unprojected": report["counts"]["unprojected_dispatch_occurrences"],
                                               "association_unknown": report["counts"]["dispatch_association_unknown"],
                                               "files": report["counts"]["files_with_unprojected_dispatch"]}, ensure_ascii=False))
    print("induced_candidates:", json.dumps({"enabled": report["induced_candidates"]["enabled"],
                                             "candidate": report["counts"]["induced_candidates"],
                                             "unknown": report["counts"]["induced_unknown"]}, ensure_ascii=False))
    for cand in report["induced_candidates"]["candidates"]:
        print(f"  {cand['id']} {cand['status']:9s} {cand['from']} -/-> {cand['to']} "
              f"path={'->'.join(cand['shortest_observed_path'])} support={cand['support']['from_role_internal_edges']}")
    print("member_coverage:", json.dumps(report["member_coverage"], ensure_ascii=False))
    for gap in report["contract_gaps"]:
        proposal = gap["proposal"] or {}
        print(f"  契约缺口 {gap['rule_id']} {gap['status']} "
              f"建议={proposal.get('layer', '?')}.{proposal.get('category_id', '?')}")
    for rule in report["rules"]:
        print(f"  {rule['id']} {rule['type']}/{rule['basis']} {rule['verdict']} in_scope={rule['in_scope_edges']} "
              f"measured={rule['measured_edges']} violations={rule['counts'][VIOLATION]} "
              f"unknown={rule['counts'][UNKNOWN]} category={rule['category']}")
    print("measurement:", json.dumps({k: v for k, v in report["measurement"].items() if k != "note"}, ensure_ascii=False))


if __name__ == "__main__":
    main()