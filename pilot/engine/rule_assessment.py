"""Read-only, evidence-aware assessment of supplied observations.

This is advisory only: it does not verify source authority, evidence references,
runtime isolation, or input authenticity and never authorizes a release gate.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

UNKNOWN = "UNKNOWN"


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
    if not isinstance(left_name, str) or left_name not in row or row[left_name] is None:
        return UNKNOWN
    left = row[left_name]
    if op in ("field_eq_field", "field_lte_field", "field_gte_field"):
        right_name = spec.get("right")
        if not isinstance(right_name, str) or right_name not in row:
            return UNKNOWN
        right = row[right_name]
    elif op in ("field_lte_const", "field_gte_const", "field_in_set") and "value" in spec:
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
        confirmed = (source.get("confirmation") == "confirmed" and source.get("kind") in ("user", "document")
                     and isinstance(ref, str) and bool(ref.strip()) and isinstance(confirmer, str) and bool(confirmer.strip()))
        items = [{"observation_id": obs["id"], "status": evaluate_observation(rule, obs, pack.get("target")),
                  "evidence_refs": obs.get("evidence_refs") if isinstance(obs.get("evidence_refs"), list) else []}
                 for obs in rows]
        states = [item["status"] for item in items]
        observed = ("FAIL" if "FAIL" in states else UNKNOWN if not states or UNKNOWN in states
                    else "PASS" if "PASS" in states else "NOT_APPLICABLE")
        results.append({"id": rule["id"], "claim": rule.get("claim", ""), "source": source,
                        "source_confirmation": "declared_confirmed" if confirmed else "unconfirmed",
                        "observed_status": observed, "status": observed if confirmed else UNKNOWN,
                        "gate_eligible": False, "observations": items})
    return {"schema": "business-rule-assessment/1", "target": pack.get("target"), "intake_status": "RULES_PRESENT" if results else UNKNOWN,
            "results": results,
            "limitations": ["source confirmation and evidence references are supplied, not verified",
                            "only provided observations are assessed; no target was executed",
                            "advisory output is not eligible for a release gate"]}


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
