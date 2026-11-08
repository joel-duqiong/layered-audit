"""类层 v2 三方差分：DB 实况 ↔ JPA 实体 ↔ DTO 声明 ↔ 实测行为。

四个差分方向：
  A 列级可空性三方一致（DB NOT NULL / 实体 nullable / DTO 约束）
  B 业务规则的三层分布（DB CHECK / DTO 约束 / 服务层实测放行）
  C 字段可达性与写入语义（DTO 可写 ↔ 实测被改列）
  D 契约边界的错误码与消息一致性

**通用骨架**在这里；项目特定的部分（DTO 到表的映射、哪些字段有专门的结论文案、
哪些实测标签对应哪条业务规则、哪些实测标签算「规则缺失」）在项目侧的
class_scenarios.json 的 contract_diff 段。

只输出差分结论，不做判定；判定权留给 class_diff.py 的排序与落盘。
"""
from __future__ import annotations

import json
import re
from typing import Any

from class_rules import render_json, render_text


def camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def load(path_declaration: str, path_contract: str, path_observation: str) -> dict:
    return {
        "decl": json.load(open(path_declaration)),
        "contract": json.load(open(path_contract)),
        "obs": json.load(open(path_observation)),
    }


def _dto_index(contract: dict) -> dict:
    """simple_name -> {字段名: {注解名: 注解详情}}"""
    out = {}
    for r in contract["reflection"]:
        if r["kind"] != "record":
            continue
        out[r["simple_name"]] = {c["name"]: c["annotations"] for c in r["components"]}
    return out


def _entity_columns(decl: dict) -> dict:
    """实体类 -> {字段名: {column, nullable}}"""
    out = {}
    for cls, e in decl["jpa"].items():
        out[cls] = {c["field"]: {"column": c["column"], "nullable": c["nullable"]}
                    for c in e.get("columns", [])}
    return out


def _parse_check(expr: str) -> dict:
    """从 CHECK 定义里抽出它管的是哪一列、阈值多少、还是枚举白名单。"""
    expr = expr.strip()
    m = re.match(r"CHECK \(\(([a-z_]+)\s*(>=|>)\s*(\d+)\)\)", expr)
    if m:
        return {"kind": "range", "column": m.group(1),
                "op": m.group(2), "value": int(m.group(3))}
    m = re.match(r"CHECK \(\(\(([a-z_]+)\)::text = ANY", expr)
    if m:
        vals = re.findall(r"'([A-Z_]+)'::character varying", expr)
        return {"kind": "enum", "column": m.group(1), "values": vals}
    return {"kind": "unknown"}


def _min_value(annotations: dict) -> int | None:
    for name in ("Min", "Positive", "PositiveOrZero"):
        if name in annotations:
            if name == "Min":
                return int(annotations["Min"].get("value", 0))
            return 1 if name == "Positive" else 0
    return None


def _finding(fid: str, sev: str, layer: str, title: str, detail: str,
             evidence: Any) -> dict:
    return {"id": fid, "severity": sev, "layer": layer, "title": title,
            "detail": detail, "evidence": evidence}


# ---------------------------------------------------------------- 差分 A
def diff_nullability(ctx: dict, cfg: dict) -> list[dict]:
    """DB NOT NULL ↔ 实体 nullable ↔ DTO 约束：谁该为"非空"负责，声明和实际是否一致。"""
    findings: list[dict] = []
    contract, decl, obs = ctx["contract"], ctx["decl"], ctx["obs"]
    dtos = _dto_index(contract)
    entities = _entity_columns(decl)
    db_tables = contract["db_actual"]["tables"]
    dto_target: dict = cfg.get("dto_target", {})
    server_derived = set(cfg.get("server_derived", []))
    notes = list(cfg.get("field_notes", []))

    probe_by_label: dict[str, dict] = {}
    for group in ("contract_boundary", "nullability"):
        for c in obs.get(group, []):
            probe_by_label[c["label"]] = c

    # 同一列在 Create/Update 两个 DTO 里都会出现，按 (表, 列) 去重后再比
    per_col: dict[tuple[str, str], dict] = {}
    for dto_name, (table, entity) in dto_target.items():
        fields = dtos.get(dto_name)
        if not fields:
            continue
        db_cols = db_tables.get(table, {}).get("columns", {})
        ent_cols = entities.get(entity, {})
        for field, anns in fields.items():
            if field in server_derived:
                continue
            col = camel_to_snake(field)
            if col not in db_cols:
                continue
            db_not_null = not db_cols[col]["nullable"]
            dto_required = ("NotNull" in anns) or ("NotBlank" in anns)
            ent = ent_cols.get(field)
            key = (table, col)
            rec = per_col.setdefault(key, {"dto": dto_name, "field": field, "column": col,
                                           "table": table, "entity": entity,
                                           "entity_nullable": ent["nullable"] if ent else None,
                                           "db_default": db_cols[col]["default"],
                                           "db_not_null": db_not_null,
                                           "dto_required": dto_required})
            rec["dto_required"] = rec["dto_required"] or dto_required

    cases = []
    both = stricter = loose = 0
    for rec in per_col.values():
        if rec["db_not_null"] and not rec["dto_required"]:
            cases.append(rec)
        elif rec["db_not_null"] and rec["dto_required"]:
            both += 1
        elif rec["dto_required"]:
            stricter += 1
        else:
            loose += 1
    aligned = both + stricter + loose

    for c in cases:
        note = next((n for n in notes
                     if n.get("match_field") == c["field"]
                     and n.get("match_table", c["table"]) == c["table"]), None)
        if note:
            probe = probe_by_label.get(
                render_text(note["probe_label"], c), {})
            row = probe.get("db_row") or {}
            ev_col = note.get("evidence_column", c["column"])
            raw = row.get(ev_col)
            values = dict(c)
            values.update({"http": probe.get("http"), "code": probe.get("code"),
                           "message": probe.get("message"),
                           "details": probe.get("details"),
                           "db_value": raw, "db_value_repr": repr(raw)})
            findings.append(_finding(
                note["id"], note["severity"], note.get("layer", "class"),
                render_text(note["title"], values),
                render_text(note["detail"], values),
                render_json(note["evidence"], values)))
        else:
            findings.append(_finding(
                "CLASS-NULLABILITY-THREE-WAY-MISMATCH", "MEDIUM", "class",
                f"{c['table']}.{c['column']} 三方可空性声明不一致",
                "DB 声明 NOT NULL，实体声明 nullable="
                f"{c['entity_nullable']}，DTO 没有任何非空约束。"
                "空值由哪一层兜底没有声明，缺失时行为不可预期。",
                c))

    if aligned:
        findings.append(_finding(
            "CLASS-NULLABILITY-ALIGNED", "INFO", "class",
            f"可空性三方一致 {aligned}/{len(per_col)} 列"
            f"（同向设防 {both} · DTO 更严 {stricter} · 都可空 {loose}）",
            "这是差分引擎的通过结论：DTO 的非空声明要么与 DB 一致，"
            "要么比 DB 更严（DTO 先拦，留给 DB 兜底），方向正确。"
            "不成立的只有下面单列出来的那几列。",
            {"aligned_columns": aligned, "total_columns": len(per_col),
             "same_direction": both, "dto_stricter": stricter,
             "both_nullable": loose}))
    return findings


# ---------------------------------------------------------------- 差分 B
def diff_rules(ctx: dict, cfg: dict) -> list[dict]:
    """业务规则在 DB CHECK / DTO 约束 / 服务层 三层的分布。"""
    findings: list[dict] = []
    contract, obs = ctx["contract"], ctx["obs"]
    dtos = _dto_index(contract)
    db_tables = contract["db_actual"]["tables"]
    probes = obs["defense_audit"]["constraint_probes"]
    dto_target: dict = cfg.get("dto_target", {})

    aligned_checks: list[dict] = []
    unaligned_checks: list[dict] = []
    for p in probes:
        if not p.get("constraint"):
            continue
        table = p["table"]
        spec = db_tables.get(table, {}).get("constraints", {}).get(p["constraint"])
        if not spec:
            continue
        parsed = _parse_check(spec.get("def", ""))
        if parsed["kind"] == "unknown":
            continue
        col = parsed["column"]
        field, dto_name = None, None
        for cand, (t, _e) in dto_target.items():
            if t != table:
                continue
            for f in dtos.get(cand, {}):
                if camel_to_snake(f) == col:
                    field, dto_name = f, cand
                    break
            if field:
                break
        hit = None
        if field and dto_name:
            anns = dtos[dto_name][field]
            if parsed["kind"] == "range":
                mv = _min_value(anns)
                expect = parsed["value"] + (1 if parsed["op"] == ">" else 0)
                if mv is not None and mv == expect:
                    hit = f"{field} @Min({mv})"
            elif parsed["kind"] == "enum":
                if "NotNull" in anns:
                    hit = f"{field} @NotNull + 枚举 {parsed['values']}"
        if hit:
            aligned_checks.append({"constraint": p["constraint"], "column": col,
                                   "dto": f"{dto_name}.{field}", "match": hit})
        else:
            unaligned_checks.append({"constraint": p["constraint"], "column": col,
                                     "check": spec.get("def"), "dto_field": field})

    if aligned_checks:
        findings.append(_finding(
            "CLASS-CHECK-DTO-ALIGNED", "INFO", "class",
            f"{len(aligned_checks)}/{len(aligned_checks) + len(unaligned_checks)} "
            "条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合",
            "DB 的 CHECK 不是孤岛：DTO 层有等价声明，两层同时设防。"
            "这类对齐决定了改一条业务规则要不要同时动三个文件。",
            {"aligned": aligned_checks}))
    if unaligned_checks:
        findings.append(_finding(
            "CLASS-CHECK-WITHOUT-DTO-COUNTERPART", "MEDIUM", "class",
            f"{len(unaligned_checks)} 条 DB CHECK 在 DTO 层找不到同义约束",
            "规则只写在迁移文件里，Java 侧无法复用，也无法在报错前拦截。"
            "改规则要改迁移文件，业务代码读不到这条规则。",
            {"unaligned": unaligned_checks}))

    rule_notes: dict = cfg.get("rule_notes", {})
    rule_excludes = set(cfg.get("rule_excludes", []))
    missing_cfg = cfg.get("rule_missing", {})
    missing = []
    for p in probes:
        if p.get("constraint") or p.get("blocked"):
            continue
        label = p.get("label", "")
        if label in rule_excludes:
            continue
        note = rule_notes.get(label, {})
        missing.append({"rule": label, "table": p["table"], "db_blocked": False,
                        "api_http": note.get("http"), "api_note": note.get("note")})
    if missing:
        findings.append(_finding(
            missing_cfg.get("id", "CLASS-RULE-MISSING-IN-ALL-LAYERS"),
            missing_cfg.get("severity", "HIGH"),
            missing_cfg.get("layer", "class"),
            render_text(missing_cfg.get(
                "title", "{{n}} 条业务不变量在 DB / DTO / 服务层三层全都不存在"),
                {"n": len(missing)}),
            render_text(missing_cfg.get("detail", ""), {"n": len(missing)}),
            {"rules": missing}))
    return findings


# ---------------------------------------------------------------- 差分 C
def diff_write_footprint(ctx: dict, cfg: dict) -> list[dict]:
    """DTO 声明可写的字段 ↔ 实测真的被改的列。"""
    findings: list[dict] = []
    obs = ctx["obs"]
    wf = cfg.get("write_footprint", {})
    put_prefix = wf.get("put_action_prefix", "")
    missing_fk = wf.get("missing_fk_action_contains", "")
    wipes_cfg = wf.get("put_wipes")
    guarded_cfg = wf.get("put_guarded")
    for w in obs.get("write_footprint", []):
        steps = {s["action"]: s for s in w["steps"]}
        if wipes_cfg:
            put = next((s for a, s in steps.items()
                        if put_prefix and a.startswith(put_prefix)), None)
            if put and wipes_cfg.get("column") in put["changed"]:
                values = {"entity": w["entity"], "id": w["id"],
                          "column": wipes_cfg["column"],
                          "changed_columns": put["changed"],
                          "final_row": w.get("final_row")}
                findings.append(_finding(
                    wipes_cfg["id"], wipes_cfg["severity"],
                    wipes_cfg.get("layer", "class"),
                    render_text(wipes_cfg["title"], values),
                    render_text(wipes_cfg["detail"], values),
                    render_json(wipes_cfg["evidence"], values)))
        if guarded_cfg:
            miss = next((s for a, s in steps.items() if missing_fk in a), None)
            if miss and miss.get("http") == guarded_cfg.get("http") and not miss["changed"]:
                values = {"entity": w["entity"], "http": miss.get("http"),
                          "changed": miss["changed"]}
                findings.append(_finding(
                    guarded_cfg["id"], guarded_cfg["severity"],
                    guarded_cfg.get("layer", "class"),
                    render_text(guarded_cfg["title"], values),
                    render_text(guarded_cfg["detail"], values),
                    render_json(guarded_cfg["evidence"], values)))
    return findings


# ---------------------------------------------------------------- 差分 D
def diff_error_contract(ctx: dict, cfg: dict) -> list[dict]:
    """错误码 / 状态码 / 文案 与真实原因是否一致。"""
    findings: list[dict] = []
    obs = ctx["obs"]
    for rule in cfg.get("error_contract", []):
        prefix = rule.get("label_prefix", "")
        want_http = rule.get("http")
        for c in obs.get("contract_boundary", []):
            if not c["label"].startswith(prefix):
                continue
            if want_http is not None and c["http"] != want_http:
                continue
            values = {"http": c["http"], "code": c.get("code"),
                      "message": c.get("message"), "label": c["label"]}
            findings.append(_finding(
                rule["id"], rule["severity"], rule.get("layer", "class"),
                render_text(rule["title"], values),
                render_text(rule["detail"], values),
                render_json(rule["evidence"], values)))
    return findings


def run(decl_path: str, contract_path: str, obs_path: str,
        cfg: dict | None = None) -> list[dict]:
    cfg = cfg or {}
    ctx = load(decl_path, contract_path, obs_path)
    out: list[dict] = []
    out += diff_nullability(ctx, cfg)
    out += diff_rules(ctx, cfg)
    out += diff_write_footprint(ctx, cfg)
    out += diff_error_contract(ctx, cfg)
    return out
