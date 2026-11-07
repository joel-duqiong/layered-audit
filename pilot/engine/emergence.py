"""涌现裁决引擎（通用）。

把「注入了哪些条件」与「目标不变式有没有被违反」的结果，按七种裁决归类。

判定式（DESIGN.md §4.1）：

    F(s)  = 以注入集合 s 执行链的步骤后，目标不变式是否被违反
    EMERGENT(c) ⟺ F(D)=1 且 ∀d∈D: F({d})=0

工程前提：注入是**条件注入**（请求字段 / 数据行 / 装配上下文），不是代码变异 ——
被测系统只读，裁决器只换输入与上下文，不动被测系统一行业务代码。

引擎零项目字面量：它不知道什么是订单、额度、容器、HTTP。
场景由项目侧的 executor 提供，引擎只调用它并做判定。判定与实验严格分离，
是为了让「同一份观测 → 同一份裁决」这件事可以脱离被测系统复算。

七种裁决的归属规则（DESIGN.md §4.2）：

| verdict              | 条件                        | owner          | category                              |
|----------------------|-----------------------------|----------------|---------------------------------------|
| EMERGENT_CONJUNCTIVE | 合起来错、单独都不错         | integration    | integration.end_to_end_business       |
| EMERGENT_CONJUNCTIVE_CORE | 求出最小核心集          | integration    | integration.end_to_end_business       |
| EMERGENT_AMPLIFIED   | 合起来错且错得更狠（≥1.5×） | integration    | integration.cross_service_performance |
| EMERGENT_MASKING     | B 掩盖了 A 的症状            | 掩盖者的层     | integration.emergent_masking          |
| NOT_EMERGENT_INDEPENDENT | 各自都错、不变量不相交 | 各低层        | 集成层不开单                           |
| NOT_EMERGENT_ADDITIVE    | 相交但无倍数效应        | 已有根因       | 集成层不开新单                         |
| FALSIFIED_CHAIN      | 假设不成立                  | 不开单          | —                                     |
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import findings_schema

# ------------------------------------------------------------------ 裁决常量

VERDICT_CONJUNCTIVE = "EMERGENT_CONJUNCTIVE"
VERDICT_CONJUNCTIVE_CORE = "EMERGENT_CONJUNCTIVE_CORE"
VERDICT_AMPLIFIED = "EMERGENT_AMPLIFIED"
VERDICT_MASKING = "EMERGENT_MASKING"
VERDICT_NOT_INDEPENDENT = "NOT_EMERGENT_INDEPENDENT"
VERDICT_NOT_ADDITIVE = "NOT_EMERGENT_ADDITIVE"
VERDICT_FALSIFIED = "FALSIFIED_CHAIN"

ALL_VERDICTS: tuple[str, ...] = (
    VERDICT_CONJUNCTIVE, VERDICT_CONJUNCTIVE_CORE, VERDICT_AMPLIFIED,
    VERDICT_MASKING, VERDICT_NOT_INDEPENDENT, VERDICT_NOT_ADDITIVE, VERDICT_FALSIFIED,
)

# 三态：与 layers.yaml 的 ground_truth 枚举对齐
GT_VERIFIED = "verified"
GT_UNVERIFIED = "unverified"
GT_MUTANT_SURVIVOR = "mutant_survivor"

# 放大效应的量级门槛（合起来偏离量 / 单注偏离量 ≥ 该值才算「错得更狠」）
AMPLIFICATION_RATIO = 1.5

# 裁决 -> (开单层, 归属关系, 契约类别)。MASKING 例外：归属层由掩盖者决定，见 _masking_filings。
VERDICT_FILING: Mapping[str, tuple[str, str, str]] = {
    VERDICT_CONJUNCTIVE: ("integration", "own", "integration.end_to_end_business"),
    VERDICT_CONJUNCTIVE_CORE: ("integration", "own", "integration.end_to_end_business"),
    VERDICT_AMPLIFIED: ("integration", "own", "integration.cross_service_performance"),
}

# 掩盖型裁决的新类别（契约里必须存在，否则说明契约还没改 —— 契约变更必须走提交）
CATEGORY_MASKING = "integration.emergent_masking"


# ------------------------------------------------------------------ 数据模型

@dataclass(frozen=True)
class Injection:
    """一条条件注入。

    defect           这条注入代表的低层根因（root_cause_key，与既有根因体系同一套指纹）
    layer            注入作用在哪一层
    target_step      注入落在链的哪一步
    mutation         条件怎么施加（请求字段 / 数据行 / 装配上下文），由项目侧解释
    expected_symptom 这条注入单独出现时，在它自己的层上应当可见的症状（不变式 id）
    alive_check      活性检查：证明这条注入在当前环境里是活的（不是死的）
    active           是否参与本链的裁决（active=false 的条目只用于敏感性分析）
    """
    id: str
    defect: str
    layer: str
    target_step: str
    mutation: Mapping[str, Any]
    expected_symptom: str
    alive_check: Mapping[str, Any]
    active: bool = True
    category: str = ""


@dataclass(frozen=True)
class Chain:
    """一条待裁决的链：目标不变式 + 缺陷集 + 元信息。"""
    id: str
    title: str
    hypothesis: str
    severity: str
    target_invariant: str
    root_cause_key: str
    location: Mapping[str, Any]
    injections: tuple[Injection, ...]
    claim: str = ""
    cwe: tuple[str, ...] = ()
    cites: tuple[str, ...] = ()
    entity: str = ""
    flow: Mapping[str, Any] = field(default_factory=dict)
    baseline_instance: str = ""
    baseline: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class Catalog:
    """注入目录（项目配置）。"""
    chains: tuple[Chain, ...]
    instances: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    database: Mapping[str, Any] = field(default_factory=dict)

    def chain(self, chain_id: str) -> Chain:
        for c in self.chains:
            if c.id == chain_id:
                return c
        raise KeyError(f"目录里没有这条链: {chain_id}")


@dataclass(frozen=True)
class Observation:
    """一次场景执行的结果：F(s) 的观测值。"""
    subset: tuple[str, ...]
    violated: bool
    magnitude: float = 0.0
    violated_invariants: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    status: str = "ok"          # ok | error
    detail: str = ""


@dataclass(frozen=True)
class AliveResult:
    """一次活性检查的结果。"""
    injection_id: str
    alive: bool
    symptom: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class Filing:
    """一条应当开出的单（层 / 类别 / 归属 / 根因指纹）。"""
    layer: str
    category: str
    owner_layer: str
    relation: str            # own | cited
    root_cause_key: str
    claim: str
    title: str
    severity: str
    confidence: float
    ground_truth: str
    cwe: tuple[str, ...] = ()
    related_root_causes: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Judgement:
    """裁决结论。"""
    chain_id: str
    verdict: str
    owner_layer: str
    owner_relation: str
    category: str
    core: tuple[str, ...]
    observations: tuple[Observation, ...]
    alive: Mapping[str, bool]
    warnings: tuple[str, ...]
    rationale: str
    ground_truth: str
    filings: tuple[Filing, ...]


class ScenarioExecutor(Protocol):
    """项目侧要实现的两个动作。

    run   执行一次场景：按给定的注入集合施加条件，跑完链的步骤，回报目标不变式有没有被违反
    alive 活性检查：证明某条注入在当前环境里是活的
    """

    def run(self, chain: Chain, subset: tuple[str, ...]) -> Observation: ...

    def alive(self, chain: Chain, injection: Injection) -> AliveResult: ...


# ------------------------------------------------------------------ 契约校验

_CATEGORY_CACHE: dict[str, tuple[str, ...]] = {}


def contract_categories() -> Mapping[str, tuple[str, ...]]:
    """契约里每层允许的类别（读 layers.yaml，不在引擎里写死）。"""
    if not _CATEGORY_CACHE:
        _CATEGORY_CACHE.update(findings_schema.load_layer_categories())
    return _CATEGORY_CACHE


def check_category(layer: str, category: str) -> None:
    """类别必须落在契约允许的列表里 —— 契约缺类别就报错，不静默降级。"""
    cats = contract_categories()
    layer_part, _, cat_part = category.partition(".")
    allowed = cats.get(layer_part)
    if allowed is None:
        raise ValueError(f"契约里没有层 {layer_part}，类别 {category} 无从校验")
    if cat_part not in allowed:
        raise ValueError(
            f"契约的 {layer_part} 层没有类别 {cat_part} —— "
            f"开这种单之前必须先改 layers.yaml 并提交（契约变更禁止运行期动态修改）")


# ------------------------------------------------------------------ 目录读取

def _require(obj: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ValueError(f"{where} 缺少必填字段 {key}")
    return obj[key]


def _load_injection(raw: Mapping[str, Any], where: str) -> Injection:
    inj = Injection(
        id=str(_require(raw, "id", where)),
        defect=str(_require(raw, "defect", where)),
        layer=str(_require(raw, "layer", where)),
        target_step=str(raw.get("target_step", "")),
        mutation=dict(_require(raw, "mutation", where)),
        expected_symptom=str(raw.get("expected_symptom", "")),
        alive_check=dict(_require(raw, "alive_check", where)),
        active=bool(raw.get("active", True)),
        category=str(raw.get("category", "")),
    )
    if not inj.mutation.get("kind"):
        raise ValueError(f"{where} 的 mutation 缺少 kind（条件注入的类型）")
    if not inj.alive_check.get("kind"):
        raise ValueError(f"{where} 的 alive_check 缺少 kind")
    return inj


def load_catalog(path: str | Path) -> Catalog:
    """读注入目录（项目配置，格式由本模块定义）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    chains: list[Chain] = []
    for c in raw.get("chains", []):
        where = f"链 {c.get('id', '?')}"
        injections = tuple(_load_injection(i, f"{where} 的注入 {i.get('id', '?')}")
                           for i in c.get("injections", []))
        seen: set[str] = set()
        for inj in injections:
            if inj.id in seen:
                raise ValueError(f"{where} 里注入 id 重复: {inj.id}")
            seen.add(inj.id)
        chains.append(Chain(
            id=str(_require(c, "id", where)),
            title=str(c.get("title", "")),
            hypothesis=str(c.get("hypothesis", "")),
            severity=str(c.get("severity", "MEDIUM")),
            target_invariant=str(_require(c, "target_invariant", where)),
            root_cause_key=str(_require(c, "root_cause_key", where)),
            location=dict(c.get("location", {})),
            injections=injections,
            claim=str(c.get("claim", "")),
            cwe=tuple(c.get("cwe", ())),
            cites=tuple(c.get("cites", ())),
            entity=str(c.get("entity", "")),
            flow=dict(c.get("flow", {})),
            baseline_instance=str(c.get("baseline_instance", "")),
            baseline=dict(c.get("baseline", {})),
            notes=str(c.get("notes", "")),
        ))
    if not chains:
        raise ValueError("注入目录里没有任何链")
    return Catalog(chains=tuple(chains),
                   instances=dict(raw.get("instances", {})),
                   database=dict(raw.get("database", {})))


# ------------------------------------------------------------------ 判定

def _minimal_core(chain: Chain, executor: ScenarioExecutor,
                  alive_ids: Sequence[str]) -> tuple[str, ...]:
    """leave-one-out：逐个剔除注入，看目标不变式是否仍然被违反。

    结果是**包含意义下最小**的核心集：去掉任何一个成员，链就不再违反不变式。
    """
    core = list(alive_ids)
    for inj_id in sorted(alive_ids):
        candidate = tuple(x for x in core if x != inj_id)
        if not candidate:
            continue
        if executor.run(chain, candidate).violated:
            core = list(candidate)
    return tuple(core)


def _overlap(singles: Mapping[str, Observation], combined: Observation) -> bool:
    """单注之间（以及单注与合起来）违反的不变量是否相交。"""
    sets = [set(o.violated_invariants) for o in singles.values()]
    union: set[str] = set()
    for s in sets:
        union |= s
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            if a & b:
                return True
    return bool(union & set(combined.violated_invariants))


def _new_invariants(singles: Mapping[str, Observation], combined: Observation) -> set[str]:
    union: set[str] = set()
    for o in singles.values():
        union |= set(o.violated_invariants)
    return set(combined.violated_invariants) - union


def _masking_filings(chain: Chain, fallback_owner_layer: str,
                     masking_ids: Sequence[str], injections: Mapping[str, Injection],
                     ground_truth: str, confidence: float) -> tuple[Filing, ...]:
    """掩盖型裁决：掩盖者的层自己开单（own），集成层只引用（cited）。"""
    mask_layers = {injections[i].layer for i in masking_ids if i in injections}
    owner_layer = sorted(mask_layers)[0] if len(mask_layers) == 1 else fallback_owner_layer
    mask_category = ""
    for i in masking_ids:
        if i in injections and injections[i].category:
            mask_category = injections[i].category
            break
    if not mask_category:
        mask_category = chain.location.get("category", "") or "integration.end_to_end_business"
    # 契约先行：掩盖型的两个类别都必须已经在 layers.yaml 里，否则不开单
    check_category(owner_layer, mask_category)
    check_category("integration", CATEGORY_MASKING)
    own = Filing(
        layer=owner_layer,
        category=mask_category,
        owner_layer=owner_layer,
        relation="own",
        root_cause_key=chain.root_cause_key,
        claim=chain.claim or chain.title,
        title=chain.title,
        severity=chain.severity,
        confidence=confidence,
        ground_truth=ground_truth,
        cwe=chain.cwe,
        related_root_causes=chain.cites,
        note="掩盖者：这条注入把另一条注入的症状盖住了 —— 单独看得见，合起来反而看不见")
    cited = Filing(
        layer="integration",
        category=CATEGORY_MASKING,
        owner_layer=owner_layer,
        relation="cited",
        root_cause_key=chain.root_cause_key,
        claim=chain.claim or chain.title,
        title=chain.title,
        severity=chain.severity,
        confidence=confidence,
        ground_truth=ground_truth,
        cwe=chain.cwe,
        related_root_causes=chain.cites,
        note="链级后果归集成层（cited），根因归掩盖者的层（own）")
    return (own, cited)


def adjudicate(chain: Chain, executor: ScenarioExecutor, *,
               amplification_ratio: float = AMPLIFICATION_RATIO,
               confidence: float = 0.9) -> Judgement:
    """裁决一条链。

    步骤：
      1. 活性检查 —— 活性不通过的注入从分母剔除并告警（不许静默）
      2. 基线 —— 基线就违反目标不变式的话，链的前提不成立
      3. 合起来 + 每条单独 —— 得到 F(D) 与 F({d})
      4. leave-one-out —— 求最小核心集
      5. 归类 —— 七种裁决之一
    """
    warnings: list[str] = []
    alive_ids: list[str] = []
    alive_map: dict[str, bool] = {}
    injections: dict[str, Injection] = {i.id: i for i in chain.injections}

    for inj in chain.injections:
        if not inj.active:
            alive_map[inj.id] = False
            continue
        res = executor.alive(chain, inj)
        alive_map[inj.id] = res.alive
        if res.alive:
            alive_ids.append(inj.id)
        else:
            warnings.append(
                f"注入 {inj.id} 活性检查不通过（{res.detail or '无说明'}）—— "
                f"该条目已从分母剔除，其结论不参与本链裁决")

    if not alive_ids:
        return Judgement(
            chain_id=chain.id, verdict=VERDICT_FALSIFIED,
            owner_layer="", owner_relation="", category="",
            core=(), observations=(), alive=alive_map,
            warnings=tuple(warnings),
            rationale="没有任何一条注入通过活性检查，链无从裁决",
            ground_truth=GT_UNVERIFIED, filings=())

    observations: list[Observation] = []

    base = executor.run(chain, ())
    observations.append(base)
    if base.status != "ok":
        return Judgement(
            chain_id=chain.id, verdict=VERDICT_FALSIFIED,
            owner_layer="", owner_relation="", category="",
            core=(), observations=tuple(observations), alive=alive_map,
            warnings=tuple(warnings + [f"基线场景执行失败: {base.detail}"]),
            rationale="基线场景执行失败，链的前提不成立",
            ground_truth=GT_UNVERIFIED, filings=())
    if base.violated:
        warnings.append("基线（不注入任何条件）就已经违反目标不变式 —— 违反不是注入造成的")
        return Judgement(
            chain_id=chain.id, verdict=VERDICT_FALSIFIED,
            owner_layer="", owner_relation="", category="",
            core=(), observations=tuple(observations), alive=alive_map,
            warnings=tuple(warnings),
            rationale="基线即违反目标不变式，链的前提不成立",
            ground_truth=GT_UNVERIFIED, filings=())

    combined = executor.run(chain, tuple(alive_ids))
    observations.append(combined)

    singles: dict[str, Observation] = {}
    for inj_id in alive_ids:
        obs = executor.run(chain, (inj_id,))
        singles[inj_id] = obs
        observations.append(obs)

    triggered = [i for i in alive_ids if singles[i].violated]
    core: tuple[str, ...] = tuple(alive_ids)
    # 只有**参与本链裁决**的注入才参与三态判定：
    # active=false 的条目只是敏感性分析的素材，它没被激活不等于它是死的。
    ground_truth = (GT_MUTANT_SURVIVOR
                    if any(not alive_map.get(i.id, True)
                           for i in chain.injections if i.active) else GT_VERIFIED)

    def verdict_of(v: str, owner_layer: str, owner_relation: str, category: str,
                   rationale: str, core_ids: tuple[str, ...],
                   filings: tuple[Filing, ...]) -> Judgement:
        return Judgement(
            chain_id=chain.id, verdict=v,
            owner_layer=owner_layer, owner_relation=owner_relation, category=category,
            core=core_ids, observations=tuple(observations), alive=alive_map,
            warnings=tuple(warnings), rationale=rationale,
            ground_truth=ground_truth, filings=filings)

    if not combined.violated:
        if triggered:
            # 掩盖者是「合起来才把症状盖住」的那条注入 —— 它的层才是 owner
            masking_ids = [i for i in alive_ids if i not in triggered]
            fallback = injections[triggered[0]].layer
            filings = _masking_filings(chain, fallback, masking_ids, injections,
                                       ground_truth, confidence)
            mask_layer = filings[0].owner_layer
            return verdict_of(
                VERDICT_MASKING, mask_layer, "own", CATEGORY_MASKING,
                f"单注触发、合起来不触发 —— 症状被掩盖（掩盖者={'+'.join(masking_ids)}，"
                f"归属层={mask_layer}）", tuple(alive_ids), filings)
        return verdict_of(
            VERDICT_FALSIFIED, "", "", "",
            "合起来也不违反目标不变式，链的前提不成立", (), ())

    if not triggered:
        core = _minimal_core(chain, executor, alive_ids)
        if len(core) < len(alive_ids):
            v = VERDICT_CONJUNCTIVE_CORE
            rationale = (f"合起来违反目标不变式，每条单独都不违反；"
                         f"leave-one-out 求出最小核心集 {'+'.join(core)}"
                         f"（缺陷集 {len(alive_ids)} 条，核心 {len(core)} 条）")
        else:
            v = VERDICT_CONJUNCTIVE
            rationale = (f"合起来违反目标不变式，每条单独都不违反；"
                         f"最小核心集 = 缺陷集（{'+'.join(core)}），没有冗余注入")
        layer, relation, category = VERDICT_FILING[v]
        check_category(layer, category)
        filings = (Filing(
            layer=layer, category=category, owner_layer=layer, relation=relation,
            root_cause_key=chain.root_cause_key,
            claim=chain.claim or chain.title, title=chain.title,
            severity=chain.severity, confidence=confidence, ground_truth=ground_truth,
            cwe=chain.cwe, related_root_causes=chain.cites,
            note="涌现链：单独注入都不触发，只有组合才触发 —— 根因不可归给任何单层，"
                 "故由集成层开单"),)
        return verdict_of(v, layer, relation, category, rationale, core, filings)

    # 有单注自己能触发：先看是不是「互相掩盖」，再看是不是「错得更狠」
    masking_pairs: list[tuple[str, str]] = []
    for t in triggered:
        for other in alive_ids:
            if other == t:
                continue
            obs = executor.run(chain, (t, other))
            observations.append(obs)
            if not obs.violated:
                masking_pairs.append((t, other))
    if masking_pairs:
        masking_ids = sorted({b for _a, b in masking_pairs})
        fallback = injections[masking_pairs[0][1]].layer
        filings = _masking_filings(chain, fallback, masking_ids, injections,
                                   ground_truth, confidence)
        mask_layer = filings[0].owner_layer
        return verdict_of(
            VERDICT_MASKING, mask_layer, "own", CATEGORY_MASKING,
            f"单注触发、成对组合后症状消失（掩盖对: {masking_pairs}，"
            f"掩盖者={'+'.join(masking_ids)}，归属层={mask_layer}）",
            tuple(alive_ids), filings)

    single_magnitude = max((singles[i].magnitude for i in triggered), default=0.0)
    if single_magnitude > 0 and combined.magnitude >= amplification_ratio * single_magnitude:
        layer, relation, category = VERDICT_FILING[VERDICT_AMPLIFIED]
        check_category(layer, category)
        filings = (Filing(
            layer=layer, category=category, owner_layer=layer, relation=relation,
            root_cause_key=chain.root_cause_key,
            claim=chain.claim or chain.title, title=chain.title,
            severity=chain.severity, confidence=confidence, ground_truth=ground_truth,
            cwe=chain.cwe, related_root_causes=chain.cites,
            note=f"合起来的偏离量 {combined.magnitude:g} ≥ 单注最大偏离量 "
                 f"{single_magnitude:g} 的 {amplification_ratio:g} 倍 —— 属于放大效应"),)
        return verdict_of(
            VERDICT_AMPLIFIED, layer, relation, category,
            f"合起来违反且偏离量放大（{combined.magnitude:g} ≥ "
            f"{amplification_ratio:g}×{single_magnitude:g}）",
            tuple(alive_ids), filings)

    overlap = _overlap(singles, combined)
    fresh = _new_invariants(singles, combined)
    if overlap:
        v = VERDICT_NOT_ADDITIVE
        rationale = "单注各自也能触发，且违反的不变量相交 —— 属加性，不是涌现"
    else:
        v = VERDICT_NOT_INDEPENDENT
        rationale = "单注各自也能触发，且违反的是互不相交的不变量 —— 各归各层，集成层不开单"
    if fresh:
        rationale += f"（组合额外引入了 {sorted(fresh)} 的违反，但单注已能触发，故仍不判为涌现）"
    return verdict_of(v, "", "", "", rationale, tuple(alive_ids), ())


# ------------------------------------------------------------------ 出单

def build_finding(judgement: Judgement, chain: Chain, filing: Filing, *,
                  source_ref: str, repro_cmd: str,
                  evidence_refs: Sequence[str] = (),
                  detail: str = "") -> dict[str, Any]:
    """把一条裁决结论变成符合证据 schema 的 finding 记录（10 个必填字段齐备）。"""
    location = {
        "file": str(chain.location.get("file", "")),
        "line": int(chain.location.get("line", 0) or 0),
        "symbol": str(chain.location.get("symbol", "")),
        "endpoint": str(chain.location.get("endpoint", "")),
    }
    refs = [f"{source_ref}#{filing.layer}:{chain.id}"] + list(evidence_refs)
    return {
        "id": chain.id,
        "layer": filing.layer,
        "category": filing.category,
        "location": location,
        "claim": filing.claim,
        "evidence_refs": refs,
        "repro_cmd": repro_cmd,
        # 归一化器按 "repro" 取复现命令（各层脚本的历史字段名），同值再给一份
        "repro": repro_cmd,
        "confidence": filing.confidence,
        "ground_truth": filing.ground_truth,
        "cwe": list(filing.cwe),
        # 以下字段给归一化器与人读
        "title": filing.title,
        "severity": filing.severity,
        "detail": detail or chain.hypothesis,
        "owner": {"layer": filing.owner_layer, "relation": filing.relation,
                  "basis": "attribution.rule：能给出可复现证据的最低层"},
        "root_cause_key": filing.root_cause_key,
        "related_root_causes": list(filing.related_root_causes),
        "verdict": judgement.verdict,
        "cites": list(chain.cites),
        "note": filing.note,
    }
