"""判据桥 —— 把采集器判据确定性转录到求值器封闭集。

封闭集 = invariants.py 与 rule_assessment.py 的交集（读源码核实，当前 13 个）：
  field_{eq,lte,gte}_field、field_{lte,gte,gt}_const、field_in_set、
  field_not_null、field_not_blank、field_size、field_pattern、
  field_default_when_absent、text_not_contains
仍然**没有** field_eq_const，也**没有**严格 < 原语。

**求值器消费的判据级字段必须逐字保留**（when / severity）。`when` 是适用域：
丢掉它会把「这行不归我管」判成「违反」—— 那不是漏判，是**制造假缺陷**。

桥同时接受**两套词汇**（上游采集器已改为直接发射求值器侧原语名）：
  · 采集器侧名字（field_cmp / in / range / not_null / not_blank / positive …）→ 走映射
  · 已是封闭原语的名字（field_gte_const / field_in_set …）→ 走**直通**，逐字不改语义
两套都不认的才报不可映射。把已经是封闭原语的判据当「未知原语」丢掉是回归（见 LESSONS 坑 11）。

映射只转录不推断：符号方向不许放宽（> 只能映射到 field_gt_const，不得变 >=），
带单位的约束不许当无量纲数值比较，封闭集没有的原语一律显式报不可映射并给出原因
—— 不静默丢弃、不猜语义。映射不了 / 缺字段 / 类型不兼容绝不变成 PASS。
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

CLOSED_PRIMITIVES = frozenset({
    "field_eq_field", "field_lte_field", "field_gte_field",
    "field_lte_const", "field_gte_const", "field_gt_const", "field_in_set",
    "field_not_null", "field_not_blank",
    "field_size", "field_pattern", "field_default_when_absent", "text_not_contains",
})

# 两个求值器真正读取的判据级字段（读源码枚举，不凭描述）：
#   invariants.evaluate/applies/evaluate_all: when, assert, id, claim, severity, category, why, oracle
#   rule_assessment.evaluate_observation/assess: when, assert, id, claim, scope, source
# 映射路径必须逐字保留这些，丢掉 when 会把「不适用」判成「违反」= 制造假缺陷。
_PRESERVED_FIELDS = ("when", "severity")

# 确认门控的两个 tier（与 rule_assessment 的常量同源，由契约守卫锁住）。
# 两者都是合法 confirmed，但 FAIL 的含义不同：业务 tier 是违反业务规则，
# 技术 tier 是违反作者写下的约束。报告必须分开计数，不得混成一个聚合值。
BUSINESS_TIERS = ("user", "document")
TECHNICAL_TIERS = ("ddl", "annotation", "openapi")


def _tier(kind: Any) -> str:
    if kind in BUSINESS_TIERS:
        return "business"
    if kind in TECHNICAL_TIERS:
        return "technical"
    return "unrecognized"


# 求值器真正读取的断言键（从两个求值器源码枚举）。**表外的键一律拒收**：
# 直通会把它们逐字带过去而求值器忽略 → 语义被静默放宽/反转
# （exclusive=true 让 >= 在边界判 pass；negate=true 让 in 反向）；
# 映射路径会把它们直接销毁且 reason="" 报成完全可映射。两条路都不许静默。
_CONSUMED_KEYS = {
    "field_eq_field": ("type", "left", "right"),
    "field_lte_field": ("type", "left", "right"),
    "field_gte_field": ("type", "left", "right"),
    "field_lte_const": ("type", "left", "value"),
    "field_gte_const": ("type", "left", "value"),
    "field_gt_const": ("type", "left", "value"),
    "field_in_set": ("type", "left", "value"),
    "field_not_null": ("type", "left"),
    "field_not_blank": ("type", "left"),
    "field_size": ("type", "left", "min", "max"),
    "field_pattern": ("type", "left", "value"),
    "field_default_when_absent": ("type", "left", "value"),
    "text_not_contains": ("type", "left", "value"),
}
_HARVESTER_KEYS = {
    "field_cmp": ("type", "field", "op", "value", "unit"),
    "in": ("type", "field", "value"),
    "range": ("type", "field", "minimum", "maximum"),
    "not_null": ("type", "field"),
    "not_blank": ("type", "field"),
    "positive": ("type", "field"),
    "positive_or_zero": ("type", "field"),
}
_EXTRA_KEY_REASON = ("{kind} 带求值器不消费的键 {keys}：直通会被求值器静默忽略"
                     "（语义被放宽或反转），映射会被静默销毁 —— 一律拒收，不猜语义")


def _extra_keys(assertion: dict[str, Any], allowed: tuple[str, ...]) -> list[str]:
    return sorted(k for k in assertion if k not in allowed)

CONFIRMED = "confirmed"
REFUTED = "refuted"
NEEDS_CONFIRMATION = "needs_confirmation"

_CMP_OPS = {">=": "field_gte_const", "<=": "field_lte_const", ">": "field_gt_const"}
_RANGE_BOUNDS = (("minimum", "field_gte_const"), ("maximum", "field_lte_const"))
_CONST_PRIMITIVES = ("field_lte_const", "field_gte_const", "field_gt_const")
_FIELD_PRIMITIVES = ("field_eq_field", "field_lte_field", "field_gte_field")
_FIELD_REQUIRED = ("field_cmp", "in", "range", "not_null", "not_blank",
                   "positive", "positive_or_zero")
_UNIT = "带单位 {unit!r}：封闭原语是无量纲数值比较，换算需先绑定观测单位，不猜"

_NO_COUNTERPART = {
    "not_empty": "封闭集无非空集合原语",
    "assert_true": "封闭集无布尔断言原语",
    "assert_false": "封闭集无布尔断言原语",
    "size": "封闭集已有 field_size（字符串数字符/数组数元素），采集器侧 size→field_size 转录尚未接线",
    "pattern": "封闭集已有 field_pattern（两侧均 re.fullmatch），采集器侧 pattern→field_pattern 转录尚未接线",
    "default": "封闭集已有 field_default_when_absent（字段存在→NOT_APPLICABLE，缺失→UNKNOWN），"
               "采集器侧 default 转录尚未接线",
    "unique": "唯一性跨行成立，两个求值器都逐行判定",
    "references": "引用完整性跨表，两个求值器都单表逐行",
    "validate_nested": "嵌套校验需展开子判据，封闭集无对应原语",
    "acceptance_scenario": "验收条款需物化成场景才能执行，不是字段断言",
    "confirmed_rule": "散文/结构规则，封闭集无对应原语，需物化层补原语或绑定",
    "needs_confirmation": "需产品确认，不得当 confirmed 判据进判定",
    "has_fields": "结构存在性断言（响应/快照含哪些字段），封闭集只比较字段值，无结构原语",
    "set_covers": "集合覆盖断言（观测集 ⊇ 要求集）；field_in_set 是单值 ∈ 集合，方向与元数都不同",
    "forbidden_terms": "封闭集已有 text_not_contains，但采集器侧 forbidden_terms 无 left"
                       "（全局文本扫描），绑定被判定字段前不转录",
}


def _finite(value: Any) -> bool:
    return not (isinstance(value, float) and not math.isfinite(value))


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float)) and _finite(value)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and _finite(value)


def map_assertion(assertion: Any) -> tuple[list[dict[str, Any]], str]:
    """采集器判据 → 封闭原语断言列表。返回 (断言列表, 不可映射原因)。

    两套词汇都认：采集器侧名字走映射，已是封闭原语的名字走直通（逐字不改）。
    列表非空即成功；复合 range 会产出多条。原因非空即不可映射。
    """
    if not isinstance(assertion, dict):
        return [], "assert 缺失或不是对象"
    kind = assertion.get("type")
    if not isinstance(kind, str) or not kind:
        return [], "assert.type 缺失"

    allowed = _CONSUMED_KEYS.get(kind) if kind in CLOSED_PRIMITIVES else _HARVESTER_KEYS.get(kind)
    if allowed is not None:
        extra = _extra_keys(assertion, allowed)
        if extra == ["unit"]:
            return [], _UNIT.format(unit=assertion["unit"])
        if extra:
            return [], _EXTRA_KEY_REASON.format(kind=kind, keys=extra)

    if kind in CLOSED_PRIMITIVES:
        left = assertion.get("left")
        if not isinstance(left, str) or not left.strip():
            return [], f"{kind} 缺 left —— 无法绑定被判定字段"
        if "unit" in assertion:
            return [], _UNIT.format(unit=assertion["unit"])
        if kind in _CONST_PRIMITIVES:
            if not _numeric(assertion.get("value")):
                return [], f"{kind} 常量 {assertion.get('value')!r} 类型不兼容：要求有限数值"
        elif kind == "field_in_set":
            values = assertion.get("value")
            if not isinstance(values, list) or not values:
                return [], "field_in_set 取值域为空或非列表"
            if not all(_scalar(v) for v in values):
                return [], "field_in_set 取值域含非标量元素，类型不兼容"
        elif kind in _FIELD_PRIMITIVES:
            right = assertion.get("right")
            if not isinstance(right, str) or not right.strip():
                return [], f"{kind} 缺 right —— 无法绑定比较字段"
        elif kind == "field_size":
            bounds = [assertion[k] for k in ("min", "max") if k in assertion]
            if not bounds:
                return [], "field_size 缺 min/max —— 无界断言不构成判据"
            if any(isinstance(b, bool) or not isinstance(b, int) or b < 0 for b in bounds):
                return [], f"field_size 边界 {bounds!r} 必须是非负整数"
        elif kind == "field_pattern":
            pattern = assertion.get("value")
            if not isinstance(pattern, str):
                return [], "field_pattern 的 value 必须是正则字符串"
            try:
                re.compile(pattern)
            except re.error as exc:
                return [], f"field_pattern 正则不合法：{exc}"
        elif kind == "text_not_contains":
            terms = assertion.get("value")
            if not isinstance(terms, list) or not terms or not all(isinstance(t, str) for t in terms):
                return [], "text_not_contains 的 value 必须是非空字符串列表"
        elif kind == "field_default_when_absent":
            if "value" not in assertion:
                return [], "field_default_when_absent 缺 value —— 默认值契约无默认值"
        return [dict(assertion)], ""

    if kind in _FIELD_REQUIRED:
        field = assertion.get("field")
        if not isinstance(field, str) or not field.strip():
            return [], f"{kind} 缺 field —— 无法绑定被判定字段"

    if kind in ("not_null", "not_blank"):
        return [{"type": f"field_{kind}", "left": field}], ""

    if kind in ("positive", "positive_or_zero"):
        primitive = "field_gt_const" if kind == "positive" else "field_gte_const"
        return [{"type": primitive, "left": field, "value": 0}], ""

    if kind == "field_cmp":
        op = assertion.get("op")
        if op not in _CMP_OPS:
            return [], f"field_cmp 算子 {op!r} 封闭集无对应原语（不得放宽符号方向）"
        if "unit" in assertion:
            return [], _UNIT.format(unit=assertion["unit"])
        value = assertion.get("value")
        if not _numeric(value):
            return [], f"field_cmp 常量 {value!r} 类型不兼容：{_CMP_OPS[op]} 要求数值"
        return [{"type": _CMP_OPS[op], "left": field, "value": value}], ""

    if kind == "in":
        values = assertion.get("value")
        if not isinstance(values, list) or not values:
            return [], "in 取值域为空或非列表"
        if not all(_scalar(v) for v in values):
            return [], "in 取值域含非标量元素，类型不兼容"
        return [{"type": "field_in_set", "left": field, "value": list(values)}], ""

    if kind == "range":
        specs: list[dict[str, Any]] = []
        for key, primitive in _RANGE_BOUNDS:
            if key not in assertion:
                continue
            if not _numeric(assertion[key]):
                return [], f"range.{key} 类型不兼容：{primitive} 要求数值"
            specs.append({"type": primitive, "left": field, "value": assertion[key]})
        if not specs:
            return [], "range 既无 minimum 也无 maximum"
        return specs, ""

    return [], _NO_COUNTERPART.get(kind, f"未知原语 {kind!r}，封闭集无对应")


def _scope(oracle: dict[str, Any]) -> str:
    target = oracle.get("target")
    target = target if isinstance(target, dict) else {}
    for key in ("table", "entity", "schema"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value
    source = oracle.get("source")
    ref = source.get("reference") if isinstance(source, dict) else None
    if isinstance(ref, str) and ref.strip():
        return ref
    return str(oracle.get("id") or "")


def bridge(oracles: list[dict[str, Any]], refuted: list[dict[str, Any]] | None = None,
           upstream_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    """把采集结果分成「可映射 / 不可映射 / 排除」三堆，每堆都带原因。

    两个分母显式报出：oracles 数组（counts.oracles_count）与含 refuted 独立桶的
    处理总数（counts.total == total_declarations）。三桶之和恒等于处理总数。
    """
    mapped: list[dict[str, Any]] = []
    unmappable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()

    for oracle in oracles:
        oid = oracle.get("id")
        if not isinstance(oid, str) or not oid:
            unmappable.append({"id": None, "primitive": None, "reason": "判据缺 id"})
            continue
        if oid in seen:
            unmappable.append({"id": oid, "primitive": None, "reason": "判据 id 重复"})
            continue
        seen.add(oid)
        confirmation = oracle.get("confirmation")
        if confirmation != CONFIRMED:
            excluded.append({"id": oid, "confirmation": confirmation,
                             "reason": f"confirmation={confirmation!r}，不得当 confirmed 进判定"})
            continue
        assertion = oracle.get("assert")
        specs, reason = map_assertion(assertion)
        primitive = assertion.get("type") if isinstance(assertion, dict) else None
        if not specs:
            unmappable.append({"id": oid, "primitive": primitive, "reason": reason})
            continue
        source = oracle.get("source") if isinstance(oracle.get("source"), dict) else {}
        preserved = {k: oracle[k] for k in _PRESERVED_FIELDS if k in oracle}
        for n, spec in enumerate(specs, start=1):
            mapped.append({
                "id": oid if len(specs) == 1 else f"{oid}#{n}",
                "oracle_id": oid, "primitive_from": primitive, "primitive_to": spec["type"],
                "level": oracle.get("level", ""), "scope": _scope(oracle),
                "claim": source.get("text", ""), "note": oracle.get("note", ""),
                "source": source, "assert": spec, **preserved,
            })

    for oracle in refuted or []:
        excluded.append({"id": oracle.get("id"), "confirmation": REFUTED,
                         "reason": "文档明确「当前未定义」——只保留观测，永不进判定"})

    refuted_list = list(refuted or [])
    oracles_count = len(oracles)
    undefined_count = len(refuted_list)
    total_declarations = oracles_count + undefined_count
    oracle_ids = {o.get("id") for o in oracles if isinstance(o.get("id"), str)}
    refuted_ids = {o.get("id") for o in refuted_list if isinstance(o.get("id"), str)}
    overlap = sorted(oracle_ids & refuted_ids)
    n_confirmed = sum(1 for o in oracles if o.get("confirmation") == CONFIRMED)
    mapped_oracles = len({m["oracle_id"] for m in mapped})
    tier_counts = {"business": 0, "technical": 0, "unrecognized": 0}
    for item in mapped:
        tier_counts[_tier(item["source"].get("kind"))] += 1
    three_bucket_sum = mapped_oracles + len(unmappable) + len(excluded)
    counts = {
        "total": total_declarations,
        "oracles_count": oracles_count,
        "explicitly_undefined_count": undefined_count,
        "total_declarations": total_declarations,
        "denominators_disjoint": not overlap,
        "denominator_overlap_ids": overlap,
        "eligible_confirmed": n_confirmed,
        "excluded": len(excluded), "mapped_oracles": mapped_oracles,
        "mapped_rules": len(mapped), "unmappable": len(unmappable),
        "three_bucket_sum": three_bucket_sum,
        "bucket_caliber": "oracles",
        "by_confirmation_tier": tier_counts,
        "numerator": mapped_oracles,
        "denominator": oracles_count,
        "denominator_all_declarations": total_declarations,
    }
    result = {
        "schema_version": 1, "engine": "oracle_bridge",
        "closed_primitives": sorted(CLOSED_PRIMITIVES),
        "counts": counts,
        "denominator_caliber": (
            f"两个不重叠集合：oracles 数组 {oracles_count} 条（confirmed/needs_confirmation）"
            f"+ explicitly_undefined 独立桶 {undefined_count} 条（全 refuted）"
            f"= 处理总数 {total_declarations}。counts.total == total_declarations（桥处理总数），"
            f"counts.oracles_count == {oracles_count}（采集器 counts.total 口径）。"
            f"差值 {undefined_count} 来自 refuted 独立成桶，不是丢判据；refuted 永不进判定。"
            f"三桶按**判据数**闭合（bucket_caliber=oracles）：可映射判据 {mapped_oracles}"
            f" + 不可映射 {len(unmappable)} + 排除 {len(excluded)} = {three_bucket_sum} == 处理总数。"
            f"一条判据可映射成多条规则（复合 range 的双边界），故 mapped_rules={len(mapped)}"
            f" >= mapped_oracles={mapped_oracles}；规则数另报，不参与三桶闭合。"
            f"可映射率两种口径都报：{mapped_oracles}/{oracles_count}（oracles 数组）"
            f"与 {mapped_oracles}/{total_declarations}（全部声明）。"),
        "mapped": mapped, "unmappable": unmappable, "excluded": excluded,
        "limitations": [
            "映射是结构转录，不是字段绑定：left 取自采集器原文（映射路径取 field，直通路径取 left），"
            "散文规则的字段名（中文主语等）不是数据库列名，求值器取不到列时返回 UNKNOWN",
            "confirmed_by 转录声明来源（kind:reference），不是独立核实的人名",
            "确认门控分两个 tier，两者都是合法 confirmed：业务 tier(user/document) 需业务方依据，"
            "技术 tier(ddl/annotation/openapi) 由作者写下即确认。FAIL 必须连同 confirmation_tier 一起读，"
            "本报告 by_confirmation_tier 分开计数；未识别 kind / 缺 reference / 缺 confirmed_by / "
            "非 confirmed 仍被压成 UNKNOWN",
            "scope 取自声明容器（table/entity/schema），无容器时退到 source.reference —— "
            "溯源占位，绑定层必须替换成真实业务域",
            "封闭集无 field_eq_const，也无严格 < 原语；带单位约束两条路径都不映射",
            "直通路径逐字复制断言，不重写语义；封闭集扩张时必须同步本表与两个求值器（LESSONS 坑 11）",
            "when/severity 逐字保留：when 是适用域，丢掉它会把 NOT_APPLICABLE 判成 FAIL（制造假缺陷）",
            "三桶按判据数闭合；一条判据可出多条规则（复合 range），规则数另报不参与闭合",
            "已武装未触发（不得当战果）：exclusive/negate/when 上游零发射，unit 6 条全 needs_confirmation，"
            "故这些闸门当前未被真实语料触发；但语义风险真实存在（求值器忽略这些键会放宽极性/反转语义），"
            "一旦上游发射即拒收并逐条报原因，不静默丢弃也不静默忽略",
            "本模块不产出观测：无观测时求值器返回 UNKNOWN 是正确行为，不是失败",
        ],
    }
    if upstream_counts is not None:
        pairs = (("oracles_count", oracles_count),
                 ("explicitly_undefined_count", undefined_count),
                 ("total_declarations", total_declarations),
                 ("denominators_disjoint", not overlap),
                 ("total", oracles_count))
        declared = {k: upstream_counts[k] for k, _ in pairs if upstream_counts.get(k) is not None}
        mismatches = {k: {"upstream": declared[k], "bridge": v}
                      for k, v in pairs if k in declared and declared[k] != v}
        result["caliber_crosscheck"] = {
            "upstream_declared": declared,
            "upstream_undeclared": [k for k, _ in pairs if k not in declared],
            "mismatches": mismatches,
            "agrees": not mismatches,
        }
    return result


def _carried(item: dict[str, Any]) -> dict[str, Any]:
    """求值器消费的额外约束字段，逐字带过去（丢 when 会制造假缺陷）。"""
    return {k: item[k] for k in _PRESERVED_FIELDS if k in item}


def to_rule_pack(result: dict[str, Any], target: str) -> dict[str, Any]:
    """映射结果 → rule_assessment 的 business-rules/1 判据包。"""
    rules = []
    for item in result["mapped"]:
        source = item["source"]
        rules.append({
            "id": item["id"], "claim": item["claim"], "scope": item["scope"],
            "assert": item["assert"],
            "source": {"kind": source.get("kind", ""), "reference": source.get("reference", ""),
                       "confirmation": CONFIRMED,
                       "confirmed_by": f"{source.get('kind', '')}:{source.get('reference', '')}"},
            **_carried(item),
        })
    return {"schema": "business-rules/1", "target": target, "rules": rules}


def to_invariants(result: dict[str, Any]) -> list[dict[str, Any]]:
    """映射结果 → invariants.evaluate_all 的不变式声明列表。"""
    return [{
        "id": item["id"], "claim": item["claim"], "category": item["level"],
        "why": item["note"], "assert": item["assert"],
        "oracle": {"source": item["source"].get("kind", ""), "confirmation": CONFIRMED,
                   "reference": item["source"].get("reference", ""), "note": item["note"]},
        **_carried(item),
    } for item in result["mapped"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="把采集判据映射到求值器封闭原语集")
    ap.add_argument("--harvested", required=True, type=Path)
    ap.add_argument("--target", required=True, help="判据包的目标标识，由调用方给出")
    ap.add_argument("--out-report", required=True, type=Path)
    ap.add_argument("--out-rules", type=Path)
    ap.add_argument("--out-invariants", type=Path)
    args = ap.parse_args()

    harvest = json.loads(args.harvested.read_text(encoding="utf-8"))
    result = bridge(harvest.get("oracles") or [], harvest.get("explicitly_undefined") or [],
                    harvest.get("counts"))
    result["untranscribed_count"] = len(harvest.get("untranscribed") or [])
    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    args.out_report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_rules:
        args.out_rules.write_text(json.dumps(to_rule_pack(result, args.target), ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    if args.out_invariants:
        args.out_invariants.write_text(json.dumps(to_invariants(result), ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    c = result["counts"]
    print(f"可映射 {c['numerator']}/{c['denominator']} 条（oracles 数组口径）→ {c['mapped_rules']} 条封闭原语规则")
    print(f"另一口径 {c['numerator']}/{c['denominator_all_declarations']}（全部声明，含 refuted 独立桶）")
    print(f"处理总数 {c['total_declarations']} = oracles {c['oracles_count']}"
          f" + refuted 独立桶 {c['explicitly_undefined_count']}（不重叠={c['denominators_disjoint']}）")
    print(f"三桶之和 {c['three_bucket_sum']} == 处理总数 {c['total']}（按判据数闭合）"
          f"：可映射判据 {c['mapped_oracles']} + 不可映射 {c['unmappable']} + 排除 {c['excluded']}")
    print(f"规则数另报：mapped_rules={c['mapped_rules']} >= mapped_oracles={c['mapped_oracles']}"
          f"（复合 range 一条判据出多条规则，不参与三桶闭合）")
    print(f"不可映射 {c['unmappable']} 条（逐条带原因）  排除 {c['excluded']} 条")
    t = c["by_confirmation_tier"]
    print(f"确认 tier 分开计数：业务 {t['business']} / 技术 {t['technical']} / 未识别 {t['unrecognized']}"
          f"（FAIL 必须连同 tier 一起读）")
    print(f"未转录（需物化，另册可见）{result['untranscribed_count']} 条")
    cross = result.get("caliber_crosscheck")
    if cross and not cross["agrees"]:
        print(f"口径与上游采集器不一致：{cross}")
        return 1
    if c["three_bucket_sum"] != c["total"] or not c["denominators_disjoint"]:
        print(f"分母口径未闭合：三桶和={c['three_bucket_sum']} 处理总数={c['total']} "
              f"重叠 id={c['denominator_overlap_ids']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())