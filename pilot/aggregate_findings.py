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
LAYER_ORDER = ("function", "file", "class", "service", "integration")
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def _load() -> list[dict[str, Any]]:
    data = json.loads((HERE / "all_findings.json").read_text(encoding="utf-8"))
    return data["findings"]


def _worst(severities: list[str]) -> str:
    for s in SEVERITY_ORDER:
        if s in severities:
            return s
    return ""


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

    payload = {
        "generated_from": "aggregate_findings.py",
        "summary": {
            "findings": len(records),
            "root_causes": len(root_causes),
            "collapsed": len(records) - len(root_causes),
            "violations": sum(1 for r in root_causes if r["violations"]),
        },
        "root_causes": root_causes,
        "coverage": cov,
    }
    (HERE / "root_causes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _report(payload, categories)
    _write_md(payload, categories)

    hard = [r for r in root_causes if r["violations"]]
    if hard and args.strict:
        raise SystemExit(1)


def _report(payload: dict[str, Any], categories: dict[str, tuple[str, ...]]) -> None:
    rcs = payload["root_causes"]
    cov = payload["coverage"]
    print(f"根因聚合 -> {HERE / 'root_causes.json'}")
    print(f"  {payload['summary']['findings']} 条发现 -> {payload['summary']['root_causes']} 条根因"
          f"（归并 {payload['summary']['collapsed']} 条）")

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
    lines: list[str] = []
    a = lines.append
    a("# 分层审计 · 根因与覆盖度报告")
    a("")
    a(f"生成物：{HERE / 'root_causes.json'}　输入：{HERE / 'all_findings.json'}")
    a("")
    a(f"- 发现 {payload['summary']['findings']} 条 → 根因 {payload['summary']['root_causes']} 条"
      f"（归并 {payload['summary']['collapsed']} 条）")
    a(f"- 硬违规 {payload['summary']['violations']} 条；契约缺口（无法归类）{len(cov['unmapped_uids'])} 条")
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

    (HERE / "audit_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  报告 -> {HERE / 'audit_report.md'}")


if __name__ == "__main__":
    main()
