"""函数层 · 差分判定（通用引擎，零项目字面量）。

把「实测行为」和「业务契约」对齐，分组成缺陷单。

**通用的是判定算法**：分组 → 有 FAIL 用例就开单 → 拼证据 → 查高层有没有开过
→ 排序 → 出 JSON + Markdown。

**项目的是缺陷语义**：哪个分组算缺陷、严重度多高、根因在哪个函数、边界挡没挡住、
关联到高层哪条单 —— 全在项目侧的 `fn_groups.json`（分组 id 与用例文件里的
`group` 对应）。用例文件回答「怎么测」，分组文件回答「测出来算不算缺陷」。

归属规则（全局一致）：owner = 能给出可复现证据的最低层。
本层出的单必须回答三件事：
  1. 根因在哪个函数（精确到方法）
  2. 这个缺陷从 HTTP 入口是否可达（可达 = 真缺陷；不可达 = 防御纵深缺失）
  3. 高层是否已经开过单（开过就只补根因证据，不重复计数）

输入（都在项目的产物目录里）：
    fn_observation.json     函数层动态实测（用例 + 判定）
    fn_declaration.json     函数层静态声明（字节码）
    findings.json / class_findings.json / integration_findings.json  高层结论（查重）

输入（项目配置目录里）：
    fn_groups.json          分组语义 + 亮点分组 + 假设裁决
    semantic_map.json       语义地图的职责断言（可选；失败即开单）

输出：fn_findings.json / fn_findings.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import findings_schema
import oracle_gate


def project_dir() -> Path:
    return findings_schema.target_path().parent


def artifacts_dir() -> Path:
    target = findings_schema.load_target()
    raw = (target.get("paths") or {}).get("artifacts_dir")
    return Path(raw) if raw else project_dir()


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _load_findings(path: Path) -> list[dict]:
    data = _load_json(path, [])
    if isinstance(data, dict):
        return data.get("findings", [])
    return data


def build_findings(cases: list[dict], groups: dict[str, dict],
                    higher: dict[str, list[dict]],
                    held: list[dict] | None = None) -> list[dict[str, Any]]:
    """分组 → 有 FAIL 用例就开一条单。判定口径是通用的；分组语义来自配置。

    分组声明了 `oracle` 时必须过来源闸门（硬约束 5）：FAIL 用例只证明
    「实测与用例断言不符」，而用例断言本身可能是没有来源的业务预期。
    被压下来的结论进 `held`，由 main() 写进疑点清单，不悄悄消失。
    """
    by_group: dict[str, list[dict]] = {}
    for c in cases:
        by_group.setdefault(c["group"], []).append(c)

    findings: list[dict[str, Any]] = []
    for group, meta in groups.items():
        failed = [c for c in by_group.get(group, []) if c["verdict"] == "FAIL"]
        if not failed:
            continue
        evidence = []
        for c in failed:
            obs = c["observation"]
            snippet = c["reason"]
            if obs.get("upload_dir_state"):
                f = obs["upload_dir_state"][0]
                snippet += f" | 落盘 {f['name']} size={f['size']} head={f.get('magic_ascii')}"
            if obs.get("duplicates") is not None:
                snippet += (f" | generated={obs['generated']} distinct={obs['distinct']}"
                            f" elapsed={obs['elapsed_ms']}ms")
            evidence.append({
                "case_id": c["id"], "function": c["target"],
                "invariant": c["invariant"], "actual": snippet,
                "exception": obs.get("exception_class"),
            })
        related = []
        for layer, ident, relation in meta.get("related", []):
            if layer == "service" and ident.startswith("test_"):
                related.append({"layer": "service", "id": ident, "relation": relation})
                continue
            hit = any(f.get("id") == ident for f in higher.get(layer, []))
            related.append({"layer": layer, "id": ident, "relation": relation,
                            "already_filed_in_that_layer": hit})
        filed = {
            "id": meta["id"], "layer": "function", "severity": meta["severity"],
            "title": meta["title"], "function": meta["function"],
            "root_cause": meta["root_cause"],
            "api_reachable": meta["api_reachable"],
            "boundary_protected": meta["boundary_protected"],
            "boundary_note": meta["boundary_note"],
            "cwe": meta["cwe"],
            "owner": f"函数层（{meta['function']}）",
            "case_count": len(failed),
            "evidence": evidence,
            "related_findings": related,
        }
        kept, gated = oracle_gate.split(meta.get("oracle"), [filed],
                                        rule_id=meta["id"])
        findings.extend(kept)
        if held is not None:
            held.extend(gated)
    return findings


def semantic_findings(cases: list[dict], sem_map: dict[str, Any],
                      held: list[dict] | None = None) -> list[dict[str, Any]]:
    """语义地图的职责断言：失败即开单（开单元数据由项目的语义地图声明）。

    职责断言声明的是「这个细胞应该做什么」，属业务预期 —— 声明了 `oracle`
    就必须过来源闸门。`source=declared` 只说明它是人写的，不等于有文档依据。
    """
    by_case = {m["id"]: m for m in sem_map.get("missions", [])}
    out: list[dict[str, Any]] = []
    for c in cases:
        if c["group"] != "SEMANTIC_OBLIGATION" or c["verdict"] != "FAIL":
            continue
        mission = by_case.get(c["id"], {})
        meta = mission.get("finding")
        if not meta:
            continue
        obs = c.get("observation") or {}
        filed = {
            "id": meta["id"], "layer": "function", "severity": meta["severity"],
            "title": meta["title"], "function": c["target"],
            "root_cause": meta.get("root_cause", ""),
            "api_reachable": meta.get("api_reachable", True),
            "boundary_protected": meta.get("boundary_protected", False),
            "boundary_note": meta.get("boundary_note", ""),
            "cwe": meta.get("cwe"),
            "owner": f"函数层（{mission.get('unit', c['target'])}）",
            "case_count": 1,
            "evidence": [{
                "case_id": c["id"], "function": c["target"],
                "invariant": c["invariant"], "actual": c.get("reason", ""),
                "exception": obs.get("exception_class"),
            }],
            "related_findings": [],
            "semantic_obligation": {
                "mission": mission.get("mission"),
                "template": mission.get("template"),
                "source": mission.get("source", "declared"),
            },
        }
        kept, gated = oracle_gate.split(mission.get("oracle"), [filed],
                                        rule_id=mission.get("id", meta["id"]))
        out.extend(kept)
        if held is not None:
            held.extend(gated)
    return out


def main() -> None:
    proj = project_dir()
    art = artifacts_dir()

    observation = _load_json(art / "fn_observation.json", {})
    declaration = _load_json(art / "fn_declaration.json", {})
    cfg = _load_json(proj / "fn_groups.json", {})
    groups = cfg.get("groups") or {}
    cases = observation.get("cases", [])

    higher = {
        "service": _load_findings(art / "findings.json"),
        "class": _load_findings(art / "class_findings.json"),
        "integration": _load_findings(art / "integration_findings.json"),
    }

    held: list[dict[str, Any]] = []
    findings = build_findings(cases, groups, higher, held)

    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 9), f["id"]))

    # 语义地图的职责断言：失败即开单。
    # 注意这里的顺序：**排在排序之后**（与改造前逐字一致）。
    # 这看着像个瑕疵（职责断言不参与严重度排序），但改它会动到既有产物的顺序 ——
    # 「通用化不改变任何结论」这条判据包括呈现顺序，所以照搬不改。
    sem_map = _load_json(proj / "semantic_map.json", {})
    findings.extend(semantic_findings(cases, sem_map, held))

    passed = [c for c in cases if c["verdict"] == "PASS"]
    highlights = set(cfg.get("highlight_groups") or [])
    passed_highlights = [
        {"case_id": c["id"], "function": c["target"],
         "invariant": c["invariant"], "actual": c["reason"]}
        for c in passed if c["group"] in highlights
    ]

    payload = {
        "meta": {
            "layer": "function",
            "carrier": "JVM 内反射直调（静态字节码 + 动态属性/边界用例）",
            "functions_declared": declaration.get("meta", {}).get("functions"),
            "cases": len(cases),
            "failed_cases": len(cases) - len(passed),
            "passed_cases": len(passed),
            "total": len(findings),
            "by_severity": {
                s: sum(1 for f in findings if f["severity"] == s)
                for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            },
            "source_vs_deployed_drift": declaration.get("meta", {}).get("source_vs_deployed_drift"),
            "expectation_held": len(held),
        },
        "findings": findings,
        "passed_checks": passed_highlights,
        "hypotheses_verdict": cfg.get("hypotheses", []),
    }
    out_json = art / "fn_findings.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 预期未确认 / 被文档否定的结论：不进缺陷台账，但必须留痕（硬约束 5）。
    # **无条件写**：一条都没压下来时也要落盘，否则「没有疑点」与「没查」不可区分。
    held_buckets = oracle_gate.buckets(held)
    (art / "fn_rule_suspects.json").write_text(json.dumps({
        "unconfirmed": held_buckets["unconfirmed"],
        "unconfirmed_count": len(held_buckets["unconfirmed"]),
        "refuted": held_buckets["refuted"],
        "refuted_count": len(held_buckets["refuted"]),
        "note": "用例 FAIL 只证明「实测与用例断言不符」。用例断言本身可能是没有"
                "来源的业务预期：未经确认的进待确认疑点（规范层风险），被文档明确"
                "否定的只保留观测。两者都不进缺陷台账，观测与证据原样留在这里。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 函数层缺陷报告（JVM 直调 · 字节码声明 vs 实测行为）", "",
          f"函数总数 {payload['meta']['functions_declared']}，用例 {payload['meta']['cases']} 条，"
          f"失败 {payload['meta']['failed_cases']} 条 / 通过 {payload['meta']['passed_cases']} 条，"
          f"归并为 {len(findings)} 条缺陷。", ""]
    for f in findings:
        md.append(f"## [{f['severity']}] {f['id']} — {f['title']}")
        md.append("")
        md.append(f"- 根因函数：`{f['function']}`")
        md.append(f"- 根因：{f['root_cause']}")
        md.append(f"- HTTP 可达：{'是' if f['api_reachable'] else '否'}"
                  f"（边界情况：{f['boundary_note']}）")
        if f.get("cwe"):
            md.append(f"- CWE: {f['cwe']}")
        md.append(f"- 证据（{f['case_count']} 条实测）：")
        for e in f["evidence"]:
            md.append(f"  - `{e['case_id']}` {e['function']} — 不变量「{e['invariant']}」→ {e['actual']}")
        if f["related_findings"]:
            md.append("- 跨层关联：")
            for r in f["related_findings"]:
                mark = "（高层已开单）" if r.get("already_filed_in_that_layer") else ""
                md.append(f"  - {r['layer']} 层 `{r['id']}`：{r['relation']}{mark}")
        md.append("")
    md.append("## 假设裁决")
    md.append("")
    for h in payload["hypotheses_verdict"]:
        md.append(f"- **{h['verdict']}**（{h['source']}）：{h['hypothesis']} → {h['evidence']}")
    md.append("")
    out_md = art / "fn_findings.md"
    out_md.write_text("\n".join(md), encoding="utf-8")

    print(f"函数层差异 -> {out_json} / {out_md}")
    print(f"  缺陷 {len(findings)} 条 " +
          " ".join(f"{k} {v}" for k, v in payload["meta"]["by_severity"].items() if v))
    print(f"  通过用例 {len(passed)} 条，断言亮点 {len(passed_highlights)} 条")
    print(f"  预期未确认 {len(held_buckets['unconfirmed'])} 条 / 被文档否定 "
          f"{len(held_buckets['refuted'])} 条（不进台账）-> {art / 'fn_rule_suspects.json'}")
    for f in findings:
        print(f"    {f['severity']:<8} {f['id']:<30} {f['function']}")


if __name__ == "__main__":
    main()
