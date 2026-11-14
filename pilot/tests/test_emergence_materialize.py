"""候选链物化器 —— 把候选提出的「条件槽」长成具体注入，且不许偷看答案。

要补的洞：候选生成器只提出 `data:table:X` 这样的**空槽位**，具体值
（`level=VIP`）一直来自人写的注入目录，执行器也是遍历人写的链。
于是「自动生成的候选链能自己抓出涌现」这句话没有证据。

物化器的取值只许来自被测系统自己的东西：库里的既有取值、装配实例清单、
项目声明的请求取值域。**不许从注入目录（答案）里取任何值。**

这组测试锁住三件事：
  1. 值来自声明的取值源，基线值不产生注入（那是空操作）
  2. 候选没提出的槽位不许凭空补，提出了但没取值源的槽位必须留痕
  3. 活性检查用 answer-free 的 `applied` —— 不许要求「单独注入就有症状」，
     因为「单独 inert、合起来才起作用」正是涌现本身
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import emergence  # noqa: E402
import emergence_materialize as em  # noqa: E402


TEMPLATE = {
    "entity": "order",
    "baseline_instance": "default",
    "injection_layer": "service",
    "flow": {"customer": {"name": "ai_probe_gen_cust", "level": "NORMAL"},
             "orders": [{"quotedAmountCent": 900000, "currency": "CNY"}]},
    "location": {"endpoint": "POST /api/orders"},
}

SOURCES = {
    "data": [{"table": "shop_customer", "target": "customer", "columns": ["level"]}],
    "request": [{"field": "currency", "values": ["CNY", "EUR"]}],
}

INSTANCES = {"default": {"base_url": "http://x:1"}, "vip": {"base_url": "http://x:2"}}


def _reader(table: str, column: str) -> list[str]:
    """项目侧提供的取值读取器（引擎不碰 SQL）。"""
    assert (table, column) == ("shop_customer", "level")
    return ["NORMAL", "VIP"]


def _candidate(conditions: list[str]) -> dict:
    return {"id": "C0000", "entry": "E0", "endpoint": "POST /api/orders",
            "invariant": "BI-PROBE", "write_sink": "table:shop_order",
            "conditions": conditions, "severity": "HIGH", "spine": ["E0", "S0"]}


def _domains() -> tuple[em.Domain, ...]:
    return em.build_domains(SOURCES, TEMPLATE, INSTANCES, _reader)


# ---------------------------------------------------------------- 取值域

def test_domains_read_values_from_reader_not_from_answer() -> None:
    # Arrange / Act
    domains = _domains()
    data = [d for d in domains if d.kind == em.KIND_DATA]

    # Assert：值来自 reader，且带上基线
    assert len(data) == 1
    assert data[0].values == ("NORMAL", "VIP")
    assert data[0].baseline == "NORMAL"
    assert data[0].target == "customer"
    assert data[0].column == "level"


def test_instance_domain_comes_from_declared_instances() -> None:
    domains = _domains()
    inst = [d for d in domains if d.kind == em.KIND_INSTANCE]
    assert len(inst) == 1
    assert inst[0].values == ("default", "vip")
    assert inst[0].baseline == "default"


def test_request_domain_baseline_taken_from_template_flow() -> None:
    domains = _domains()
    req = [d for d in domains if d.kind == em.KIND_REQUEST]
    assert len(req) == 1
    assert req[0].values == ("CNY", "EUR")
    assert req[0].baseline == "CNY"


# ---------------------------------------------------------------- 槽位匹配

def test_slot_key_takes_last_segment_of_dotted_path() -> None:
    assert em.slot_key("request:com.x.CreateOrderRequest.currency") == ("request", "currency")
    assert em.slot_key("data:table:shop_customer") == ("data", "shop_customer")
    assert em.slot_key("instance:com.x.PricingStrategy.settle") == ("instance", "")
    assert em.slot_key("xrepo:python-svc") == ("xrepo", "python-svc")


# ---------------------------------------------------------------- 物化

def test_baseline_values_produce_no_injection() -> None:
    # Arrange
    cand = _candidate(["data:table:shop_customer", "instance:com.x.P.settle",
                       "request:com.x.R.currency"])

    # Act
    out = em.materialize(cand, TEMPLATE, _domains())

    # Assert：NORMAL / default / CNY 都是基线，不该变成注入
    values = {i.id: i.mutation for i in out.chain.injections}
    assert len(values) == 3
    assert {m.get("set", {}).get("level") for m in values.values()
            if m["kind"] == "data"} == {"VIP"}
    assert {m["instance"] for m in values.values() if m["kind"] == "instance"} == {"vip"}
    assert {m.get("set", {}).get("currency") for m in values.values()
            if m["kind"] == "request"} == {"EUR"}
    assert len(out.skipped) == 3          # 三个基线值被跳过，且留痕


def test_unproposed_slot_is_not_invented() -> None:
    # Arrange：候选只提出了 data，没提出 instance / request
    cand = _candidate(["data:table:shop_customer"])

    # Act
    out = em.materialize(cand, TEMPLATE, _domains())

    # Assert
    assert [i.mutation["kind"] for i in out.chain.injections] == ["data"]


def test_slot_without_value_source_is_recorded_not_silently_dropped() -> None:
    cand = _candidate(["data:table:shop_customer", "xrepo:python-svc"])
    out = em.materialize(cand, TEMPLATE, _domains())
    assert "xrepo:python-svc" in out.unmatched


def test_generated_injections_are_all_active() -> None:
    """人手写的 active=false 是人的判断；物化器没有这个信息，不许替人决定。"""
    cand = _candidate(["data:table:shop_customer", "instance:com.x.P.settle",
                       "request:com.x.R.currency"])
    out = em.materialize(cand, TEMPLATE, _domains())
    assert all(i.active for i in out.chain.injections)


def test_alive_check_is_answer_free() -> None:
    """活性检查不许要求「单独注入就有症状」—— 那会把只在组合里起作用的
    条件判成死注入，而那正是涌现。物化器只声明「施加成功即活」。"""
    cand = _candidate(["instance:com.x.P.settle"])
    out = em.materialize(cand, TEMPLATE, _domains())
    assert out.chain.injections[0].alive_check == {"kind": em.ALIVE_APPLIED}


def test_chain_targets_candidate_invariant_and_template_baseline() -> None:
    cand = _candidate(["data:table:shop_customer"])
    out = em.materialize(cand, TEMPLATE, _domains())
    assert out.chain.target_invariant == "BI-PROBE"
    assert out.chain.entity == "order"
    assert out.chain.baseline_instance == "default"
    assert out.chain.flow == TEMPLATE["flow"]


def test_mutation_shape_matches_probe_contract() -> None:
    cand = _candidate(["data:table:shop_customer", "request:com.x.R.currency"])
    out = em.materialize(cand, TEMPLATE, _domains())
    by_kind = {i.mutation["kind"]: i.mutation for i in out.chain.injections}
    assert by_kind["data"] == {"kind": "data", "target": "customer", "set": {"level": "VIP"}}
    assert by_kind["request"] == {"kind": "request", "set": {"currency": "EUR"}}


def test_materialize_is_deterministic() -> None:
    cand = _candidate(["data:table:shop_customer", "instance:com.x.P.settle",
                       "request:com.x.R.currency"])
    first = em.materialize(cand, TEMPLATE, _domains())
    second = em.materialize(cand, TEMPLATE, _domains())
    assert first == second


def test_empty_candidate_yields_no_injections() -> None:
    out = em.materialize(_candidate([]), TEMPLATE, _domains())
    assert out.chain.injections == ()


def test_generated_chain_is_loadable_by_adjudicator() -> None:
    """物化出来的必须是裁决器能直接吃的 Chain，不是另一套结构。"""
    cand = _candidate(["data:table:shop_customer"])
    out = em.materialize(cand, TEMPLATE, _domains())
    assert isinstance(out.chain, emergence.Chain)
    assert isinstance(out.chain.injections[0], emergence.Injection)


# ---------------------------------------------------------------- 适用域钉值

def test_pins_extracted_from_invariant_when_clause() -> None:
    """判据的 when 就是适用域声明 —— 机器能读，不用人告诉它。"""
    assert em.pins_from_when({"field": "currency", "op": "eq", "value": "CNY"}) == {"currency": "CNY"}
    assert em.pins_from_when([{"field": "currency", "op": "eq", "value": "CNY"},
                              {"field": "customer_level", "op": "eq", "value": "VIP"}]) == {
        "currency": "CNY", "customer_level": "VIP"}


def test_non_eq_constraint_is_not_turned_into_a_pin() -> None:
    """gt/lt 之类的约束钉不出单一值 —— 不许瞎猜一个。"""
    assert em.pins_from_when({"field": "amount", "op": "gt", "value": 100}) == {}


def test_pinned_field_is_not_enumerated_out_of_scope() -> None:
    # Arrange：判据只在 CNY 下适用
    cand = _candidate(["data:table:shop_customer", "request:com.x.R.currency"])
    pins = em.pins_from_when({"field": "currency", "op": "eq", "value": "CNY"})

    # Act
    out = em.materialize(cand, TEMPLATE, _domains(), pins=pins)

    # Assert：currency 被钉在 CNY，不产生 EUR 注入；level 照常枚举
    assert [i.mutation["kind"] for i in out.chain.injections] == ["data"]
    assert out.pinned == ({"condition": "request:com.x.R.currency",
                           "field": "currency", "value": "CNY"},)


def test_pin_matching_no_domain_is_recorded() -> None:
    """钉值对不上任何取值域时必须留痕 —— 否则适用域约束静默失效。"""
    cand = _candidate(["data:table:shop_customer"])
    out = em.materialize(cand, TEMPLATE, _domains(), pins={"customer_level": "VIP"})
    assert "customer_level" in out.unmatched_pins


def test_pins_default_to_empty_and_change_nothing() -> None:
    cand = _candidate(["data:table:shop_customer", "request:com.x.R.currency"])
    assert em.materialize(cand, TEMPLATE, _domains()) == em.materialize(
        cand, TEMPLATE, _domains(), pins={})


# ---------------------------------------------------------------- 别名与基线钉值

def test_pin_matches_declared_read_alias() -> None:
    """判据的 when 用的是读出来的列别名（customer_level），
    注入用的是真实列名（level）—— 取值源声明别名后必须能对上。"""
    sources = {"data": [{"table": "shop_customer", "target": "customer",
                         "columns": ["level"], "read_as": {"level": "customer_level"}}]}
    domains = em.build_domains(sources, TEMPLATE, INSTANCES, _reader)
    cand = _candidate(["data:table:shop_customer"])
    out = em.materialize(cand, TEMPLATE, domains, pins={"customer_level": "VIP"})
    assert out.unmatched_pins == ()
    assert out.pinned[0]["field"] == "customer_level"


def test_pin_rewrites_baseline_so_scope_holds_at_baseline() -> None:
    """钉值不只是「不枚举出去」—— 基线本身也得落在适用域内，
    否则基线观测就是「不适用」，整条链判不了。"""
    sources = {"data": [{"table": "shop_customer", "target": "customer",
                         "columns": ["level"], "read_as": {"level": "customer_level"}}]}
    domains = em.build_domains(sources, TEMPLATE, INSTANCES, _reader)
    cand = _candidate(["data:table:shop_customer", "instance:x.settle"])
    out = em.materialize(cand, TEMPLATE, domains, pins={"customer_level": "VIP"})
    assert out.chain.flow["customer"]["level"] == "VIP"
    # 钉住后 level 不再有非基线取值，只剩装配这一条注入
    assert [i.mutation["kind"] for i in out.chain.injections] == ["instance"]


def test_pin_does_not_mutate_caller_template() -> None:
    """不可变：钉值改的是链自己的 flow，不许回写调用方的模板。"""
    sources = {"data": [{"table": "shop_customer", "target": "customer",
                         "columns": ["level"], "read_as": {"level": "customer_level"}}]}
    domains = em.build_domains(sources, TEMPLATE, INSTANCES, _reader)
    cand = _candidate(["data:table:shop_customer"])
    em.materialize(cand, TEMPLATE, domains, pins={"customer_level": "VIP"})
    assert TEMPLATE["flow"]["customer"]["level"] == "NORMAL"


# ---------------------------------------------------------------- 审查补洞
# 独立审查（2026-11-15）用最小复现确认的三处确定性缺陷。
# 共同点：判据引擎/建域阶段容忍的配置，到了物化阶段崩溃或静默丢东西。


def test_eq_clause_without_value_is_skipped_not_crash():
    """eq 子句缺 value：invariants.applies 用 .get 容忍，物化器不许 KeyError。"""
    assert em.pins_from_when({"field": "currency", "op": "eq"}) == {}


def test_eq_clause_with_none_value_is_skipped():
    """value 为 None 不许钉出字面字符串 "None" 污染基线。"""
    assert em.pins_from_when({"field": "currency", "op": "eq", "value": None}) == {}


def test_pin_writeback_tolerates_missing_data_block():
    """模板缺 customer 块：build_domains 容忍了，钉值回写不许 KeyError。"""
    template = {**TEMPLATE, "flow": {"orders": [{"currency": "CNY"}]}}
    domains = em.build_domains(SOURCES, template, INSTANCES, _reader)
    out = em.materialize(_candidate(["data:table:shop_customer"]), template, domains,
                         pins={"level": "VIP"})
    assert "level" in out.unmatched_pins
    assert out.pinned == ()


def test_pin_writeback_tolerates_missing_orders_block():
    template = {**TEMPLATE, "flow": {"customer": {"level": "NORMAL"}}}
    domains = em.build_domains(SOURCES, template, INSTANCES, _reader)
    out = em.materialize(_candidate(["request:a.b.R.currency"]), template, domains,
                         pins={"currency": "CNY"})
    assert "currency" in out.unmatched_pins


def test_pin_writeback_tolerates_empty_orders_list():
    template = {**TEMPLATE, "flow": {"customer": {"level": "NORMAL"}, "orders": []}}
    domains = em.build_domains(SOURCES, template, INSTANCES, _reader)
    out = em.materialize(_candidate(["request:a.b.R.currency"]), template, domains,
                         pins={"currency": "CNY"})
    assert "currency" in out.unmatched_pins


def test_same_column_in_two_tables_yields_two_injections():
    """两张表同列名同取值：id 不含表名会让第二条被 setdefault 静默丢掉。

    少一个条件 -> 合起来不违反 -> 假 FALSIFIED_CHAIN。模块自己声明「不静默丢」。
    """
    sources = {"data": [{"table": "shop_customer", "target": "customer", "columns": ["level"]},
                        {"table": "shop_agent", "target": "agent", "columns": ["level"]}]}
    template = {**TEMPLATE, "flow": {**TEMPLATE["flow"],
                                     "agent": {"name": "ai_probe_gen_agent", "level": "NORMAL"}}}
    domains = em.build_domains(sources, template, INSTANCES,
                               lambda table, column: ["NORMAL", "VIP"])
    out = em.materialize(_candidate(["data:table:shop_customer", "data:table:shop_agent"]),
                         template, domains)
    assert len(out.chain.injections) == 2
    assert len({i.id for i in out.chain.injections}) == 2
    assert {i.mutation["target"] for i in out.chain.injections} == {"customer", "agent"}
