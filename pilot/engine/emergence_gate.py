"""涌现 FDR 门禁 —— 「该抓的抓到了吗？抓对层了吗？」

这个门禁度量的是**审计系统本身**，不是被测系统：
注入目录里每条 active=true 的注入，代表一个已知的低层根因；
审计跑完之后，这条根因要么被某一层正确开单（检出且归属正确），要么没有。

    FDR_inj  = 检出且归属正确 / 分母          （主指标）
    Attr_acc = 检出且归属正确 / 检出          （抓到的里面，归对层的比例）
    另有：分类别 FDR、分母哈希、集合哈希、漏检清单

退出码：0 绿 / 1 红 / 2 不完整（没查完）。
「没查完」与「查了没问题」必须分开，否则覆盖度矩阵会骗人。

口径（判断记录见 EMERGENCE_CANDIDATES_RESULT.md）：
  归属正确 = 恰好一个 own 单认领该根因，且认领层就是所有提及该根因的层里最低的一层，
             且该单的类别在该层契约允许范围内、ground_truth=verified。
  只被引用（cited）没有 own 单 = 这个缺陷没有归属 = 不算检出
  （「可归属」是本项目的核心承诺，归错层等于没检出来）。
  活性检查不过的注入从分母剔除，但必须告警并记录，不许静默剔除。
  分母哈希变化 = 红（防「把检不出来的注入删掉来提高 FDR」）。

引擎零项目字面量：路径、层序、类别允许范围全部从 target.json 与契约读。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import findings_schema
from emergence import load_catalog

SCHEMA_VERSION = 1
ENGINE_NAME = "emergence_gate"

RED = "RED"
GREEN = "GREEN"
INCOMPLETE = "INCOMPLETE"

EXIT_GREEN = 0
EXIT_RED = 1
EXIT_INCOMPLETE = 2

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "fdr_inj": 0.85,
    "attr_acc": 0.95,
    "category_min_count": 3,
    "category_min_fdr": 0.0,
}

UNCATEGORIZED = "uncategorized"


# ------------------------------------------------------------------ 基础工具

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(items: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()[:16]


def artifacts_dir() -> Path:
    target = findings_schema.load_target()
    raw = (target.get("paths") or {}).get("artifacts_dir")
    return Path(raw) if raw else project_dir()


def project_dir() -> Path:
    return findings_schema.target_path().parent


def tool_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def default_paths() -> dict[str, Path]:
    return {
        "catalog": project_dir() / "emergence_injections.json",
        "results": artifacts_dir() / "emergence_chains.json",
        "findings": project_dir() / "all_findings.json",
        "baseline": tool_dir() / "emergence_baseline.json",
        "out": artifacts_dir() / "emergence_gate.json",
    }


def rel_to_project(path: Path) -> str:
    try:
        return str(path.relative_to(project_dir()))
    except ValueError:
        return path.name


# ------------------------------------------------------------------ 输入解析

def read_entries(catalog_path: Path) -> list[dict[str, Any]]:
    """把注入目录摊平成条目列表。目录格式由 emergence.load_catalog 定义。"""
    catalog = load_catalog(catalog_path)
    entries: list[dict[str, Any]] = []
    for chain in catalog.chains:
        for inj in chain.injections:
            entries.append({
                "chain": chain.id,
                "id": inj.id,
                "defect": inj.defect,
                "layer": inj.layer,
                "category": inj.category or "",
                "active": bool(inj.active),
                "mutation_kind": inj.mutation.get("kind", "") if isinstance(inj.mutation, dict) else "",
            })
    return entries


def read_alive(results_path: Path) -> tuple[dict[str, bool], set[str]]:
    """返回 (注入 id -> 活性检查结果, 已裁决的链 id 集合)。"""
    payload = load_json(results_path)
    alive: dict[str, bool] = {}
    done: set[str] = set()
    for chain in payload.get("chains", []):
        done.add(str(chain.get("id", "")))
        for inj_id, ok in (chain.get("alive") or {}).items():
            alive[str(inj_id)] = bool(ok)
    return alive, done


def read_findings(findings_path: Path) -> list[dict[str, Any]]:
    payload = load_json(findings_path)
    if isinstance(payload, list):
        return [f for f in payload if isinstance(f, dict)]
    return [f for f in payload.get("findings", []) if isinstance(f, dict)]


def owner_layer(finding: dict[str, Any]) -> str:
    owner = finding.get("owner") or {}
    return str(owner.get("layer") or finding.get("layer") or "")


def owner_relation(finding: dict[str, Any]) -> str:
    owner = finding.get("owner") or {}
    return str(owner.get("relation") or "")


def index_findings(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    idx: dict[str, list[dict[str, Any]]] = {"own": {}, "cited": {}}
    for f in findings:
        key = str(f.get("root_cause_key") or "")
        if key and owner_relation(f) == "own":
            idx["own"].setdefault(key, []).append(f)
        for cited in (f.get("related_root_causes") or []):
            idx["cited"].setdefault(str(cited), []).append(f)
    return idx


# ------------------------------------------------------------------ 归属判定

def layer_rank(layer: str) -> int:
    layers = findings_schema.LAYERS
    return layers.index(layer) if layer in layers else len(layers)


def attribute(entry: dict[str, Any], idx: dict[str, list[dict[str, Any]]],
              categories: dict[str, tuple[str, ...]],
              out_of_scope: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """判定一条注入代表的根因：检出没有？归对层没有？"""
    defect = entry["defect"]
    owners = idx["own"].get(defect, [])
    citers = idx["cited"].get(defect, [])
    res: dict[str, Any] = {
        "detected": bool(owners or citers),
        "attributed": False,
        "status": "",
        "reason": "",
        "owners": [str(f.get("id", "")) for f in owners],
        "citers": [str(f.get("id", "")) for f in citers],
    }
    if not owners:
        if citers:
            res["status"] = "cited_only"
            res["reason"] = "只被引用、没有 own 单：这个根因没有归属，按「可归属」口径不算检出"
        else:
            res["status"] = "not_detected"
            res["reason"] = "全库没有任何 finding 提及该根因"
        return res
    if len(owners) > 1:
        res["status"] = "multiple_owners"
        res["reason"] = "多个 own 单认领同一根因：违反「一个缺陷只属于一层」"
        return res

    owner = owners[0]
    layer = owner_layer(owner)
    problems: list[str] = []

    if layer != str(owner.get("layer") or ""):
        problems.append(f"own 单的 layer({owner.get('layer')}) 与 owner.layer({layer}) 不一致")
    if layer not in findings_schema.LAYERS:
        problems.append(f"归属层 {layer} 不在契约层序内")

    category = str(owner.get("category") or "")
    if category and category != findings_schema.UNMAPPED:
        layer_part, _, cat_part = category.partition(".")
        if layer_part in categories and cat_part not in categories[layer_part]:
            problems.append(f"类别 {category} 不在 {layer_part} 层的允许列表")
        if layer_part in out_of_scope and cat_part in out_of_scope[layer_part]:
            problems.append(f"{layer_part} 层对类别 {cat_part} 是 out_of_scope，不许开单")
    elif not category:
        problems.append("own 单没有 category")

    ground_truth = str(owner.get("ground_truth") or "")
    if ground_truth != "verified":
        problems.append(f"own 单的 ground_truth={ground_truth or '空'}，不是已确认的检出")

    lower_citers = [c for c in citers
                    if layer_rank(owner_layer(c)) < layer_rank(layer)]
    if lower_citers:
        problems.append(
            "有更低层引用了该根因却由更高层开单：owner 应为能给出可复现证据的最低层")

    if problems:
        res["status"] = "misattributed"
        res["reason"] = "；".join(problems)
        return res
    res["status"] = "attributed"
    res["attributed"] = True
    res["reason"] = f"own 单 {res['owners'][0]} 在 {layer} 层认领，层序最低、类别合法"
    return res


# ------------------------------------------------------------------ 基线

def load_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "engine": ENGINE_NAME,
                "thresholds": dict(DEFAULT_THRESHOLDS), "targets": {}}
    try:
        return load_json(path)
    except (ValueError, OSError):
        return {"schema_version": SCHEMA_VERSION, "engine": ENGINE_NAME,
                "thresholds": dict(DEFAULT_THRESHOLDS), "targets": {}}


def write_baseline(path: Path, baseline: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ------------------------------------------------------------------ 主计算

def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    paths = default_paths()
    catalog_path = Path(args.catalog) if args.catalog else paths["catalog"]
    results_path = Path(args.results) if args.results else paths["results"]
    findings_path = Path(args.findings) if args.findings else paths["findings"]
    baseline_path = Path(args.baseline) if args.baseline else paths["baseline"]

    target = findings_schema.load_target()
    target_name = str(target.get("target") or "")
    categories = findings_schema.load_layer_categories()
    out_of_scope = findings_schema.load_out_of_scope()

    problems: list[dict[str, Any]] = []
    warnings: list[str] = []

    def problem(level: str, kind: str, detail: str) -> None:
        problems.append({"level": level, "kind": kind, "detail": detail})

    # ---- 输入齐不齐（缺输入 = 没查完，不是没问题）
    missing: list[str] = []
    if not catalog_path.exists():
        missing.append(f"注入目录不存在: {rel_to_project(catalog_path)}")
    if not results_path.exists():
        missing.append(f"涌现裁决产物不存在: {rel_to_project(results_path)}")
    if not findings_path.exists():
        missing.append(f"发现集合不存在: {rel_to_project(findings_path)}")
    if missing:
        for m in missing:
            problem(INCOMPLETE, "input_missing", m)
        return _pack(target_name, None, None, problems, warnings, {}, baseline_path,
                     catalog_path, results_path, findings_path, None)

    entries = read_entries(catalog_path)
    alive, done_chains = read_alive(results_path)
    findings = read_findings(findings_path)
    idx = index_findings(findings)

    # ---- 分母：active=true，减去活性检查不过的（告警并记录，不静默剔除）
    declared = [e for e in entries if e["active"]]
    inactive = [e for e in entries if not e["active"]]
    for e in inactive:
        warnings.append(f"注入 {e['id']} 是 inactive：不进分母（只用于敏感性分析）")

    alive_dropped: list[dict[str, Any]] = []
    not_run: list[dict[str, Any]] = []
    denominator: list[dict[str, Any]] = []
    for e in declared:
        if e["id"] not in alive:
            not_run.append(e)
            continue
        if alive[e["id"]]:
            denominator.append(e)
        else:
            alive_dropped.append(e)

    for e in not_run:
        problem(INCOMPLETE, "injection_not_run",
                f"注入 {e['id']} 没有裁决产物里的活性检查结果：本轮没查完")
    for e in alive_dropped:
        warnings.append(
            f"注入 {e['id']} 活性检查不通过：已从分母剔除并记录（不许静默剔除）"
            f" —— {e['defect']}")

    # ---- 归属判定（分母内 + 被剔除的，信息都要留）
    per_injection: list[dict[str, Any]] = []
    for e in entries:
        attr = attribute(e, idx, categories, out_of_scope)
        in_denominator = any(d["id"] == e["id"] for d in denominator)
        rec = {
            "injection": e["id"],
            "chain": e["chain"],
            "defect": e["defect"],
            "inject_layer": e["layer"],
            "category": e["category"] or f"layer:{e['layer'] or '?'}",
            "active": e["active"],
            "alive": alive.get(e["id"]),
            "in_denominator": in_denominator,
            "detected": attr["detected"],
            "attributed": attr["attributed"],
            "status": attr["status"],
            "reason": attr["reason"],
            "owners": attr["owners"],
            "citers": attr["citers"],
        }
        if not e["active"]:
            rec["excluded_reason"] = "inactive"
        elif e["id"] in {x["id"] for x in alive_dropped}:
            rec["excluded_reason"] = "alive_check_failed"
        elif e["id"] in {x["id"] for x in not_run}:
            rec["excluded_reason"] = "not_run"
        per_injection.append(rec)

    # ---- 指标
    k_den = len(denominator)
    detected = [r for r in per_injection if r["in_denominator"] and r["detected"]]
    attributed = [r for r in per_injection if r["in_denominator"] and r["attributed"]]
    fdr = (len(attributed) / k_den) if k_den else None
    attr_acc = (len(attributed) / len(detected)) if detected else None

    per_category: dict[str, dict[str, Any]] = {}
    for r in per_injection:
        if not r["in_denominator"]:
            continue
        bucket = per_category.setdefault(r["category"], {"total": 0, "detected": 0,
                                                          "attributed": 0, "fdr": None})
        bucket["total"] += 1
        bucket["detected"] += 1 if r["detected"] else 0
        bucket["attributed"] += 1 if r["attributed"] else 0
    for bucket in per_category.values():
        bucket["fdr"] = (bucket["attributed"] / bucket["total"]) if bucket["total"] else None

    denominator_hash = digest([f"{e['id']}|{e['defect']}" for e in declared])
    set_hash = digest([f"{e['chain']}|{e['id']}|{e['defect']}" for e in entries])

    # ---- 阈值
    baseline = load_baseline(baseline_path)
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update({k: v for k, v in (baseline.get("thresholds") or {}).items()
                       if k in DEFAULT_THRESHOLDS})
    if args.fdr_threshold is not None:
        thresholds["fdr_inj"] = args.fdr_threshold
    if args.attr_threshold is not None:
        thresholds["attr_acc"] = args.attr_threshold

    if k_den == 0:
        problem(INCOMPLETE, "empty_denominator",
                "分母为空（没有 active 且活性通过的注入）：这是「没查完」，不是「没问题」")
    else:
        if fdr is not None and fdr < float(thresholds["fdr_inj"]):
            problem(RED, "fdr_below_threshold",
                    f"FDR_inj={fdr:.3f} < 阈值 {thresholds['fdr_inj']}")
        if attr_acc is not None and attr_acc < float(thresholds["attr_acc"]):
            problem(RED, "attr_acc_below_threshold",
                    f"Attr_acc={attr_acc:.3f} < 阈值 {thresholds['attr_acc']}")
        for name, bucket in sorted(per_category.items()):
            if bucket["total"] >= int(thresholds["category_min_count"]) \
                    and (bucket["fdr"] or 0.0) <= float(thresholds["category_min_fdr"]):
                problem(RED, "category_blind",
                        f"类别 {name} 有 {bucket['total']} 条注入、检出 0 → 该类别是盲区")

    # ---- 集合漂移（防「把检不出来的注入删掉来提高 FDR」）
    section = (baseline.get("targets") or {}).get(target_name) or {}
    drift = {"denominator": False, "set": False, "alive_set": False}
    if section:
        if str(section.get("denominator_hash") or "") != denominator_hash:
            drift["denominator"] = True
            problem(RED, "denominator_drift",
                    f"分母哈希漂移：基线 {section.get('denominator_hash')} → 本轮 {denominator_hash}"
                    "（分母被改过 = 有人在动注入目录）")
        if str(section.get("set_hash") or "") != set_hash:
            drift["set"] = True
            problem(RED, "set_drift",
                    f"集合哈希漂移：基线 {section.get('set_hash')} → 本轮 {set_hash}")
        base_alive = sorted(str(x) for x in (section.get("alive_dropped") or []))
        now_alive = sorted(e["id"] for e in alive_dropped)
        if base_alive != now_alive:
            drift["alive_set"] = True
            problem(INCOMPLETE, "alive_set_drift",
                    f"活性检查剔除集合漂移：基线 {base_alive} → 本轮 {now_alive}"
                    "（本轮数字与基线不可比，重跑或 --init 重钉）")
    else:
        problem(INCOMPLETE, "baseline_missing",
                f"该目标还没有基线（{rel_to_project(baseline_path)}）：先跑 --init 钉住分母")

    # ---- 漏检清单
    missed = [
        {
            "injection": r["injection"],
            "chain": r["chain"],
            "defect": r["defect"],
            "category": r["category"],
            "detected": r["detected"],
            "status": r["status"],
            "reason": r["reason"],
            "owners": r["owners"],
            "citers": r["citers"],
        }
        for r in per_injection
        if r["in_denominator"] and not r["attributed"]
    ]

    metrics = {
        "declared_active": len(declared),
        "inactive": len(inactive),
        "denominator": k_den,
        "detected": len(detected),
        "attributed": len(attributed),
        "fdr_inj": fdr,
        "attr_acc": attr_acc,
        "per_category": per_category,
        "denominator_hash": denominator_hash,
        "set_hash": set_hash,
        "alive_dropped": [e["id"] for e in alive_dropped],
        "not_run": [e["id"] for e in not_run],
    }

    extra = {
        "chains_total": len(entries and {e["chain"] for e in entries}),
        "chains_adjudicated": len(done_chains),
        "verdicts": _verdict_counts(results_path),
        "findings_total": len(findings),
        "per_injection": per_injection,
        "missed": missed,
        "drift": drift,
        "baseline_section": section,
        "thresholds": thresholds,
    }
    return _pack(target_name, metrics, extra, problems, warnings, {"target": target_name},
                 baseline_path, catalog_path, results_path, findings_path, entries)


def _verdict_counts(results_path: Path) -> dict[str, int]:
    payload = load_json(results_path)
    counts: dict[str, int] = {}
    for chain in payload.get("chains", []):
        verdict = str(chain.get("verdict") or "?")
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _pack(target_name: str, metrics: dict[str, Any] | None, extra: dict[str, Any] | None,
          problems: list[dict[str, Any]], warnings: list[str],
          _unused: dict[str, Any], baseline_path: Path, catalog_path: Path,
          results_path: Path, findings_path: Path,
          entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    levels = {p["level"] for p in problems}
    if RED in levels:
        verdict, exit_code = RED, EXIT_RED
    elif INCOMPLETE in levels:
        verdict, exit_code = INCOMPLETE, EXIT_INCOMPLETE
    else:
        verdict, exit_code = GREEN, EXIT_GREEN

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE_NAME,
        "generated_at": now_iso(),
        "target": target_name,
        "verdict": verdict,
        "exit_code": exit_code,
        "inputs": {
            "catalog": rel_to_project(catalog_path),
            "results": rel_to_project(results_path),
            "findings": rel_to_project(findings_path),
            "baseline": rel_to_project(baseline_path),
        },
        "metrics": metrics or {},
        "problems": problems,
        "warnings": warnings,
    }
    payload.update(extra or {})
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def _fingerprint(payload: dict[str, Any]) -> str:
    """决策指纹：只覆盖会影响结论的内容，生成时间不进指纹（同输入两次跑指纹一致）。"""
    core = {
        "verdict": payload.get("verdict"),
        "metrics": {k: v for k, v in (payload.get("metrics") or {}).items()
                    if k not in ("per_category",)},
        "per_category": payload.get("metrics", {}).get("per_category"),
        "missed": [m.get("injection") for m in payload.get("missed") or []],
        "problems": sorted(f"{p.get('level')}:{p.get('kind')}" for p in payload.get("problems") or []),
    }
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 基线管理

def do_init(args: argparse.Namespace) -> int:
    paths = default_paths()
    catalog_path = Path(args.catalog) if args.catalog else paths["catalog"]
    baseline_path = Path(args.baseline) if args.baseline else paths["baseline"]
    target = findings_schema.load_target()
    target_name = str(target.get("target") or "")

    if not catalog_path.exists():
        print(f"注入目录不存在：{rel_to_project(catalog_path)} —— 没有分母可钉", file=sys.stderr)
        return EXIT_INCOMPLETE
    entries = read_entries(catalog_path)
    declared = [e for e in entries if e["active"]]
    denominator_hash = digest([f"{e['id']}|{e['defect']}" for e in declared])
    set_hash = digest([f"{e['chain']}|{e['id']}|{e['defect']}" for e in entries])

    alive_dropped: list[str] = []
    results_path = Path(args.results) if args.results else paths["results"]
    if results_path.exists():
        alive, _ = read_alive(results_path)
        alive_dropped = [e["id"] for e in declared if alive.get(e["id"]) is False]

    baseline = load_baseline(baseline_path)
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update({k: v for k, v in (baseline.get("thresholds") or {}).items()
                       if k in DEFAULT_THRESHOLDS})
    if args.fdr_threshold is not None:
        thresholds["fdr_inj"] = args.fdr_threshold
    if args.attr_threshold is not None:
        thresholds["attr_acc"] = args.attr_threshold

    section = {
        "denominator_hash": denominator_hash,
        "set_hash": set_hash,
        "denominator": [e["id"] for e in declared],
        "alive_dropped": alive_dropped,
        "chains": sorted({e["chain"] for e in entries}),
        "catalog": rel_to_project(catalog_path),
        "created_at": now_iso(),
    }
    existing = (baseline.get("targets") or {}).get(target_name)
    if existing and not args.accept:
        changed = {k: (existing.get(k), section.get(k))
                   for k in ("denominator_hash", "set_hash", "denominator", "alive_dropped")
                   if existing.get(k) != section.get(k)}
        if changed:
            print("基线已存在且与本轮不一致 —— 重钉会掩盖漂移，需显式 --accept：", file=sys.stderr)
            for k, (before, after) in changed.items():
                print(f"  {k}: {before} -> {after}", file=sys.stderr)
            return EXIT_RED
    baseline.setdefault("schema_version", SCHEMA_VERSION)
    baseline.setdefault("engine", ENGINE_NAME)
    baseline["thresholds"] = thresholds
    baseline.setdefault("targets", {})[target_name] = section
    write_baseline(baseline_path, baseline)
    print(f"基线 -> {baseline_path}")
    print(f"  目标 {target_name}：分母 {len(declared)} 条，分母哈希 {denominator_hash}，"
          f"集合哈希 {set_hash}")
    return EXIT_GREEN


# ------------------------------------------------------------------ 渲染

def render_md(report: dict[str, Any]) -> str:
    m = report.get("metrics") or {}
    lines: list[str] = []
    lines.append(f"# 涌现 FDR 门禁 —— {report.get('target')}")
    lines.append("")
    lines.append(f"- 结论：**{report.get('verdict')}**（退出码 {report.get('exit_code')}）")
    lines.append(f"- 指纹：`{report.get('fingerprint')}`")
    lines.append(f"- 生成时间：{report.get('generated_at')}")
    lines.append("")
    lines.append("## 指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for key, label in (("declared_active", "目录里 active=true 条数"),
                       ("denominator", "分母（active ∧ 活性通过）"),
                       ("detected", "检出（根因被提及）"),
                       ("attributed", "检出且归属正确"),
                       ("fdr_inj", "FDR_inj（主指标）"),
                       ("attr_acc", "Attr_acc")):
        val = m.get(key)
        if isinstance(val, float):
            val = f"{val:.3f}"
        lines.append(f"| {label} | {val if val is not None else '—'} |")
    lines.append("")
    lines.append(f"- 分母哈希：`{m.get('denominator_hash')}`")
    lines.append(f"- 集合哈希：`{m.get('set_hash')}`")
    lines.append("")
    pc = m.get("per_category") or {}
    if pc:
        lines.append("## 分类别 FDR")
        lines.append("")
        lines.append("| 类别 | 条数 | 检出 | 归属正确 | FDR |")
        lines.append("|---|---|---|---|---|")
        for name, b in sorted(pc.items()):
            f = b.get("fdr")
            lines.append(f"| {name} | {b.get('total')} | {b.get('detected')} | "
                         f"{b.get('attributed')} | {f if f is None else f'{f:.3f}'} |")
        lines.append("")
    missed = report.get("missed") or []
    lines.append("## 漏检清单")
    lines.append("")
    if not missed:
        lines.append("（空）")
    else:
        lines.append("| 注入 | 根因 | 检出 | 状态 | 原因 |")
        lines.append("|---|---|---|---|---|")
        for x in missed:
            lines.append(f"| {x.get('injection')} | `{x.get('defect')}` | "
                         f"{'是' if x.get('detected') else '否'} | {x.get('status')} | "
                         f"{x.get('reason')} |")
    lines.append("")
    warns = report.get("warnings") or []
    if warns:
        lines.append("## 告警")
        lines.append("")
        for w in warns:
            lines.append(f"- {w}")
        lines.append("")
    probs = report.get("problems") or []
    if probs:
        lines.append("## 门禁问题")
        lines.append("")
        for p in probs:
            lines.append(f"- **{p.get('level')}** `{p.get('kind')}`：{p.get('detail')}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="涌现 FDR 门禁（通用引擎）")
    ap.add_argument("--catalog", default="", help="注入目录（默认 <项目目录>/emergence_injections.json）")
    ap.add_argument("--results", default="", help="涌现裁决产物（默认 <产物目录>/emergence_chains.json）")
    ap.add_argument("--findings", default="", help="发现集合（默认 <项目目录>/all_findings.json）")
    ap.add_argument("--baseline", default="", help="基线文件（默认 <工具目录>/emergence_baseline.json）")
    ap.add_argument("--out", default="", help="报告输出（默认 <产物目录>/emergence_gate.json）")
    ap.add_argument("--md", default="", help="人读版输出（默认与 --out 同目录 .md）")
    ap.add_argument("--init", action="store_true", help="钉基线（首次）")
    ap.add_argument("--accept", action="store_true", help="接受漂移并重钉基线")
    ap.add_argument("--fdr-threshold", type=float, default=None, help="FDR_inj 阈值")
    ap.add_argument("--attr-threshold", type=float, default=None, help="Attr_acc 阈值")
    args = ap.parse_args()

    if args.init:
        return do_init(args)

    report = evaluate(args)
    paths = default_paths()
    out_path = Path(args.out) if args.out else paths["out"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    md_path = Path(args.md) if args.md else out_path.with_suffix(".md")
    md_path.write_text(render_md(report), encoding="utf-8")

    m = report.get("metrics") or {}
    fdr = m.get("fdr_inj")
    print(f"涌现 FDR 门禁：{report.get('verdict')}（退出码 {report.get('exit_code')}）")
    print(f"  分母 {m.get('denominator')}｜检出 {m.get('detected')}｜归属正确 {m.get('attributed')}")
    print(f"  FDR_inj {fdr if fdr is None else round(fdr, 4)}｜"
          f"Attr_acc {m.get('attr_acc') if m.get('attr_acc') is None else round(m['attr_acc'], 4)}")
    print(f"  分母哈希 {m.get('denominator_hash')}｜集合哈希 {m.get('set_hash')}")
    missed = report.get("missed") or []
    if missed:
        print(f"  漏检 {len(missed)} 条：" + ", ".join(str(x.get('injection')) for x in missed))
    for p in report.get("problems") or []:
        print(f"  [{p.get('level')}] {p.get('kind')}: {p.get('detail')}")
    for w in report.get("warnings") or []:
        print(f"  [WARN] {w}")
    print(f"  指纹 {report.get('fingerprint')}")
    print(f"  报告 -> {out_path}")
    return int(report.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
