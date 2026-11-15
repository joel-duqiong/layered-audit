"""观测适配器 —— 把已有产物里真实记录的行观测纯转录成求值器观测包。

契约（以 rule_assessment.py 源码为准）：
  观测包 {"observations": [{id, target, scope, reached, row, evidence_refs}]}。
  求值器要求 target/scope 与判据逐字一致、reached === true、row 是对象、
  evidence_refs 是非空字符串数组，否则 UNKNOWN；缺字段 UNKNOWN，绝不补 null。

铁律：
  · 只转录源产物已记录的值：不造字段、不补 null、不猜默认值、不改写值；
  · 源产物真的记录了该行才标 reached；未记录行进 not_reached 台账，永不进包；
  · 每条观测带 evidence_refs：源产物文件 + JSON Pointer 位置 + 绑定证据；
  · scope 只来自绑定配置（固定值 / 记录字段 / 标签前缀），解析不出就不产出、缺口可见；
  · 包按 (batch, scope) 切分：求值器对每条规则聚合包内**全部**观测，
    混 scope 或混行形会互相洗成 UNKNOWN（assess() 源码核实），故一包一 scope 一行形。

绑定配置是项目侧数据（声明 scope 与提取路径，并附绑定证据）；引擎零项目字面量。
本模块不执行被测系统、不连库、不发 HTTP；输出逐字节确定（无时间戳）。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROW_SOURCES = ("row_key", "row_path", "row_map")
SCOPE_SOURCES = ("scope", "scope_key", "scope_label_prefix")
STATUS_PRECEDENCE = ("FAIL", "UNKNOWN", "PASS", "NOT_APPLICABLE")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pointer(*parts: Any) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or not _nonempty_str(config.get("target")):
        raise ValueError("绑定配置必须有非空 target")
    bindings = config.get("bindings")
    if not isinstance(bindings, list):
        raise ValueError("绑定配置必须有 bindings 数组")
    seen = set()
    for i, b in enumerate(bindings):
        if not isinstance(b, dict):
            raise ValueError(f"bindings[{i}] 不是对象")
        for key in ("artifact", "section"):
            if not _nonempty_str(b.get(key)):
                raise ValueError(f"bindings[{i}] 缺 {key}")
        row_sources = [k for k in ROW_SOURCES if k in b]
        if len(row_sources) != 1:
            raise ValueError(f"bindings[{i}] 必须恰有一个行来源 {ROW_SOURCES}")
        scope_sources = [k for k in SCOPE_SOURCES if k in b]
        if len(scope_sources) != 1:
            raise ValueError(f"bindings[{i}] 必须恰有一个 scope 来源 {SCOPE_SOURCES}")
        if "scope" in b and not _nonempty_str(b["scope"]):
            raise ValueError(f"bindings[{i}] scope 为空")
        if "row_map" in b:
            rm = b["row_map"]
            if not isinstance(rm, dict) or not rm or not all(
                    _nonempty_str(k) and _nonempty_str(v) for k, v in rm.items()):
                raise ValueError(f"bindings[{i}] row_map 必须是非空 字段->源键 映射")
        evidence = b.get("scope_evidence")
        if not isinstance(evidence, list) or not evidence or not all(_nonempty_str(e) for e in evidence):
            raise ValueError(f"bindings[{i}] 缺 scope_evidence —— 无绑定证据不得产出观测")
        filt = b.get("filter")
        if filt is not None:
            if not isinstance(filt, dict) or not filt or not all(
                    _nonempty_str(k) and isinstance(v, list) and v and all(_nonempty_str(x) for x in v)
                    for k, v in filt.items()):
                raise ValueError(f"bindings[{i}] filter 必须是 字段->非空字符串数组")
        batch = b.get("batch", b["section"])
        if not _nonempty_str(batch):
            raise ValueError(f"bindings[{i}] batch 为空")
        key = (b["artifact"], b["section"], batch)
        if key in seen:
            raise ValueError(f"bindings[{i}] 重复绑定 {key}")
        seen.add(key)
    return config


def _resolve_scope(b: dict[str, Any], entry: dict[str, Any]) -> str | None:
    if "scope" in b:
        return b["scope"]
    if "scope_key" in b:
        value = entry.get(b["scope_key"])
        return value if _nonempty_str(value) else None
    label = entry.get(b.get("label_key", "label"))
    if not _nonempty_str(label):
        return None
    token = label.split()[0]
    head, sep, _ = token.partition(".")
    return head if sep and head else None


def _matches_filter(b: dict[str, Any], entry: dict[str, Any]) -> bool:
    filt = b.get("filter")
    if not filt:
        return True
    return all(entry.get(k) in vals for k, vals in filt.items())


def _extract_row(b: dict[str, Any], entry: dict[str, Any]) -> tuple[Any, str | None]:
    """返回 (行值或列表, 未到达原因)。行值逐字深拷贝，绝不增删键。"""
    if "row_map" in b:
        if "reached_key" in b and entry.get(b["reached_key"]) is not True:
            return None, f"reached_key {b['reached_key']!r} 不为 true"
        row = {out: copy.deepcopy(entry[src]) for out, src in b["row_map"].items()
               if src in entry and entry[src] is not None}
        if not row:
            return None, "row_map 源键均未记录有效值"
        return row, None
    key = b.get("row_key")
    if key is not None:
        if key not in entry:
            return None, f"行键 {key!r} 未记录"
        value = entry[key]
    else:
        value = _get_path(entry, b["row_path"])
        if value is None:
            return None, f"行路径 {b['row_path']!r} 未记录有效值"
    if value is None:
        return None, "行值未记录（null）"
    return copy.deepcopy(value), None


def transcribe(artifacts_dir: Path, config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯转录。返回 (包列表, 转录报告)。包按 (batch, scope) 切分，保持产物原序。"""
    config = validate_config(config)
    target = config["target"]
    packs: dict[tuple[str, str], dict[str, Any]] = {}
    batch_meta: dict[str, dict[str, Any]] = {}
    pack_meta: dict[tuple[str, str], dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    bound_sections: dict[str, set[str]] = {}

    for b in config["bindings"]:
        artifact = b["artifact"]
        section_name = b["section"]
        batch = b.get("batch", section_name)
        bound_sections.setdefault(artifact, set()).add(section_name)
        art_path = artifacts_dir / artifact
        if not art_path.is_file():
            raise ValueError(f"绑定声明的观测产物不存在: {art_path}")
        if artifact not in artifacts:
            artifacts[artifact] = {"path": str(art_path), "sha256": _sha256(art_path)}
        data = json.loads(art_path.read_text(encoding="utf-8"))
        section = data.get(section_name) if isinstance(data, dict) else None
        if section is None:
            gaps.append({"bucket": "section_missing", "artifact": artifact, "section": section_name,
                         "reason": "产物里没有绑定声明的节"})
            continue
        if "map_key" in b:
            container = section.get(b["map_key"]) if isinstance(section, dict) else None
            if not isinstance(container, dict):
                gaps.append({"bucket": "section_malformed", "artifact": artifact, "section": section_name,
                             "reason": f"map_key {b['map_key']!r} 不是对象"})
                continue
            entries = list(container.items())
        else:
            if not isinstance(section, list):
                gaps.append({"bucket": "section_malformed", "artifact": artifact, "section": section_name,
                             "reason": "节不是数组"})
                continue
            entries = list(enumerate(section))

        for idx, entry in entries:
            base_parts = (section_name, b["map_key"], idx) if "map_key" in b else (section_name, idx)
            entry_pointer = _pointer(*base_parts)
            meta = batch_meta.setdefault(batch, {
                "batch": batch, "artifact": artifact, "section": section_name,
                "attempted": 0, "filtered_out": 0, "not_reached": [],
                "scope_evidence": list(b["scope_evidence"])})
            meta["attempted"] += 1
            if not isinstance(entry, dict):
                gaps.append({"bucket": "entry_malformed", "artifact": artifact,
                             "pointer": entry_pointer, "reason": "条目不是对象"})
                continue
            if not _matches_filter(b, entry):
                meta["filtered_out"] += 1
                continue
            scope = _resolve_scope(b, entry)
            if scope is None:
                gaps.append({"bucket": "scope_unresolved", "artifact": artifact,
                             "pointer": entry_pointer,
                             "reason": "记录里解析不出 scope —— 不猜，不产出"})
                continue
            row, reason = _extract_row(b, entry)
            if row is None:
                meta["not_reached"].append({"pointer": f"{artifact}#{entry_pointer}", "reason": reason})
                continue
            if b.get("iterate"):
                if not isinstance(row, list):
                    gaps.append({"bucket": "row_malformed", "artifact": artifact,
                                 "pointer": entry_pointer, "reason": "iterate 绑定的行值不是数组"})
                    continue
                pieces = list(enumerate(row))
            else:
                pieces = [(None, row)]
            pack_key = (batch, scope)
            pack = packs.setdefault(pack_key, {"schema": "observation-pack/1", "target": target,
                                               "scope": scope, "observations": []})
            pmeta = pack_meta.setdefault(pack_key, {"scope": scope, "emitted": 0})
            for sub, piece in pieces:
                if not isinstance(piece, dict):
                    gaps.append({"bucket": "row_malformed", "artifact": artifact,
                                 "pointer": entry_pointer, "reason": "行值不是对象"})
                    continue
                parts = base_parts + ((sub,) if sub is not None else ())
                if "row_key" in b:
                    value_parts = base_parts + (b["row_key"],)
                elif "row_path" in b:
                    value_parts = base_parts + tuple(b["row_path"].split("."))
                else:
                    value_parts = base_parts
                if sub is not None:
                    value_parts = value_parts + (sub,)
                value_pointer = _pointer(*value_parts)
                obs_id = f"{batch}:{artifact}#{section_name}/{idx}" + (f"/{sub}" if sub is not None else "")
                refs = [f"{artifact}#{value_pointer}"]
                if "row_map" in b:
                    refs.extend(f"{artifact}#{_pointer(*(parts + (src,)))}" for src in b["row_map"].values())
                refs.extend(b["scope_evidence"])
                pack["observations"].append({
                    "id": obs_id, "target": target, "scope": scope, "reached": True,
                    "row": piece, "evidence_refs": refs})
                pmeta["emitted"] += 1
    for artifact, info in artifacts.items():
        data = json.loads(Path(info["path"]).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in data:
                if key not in bound_sections.get(artifact, set()):
                    gaps.append({"bucket": "section_not_bound", "artifact": artifact, "section": key,
                                 "reason": "产物节未声明绑定 —— 未转录，可见缺口"})
    batches = []
    for batch, meta in batch_meta.items():
        scoped = [{"scope": scope, "emitted": pm["emitted"],
                   "pack_file": f"observations_{_safe_name(batch)}__{_safe_name(scope)}.json"}
                  for (b2, scope), pm in pack_meta.items() if b2 == batch]
        batches.append({**meta, "packs": scoped,
                        "emitted": sum(p["emitted"] for p in scoped)})
    pack_list = [{"file": f"observations_{_safe_name(batch)}__{_safe_name(scope)}.json", "pack": pack}
                 for (batch, scope), pack in packs.items()]
    report = {
        "schema": "observation-adaptation/1", "target": target,
        "inputs": {"artifacts": artifacts},
        "batches": batches, "packs": [p["file"] for p in pack_list],
        "gaps": gaps,
        "totals": {"packs": len(pack_list),
                   "observations": sum(len(p["pack"]["observations"]) for p in pack_list),
                   "not_reached": sum(len(m["not_reached"]) for m in batch_meta.values()),
                   "gaps": len(gaps)},
        "limitations": [
            "只转录绑定配置声明的节；未绑定节逐条列入 gaps，不静默丢弃",
            "reached 仅当源产物记录了行值；未到达行进 not_reached 台账，不进观测包",
            "row 逐字深拷贝源值：不补字段、不填 null、不改写；缺字段由求值器判 UNKNOWN",
            "scope 绑定是配置声明（附证据），不是本模块对业务域独立核实",
            "evidence_refs 指向源产物位置，本模块不验证源产物自身的真实性",
        ],
    }
    return pack_list, report


def _merge_status(statuses: list[str]) -> str:
    for status in STATUS_PRECEDENCE:
        if status in statuses:
            return status
    return "UNKNOWN"


def summarize(rules_pack: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """合并逐批评估报告。runs: [{pack, assessment}]。

    每条规则只消费 scope 与其一致的批（其余批的 UNKNOWN 是 scope 不匹配，不是观测证据）；
    跨批合并保持求值器保守语义：FAIL > UNKNOWN > PASS > NOT_APPLICABLE，unknown 绝不洗成 pass。
    """
    if rules_pack.get("schema") != "business-rules/1" or not isinstance(rules_pack.get("rules"), list):
        raise ValueError("expected business-rules/1 with a rules array")
    by_scope: dict[str, list[dict[str, Any]]] = {}
    run_meta = []
    for run in runs:
        pack, assessment = run["pack"], run["assessment"]
        scope = pack.get("scope")
        if not _nonempty_str(scope):
            raise ValueError("观测包缺 scope")
        if assessment.get("schema") != "business-rule-assessment/1":
            raise ValueError("评估报告 schema 不符")
        by_scope.setdefault(scope, []).append(run)
        observed_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for item in assessment.get("results", []):
            observed_counts[item["observed_status"]] = observed_counts.get(item["observed_status"], 0) + 1
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        run_meta.append({"pack_file": run.get("pack_file", ""), "scope": scope,
                         "observations": len(pack.get("observations", [])),
                         "observed_status_counts": observed_counts, "status_counts": status_counts})
    obs_by_id = {}
    for run in runs:
        for obs in run["pack"].get("observations", []):
            obs_by_id[obs["id"]] = obs
    merged_rules = []
    merged_observed: dict[str, int] = {}
    merged_status: dict[str, int] = {}
    for rule in rules_pack["rules"]:
        applicable = by_scope.get(rule.get("scope"), [])
        per_run = []
        fail_chains = []
        for run in applicable:
            item = next((r for r in run["assessment"]["results"] if r["id"] == rule["id"]), None)
            if item is None:
                raise ValueError(f"评估报告缺规则 {rule['id']}")
            per_run.append({"pack_file": run.get("pack_file", ""),
                            "observed_status": item["observed_status"], "status": item["status"]})
            if item["observed_status"] == "FAIL":
                for detail in item.get("observations", []):
                    if detail.get("status") != "FAIL":
                        continue
                    obs = obs_by_id.get(detail.get("observation_id"), {})
                    fail_chains.append({
                        "observation_id": detail.get("observation_id"),
                        "row": obs.get("row"), "evidence_refs": detail.get("evidence_refs", []),
                        "rule_assert": rule.get("assert"),
                        "rule_source": rule.get("source"), "claim": rule.get("claim", "")})
        if applicable:
            observed = _merge_status([p["observed_status"] for p in per_run])
            status = _merge_status([p["status"] for p in per_run])
        else:
            observed, status = "UNKNOWN", "UNKNOWN"
        merged_observed[observed] = merged_observed.get(observed, 0) + 1
        merged_status[status] = merged_status.get(status, 0) + 1
        merged_rules.append({"id": rule["id"], "scope": rule.get("scope"),
                             "observed_status": observed, "status": status,
                             "no_observations_for_scope": not applicable,
                             "per_run": per_run, "fail_evidence": fail_chains})
    return {
        "schema": "observation-assessment-summary/1", "target": rules_pack.get("target"),
        "runs": run_meta,
        "merged": {"rules_total": len(merged_rules),
                   "observed_status_counts": merged_observed, "status_counts": merged_status,
                   "rules_without_observations": sum(1 for r in merged_rules if r["no_observations_for_scope"])},
        "rules": merged_rules,
        "limitations": [
            "合并只消费 scope 一致的批；跨批 FAIL > UNKNOWN > PASS > NOT_APPLICABLE，unknown 不洗成 pass",
            "status 受求值器来源确认闸门约束：confirmed 的 business(user/document) 与 technical(ddl/annotation/openapi) 放行，未确认强制 UNKNOWN；FAIL 须连同 confirmation_tier 读",
            "observed_status 是对所给观测的判定，不是门禁；未观测 scope 的规则保持 UNKNOWN",
        ],
    }


def _run_transcribe(args: argparse.Namespace) -> int:
    config = json.loads(args.bindings.read_text(encoding="utf-8"))
    pack_list, report = transcribe(args.artifacts_dir, config)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.rules is not None:
        rules_pack = json.loads(args.rules.read_text(encoding="utf-8"))
        if rules_pack.get("schema") != "business-rules/1" or not isinstance(rules_pack.get("rules"), list):
            raise ValueError("expected business-rules/1 with a rules array")
        report["inputs"]["rules"] = {"path": str(args.rules), "sha256": _sha256(args.rules)}
        scopes_observed = {p["pack"]["scope"] for p in pack_list}
        by_scope: dict[str, list[str]] = {}
        for rule in rules_pack.get("rules", []):
            by_scope.setdefault(rule.get("scope"), []).append(rule["id"])
        report["rule_coverage"] = {
            "rules_total": len(rules_pack.get("rules", [])),
            "scopes_with_observations": sorted(scopes_observed),
            "scopes_without_observations": {s: ids for s, ids in by_scope.items() if s not in scopes_observed},
        }
    report["inputs"]["bindings"] = {"path": str(args.bindings), "sha256": _sha256(args.bindings)}
    report["repro_cmd"] = list(sys.argv)
    for item in pack_list:
        (args.out_dir / item["file"]).write_text(
            json.dumps(item["pack"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "transcription_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    t = report["totals"]
    print(f"转录 {t['observations']} 条观测 → {t['packs']} 个包；未到达 {t['not_reached']} 条；缺口 {t['gaps']} 条")
    for batch in report["batches"]:
        scopes = ",".join(p["scope"] for p in batch["packs"])
        print(f"  {batch['batch']} scopes=[{scopes}] emitted={batch['emitted']} "
              f"attempted={batch['attempted']} filtered_out={batch['filtered_out']} "
              f"not_reached={len(batch['not_reached'])}")
    return 0


def _run_summarize(args: argparse.Namespace) -> int:
    rules_path = args.rules
    rules_pack = json.loads(rules_path.read_text(encoding="utf-8"))
    runs = []
    for pack_path in sorted(args.out_dir.glob("observations_*.json")):
        stem = pack_path.name[len("observations_"):-len(".json")]
        assessment_path = args.out_dir / f"assessment_{stem}.json"
        if not assessment_path.is_file():
            raise ValueError(f"缺评估报告: {assessment_path} —— 先逐包跑 rule_assessment，不出半套汇总")
        runs.append({"pack_file": pack_path.name, "assessment_file": assessment_path.name,
                     "pack": json.loads(pack_path.read_text(encoding="utf-8")),
                     "assessment": json.loads(assessment_path.read_text(encoding="utf-8"))})
    if not runs:
        raise ValueError(f"{args.out_dir} 下没有观测包 —— 无汇总对象")
    summary = summarize(rules_pack, runs)
    summary["inputs"] = {"rules": {"path": str(rules_path), "sha256": _sha256(rules_path)},
                         "runs": [{"pack_file": r["pack_file"],
                                   "pack_sha256": _sha256(args.out_dir / r["pack_file"]),
                                   "assessment_sha256": _sha256(args.out_dir / r["assessment_file"])}
                                  for r in runs]}
    summary["repro_cmd"] = list(sys.argv)
    out = args.out or (args.out_dir / "summary.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = summary["merged"]
    print(f"合并 {m['rules_total']} 条规则：observed_status={m['observed_status_counts']}")
    print(f"最终 status（来源确认闸门后）={m['status_counts']}；无观测 scope 规则 {m['rules_without_observations']} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把已有观测产物纯转录成求值器观测包（不执行被测系统）")
    parser.add_argument("--bindings", type=Path, help="项目侧绑定配置（含绑定证据）")
    parser.add_argument("--artifacts-dir", type=Path, help="观测产物目录")
    parser.add_argument("--out-dir", type=Path, help="输出目录（观测包 / 转录报告 / 汇总）")
    parser.add_argument("--rules", type=Path, help="判据包（转录模式做覆盖分析；汇总模式必填）")
    parser.add_argument("--summarize", action="store_true", help="汇总模式：合并逐包评估报告")
    parser.add_argument("--out", type=Path, help="汇总输出路径（默认 out-dir/summary.json）")
    args = parser.parse_args()
    if args.summarize:
        if args.rules is None or args.out_dir is None:
            parser.error("汇总模式需要 --rules 与 --out-dir")
        return _run_summarize(args)
    if args.bindings is None or args.artifacts_dir is None or args.out_dir is None:
        parser.error("转录模式需要 --bindings --artifacts-dir --out-dir")
    return _run_transcribe(args)


if __name__ == "__main__":
    raise SystemExit(main())