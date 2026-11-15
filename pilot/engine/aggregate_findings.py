"""根因聚合 + 覆盖度自检（B2 / A4 / B3）。

读 all_findings.json，产出三件事：

1. 根因归并：按 root_cause_key 把同根因的多条发现合成一条根因记录。
   attribution.rule 明确「高层可引用低层缺陷作佐证，但不得重复开单」，
   所以「同层同根因开多次」= 重复开单；「同根因在多层都 claim 自己 own」= 跨层重复开单。
2. 归属检查：每条根因必须恰好有一个 owner 层，且 owner = 能给出可复现证据的最低层。
3. 覆盖度矩阵：layer × 契约里声明的 category，命中数为 0 的格子 = 该类没人管。

退出码：0 = 没有硬违规；1 = 有重复开单/归属冲突（--strict 或默认都会报，
但只有 --strict 才影响退出码，方便先把报告拿全）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema

HERE = Path(__file__).resolve().parent
# 产物落在「目标项目目录」= target.json 所在目录。引擎目录只放代码。
PROJECT_DIR = findings_schema.target_path().parent

LAYER_ORDER = ("function", "file", "class", "service", "integration")
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def _load() -> list[dict[str, Any]]:
    data = json.loads((PROJECT_DIR / "all_findings.json").read_text(encoding="utf-8"))
    return data["findings"]


def _load_warrant_coverage() -> dict[str, Any]:
    data = json.loads((PROJECT_DIR / "all_findings.json").read_text(encoding="utf-8"))
    return data.get("warrant_coverage") or {}


def _worst(severities: list[str]) -> str:
    for s in SEVERITY_ORDER:
        if s in severities:
            return s
    return ""


FIDELITY_ORDER = ("container", "hybrid", "stubbed", "static", "unknown")


def _weakest_fidelity(values: list[str]) -> str:
    """一组证据里最弱的那一档 —— 根因的可信度由最弱的证据决定。"""
    known = [v for v in values if v in FIDELITY_ORDER]
    if not known:
        return "unknown"
    return max(known, key=FIDELITY_ORDER.index)


def _fidelity_counts(group: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in group:
        f = r.get("runtime_fidelity", "unknown")
        counts[f] = counts.get(f, 0) + 1
    return counts


def aggregate(records: list[dict[str, Any]],
              targets: dict[str, tuple[str, ...]],
              out_of_scope: dict[str, tuple[str, ...]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        buckets[rec["root_cause_key"]].append(rec)

    out: list[dict[str, Any]] = []
    for key, group in buckets.items():
        own = [r for r in group if r["owner"]["relation"] == "own"]
        cited = [r for r in group if r["owner"]["relation"] == "cited"]
        layers_own = sorted({r["layer"] for r in own}, key=LAYER_ORDER.index)
        layers_all = sorted({r["layer"] for r in group}, key=LAYER_ORDER.index)
        declared_owners = sorted({r["owner"]["layer"] for r in group},
                                 key=LAYER_ORDER.index)
        lowest = layers_own[0] if layers_own else (layers_all[0] if layers_all else "")
        owner = declared_owners[0] if declared_owners else lowest

        violations: list[str] = []
        notes: list[str] = []
        merges: list[str] = []

        # 规则一：同一层同一根因开多条 own —— 归并提示（修一次即可，不是跨层违规）
        own_per_layer: dict[str, int] = defaultdict(int)
        for r in own:
            own_per_layer[r["layer"]] += 1
        for layer, n in sorted(own_per_layer.items(), key=lambda kv: LAYER_ORDER.index(kv[0])):
            if n > 1:
                merges.append(f"{layer} 层同一根因开了 {n} 条 own，可归并为 1 条根因")

        # 规则二：多层都声明 own = 跨层重复开单
        if len(layers_own) > 1:
            violations.append(f"跨层重复开单：{', '.join(layers_own)} 都声明 own")

        # 规则三：声明的 owner 与实际开单层不一致 —— 合法上提还是真冲突
        if own and owner != lowest:
            filing_layers = sorted({r["layer"] for r in own}, key=LAYER_ORDER.index)
            if any(findings_schema.can_escalate(fl, owner, targets) for fl in filing_layers):
                notes.append(f"合法上提：开单层 {filing_layers} → owner {owner}（契约 escalation_to 允许）")
            else:
                violations.append(
                    f"归属冲突：声明 owner={owner}，实际开单层 {filing_layers}，"
                    f"契约未允许该上提")

        # 规则三之二：开单层的类别落在自己的 out_of_scope 里 = 越界开单
        for r in own:
            cat = r["category"].partition(".")[2]
            if cat and cat in out_of_scope.get(r["layer"], ()):
                violations.append(
                    f"越界开单：{r['layer']} 层的 out_of_scope 明确含 {cat}，"
                    f"该条应由 {owner} 层开单")

        # 规则四：一条 own 都没有 = 只有引用没有归属
        if not own:
            violations.append("无 own 归属：所有条目都只是引用别的层")

        # 引用层里出现完全相同的 claim = 同层真重复（不是「两条链级后果」）
        claim_layer: dict[tuple[str, str], int] = defaultdict(int)
        for r in cited:
            claim_layer[(r["layer"], r["claim"])] += 1
        for (layer, _claim), n in claim_layer.items():
            if n > 1:
                violations.append(f"重复开单：{layer} 层同一根因同一主张引用了 {n} 次")

        owner_rec = next((r for r in group if r["owner"]["layer"] == owner and r["layer"] == owner),
                         own[0] if own else group[0])
        out.append({
            "root_cause_key": key,
            "title": owner_rec["title"],
            "severity": _worst([r["severity"] for r in group]),
            "owner_layer": owner,
            "categories": sorted({r["category"] for r in group if r["category"] != "UNMAPPED"}),
            "category_fit": sorted({r["category_fit"] for r in group}),
            "finding_count": len(group),
            "finding_uids": [r["uid"] for r in group],
            "layers_own": layers_own,
            "layers_all": layers_all,
            "cited_from": sorted({r["layer"] for r in cited}),
            "cwe": sorted({c for r in group for c in r["cwe"]}),
            "confidence": max(r["confidence"] for r in group),
            "ground_truth": ("verified" if any(r["ground_truth"] == "verified" for r in group)
                             else "unverified"),
            "repro_cmds": sorted({r["repro_cmd"] for r in group if r["repro_cmd"]}),
            "violations": violations,
            "notes": notes,
            "merges": merges,
            "evidence_fidelity": _weakest_fidelity([r["runtime_fidelity"] for r in group]),
            "fidelity_breakdown": _fidelity_counts(group),
            "related_root_causes": sorted({
                rc for r in group for rc in r.get("related_root_causes", [])}),
        })
    out.sort(key=lambda r: (SEVERITY_ORDER.index(r["severity"]) if r["severity"] in SEVERITY_ORDER else 9,
                             -r["finding_count"], r["root_cause_key"]))
    return out


def coverage(records: list[dict[str, Any]], root_causes: list[dict[str, Any]],
             categories: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    matrix: dict[str, dict[str, int]] = {}
    for layer, cats in categories.items():
        row = {c: 0 for c in cats}
        for rec in records:
            if rec["layer"] != layer:
                continue
            cat = rec["category"].partition(".")[2]
            if cat in row:
                row[cat] += 1
        matrix[layer] = row

    gaps = []
    for layer, row in matrix.items():
        for cat, n in row.items():
            if n == 0:
                gaps.append(f"{layer}.{cat}")
    unmapped = [r["uid"] for r in records if r["category"] == "UNMAPPED"]
    unmapped_by_layer: dict[str, int] = defaultdict(int)
    for r in records:
        if r["category"] == "UNMAPPED":
            unmapped_by_layer[r["layer"]] += 1

    # 同一文件承载多个根因 —— 文件层的内聚信号
    by_file: dict[str, set[str]] = defaultdict(set)
    for rec in records:
        f = rec["location"].get("file")
        if f:
            by_file[f].add(rec["root_cause_key"])
    multi_root_files = {f: sorted(keys) for f, keys in by_file.items() if len(keys) > 1}

    return {
        "matrix": matrix,
        "zero_coverage_categories": gaps,
        "unmapped_uids": unmapped,
        "unmapped_by_layer": dict(unmapped_by_layer),
        "files_with_multiple_root_causes": multi_root_files,
        "root_cause_count": len(root_causes),
        "finding_count": len(records),
    }


# ------------------------------------------------- 疑点收集（不进台账，但必须可见）
SUSPECT_BUCKETS = ("unconfirmed", "refuted")

WHY_NOT_FILED = {
    "unconfirmed": "推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。"
                   "既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。",
    "refuted": "文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。",
}


def _normalize_suspect(rec: Any, layer: str, bucket: str,
                       source_file: str) -> dict[str, Any] | None:
    """把上游三种记录形态归一。认不出的返回 None，由调用方计数外露（不静默丢）。

    A held 形：{rule_id, oracle, finding:{id,severity,title,detail,evidence}}
    B 桶形：  {rule, table, api_http, api_note, oracle}
    C 结论形：{id, severity, title, detail, evidence, oracle}
    """
    if not isinstance(rec, dict) or not isinstance(rec.get("oracle"), dict):
        return None
    finding = rec.get("finding")
    if isinstance(finding, dict):
        rid = rec.get("rule_id") or finding.get("id") or ""
        expectation, severity = finding.get("title") or "", finding.get("severity") or ""
        observation, detail = finding.get("evidence"), finding.get("detail") or ""
        repro = finding.get("repro") or ""
    elif isinstance(rec.get("rule"), str):
        rid = rec.get("rule_id") or ""
        expectation, severity = rec["rule"], ""
        observation = {k: rec[k] for k in ("table", "api_http", "api_note") if k in rec}
        detail, repro = "", ""
    elif isinstance(rec.get("id"), str):
        rid = rec["id"]
        expectation, severity = rec.get("title") or "", rec.get("severity") or ""
        observation, detail = rec.get("evidence"), rec.get("detail") or ""
        repro = rec.get("repro") or ""
    else:
        return None
    oracle = rec["oracle"]
    return {
        "layer": layer, "bucket": bucket, "source_file": source_file,
        "rule_id": rid, "expectation": expectation,
        "risk_severity": severity,
        "oracle": oracle,
        "oracle_state": str(oracle.get("confirmation") or ""),
        "why_not_filed": WHY_NOT_FILED[bucket],
        "oracle_note": oracle.get("note") or "",
        "detail": detail, "observation": observation, "repro": repro,
    }


def collect_suspects(sources: list[dict[str, Any]],
                     project_dir: Path) -> dict[str, Any]:
    """收集各层 oracle 闸门压下来的结论。

    **未测量 ≠ 测得 0**：没声明来源、文件缺失、桶键缺失，一律 INCOMPLETE + 告警，
    不得静默输出 0 让读者以为「没有疑点」。认不出的记录形态与状态不符同样外露。
    """
    out: dict[str, Any] = {
        "sources": [], "unconfirmed": [], "refuted": [],
        "counts": {"unconfirmed": 0, "refuted": 0, "by_layer": {}},
        "missing_sources": [], "missing_keys": [], "unrecognized_records": [],
        "state_mismatches": [], "input_warnings": [], "channel_status": "OK",
        "caliber_note": "待确认疑点与已证伪观测**都不在缺陷台账内**，三个口径不得混算。"
                        "unconfirmed 是「不知道，需业务方确认」，不是缺陷也不是已排除。",
    }
    if not sources:
        out["channel_status"] = "INCOMPLETE"
        out["input_warnings"].append(
            "项目绑定未声明 suspect_sources：疑点通道未测量，不得读成「没有疑点」")
        return out

    for src in sources:
        layer, name = src.get("layer", ""), src.get("file", "")
        path = project_dir / name
        rec_out: dict[str, Any] = {"layer": layer, "file": name, "present": path.is_file()}
        if not path.is_file():
            out["missing_sources"].append(name)
            out["channel_status"] = "INCOMPLETE"
            out["input_warnings"].append(
                f"疑点来源 {name}（{layer} 层）不存在：该层疑点未测量，不得读成 0")
            out["sources"].append(rec_out)
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for bucket in SUSPECT_BUCKETS:
            key = src.get(f"{bucket}_key", bucket)
            if key not in doc:
                out["missing_keys"].append({"file": name, "bucket": bucket, "key": key})
                out["channel_status"] = "INCOMPLETE"
                out["input_warnings"].append(
                    f"{name} 缺桶键 {key!r}（{bucket}）：形态漂移，不得读成 0")
                rec_out[bucket] = None
                continue
            rows = doc[key]
            kept = []
            for i, rec in enumerate(rows if isinstance(rows, list) else []):
                norm = _normalize_suspect(rec, layer, bucket, name)
                if norm is None:
                    out["unrecognized_records"].append(
                        {"file": name, "bucket": bucket, "index": i})
                    out["channel_status"] = "INCOMPLETE"
                    continue
                if norm["oracle_state"] != bucket:
                    out["state_mismatches"].append(
                        {"file": name, "bucket": bucket, "rule_id": norm["rule_id"],
                         "oracle_state": norm["oracle_state"]})
                    out["channel_status"] = "INCOMPLETE"
                kept.append(norm)
            out[bucket].extend(kept)
            rec_out[bucket] = len(kept)
            bl = out["counts"]["by_layer"].setdefault(layer, {})
            bl[bucket] = bl.get(bucket, 0) + len(kept)
        out["sources"].append(rec_out)

    out["counts"]["unconfirmed"] = len(out["unconfirmed"])
    out["counts"]["refuted"] = len(out["refuted"])
    if out["unrecognized_records"]:
        out["input_warnings"].append(
            f"{len(out['unrecognized_records'])} 条疑点记录形态认不出，已计数外露，未静默丢弃")
    if out["state_mismatches"]:
        out["input_warnings"].append(
            f"{len(out['state_mismatches'])} 条疑点的 oracle 状态与所在桶不符，需拥有者核对")
    return out


def calibers(records: list[dict[str, Any]],
             suspects: dict[str, Any]) -> dict[str, Any]:
    """三个口径分开算，**绝不混算**：台账按 kind 分解 + 疑点两桶。

    疑点（unconfirmed / refuted）**不进缺陷分子** —— 它们不是缺陷；
    也**不是已排除** —— unconfirmed 是「不知道，需业务方确认」。
    """
    by_kind = {k: sum(1 for r in records if r.get("kind") == k)
               for k in ("defect", "positive", "observation")}
    sc = suspects.get("counts") or {}
    return {"by_kind": by_kind,
            "unconfirmed": sc.get("unconfirmed", 0),
            "refuted": sc.get("refuted", 0)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="根因聚合 + 覆盖度自检")
    parser.add_argument("--strict", action="store_true", help="有硬违规时退出码 1")
    args = parser.parse_args()

    categories = findings_schema.load_layer_categories()
    targets = findings_schema.load_escalation_targets()
    out_of_scope = findings_schema.load_out_of_scope()
    records = _load()
    root_causes = aggregate(records, targets, out_of_scope)
    cov = coverage(records, root_causes, categories)
    suspects = collect_suspects(
        findings_schema.load_target().get("suspect_sources") or [], PROJECT_DIR)
    by_kind = calibers(records, suspects)["by_kind"]

    payload = {
        "generated_from": "aggregate_findings.py",
        "summary": {
            "findings": len(records),
            "root_causes": len(root_causes),
            "collapsed": len(records) - len(root_causes),
            "violations": sum(1 for r in root_causes if r["violations"]),
            "by_kind": by_kind,
            "unconfirmed_suspects": suspects["counts"]["unconfirmed"],
            "refuted_observations": suspects["counts"]["refuted"],
            "suspect_channel_status": suspects["channel_status"],
        },
        "root_causes": root_causes,
        "coverage": cov,
        "suspects": suspects,
        "warrant_coverage": _load_warrant_coverage(),
    }
    (PROJECT_DIR / "root_causes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _report(payload, categories)
    _write_md(payload, categories)

    hard = [r for r in root_causes if r["violations"]]
    if hard and args.strict:
        raise SystemExit(1)


def _report(payload: dict[str, Any], categories: dict[str, tuple[str, ...]]) -> None:
    rcs = payload["root_causes"]
    cov = payload["coverage"]
    print(f"根因聚合 -> {PROJECT_DIR / 'root_causes.json'}")
    print(f"  {payload['summary']['findings']} 条发现 -> {payload['summary']['root_causes']} 条根因"
          f"（归并 {payload['summary']['collapsed']} 条）")
    bk = payload["summary"].get("by_kind") or {}
    sus = payload.get("suspects") or {}
    sc = sus.get("counts") or {"unconfirmed": 0, "refuted": 0}
    print(f"  台账按 kind：确认缺陷 {bk.get('defect', 0)} / 正面结论 {bk.get('positive', 0)}"
          f" / 观测 {bk.get('observation', 0)}")
    print(f"  确认缺陷 {bk.get('defect', 0)} 条 / 待确认疑点 {sc['unconfirmed']} 条 /"
          f" 已证伪只留观测 {sc['refuted']} 条（后两者不在台账内，三个口径不混算）")
    if sus.get("channel_status") == "INCOMPLETE":
        print("  疑点通道 INCOMPLETE（未测量 ≠ 没有疑点）：")
        for w in sus.get("input_warnings") or []:
            print(f"    - {w}")

    print("\n[按严重度]")
    by_sev: dict[str, int] = defaultdict(int)
    for rc in rcs:
        by_sev[rc["severity"]] += 1
    for sev in SEVERITY_ORDER:
        if sev in by_sev:
            print(f"  {sev:<9} {by_sev[sev]}")

    print("\n[被归并最狠的根因]")
    for rc in sorted(rcs, key=lambda r: -r["finding_count"])[:6]:
        print(f"  {rc['finding_count']:>3} 条  {rc['root_cause_key']:<46} owner={rc['owner_layer']}")

    print("\n[硬违规：违反 attribution.rule / out_of_scope]")
    hard = [rc for rc in rcs if rc["violations"]]
    if not hard:
        print("  无")
    for rc in hard:
        print(f"  {rc['root_cause_key']} ({rc['severity']}, owner={rc['owner_layer']})")
        for v in rc["violations"]:
            print(f"    - 违规 {v}")
        for n in rc.get("notes", []):
            print(f"    · 说明 {n}")

    print("\n[归并提示：同层同根因多开，修一次即可]")
    merges = [rc for rc in rcs if rc.get("merges")]
    if not merges:
        print("  无")
    for rc in merges:
        for m in rc["merges"]:
            print(f"  {rc['root_cause_key']:<46} {m}")

    print("\n[跨层佐证：高层引用低层根因（合法，链级后果归高层）]")
    for rc in rcs:
        if rc["cited_from"] and rc["layers_own"]:
            print(f"  {rc['root_cause_key']:<46} owner={rc['owner_layer']:<7} 被引用层={rc['cited_from']}")

    print("\n[运行时保真度：结论是在什么语义下测出来的]")
    fid = [rc for rc in rcs]
    by_fid: dict[str, int] = defaultdict(int)
    for rc in fid:
        by_fid[rc.get("evidence_fidelity", "unknown")] += 1
    for name in FIDELITY_ORDER:
        if name in by_fid:
            print(f"  {name:<12} {by_fid[name]} 条根因")
    weak = [rc for rc in rcs if rc.get("evidence_fidelity") in ("static", "unknown", "stubbed")]
    print(f"  非容器内验证的根因：{len(weak)} 条"
          f"（container = 在真容器里测的，DI/切面/代理语义为真；"
          f"stubbed = 拔离容器测的，不能按容器语义解读）")
    for rc in weak[:8]:
        print(f"    {rc['evidence_fidelity']:<10}{rc['root_cause_key']}")

    print("\n[跨根因关联：A 与 B 是同一处代码/同一条链上的两件事]")
    related = [(rc["root_cause_key"], dep) for rc in rcs
               for dep in rc.get("related_root_causes", [])]
    if not related:
        print("  无")
    for key, dep in related:
        print(f"  {key:<52} -> {dep}")

    print("\n[覆盖度矩阵：层 × 契约声明的 category]")
    for layer in LAYER_ORDER:
        if layer not in cov["matrix"]:
            continue
        row = cov["matrix"][layer]
        cats = list(categories[layer])
        print(f"  {layer}")
        for c in cats:
            n = row.get(c, 0)
            flag = "  <== 零覆盖" if n == 0 else ""
            print(f"      {c:<26} {n}{flag}")

    print("\n[零覆盖类别（该类没人管）]")
    if cov["zero_coverage_categories"]:
        for g in cov["zero_coverage_categories"]:
            print(f"  {g}")
    else:
        print("  无")

    print("\n[契约缺口：无法归类的发现]")
    print(f"  合计 {len(cov['unmapped_uids'])} 条，按层：{cov['unmapped_by_layer']}")

    print("\n[同一文件承载多个根因 —— 文件层内聚信号]")
    if not cov["files_with_multiple_root_causes"]:
        print("  无")
    for f, keys in sorted(cov["files_with_multiple_root_causes"].items(),
                          key=lambda kv: -len(kv[1])):
        print(f"  {len(keys)} 个根因  {f}")
        for k in keys:
            print(f"      {k}")

    print("\n[根因清单]")
    for rc in rcs:
        print(f"  {rc['severity']:<9}{rc['owner_layer']:<12}{rc['root_cause_key']:<46}"
              f"发现 {rc['finding_count']:>2}  置信 {rc['confidence']}")


def _write_md(payload: dict[str, Any], categories: dict[str, tuple[str, ...]]) -> None:
    rcs = payload["root_causes"]
    cov = payload["coverage"]
    sus = payload.get("suspects") or {}
    sc = sus.get("counts") or {"unconfirmed": 0, "refuted": 0}
    bk = payload["summary"].get("by_kind") or {}
    lines: list[str] = []
    a = lines.append
    a("# 分层审计 · 根因与覆盖度报告")
    a("")
    a(f"生成物：{PROJECT_DIR / 'root_causes.json'}　输入：{PROJECT_DIR / 'all_findings.json'}")
    a("")
    a(f"- 发现 {payload['summary']['findings']} 条 → 根因 {payload['summary']['root_causes']} 条"
      f"（归并 {payload['summary']['collapsed']} 条）")
    a(f"- 台账按 kind：确认缺陷 {bk.get('defect', 0)} / 正面结论 {bk.get('positive', 0)}"
      f" / 观测 {bk.get('observation', 0)}（三者不混算）")
    a(f"- **确认缺陷 {bk.get('defect', 0)} 条 / 待确认疑点 {sc['unconfirmed']} 条 /"
      f" 已证伪只留观测 {sc['refuted']} 条** —— 后两者**不在台账"
      f" {payload['summary']['findings']} 条内**，见第八、九节")
    if sus.get("channel_status") == "INCOMPLETE":
        a(f"- 疑点通道 **INCOMPLETE**（未测量 ≠ 没有疑点）："
          + "；".join(sus.get("input_warnings") or []))
    a(f"- 硬违规 {payload['summary']['violations']} 条；契约缺口（无法归类）{len(cov['unmapped_uids'])} 条")
    wc = payload.get("warrant_coverage") or {}
    if wc:
        gm = wc.get("gate_modes") or {}
        a(f"- 闸门覆盖（{wc.get('producer_count', 0)} 个生产者）：A/B 判据二分 {gm.get('warrant', 0)}"
          f" / 仅 oracle {gm.get('oracle', 0)} / **未接 {gm.get('none', 0) + gm.get('unknown', 0)}**；"
          f"未接名下**未裁定 defect {wc.get('unadjudicated_defects', 0)} 条**（见第十节）")
    a("")

    a("## 一、修复顺序（按根因）")
    a("")
    a("| # | 严重度 | 归属层 | 根因 | 标题 | 发现数 | 置信 | ground_truth | 可跑复现 |")
    a("|---|--------|--------|------|------|--------|------|---------------|----------|")
    for i, rc in enumerate(rcs, start=1):
        repro = rc["repro_cmds"][0] if rc["repro_cmds"] else "—"
        if len(repro) > 60:
            repro = repro[:57] + "…"
        a(f"| {i} | {rc['severity']} | {rc['owner_layer']} | `{rc['root_cause_key']}` | "
          f"{rc['title']} | {rc['finding_count']} | {rc['confidence']} | {rc['ground_truth']} | `{repro}` |")
    a("")

    hard = [rc for rc in rcs if rc["violations"]]
    a("## 二、硬违规（违反 attribution.rule / out_of_scope）")
    a("")
    if not hard:
        a("无。")
    else:
        a("| 根因 | 归属层 | 违规 |")
        a("|------|--------|------|")
        for rc in hard:
            for v in rc["violations"]:
                a(f"| `{rc['root_cause_key']}` | {rc['owner_layer']} | {v} |")
        for rc in hard:
            for n in rc.get("notes", []):
                a(f"| `{rc['root_cause_key']}` | {rc['owner_layer']} | 说明：{n} |")
    a("")

    a("## 三、归并提示（同层同根因多开）")
    a("")
    a("| 根因 | 归属层 | 说明 |")
    a("|------|--------|------|")
    for rc in rcs:
        for m in rc.get("merges", []):
            a(f"| `{rc['root_cause_key']}` | {rc['owner_layer']} | {m} |")
    a("")

    a("## 四、跨根因关联")
    a("")
    a("| 根因 | 关联到 |")
    a("|------|--------|")
    for rc in rcs:
        for dep in rc.get("related_root_causes", []):
            a(f"| `{rc['root_cause_key']}` | `{dep}` |")
    a("")

    a("## 五、覆盖度矩阵（层 × 契约声明的 category）")
    a("")
    for layer in LAYER_ORDER:
        if layer not in cov["matrix"]:
            continue
        a(f"**{layer}**")
        a("")
        a("| category | 命中 | 状态 |")
        a("|----------|------|------|")
        for c in categories[layer]:
            n = cov["matrix"][layer].get(c, 0)
            a(f"| {c} | {n} | {'零覆盖 —— 没人管' if n == 0 else 'ok'} |")
        a("")

    a("## 六、契约缺口（category 装不下）")
    a("")
    a(f"合计 {len(cov['unmapped_uids'])} 条，按层：{cov['unmapped_by_layer']}")
    a("")
    a("| uid | 建议类别 |")
    a("|-----|----------|")
    for uid in cov["unmapped_uids"]:
        a(f"| `{uid}` | 见 all_findings.json 的 suggested_category |")
    a("")

    a("## 七、同一文件承载多个根因（文件层内聚信号）")
    a("")
    a("| 文件 | 根因数 | 根因 |")
    a("|------|--------|------|")
    for f, keys in sorted(cov["files_with_multiple_root_causes"].items(),
                           key=lambda kv: -len(kv[1])):
        a(f"| `{f}` | {len(keys)} | {', '.join(f'`{k}`' for k in keys)} |")
    a("")

    _suspect_section(a, sus, "unconfirmed", "八、待确认疑点（规范层风险 · 需业务方确认）",
                     "这些结论的**观测是真的**，但「所以这是缺陷」所依赖的业务预期"
                     "**没有文档/业务方依据**。它们是「不知道是否成立，需业务方确认」——"
                     "**既不是缺陷，也不是已排除**。不得计入缺陷分子，也不得当成已澄清而忽略。")
    _suspect_section(a, sus, "refuted", "九、已证伪（文档明确否定 · 只保留观测）",
                     "这些结论所依赖的业务预期被**文档明确否定**（例如文档写明该字段可为空、"
                     "或该规则「当前未定义」）。观测保留以便复核，**不判缺陷、也不再等业务确认**。")
    _warrant_coverage_section(a, payload.get("warrant_coverage") or {})

    (PROJECT_DIR / "audit_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  报告 -> {PROJECT_DIR / 'audit_report.md'}")


def _warrant_coverage_section(a: Any, wc: dict[str, Any]) -> None:
    """闸门覆盖清单：已接 / 未接 / 未裁定并排报，不得只报已接的那部分。"""
    a("## 十、判据闸门覆盖清单（已接 / 未接 / 未裁定）")
    a("")
    if not wc:
        a("**未测量**：台账里没有 warrant_coverage 块，本节不是「全部已接闸门」。")
        a("")
        return
    a(wc.get("caliber_note", ""))
    a("")
    gm = wc.get("gate_modes") or {}
    a(f"生产者 {wc.get('producer_count', 0)} 个：A/B 判据二分 **{gm.get('warrant', 0)}** / "
      f"仅 oracle **{gm.get('oracle', 0)}** / 未接 **{gm.get('none', 0)}** / "
      f"未声明 **{gm.get('unknown', 0)}**")
    a("")
    a(f"登记册条目 {wc.get('registry_size', 0)} 条；台账漏斗拦下 {wc.get('held_at_ledger', 0)} 条；"
      f"**未接闸门名下未裁定 defect {wc.get('unadjudicated_defects', 0)} 条**")
    a("")
    for w in wc.get("input_warnings") or []:
        a(f"- 告警：{w}")
    a("")
    a("| 生产者产物 | 生产脚本 | 闸门 | 生产条数 | defect | 登记册覆盖 | 漏斗拦下 | 未裁定 |")
    a("|---|---|---|---|---|---|---|---|")
    for e in wc.get("by_producer") or []:
        a(f"| `{e['producer']}` | `{e.get('script') or '未声明'}` | {e['gate']} | "
          f"{e['total']} | {e['defects']} | {e['registry_covered']} | {e['held']} | "
          f"{e['unadjudicated']} |")
    a("")
    a("「生产条数」是该生产者产出的条数（含被漏斗拦下的）；进台账的 = 生产条数 − 漏斗拦下。"
      "「未裁定」只统计**未接闸门**生产者名下、且不在登记册里的 defect —— "
      "它们既没被裁定为技术可自证，也没被裁定为业务预期，**不得读成已澄清**。")
    a("")


def _suspect_section(a: Any, sus: dict[str, Any], bucket: str,
                     title: str, preface: str) -> None:
    """渲染一个疑点桶。空桶也要显式写「0 条」，并区分「测得 0」与「未测量」。"""
    rows = sus.get(bucket) or []
    a(f"## {title}")
    a("")
    a(preface)
    a("")
    if not sus:
        a("**未测量**：项目绑定未声明 suspect_sources，本节不是「0 条疑点」。")
        a("")
        return
    by_layer: dict[str, int] = defaultdict(int)
    for r in rows:
        by_layer[r["layer"]] += 1
    a(f"合计 **{len(rows)} 条**（不在缺陷台账内）；按层："
      + ("、".join(f"{k} {v}" for k, v in sorted(by_layer.items())) or "无"))
    a("")
    for src in sus.get("sources") or []:
        if src.get(bucket) is None:
            a(f"- 来源 `{src['file']}`（{src['layer']} 层）：桶键缺失或文件不存在，"
              "**该层未测量**，不得读成 0")
    if sus.get("unrecognized_records"):
        a(f"- 另有 {len(sus['unrecognized_records'])} 条记录形态认不出，"
          "已计数外露（见 root_causes.json 的 suspects.unrecognized_records）")
    a("")
    if not rows:
        a("本桶 0 条。")
        a("")
        return
    for r in rows:
        # 上游桶形记录本来就没有 id：用预期原文当标识，不编造 id
        rid = r["rule_id"] or f"（无 id）{r['expectation'][:60]}"
        a(f"### [{r['layer']}] `{rid}`")
        a("")
        a(f"- **预期原文**：{r['expectation']}")
        a(f"- **oracle 状态**：`{r['oracle_state']}`（source=`{r['oracle'].get('source', '')}`）")
        a(f"- **reference**：{r['oracle'].get('reference') or '（未给出处）'}")
        if r["oracle"].get("text"):
            a(f"- **原文引用**：{r['oracle']['text']}")
        a(f"- **为何不进台账**：{r['why_not_filed']}")
        if r["oracle_note"]:
            a(f"- **裁定说明**：{r['oracle_note']}")
        if r["risk_severity"]:
            label = ("**风险评级**" if bucket == "unconfirmed" else "**原结论严重度（已撤销）**")
            tail = ("这是风险优先级，**不是缺陷严重度**，不参与缺陷计数"
                    if bucket == "unconfirmed"
                    else "预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**")
            a(f"- {label}：{r['risk_severity']}（{tail}）")
        if r["observation"] is not None:
            a("- **观测证据**：")
            a("")
            a("```json")
            a(json.dumps(r["observation"], ensure_ascii=False, indent=2))
            a("```")
        if r["repro"]:
            a(f"- **复现**：`{r['repro']}`")
        a(f"- **来源**：`{r['source_file']}`")
        a("")


if __name__ == "__main__":
    main()
