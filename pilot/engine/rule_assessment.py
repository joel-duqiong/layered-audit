"""Read-only, evidence-aware assessment of supplied observations.

This is advisory only: it does not verify source authority, evidence references,
runtime isolation, or input authenticity and never authorizes a release gate.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

UNKNOWN = "UNKNOWN"

# 确认分两个 tier,不得混同,两者都是合法 confirmed:
#   业务规则 tier(user/document)—— 需业务方/PRD 依据;
#   技术约束 tier(ddl/annotation/openapi)—— 作者写下迁移/注解/契约即确认,
#   是已在生产生效的客观事实,不是编造的预期。
# 从技术约束推断的业务意图(如「地址必须非空白」)不属于任何 tier:
# 采集器标 needs_confirmation,本门控只认字面 "confirmed",推断不得混入。
BUSINESS_KINDS = ("user", "document")
TECHNICAL_KINDS = ("ddl", "annotation", "openapi")


def scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float)) and not (isinstance(value, float) and not math.isfinite(value))


def comparable(a: Any, b: Any) -> bool:
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return scalar(a) and scalar(b) and ((numeric(a) and numeric(b)) or type(a) is type(b))


def condition(row: dict[str, Any], clause: Any) -> str:
    if not isinstance(clause, dict):
        return UNKNOWN
    field, op, value = clause.get("field"), clause.get("op", "eq"), clause.get("value")
    if not isinstance(field, str) or field not in row or row[field] is None or value is None:
        return UNKNOWN
    actual = row[field]
    if op in ("eq", "ne"):
        if not comparable(actual, value):
            return UNKNOWN
        result = actual == value
        return "YES" if (result if op == "eq" else not result) else "NO"
    if op in ("in", "not_in") and isinstance(value, list) and scalar(actual) and all(comparable(actual, x) for x in value):
        result = actual in value
        return "YES" if (result if op == "in" else not result) else "NO"
    return UNKNOWN


def evaluate_observation(rule: dict[str, Any], obs: dict[str, Any], target: Any) -> str:
    refs = obs.get("evidence_refs")
    row = obs.get("row")
    scope = rule.get("scope")
    if (not isinstance(target, str) or not target.strip() or obs.get("target") != target
            or not isinstance(scope, str) or not scope.strip() or obs.get("scope") != scope):
        return UNKNOWN
    if obs.get("reached") is not True or not isinstance(row, dict) or not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
        return UNKNOWN
    when = rule.get("when")
    if isinstance(when, list) and not when:
        return UNKNOWN
    clauses = when if isinstance(when, list) else ([] if when is None else [when])
    conditions = [condition(row, c) for c in clauses]
    if "NO" in conditions:
        return "NOT_APPLICABLE"
    if UNKNOWN in conditions:
        return UNKNOWN
    spec = rule.get("assert")
    if not isinstance(spec, dict):
        return UNKNOWN
    op, left_name = spec.get("type"), spec.get("left")
    if not isinstance(left_name, str) or left_name not in row:
        return UNKNOWN
    left = row[left_name]
    if op in ("field_not_null", "field_not_blank"):
        # null 是明确违反，不是「判不出来」；缺字段在上面已判 UNKNOWN。
        if left is None:
            return "FAIL"
        if op == "field_not_null":
            return "PASS"
        if not isinstance(left, str):
            return UNKNOWN
        return "PASS" if left.strip() else "FAIL"
    if op == "field_default_when_absent":
        # 字段存在 ⇒「不传该字段时」的条件不成立 ⇒ 不适用，不是 PASS。
        # 字段缺失在上面已判 UNKNOWN：契约被触发，但默认值落没落地需结果态观测。
        return "NOT_APPLICABLE"
    if op == "field_size":
        if left is None:
            return UNKNOWN
        lo, hi = spec.get("min"), spec.get("max")
        if lo is None and hi is None:
            return UNKNOWN
        if any(b is not None and (isinstance(b, bool) or not isinstance(b, int) or b < 0)
               for b in (lo, hi)):
            return UNKNOWN
        if isinstance(left, str):
            n = len(left)          # 字符数
        elif isinstance(left, list):
            n = len(left)          # 元素数 —— 与字符数是两种计数，不得混
        else:
            return UNKNOWN
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        return "PASS" if ok else "FAIL"
    if op == "field_pattern":
        if left is None or not isinstance(left, str):
            return UNKNOWN
        pattern = spec.get("value")
        if not isinstance(pattern, str):
            return UNKNOWN
        try:
            matched = re.fullmatch(pattern, left) is not None
        except re.error:
            return UNKNOWN
        return "PASS" if matched else "FAIL"
    if op == "text_not_contains":
        if left is None or not isinstance(left, str):
            return UNKNOWN
        terms = spec.get("value")
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) for t in terms):
            return UNKNOWN
        return "FAIL" if any(t in left for t in terms) else "PASS"
    if left is None:
        return UNKNOWN
    if op in ("field_eq_field", "field_lte_field", "field_gte_field"):
        right_name = spec.get("right")
        if not isinstance(right_name, str) or right_name not in row:
            return UNKNOWN
        right = row[right_name]
    elif op in ("field_lte_const", "field_gte_const", "field_gt_const", "field_in_set") and "value" in spec:
        right = spec["value"]
    else:
        return UNKNOWN
    if op == "field_in_set":
        if not isinstance(right, list) or not scalar(left) or not all(comparable(left, v) for v in right):
            return UNKNOWN
        return "PASS" if left in right else "FAIL"
    if right is None or not comparable(left, right):
        return UNKNOWN
    if op != "field_eq_field" and (isinstance(left, bool) or isinstance(right, bool)
                                   or not isinstance(left, (int, float)) or not isinstance(right, (int, float))):
        return UNKNOWN
    try:
        if op == "field_eq_field":
            ok = left == right
        elif op in ("field_lte_field", "field_lte_const"):
            if isinstance(left, bool):
                return UNKNOWN
            ok = left <= right
        elif op == "field_gt_const":
            if isinstance(left, bool):
                return UNKNOWN
            ok = left > right
        else:
            if isinstance(left, bool):
                return UNKNOWN
            ok = left >= right
    except (TypeError, ValueError):
        return UNKNOWN
    return "PASS" if ok else "FAIL"


def assess(pack: dict[str, Any], observations: dict[str, Any]) -> dict[str, Any]:
    if pack.get("schema") != "business-rules/1" or not isinstance(pack.get("rules"), list):
        raise ValueError("expected business-rules/1 with a rules array")
    rows = observations.get("observations")
    if not isinstance(rows, list):
        raise ValueError("observations must be an array")
    seen: set[str] = set()
    for obs in rows:
        if not isinstance(obs, dict) or not isinstance(obs.get("id"), str) or not obs["id"] or obs["id"] in seen:
            raise ValueError("observation IDs must be unique nonempty strings")
        seen.add(obs["id"])
    results = []
    seen.clear()
    for rule in pack["rules"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"] or rule["id"] in seen:
            raise ValueError("rule IDs must be unique nonempty strings")
        seen.add(rule["id"])
        source = rule.get("source") if isinstance(rule.get("source"), dict) else {}
        ref, confirmer = source.get("reference"), source.get("confirmed_by")
        kind = source.get("kind")
        if source.get("kind") in ("user", "document", "ddl", "annotation", "openapi"):
            tier = "business" if kind in BUSINESS_KINDS else "technical"
        else:
            tier = None
        confirmed = (source.get("confirmation") == "confirmed" and tier is not None
                     and isinstance(ref, str) and bool(ref.strip()) and isinstance(confirmer, str) and bool(confirmer.strip()))
        items = [{"observation_id": obs["id"], "status": evaluate_observation(rule, obs, pack.get("target")),
                  "evidence_refs": obs.get("evidence_refs") if isinstance(obs.get("evidence_refs"), list) else []}
                 for obs in rows]
        states = [item["status"] for item in items]
        observed = ("FAIL" if "FAIL" in states else UNKNOWN if not states or UNKNOWN in states
                    else "PASS" if "PASS" in states else "NOT_APPLICABLE")
        results.append({"id": rule["id"], "claim": rule.get("claim", ""), "source": source,
                        "source_confirmation": "declared_confirmed" if confirmed else "unconfirmed",
                        "confirmation_tier": tier,
                        "observed_status": observed, "status": observed if confirmed else UNKNOWN,
                        "gate_eligible": False, "observations": items})
    return {"schema": "business-rule-assessment/1", "target": pack.get("target"), "intake_status": "RULES_PRESENT" if results else UNKNOWN,
            "results": results,
            "confirmation_tiers": _tier_summary(results),
            "limitations": ["source confirmation and evidence references are supplied, not verified",
                            "only provided observations are assessed; no target was executed",
                            "advisory output is not eligible for a release gate",
                            "business tier (user/document) needs business-side confirmation; "
                            "technical tier (ddl/annotation/openapi) is confirmed by authorship; "
                            "a FAIL must be read together with confirmation_tier"]}


def _tier_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """两个 tier 分开计数 —— 使用者必须能分辨 FAIL 是违反业务规则还是技术约束。"""
    out: dict[str, Any] = {}
    for tier in ("business", "technical"):
        rs = [r for r in results if r["confirmation_tier"] == tier]
        statuses: dict[str, int] = {}
        for r in rs:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        out[tier] = {"rules": len(rs),
                     "declared_confirmed": sum(1 for r in rs if r["source_confirmation"] == "declared_confirmed"),
                     "status_counts": statuses}
    out["unrecognized_kind"] = sum(1 for r in results if r["confirmation_tier"] is None)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess supplied observations against project-declared business rules")
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.resolve() in (args.rules.resolve(), args.observations.resolve()):
        parser.error("output must differ from inputs")
    report = assess(json.loads(args.rules.read_text()), json.loads(args.observations.read_text()))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"assessed {len(report['results'])} rules; advisory only")


if __name__ == "__main__":
    main()
