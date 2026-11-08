"""类层差分：JPA 实体声明 ↔ Flyway DDL 声明 ↔ DB 实测行为 ↔ DTO 契约。

通用骨架：
  - D1 JPA ↔ DDL 列级差分（语言级，不需要项目配置）
  - D2 项目声明的差分规则（class_scenarios.json 的 diff_rules 段，逐条派发到 class_rules.RULES）
  - D3 三方差分（class_scenarios.json 的 contract_diff 段）

项目特定的一切（文案、涉及哪张表、哪个枚举文件、哪个索引名）都在项目侧的
class_scenarios.json 里 —— 这个文件里没有项目字面量。
"""
from __future__ import annotations

import json
import os

import class_contract_diff
import project
import file_carrier
from class_rules import RULES

D = str(project.artifact_dir())


def _load_scenarios() -> dict:
    rel = project.path("class_scenarios", "class_scenarios.json")
    return json.loads((project.project_dir() / rel).read_text(encoding="utf-8"))


def _finding(fid: str, sev: str, layer: str, title: str, detail: str,
             evidence) -> dict:
    return {"id": fid, "severity": sev, "layer": layer, "title": title,
            "detail": detail, "evidence": evidence}


def main() -> None:
    cfg = _load_scenarios()
    decl = json.load(open(os.path.join(D, "class_declaration.json")))
    obs = json.load(open(os.path.join(D, "class_observation.json")))
    findings: list[dict] = []

    ddl = decl["ddl"]["tables"]
    jpa = decl["jpa"]

    # D1 JPA 与 DDL 的列级差分（语言级规则，不依赖项目配置）
    for cls, e in jpa.items():
        t = e["table"]
        if not t or t not in ddl:
            continue
        cols = ddl[t]["columns"]
        for c in e["columns"]:
            col = c["column"]
            if col not in cols:
                findings.append(_finding(
                    "JPA_COLUMN_MISSING_IN_DDL", "HIGH", "class",
                    f"{cls}.{c['field']} 映射的列 {col} 在 DDL 里不存在",
                    "JPA 声明与 Flyway 迁移不一致：要么实体是新的而迁移没写，要么列名写错。",
                    {"entity": cls, "field": c["field"], "column": col, "table": t}))
                continue
            d = cols[col]
            if c["nullable"] is False and d["not_null"] is False:
                findings.append(_finding(
                    "NULLABILITY_DRIFT", "HIGH", "class",
                    f"{cls}.{c['field']} Java 声明 NOT NULL，DDL 允许空",
                    "Java 侧假定该列恒非空，DB 侧却允许 NULL —— 一旦有别的写入方"
                    "（脚本/运维/数据修复）写入 NULL，Java 代码会在没有校验的地方直接 NPE。",
                    {"table": t, "column": col, "jpa_nullable": False,
                     "ddl_not_null": False}))
            if c["unique"] is True and d["unique"] is False:
                uk = [k for k, v in ddl[t]["constraints"].items() if v["kind"] == "UNIQUE"]
                if not any(col in v["expr"] for v in ddl[t]["constraints"].values()
                           if v["kind"] == "UNIQUE"):
                    findings.append(_finding(
                        "UNIQUE_DRIFT", "HIGH", "class",
                        f"{cls}.{c['field']} Java 声明 unique=true，DDL 没有唯一约束",
                        "应用层以为有唯一性保护，实际 DB 层没有 —— 并发下会写入重复值。",
                        {"table": t, "column": col}))

    # D2 项目声明的差分规则：配置里的顺序就是报告里的顺序
    ctx = {"decl": decl, "obs": obs, "entity_dir": str(project.entity_dir()),
           "file_detectors": file_carrier.load(D)}
    for rule in cfg.get("diff_rules", []):
        handler = RULES.get(rule.get("id"))
        if handler is None:
            raise KeyError(f"class_scenarios.json 里引用了未知的差分规则: {rule.get('id')}")
        findings.extend(handler(rule, ctx))

    # D3 三方差分（DB 实况 ↔ 实体 ↔ DTO ↔ 实测）
    contract_path = os.path.join(D, "class_contract.json")
    if os.path.exists(contract_path):
        for f in class_contract_diff.run(os.path.join(D, "class_declaration.json"),
                                         contract_path,
                                         os.path.join(D, "class_observation.json"),
                                         cfg.get("contract_diff", {})):
            findings.append({"id": f["id"], "severity": f["severity"],
                             "layer": f["layer"], "title": f["title"],
                             "detail": f["detail"], "evidence": f["evidence"]})

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    # 覆盖缺口：文件载体看到、但没有任何结论认领的观测 —— 不让它悄悄消失
    unowned, skipped = file_carrier.unowned(
        ctx.get("file_detectors") or {},
        ctx.get("file_carrier_consumed") or set(),
        cfg.get("file_carrier_exclusions") or [])
    gap_path = os.path.join(D, "class_carrier_gap.json")
    with open(gap_path, "w", encoding="utf-8") as fh:
        json.dump({"unowned": unowned, "count": len(unowned),
                   "declared_out_of_scope": skipped,
                   "declared_out_of_scope_count": len(skipped)}, fh,
                  ensure_ascii=False, indent=2)

    findings.sort(key=lambda f: sev_order.get(f["severity"], 9))
    with open(os.path.join(D, "class_findings.json"), "w") as fh:
        json.dump(findings, fh, ensure_ascii=False, indent=2)

    target = project.target_name() or "target"
    lines = [f"# 类层差分报告（{target}）", "",
             f"JPA 实体 {len(jpa)} 个 | DDL 表 {len(ddl)} 张 | "
             f"索引 {len(decl['ddl']['indexes'])} 个 | finding {len(findings)} 条",
             "", "| 级别 | 层 | 缺陷 |", "|---|---|---|"]
    for f in findings:
        lines.append(f"| {f['severity']} | {f['layer']} | {f['title']} |")
    lines.append("")
    for f in findings:
        lines += [f"## [{f['severity']}] {f['title']}", "", f["detail"], "",
                  "```json", json.dumps(f["evidence"], ensure_ascii=False, indent=2),
                  "```", ""]
    # 第二载体复核：把「谁复现了这条结论」与「什么没人认领」写进报告，
    # 不让它们只躺在 JSON 里 —— 报告是给人看的，缺口也是给人看的。
    if ctx.get("file_detectors"):
        total_obs = sum(len(v) for v in ctx["file_detectors"].values())
        corroborated = sum(1 for f in findings
                           if isinstance(f.get("evidence"), dict) and "file_carrier" in f["evidence"])
        lines += ["## 第二载体复核（文件载体 × 类层载体）", "",
                  f"文件载体观测 {total_obs} 条｜被类层结论独立复现 {corroborated} 条"
                  f"｜无人认领 {len(unowned)} 条｜项目声明不适用 {len(skipped)} 条", ""]
        if unowned:
            lines += ["### 无人认领的观测（覆盖缺口）", ""]
            for u in unowned:
                o = u["observation"]
                lines.append(f"- `{u['detector']}` → `{o.get('class')}.{o.get('method', '')}`")
            lines += [""]
        if skipped:
            lines += ["### 项目声明不适用的观测（决策留痕）", ""]
            for u in skipped:
                o = u["observation"]
                lines.append(f"- `{u['detector']}` → `{o.get('class')}.{o.get('method', '')}`"
                             f" —— {u['reason']}")
            lines += [""]

    with open(os.path.join(D, "class_findings.md"), "w") as fh:
        fh.write("\n".join(lines))

    print(f"类层差分：{len(findings)} 条")
    if ctx.get("file_detectors"):
        print(f"  第二载体复核：文件载体观测 {sum(len(v) for v in ctx['file_detectors'].values())} 条"
              f"｜已认领 {len(ctx.get('file_carrier_consumed') or set())} 条"
              f"｜无人开单 {len(unowned)} 条｜已声明不适用 {len(skipped)} 条 -> {gap_path}")
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        n = sum(1 for f in findings if f["severity"] == s)
        if n:
            print(f"  {s:8} {n}")
    print()
    for f in findings:
        print(f"[{f['severity']}] ({f['layer']}) {f['title']}")


if __name__ == "__main__":
    main()
