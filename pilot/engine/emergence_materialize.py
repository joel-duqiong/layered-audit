"""候选链物化器 —— 把候选的「条件槽」长成具体注入（零项目字面量）。

候选生成器（emergence_candidates）只提出条件**位**：`data:table:X`、
`request:...currency`、`instance:...settle`。位上没有值，值一直来自人写的
注入目录 —— 于是「自动生成的候选链能自己抓出涌现」这句话没有证据支撑。

本模块补的就是这一段：**取值只来自被测系统自己的东西**
  · data     库里该列的既有取值（由项目侧的 reader 提供，引擎不碰 SQL）
  · instance 项目声明的装配实例清单
  · request  项目声明的取值域

**不许从注入目录（答案）里取值。** 基线值不产生注入 —— 那是空操作。
候选没提出的位不许凭空补；提出了却没有取值源的位记进 unmatched，不静默丢。

活性检查用 answer-free 的 `applied`（施加成功即活），不是「单独注入就有症状」：
后者会把**单独 inert、合起来才起作用**的条件判成死注入，而那正是涌现本身。
这是比手写活性检查更弱的判据，弱在哪里必须说出来，不许当成等价。

用法（项目侧）：
    domains = build_domains(sources, template, instances, reader)
    out = materialize(candidate, template, domains)
    judgement = emergence.adjudicate(out.chain, executor)
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import emergence

KIND_DATA = "data"
KIND_INSTANCE = "instance"
KIND_REQUEST = "request"

# 施加成功即活 —— 不预设症状，症状交给留一法去测
ALIVE_APPLIED = "applied"

ValueReader = Callable[[str, str], Sequence[str]]

_SLUG = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True)
class Domain:
    """一个条件位的取值域。slot 是匹配键，baseline 是空操作值。"""
    kind: str
    slot: str
    target: str
    column: str
    values: tuple[str, ...]
    baseline: str
    read_as: str = ""          # 判据 when 里用的列别名（与真实列名可能不同）


@dataclass(frozen=True)
class Materialized:
    """物化结果：链 + 取值 provenance（跳过的基线值、没取值源的位）。"""
    chain: emergence.Chain
    skipped: tuple[Mapping[str, Any], ...]
    unmatched: tuple[str, ...]
    pinned: tuple[Mapping[str, Any], ...] = ()
    unmatched_pins: tuple[str, ...] = ()


def _slug(text: str) -> str:
    return _SLUG.sub("-", str(text).upper()).strip("-")


def slot_key(condition: str) -> tuple[str, str]:
    """候选条件 -> (种类, 匹配键)。

    data:table:shop_customer      -> ("data", "shop_customer")
    request:a.b.R.currency        -> ("request", "currency")
    instance:a.b.P.settle         -> ("instance", "")   装配位不按 callee 区分
    """
    kind, _, rest = str(condition).partition(":")
    if kind == KIND_INSTANCE:
        return kind, ""
    tail = rest.rsplit(":", 1)[-1]
    return kind, tail.rsplit(".", 1)[-1]


def pins_from_when(when: Any) -> dict[str, str]:
    """判据的 when 子句 -> 适用域钉值。

    不变式自己声明了「我只在什么行上适用」。物化时把被 eq 约束的字段钉在
    域内值上，否则枚举会把观测推出适用域 —— 判据变成「不适用」，
    而「不适用」不是「没违反」。

    只有 eq 能钉出单一值；gt/lt 之类钉不出来，不猜。
    """
    clauses = when if isinstance(when, (list, tuple)) else ([when] if when else [])
    out: dict[str, str] = {}
    for clause in clauses:
        if not (isinstance(clause, Mapping) and clause.get("op") == "eq"
                and clause.get("field")):
            continue
        # 缺 value / value 为 None 的子句跳过 —— invariants.applies 用 .get 容忍它们
        # （那条判据静默永不适用）。这里若硬下标会 KeyError，若 str(None) 会把基线
        # 改写成字面字符串 "None"。同一份配置，判据引擎接受的物化器也必须接受。
        if clause.get("value") is None:
            continue
        out[str(clause["field"])] = str(clause["value"])
    return out


def build_domains(sources: Mapping[str, Any], template: Mapping[str, Any],
                  instances: Mapping[str, Any], reader: ValueReader) -> tuple[Domain, ...]:
    """项目声明的取值源 + 模板基线 -> 取值域。

    约定：data 源的 `target` 别名同时也是模板 flow 里基线块的名字，
    基线值从那里取 —— 换项目不用改本模块。
    """
    flow = template.get("flow") or {}
    out: list[Domain] = []
    for entry in sources.get(KIND_DATA) or []:
        table = str(entry["table"])
        target = str(entry["target"])
        base_block = flow.get(target) or {}
        read_as = entry.get("read_as") or {}
        for column in entry.get("columns") or []:
            out.append(Domain(
                kind=KIND_DATA, slot=table, target=target, column=str(column),
                values=tuple(str(v) for v in reader(table, str(column))),
                baseline=str(base_block.get(column, "")),
                read_as=str(read_as.get(column, ""))))
    out.append(Domain(
        kind=KIND_INSTANCE, slot="", target="", column="",
        values=tuple(str(k) for k in instances),
        baseline=str(template.get("baseline_instance", ""))))
    order = (flow.get("orders") or [{}])[0]
    for entry in sources.get(KIND_REQUEST) or []:
        field = str(entry["field"])
        out.append(Domain(
            kind=KIND_REQUEST, slot=field, target=field, column="",
            values=tuple(str(v) for v in entry.get("values") or []),
            baseline=str(order.get(field, ""))))
    return tuple(out)


def _mutation(domain: Domain, value: str) -> dict[str, Any]:
    if domain.kind == KIND_DATA:
        return {"kind": KIND_DATA, "target": domain.target, "set": {domain.column: value}}
    if domain.kind == KIND_REQUEST:
        return {"kind": KIND_REQUEST, "set": {domain.target: value}}
    return {"kind": KIND_INSTANCE, "instance": value}


def _injection_id(domain: Domain, value: str) -> str:
    """注入 id 必须含**槽位**：两张表同列名同取值时，只按列名生成会撞 id，
    第二条被 setdefault 静默丢掉 -> 链少一个条件 -> 假 FALSIFIED_CHAIN。

    相邻重复段去掉，所以 request（slot 与 label 同名）与 instance 的 id 不变。
    """
    label = domain.column or domain.target or KIND_INSTANCE
    parts: list[str] = []
    for raw in (domain.kind, domain.slot, label, value):
        token = _slug(raw)
        if token and (not parts or parts[-1] != token):
            parts.append(token)
    return "GEN-" + "-".join(parts)


def _injection(domain: Domain, value: str, candidate: Mapping[str, Any],
               template: Mapping[str, Any]) -> emergence.Injection:
    label = domain.column or domain.target or KIND_INSTANCE
    return emergence.Injection(
        id=_injection_id(domain, value),
        defect=f"generated.{domain.kind}.{_slug(label).lower()}",
        layer=str(template.get("injection_layer", "")),
        target_step=str(candidate.get("endpoint", "")),
        mutation=_mutation(domain, value),
        expected_symptom=str(candidate.get("invariant", "")),
        alive_check={"kind": ALIVE_APPLIED},
        active=True)


def materialize(candidate: Mapping[str, Any], template: Mapping[str, Any],
                domains: Sequence[Domain], *,
                pins: Mapping[str, str] | None = None) -> Materialized:
    """候选 + 模板 + 取值域 -> 可裁决的链。确定性：同输入两次跑结果相同。

    pins 来自判据的 when 子句：被钉住的字段不枚举出适用域。
    """
    pins = dict(pins or {})
    flow = copy.deepcopy(dict(template.get("flow") or {}))
    injections: dict[str, emergence.Injection] = {}
    skipped: list[Mapping[str, Any]] = []
    unmatched: list[str] = []
    pinned: list[Mapping[str, Any]] = []
    matched_pins: set[str] = set()
    for condition in candidate.get("conditions") or []:
        kind, key = slot_key(condition)
        domain = next((d for d in domains
                       if d.kind == kind and (kind == KIND_INSTANCE or d.slot == key)), None)
        if domain is None:
            unmatched.append(str(condition))
            continue
        pin_field = next((f for f in pins
                          if f in (domain.column, domain.target, domain.read_as)), "")
        if pin_field:
            # 回写前确认基线块存在 —— build_domains 对缺块是容忍的（基线取空串），
            # 这里若硬下标，同一份配置会「无钉值时正常、有钉值时崩」。
            block = (flow.get(domain.target) if domain.kind == KIND_DATA
                     else (flow.get("orders") or [None])[0])
            if not isinstance(block, dict):
                continue        # 钉不上：留在 unmatched_pins 里，不静默当成钉好了
            block[domain.column if domain.kind == KIND_DATA else domain.target] = pins[pin_field]
            matched_pins.add(pin_field)
            pinned.append({"condition": str(condition), "field": pin_field,
                           "value": pins[pin_field]})
            continue
        for value in domain.values:
            if value == domain.baseline:
                skipped.append({"condition": str(condition), "value": value,
                                "reason": "与基线相同，注入是空操作"})
                continue
            inj = _injection(domain, value, candidate, template)
            injections.setdefault(inj.id, inj)

    cid = str(candidate.get("id", "C"))
    chain = emergence.Chain(
        id=f"GEN-{cid}",
        title=f"候选 {cid} 物化链（取值来自被测系统自身，不含人工答案）",
        hypothesis="候选提出的条件位按取值域枚举后，由留一法判定哪些组合才违反目标不变式",
        severity=str(candidate.get("severity", "MEDIUM")),
        target_invariant=str(candidate.get("invariant", "")),
        root_cause_key=f"generated.{cid}",
        location=dict(template.get("location") or {}),
        injections=tuple(injections.values()),
        claim=str(candidate.get("invariant", "")),
        entity=str(template.get("entity", "")),
        flow=flow,
        baseline_instance=str(template.get("baseline_instance", "")),
        notes="物化链只用于验证生成能力，不进缺陷台账 —— 它的 defect 是占位键，不是已归属的根因")
    return Materialized(chain=chain, skipped=tuple(skipped), unmatched=tuple(unmatched),
                        pinned=tuple(pinned),
                        unmatched_pins=tuple(sorted(set(pins) - matched_pins)))
