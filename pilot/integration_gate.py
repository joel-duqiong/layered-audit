"""集权层门禁：把三方对账与攻击链结论钉成基线，任何回归都让流水线红。

门禁只认三类回归（判红）：
  1. 基线里 ALIGNED 的对账项，这次不再是 ALIGNED —— 前端 mapper 或后端 DTO 改了，三方对不上
  2. 基线里没有的新对账项，verdict 是 VALUE_MISMATCH / SEMANTIC_MISMATCH / FRONTEND_MISSING
  3. 基线里没被确认的攻击链，这次被确认了 —— 之前没复现出来的问题复现了

其余变化（新出现的 DERIVED、对齐项变好、链被修掉）判绿，只做提示。

用法：
  python3 integration_gate.py --init      用当前产物建立基线
  python3 integration_gate.py --refresh   先重跑对账与攻击链，再比对
  python3 integration_gate.py             只比对，退出码 1 = 有回归
  python3 integration_gate.py --accept    比对通过后把当前结果写成新基线
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from typing import Any

D = os.path.dirname(os.path.abspath(__file__))
RECON = os.path.join(D, "integration_reconciliation.json")
CHAINS = os.path.join(D, "integration_chains.json")
BASELINE = os.path.join(D, "integration_baseline.json")
REPORT = os.path.join(D, "integration_gate_report.json")
REPORT_MD = os.path.join(D, "integration_gate_report.md")

# 只有这三类新出现的 verdict 算「新缺陷」
NEW_MISMATCH = {"VALUE_MISMATCH", "SEMANTIC_MISMATCH", "FRONTEND_MISSING"}
RED = "RED"
GREEN = "GREEN"


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def row_key(row: dict) -> str:
    """对账项的稳定标识。

    同一个 (实体, 字段) 在对账表里可能同时存在多行，必须靠结构性差异区分开，
    否则基线会把它们折叠成一行，回归就看不见了：
      - 列级行：field 是 DB 列名，api_field 是驼峰
      - 语义级行：field 与 api_field 相同，用 frontend_usage 区分同一字段上的多个语义检查
      - 纯 API 行：field 为空，按结论分桶（派生值 / 前端不消费 / 前端要但没有）
    """
    entity = row.get("entity")
    field = row.get("field")
    api_field = row.get("api_field")
    if field is None:
        bucket = {"DERIVED": "derived", "FRONTEND_IGNORES": "ignored",
                  "FRONTEND_MISSING": "missing"}.get(row.get("verdict"), "api")
        return f"{entity}.{api_field}@{bucket}"
    if api_field == field:
        return f"{entity}.{field}@semantic:{row.get('frontend_usage') or ''}"
    return f"{entity}.{field}"


def collect_rows(recon: dict) -> dict[str, str]:
    return {row_key(r): r["verdict"] for r in recon.get("reconciliation", [])}


def collect_chains(chains: dict) -> dict[str, str]:
    return {c["id"]: c["verdict"] for c in chains.get("chains", [])}


def build_baseline(recon: dict, chains: dict) -> dict:
    rows = collect_rows(recon)
    return {
        "created_at": datetime.datetime.now().isoformat(),
        "base_url": recon.get("base_url"),
        "rows": rows,
        "counts": dict(recon.get("verdict_counts", {})),
        "aligned": sum(1 for v in rows.values() if v == "ALIGNED"),
        "chains": collect_chains(chains),
    }


def compare(baseline: dict, recon: dict, chains: dict) -> list[dict]:
    """比对基线与当前结果，返回差异清单。"""
    diffs: list[dict] = []
    base_rows: dict[str, str] = baseline.get("rows", {})
    cur_rows = collect_rows(recon)

    for key, verdict in sorted(cur_rows.items()):
        prev = base_rows.get(key)
        if prev is None:
            if verdict in NEW_MISMATCH:
                diffs.append({"level": RED, "kind": "new_mismatch", "key": key,
                              "before": "（基线无此项）", "after": verdict})
            else:
                diffs.append({"level": GREEN, "kind": "new_row", "key": key,
                              "before": "（基线无此项）", "after": verdict})
            continue
        if prev == verdict:
            continue
        if prev == "ALIGNED" and verdict != "ALIGNED":
            diffs.append({"level": RED, "kind": "aligned_lost", "key": key,
                          "before": prev, "after": verdict})
        elif verdict == "ALIGNED":
            diffs.append({"level": GREEN, "kind": "aligned_gained", "key": key,
                          "before": prev, "after": verdict})
        elif verdict in NEW_MISMATCH:
            diffs.append({"level": RED, "kind": "verdict_worsened", "key": key,
                          "before": prev, "after": verdict})
        else:
            diffs.append({"level": GREEN, "kind": "verdict_changed", "key": key,
                          "before": prev, "after": verdict})

    for key, verdict in sorted(base_rows.items()):
        if key not in cur_rows:
            diffs.append({"level": GREEN, "kind": "row_gone", "key": key,
                          "before": verdict, "after": "（本次无此项）"})

    base_chains: dict[str, str] = baseline.get("chains", {})
    cur_chains = collect_chains(chains)
    for cid, verdict in sorted(cur_chains.items()):
        prev = base_chains.get(cid)
        if prev == verdict:
            continue
        if verdict == "CONFIRMED":
            diffs.append({"level": RED, "kind": "chain_newly_confirmed", "key": cid,
                          "before": prev or "（基线无此链）", "after": verdict})
        else:
            diffs.append({"level": GREEN, "kind": "chain_fixed", "key": cid,
                          "before": prev or "（基线无此链）", "after": verdict})
    for cid, verdict in sorted(base_chains.items()):
        if cid not in cur_chains:
            diffs.append({"level": GREEN, "kind": "chain_gone", "key": cid,
                          "before": verdict, "after": "（本次无此链）"})
    return diffs


def render_md(report: dict) -> str:
    lines = ["# 集权层门禁报告", ""]
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 结论：**{report['verdict']}**"
                 f"（红 {report['red_count']} / 绿 {report['green_count']}）")
    lines.append(f"- 对齐项：基线 {report['baseline_aligned']} → 当前 {report['current_aligned']}")
    lines.append(f"- 攻击链：基线 {report['baseline_confirmed']} 条确认 → "
                 f"当前 {report['current_confirmed']} 条确认")
    lines.append("")
    if not report["diffs"]:
        lines.append("无差异：三方对账与攻击链结论与基线完全一致。")
    else:
        lines.append("| 判定 | 类型 | 项目 | 基线 | 当前 |")
        lines.append("|---|---|---|---|---|")
        for d in report["diffs"]:
            lines.append(f"| {d['level']} | {d['kind']} | {d['key']} | "
                         f"{d['before']} | {d['after']} |")
    lines.append("")
    return "\n".join(lines)


def rerun() -> None:
    for script in ("integration_reconcile.py", "integration_chain.py"):
        print(f"--- 重跑 {script} ---")
        subprocess.run([sys.executable, os.path.join(D, script)], check=True, cwd=D)


def main() -> int:
    args = set(sys.argv[1:])
    if "--refresh" in args:
        rerun()

    if not (os.path.exists(RECON) and os.path.exists(CHAINS)):
        print("门禁：缺少对账或攻击链产物，先跑 --refresh")
        return 0

    recon = load_json(RECON)
    chains = load_json(CHAINS)

    if "--init" in args:
        base = build_baseline(recon, chains)
        dump_json(BASELINE, base)
        print(f"门禁基线 -> {BASELINE}")
        print(f"  对账项 {len(base['rows'])}（ALIGNED {base['aligned']}），"
              f"攻击链 {len(base['chains'])} 条")
        return 0

    if not os.path.exists(BASELINE):
        print("门禁：基线不存在，跳过（首次请跑 integration_gate.py --init）")
        return 0

    baseline = load_json(BASELINE)
    diffs = compare(baseline, recon, chains)
    red = [d for d in diffs if d["level"] == RED]
    green = [d for d in diffs if d["level"] == GREEN]
    cur_rows = collect_rows(recon)
    cur_chains = collect_chains(chains)

    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "verdict": RED if red else GREEN,
        "red_count": len(red),
        "green_count": len(green),
        "baseline_aligned": baseline.get("aligned", 0),
        "current_aligned": sum(1 for v in cur_rows.values() if v == "ALIGNED"),
        "baseline_confirmed": sum(1 for v in baseline.get("chains", {}).values()
                                  if v == "CONFIRMED"),
        "current_confirmed": sum(1 for v in cur_chains.values() if v == "CONFIRMED"),
        "diffs": diffs,
    }
    dump_json(REPORT, report)
    with open(REPORT_MD, "w", encoding="utf-8") as fh:
        fh.write(render_md(report))

    print(f"门禁报告 -> {REPORT_MD}")
    print(f"  结论 {report['verdict']}：红 {len(red)} / 绿 {len(green)}")
    print(f"  对齐项 {report['baseline_aligned']} -> {report['current_aligned']}"
          f"；攻击链确认 {report['baseline_confirmed']} -> {report['current_confirmed']}")
    for d in diffs:
        print(f"    [{d['level']}] {d['kind']:22} {d['key']}: {d['before']} -> {d['after']}")

    if red:
        print("门禁失败：存在回归（红项见上表）")
        return 1

    if "--accept" in args:
        dump_json(BASELINE, build_baseline(recon, chains))
        print("门禁：已接受当前结果为新基线")
    else:
        print("门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
