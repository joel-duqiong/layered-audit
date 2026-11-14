"""Score completed runs against separately supplied, independently prepared labels.

This tool does not execute methods or establish that a label is valid. Missing
results and results without evidence are UNKNOWN, never an implicit pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def load(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def index_cases(data: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    rows = data.get("cases")
    if not isinstance(rows, list):
        raise ValueError(f"{name}: cases must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{name}: case id missing")
        if row["id"] in indexed:
            raise ValueError(f"{name}: duplicate case {row['id']}")
        indexed[row["id"]] = row
    return indexed


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compare(manifest: dict[str, Any], truth: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    cases = index_cases(manifest, "manifest")
    labels = index_cases(truth, "truth")
    if not cases or set(cases) != set(labels):
        raise ValueError("truth must cover exactly the nonempty manifest")
    if any(row.get("label") not in ("positive", "negative") for row in labels.values()):
        raise ValueError("truth labels must be positive or negative")
    seen: set[str] = set()
    results = []
    for run in runs:
        method = run.get("method")
        if not isinstance(method, str) or not method.strip() or method in seen:
            raise ValueError("each run must have a unique method name")
        seen.add(method)
        outputs = index_cases(run, method)
        if set(outputs) - set(cases):
            raise ValueError(f"{method}: output contains unknown case IDs")
        elapsed = run.get("elapsed_seconds")
        if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError(f"{method}: elapsed_seconds must be nonnegative or null")
        counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unknown_positive": 0, "unknown_negative": 0}
        case_results = []
        for case_id in sorted(cases):
            record = outputs.get(case_id)
            verdict = record.get("verdict") if record else "UNKNOWN"
            if verdict not in ("PASS", "FAIL", "UNKNOWN"):
                raise ValueError(f"{method}/{case_id}: invalid verdict")
            refs = record.get("evidence_refs") if record else None
            if verdict != "UNKNOWN" and (not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs)):
                verdict = "UNKNOWN"
            label = labels[case_id]["label"]
            bucket = ("unknown_" + label if verdict == "UNKNOWN" else
                      "tp" if verdict == "FAIL" and label == "positive" else
                      "fp" if verdict == "FAIL" else "fn" if label == "positive" else "tn")
            counts[bucket] += 1
            case_results.append({"id": case_id, "outcome": bucket})
        positive_total = counts["tp"] + counts["fn"] + counts["unknown_positive"]
        results.append({"method": method, "counts": counts,
                        "precision": ratio(counts["tp"], counts["tp"] + counts["fp"]),
                        "positive_detected_fraction": ratio(counts["tp"], positive_total),
                        "completed_fraction": ratio(counts["tp"] + counts["fp"] + counts["fn"] + counts["tn"], len(cases)),
                        "elapsed_seconds": elapsed, "cost": run.get("cost"), "case_results": case_results})
    if not results:
        raise ValueError("at least one method run is required")
    return {"schema": "audit-comparison/1", "case_count": len(cases), "methods": results,
            "limitations": ["labels and evidence references are supplied, not independently verified",
                            "this report does not establish experiment fairness or generalization"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare completed audit runs against independent labels")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--run", required=True, action="append", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.resolve() in {p.resolve() for p in [args.manifest, args.truth, *args.run]}:
        parser.error("output must differ from inputs")
    manifest, manifest_hash = load(args.manifest)
    truth, truth_hash = load(args.truth)
    loaded = [load(path) for path in args.run]
    report = compare(manifest, truth, [data for data, _ in loaded])
    report["input_sha256"] = {"manifest": manifest_hash, "truth": truth_hash,
                              "runs": [{"method": data["method"], "sha256": sha} for data, sha in loaded]}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"comparison report: {args.out} ({report['case_count']} cases; {len(loaded)} methods)")


if __name__ == "__main__":
    main()
