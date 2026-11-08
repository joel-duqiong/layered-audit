"""特性覆盖矩阵 —— 回答「这套审计在框架特性上看得见什么、看不见什么」。

跟 category 覆盖度矩阵是同一个模式，只是第二根轴：
  category 覆盖度矩阵：层 × 契约类别    → 「哪类缺陷没人查」
  特性覆盖矩阵：      特性 × 载体      → 「哪种运行时语义我们看不见」

引擎本身是通用的：
- 特性清单、载体清单都在 target.json（项目配置）里；
- 检测器是项目侧脚本，引擎只负责调用并汇总结论；
- 换语言 = 换特性清单与检测器，引擎不动。

输出：
  feature_coverage.json  矩阵 + 盲区清单 + 受影响结论数
  feature_coverage.md    人读版
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import findings_schema

HERE = Path(__file__).resolve().parent
PROJECT_DIR = findings_schema.target_path().parent


def artifacts_dir() -> Path:
    """产物目录（跟别的层一致）—— 换目标时落到目标自己的目录。"""
    target = findings_schema.load_target()
    raw = (target.get("paths") or {}).get("artifacts_dir")
    return Path(raw) if raw else PROJECT_DIR
# 工具目录：pilot/ —— 各层探针脚本的家（engine/ 的上一级）
TOOL_DIR = Path(__file__).resolve().parent.parent


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    target = findings_schema.load_target()
    return target.get("carriers", {}), target.get("features", [])


def run_detector(name: str) -> dict[str, Any]:
    """跑项目侧检测器；跑不起来就记下来，不静默吞。

    检测器脚本住在**工具目录**（pilot/）里 —— 那是各层探针的家；
    项目目录（target.json 所在处）里也可能有自己的检测器，先找项目侧。
    """
    path = PROJECT_DIR / name
    if not path.exists():
        path = TOOL_DIR / name
    if not path.exists():
        return {"ok": False, "error": f"检测器不存在: {name}"}
    proc = subprocess.run(["python3", str(path)], capture_output=True, text=True,
                          timeout=300, cwd=str(PROJECT_DIR))
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[-300:]}
    out: dict[str, Any] = {"ok": True}
    findings_file = PROJECT_DIR / "feature_findings.json"
    if findings_file.exists():
        out["findings"] = json.loads(findings_file.read_text(encoding="utf-8"))
    return out


def build() -> dict[str, Any]:
    carriers, features = _load()
    all_findings = json.loads((PROJECT_DIR / "all_findings.json").read_text(encoding="utf-8"))
    records = all_findings["findings"]

    detector_cache: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for feat in features:
        det = feat.get("detector")
        if det and det not in detector_cache:
            detector_cache[det] = run_detector(det)
        det_result = detector_cache.get(det) if det else None
        covered = feat.get("covered_by", [])
        blind = feat.get("blind_carriers", [])
        affected = sorted({
            f"{r['layer']}.{r['category'].partition('.')[2]}"
            for r in records
            for c in blind
            if carriers.get(c, {}).get("layer") == r["layer"]
        })
        rows.append({
            "feature": feat["id"],
            "label": feat["label"],
            "detector": det,
            "detector_ok": None if det is None else bool(det_result and det_result.get("ok")),
            "covered_by": covered,
            "blind_carriers": blind,
            "status": "covered" if covered and not blind else ("partial" if covered else "blind"),
            "affected_findings": affected,
            "affected_count": len(affected),
            "note": feat.get("note", ""),
        })

    blind_spots = []
    for name, carrier in carriers.items():
        blind_spots.append({
            "carrier": name,
            "layer": carrier.get("layer"),
            "language": carrier.get("language"),
            "blind_to": carrier.get("blind_to", []),
        })

    uncovered = [r for r in rows if r["status"] == "blind"]
    return {
        "generated_from": "feature_coverage.py",
        "carriers": blind_spots,
        "matrix": rows,
        "summary": {
            "features": len(rows),
            "covered": sum(1 for r in rows if r["status"] == "covered"),
            "partial": sum(1 for r in rows if r["status"] == "partial"),
            "blind": len(uncovered),
            "detectors_failing": [d for d, r in detector_cache.items() if not r.get("ok")],
        },
    }


def main() -> None:
    payload = build()
    (artifacts_dir() / "feature_coverage.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(payload)
    s = payload["summary"]
    print(f"特性覆盖矩阵 -> {artifacts_dir() / 'feature_coverage.json'}")
    print(f"  特性 {s['features']} 个：覆盖 {s['covered']} / 部分 {s['partial']} / 盲区 {s['blind']}")
    if s["detectors_failing"]:
        print(f"  检测器失败：{s['detectors_failing']}")


def _write_md(payload: dict[str, Any]) -> None:
    lines: list[str] = []
    a = lines.append
    a("# 特性覆盖矩阵（特性 × 载体）")
    a("")
    a("回答的是：这套审计在**框架运行时语义**上看得见什么、看不见什么。")
    a("跟 category 覆盖度矩阵是同一模式，只是第二根轴。")
    a("")
    a("| 特性 | 检测器 | 覆盖它的载体 | 看不见它的载体 | 状态 | 受影响结论 |")
    a("|---|---|---|---|---|---|")
    for r in payload["matrix"]:
        det = r["detector"] or "—"
        if r["detector_ok"] is False:
            det += "（失败）"
        a(f"| {r['label']} | `{det}` | {', '.join(r['covered_by']) or '—'} | "
          f"{', '.join(r['blind_carriers']) or '—'} | {r['status']} | {r['affected_count']} 条 |")
    a("")
    a("## 载体的盲区")
    a("")
    for c in payload["carriers"]:
        a(f"### {c['carrier']}（{c['layer']} 层 / {c['language']}）")
        a("")
        for b in c["blind_to"]:
            a(f"- {b}")
        a("")
    (PROJECT_DIR / "feature_coverage.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
