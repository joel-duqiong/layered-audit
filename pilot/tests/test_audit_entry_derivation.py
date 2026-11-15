"""audit_entry 的「两项输入可推导」契约守卫（W21）。

锁的正确行为（每条都配精确变异，见 /tmp/w21 变异 harness）：
  A 锚表推导：只吃已确认判据；桥过滤若回归必须**拒绝**而不是洗白；
    entity 只在唯一落到真实 JPA 实体时才写（写 DTO 名会让消费者兜底造出不存在的表）；
    人工供给优先；推导必须显式声明降级，不得冒充人工声明。
  B 入口契约：类级 + 方法级 path 拼接正确（空 path / 尾斜杠 / 多 path）；
    类级 RequestMapping 不许被当方法级重复前缀；分不清的记 ambiguous 不猜；
    推不出时诚实报缺口（反向：合法仓库不得误红）；人工 openapi 优先；
    合成契约只供入口绑定，**不进判据采集**（避免与 annotation 判据重复计数）。

全部合成仓库 + 真引擎函数（bridge / relation_net 都是真调用，不是桩），不跑工具链。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PILOT / "engine"))

import audit_entry as ae  # noqa: E402
import oracle_bridge as ob  # noqa: E402
import relation_net as rn  # noqa: E402


# ---------------------------------------------------------------- 夹具

def _oracle(oid, confirmation="confirmed", target=None, left="final_amount", kind="ddl"):
    return {"id": oid, "confirmation": confirmation, "level": "L1_executable_constraint",
            "target": target if target is not None else {"table": "orders", "field": left},
            "source": {"kind": kind, "reference": "V1__x.sql:1", "text": f"{left} NOT NULL"},
            "assert": {"type": "field_not_null", "left": left}, "note": "", "severity": "HIGH"}


def _bridge_report(tmp_path, oracles, name="bridge.json"):
    """真跑桥（不是手搓 mapped），保证守卫吃的是真实转录结果。"""
    res = ob.bridge(oracles, [])
    p = tmp_path / name
    p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return p


def _harvested(tmp_path, oracles, name="harvested.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"oracles": oracles}, ensure_ascii=False), encoding="utf-8")
    return p


CLASS_DECL = {"jpa": {"com.x.entity.BanquetOrder": {"table": "orders",
                                                    "columns": [{"field": "finalAmount",
                                                                 "column": "final_amount"}]}},
              "ddl": {"tables": {"orders": {"columns": {"final_amount": {"type": "int"}}}}}}


def _fn(cls, method, *anns):
    return {"class": cls, "method": method,
            "annotations": [{"type": f"org.springframework.web.bind.annotation.{t}", "raw": r}
                            for t, r in anns]}


CONTROLLER = "com.x.controller.OrderController"


def _fn_decl(*funcs):
    return {"functions": list(funcs)}


# ---------------------------------------------------------------- A：确认门控

def test_derived_anchors_come_only_from_confirmed_oracles(tmp_path):
    oracles = [_oracle("ok-1"), _oracle("nc-1", "needs_confirmation"), _oracle("rf-1", "refuted")]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert [i["id"] for i in invs] == ["ok-1"]
    assert info["invariants"] == 1 and info["refused_unconfirmed"] == []
    assert all(i["oracle"]["confirmation"] == ob.CONFIRMED for i in invs)


def test_unconfirmed_leaked_into_mapped_is_refused_not_laundered(tmp_path):
    """精确复刻「桥的 confirmation 过滤回归」缺陷态：mapped 里混进 needs_confirmation。

    推导必须逐条回核采集器并**拒绝**，不得因为 to_invariants 硬写 CONFIRMED 就洗白。
    """
    oracles = [_oracle("ok-1"), _oracle("nc-1", "needs_confirmation")]
    bp = _bridge_report(tmp_path, oracles)
    res = json.loads(bp.read_text(encoding="utf-8"))
    leaked = ob.bridge([_oracle("nc-1", "confirmed")], [])["mapped"]
    assert leaked, "夹具自身必须真的产出一条 mapped，否则本守卫空转"
    res["mapped"] = res["mapped"] + leaked
    bp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    invs, info = ae.derive_invariant_anchors(bp, _harvested(tmp_path, oracles), CLASS_DECL)
    assert [i["id"] for i in invs] == ["ok-1"], "unconfirmed 被洗成 confirmed 进了锚表"
    assert info["refused_unconfirmed"] == ["nc-1"]


def test_refuted_oracle_never_reaches_anchor_table(tmp_path):
    oracles = [_oracle("rf-1", "refuted")]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert invs == [] and info["invariants"] == 0


# ---------------------------------------------------------------- A：容器绑定（检测力）

def test_table_resolving_to_unique_entity_binds_real_table(tmp_path):
    oracles = [_oracle("ok-1", target={"table": "orders", "field": "final_amount"})]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert invs[0]["entity"] == "BanquetOrder"
    assert info["entity_bound"] == 1 and info["no_container"] == 0
    ent = rn.entity_tables(CLASS_DECL)
    assert rn.anchor_tables(invs[0], ent) == ["orders"]
    ddl = CLASS_DECL["ddl"]["tables"]
    assert rn._covered_columns(ddl, ["orders"]) & set(rn.assert_fields(invs[0])), \
        "锚落到真表但字段覆盖为空 —— 检测力为 0，不许当成已绑定"


def test_dto_container_must_not_fabricate_a_table(tmp_path):
    """target.entity 是 DTO 名时写进去，消费者 snake_case 兜底会造出 create_order_request 这张假表。"""
    oracles = [_oracle("dto-1", target={"entity": "CreateOrderRequest", "field": "finalAmount"},
                       kind="annotation")]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert invs[0].get("entity", "") == "", "DTO 名被当成 JPA 实体写进锚表"
    assert info["dto_container_not_entity"] == 1 and info["no_container"] == 1
    assert rn.anchor_tables(invs[0], rn.entity_tables(CLASS_DECL)) == [], \
        "锚指向了不存在的表（伪造绑定比缺绑定更坏）"


def test_ambiguous_table_to_entity_is_not_guessed(tmp_path):
    cd = {"jpa": {"com.x.entity.A": {"table": "orders"}, "com.x.entity.B": {"table": "orders"}},
          "ddl": CLASS_DECL["ddl"]}
    oracles = [_oracle("ok-1", target={"table": "orders"})]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), cd)
    assert invs[0].get("entity", "") == "" and info["no_container"] == 1


def test_no_container_anchors_are_counted_not_hidden(tmp_path):
    oracles = [_oracle("ok-1", target={"table": "orders"}),
               _oracle("dto-1", target={"entity": "SomeRequest"}, kind="annotation"),
               _oracle("bare-1", target={"scope_bindable": False})]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert info["invariants"] == len(invs) == 3
    assert info["entity_bound"] == 1 and info["no_container"] == 2
    assert info["entity_bound"] + info["no_container"] == info["invariants"], "分类不守恒"
    assert "非人工业务声明" in info["note"] and info["human_declared"] is False


def test_when_applicability_domain_and_severity_are_preserved(tmp_path):
    """when 是适用域：丢掉它会把「这行不归我管」判成违反 = 制造假缺陷（桥模块自己的红线）。"""
    o = _oracle("ok-1")
    o["when"] = [{"field": "banquet_type", "op": "eq", "value": "WEDDING"}]
    o["severity"] = "HIGH"
    invs, _ = ae.derive_invariant_anchors(_bridge_report(tmp_path, [o]),
                                          _harvested(tmp_path, [o]), CLASS_DECL)
    assert invs[0]["when"] == o["when"], "适用域被丢弃"
    assert invs[0]["severity"] == "HIGH"


def test_anchor_never_points_at_table_absent_from_ddl(tmp_path):
    """jpa 可能声明了没有迁移的表（基类/遗留映射）。锚指向 DDL 不存在的表 = 伪造绑定，必须红。"""
    cd = {"jpa": {"com.x.entity.Ghost": {"table": "ghost_table"},
                  "com.x.entity.BanquetOrder": {"table": "orders"}},
          "ddl": {"tables": {"orders": {"columns": {"final_amount": {"type": "int"}}}}}}
    oracles = [_oracle("ghost-1", target={"table": "ghost_table"}),
               _oracle("ghost-2", target={"entity": "Ghost"}, kind="annotation"),
               _oracle("ok-1", target={"table": "orders"})]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), cd)
    ent = rn.entity_tables(cd)  # 消费者的 ent_map 不过滤 DDL，所以必须生产者自己挡住
    bound = [i for i in invs if rn.anchor_tables(i, ent)]
    assert bound, "本守卫必须真有锚落到表，否则「表都在 DDL 里」恒真（坑 2）"
    for i in invs:
        for t in rn.anchor_tables(i, ent):
            assert t in cd["ddl"]["tables"], f"锚指向 DDL 不存在的表: {i['id']} -> {t}"
    assert info["entity_bound"] == 1 and info["no_container"] == 2


def test_cross_field_claim_is_machine_visible_not_prose(tmp_path):
    """「单字段技术约束」必须是机器可核的计数，不是散文限定（坑 66）。"""
    oracles = [_oracle("ok-1"), _oracle("ok-2", left="deposit_cent")]
    oracles[1]["assert"] = {"type": "field_lte_field", "left": "deposit_cent", "right": "final_price_cent"}
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), CLASS_DECL)
    assert info["cross_field_assertions"] == 1 and info["single_field_assertions"] == 1
    assert info["cross_field_assertions"] + info["single_field_assertions"] == info["invariants"]
    assert "跨字段业务口径" in info["note"] and "仍需人工" in info["note"]


def test_missing_class_declaration_degrades_to_no_container_not_crash(tmp_path):
    oracles = [_oracle("ok-1", target={"table": "orders"})]
    invs, info = ae.derive_invariant_anchors(_bridge_report(tmp_path, oracles),
                                             _harvested(tmp_path, oracles), {})
    assert info["entity_bound"] == 0 and info["no_container"] == 1
    assert info["jpa_entities_available"] == 0


# ---------------------------------------------------------------- B：path 拼接

def test_join_entry_path_cases():
    cases = [(("", ""), "/"), (("/api/orders", ""), "/api/orders"),
             (("/api/orders", "/{id}"), "/api/orders/{id}"),
             (("/api/orders/", "/{id}"), "/api/orders/{id}"),
             (("/api/orders/", ""), "/api/orders/"),
             (("", "/{id}"), "/{id}"), (("/api/orders", "/{id}/status"), "/api/orders/{id}/status"),
             (("/api/orders", "/status/"), "/api/orders/status/"),
             (("/", "/x"), "/x")]
    # 只锁「首尾斜杠拼接」这一可证语义。注解值**内部**的双斜杠不归一化：
    # 没有证据表明 Spring 会归一化它，凭空断言就是编造期望（硬约束 5）。
    for (base, sub), want in cases:
        assert ae.join_entry_path(base, sub) == want, (base, sub, want, ae.join_entry_path(base, sub))


def test_annotation_paths_forms():
    assert ae.annotation_paths("") == []
    assert ae.annotation_paths('value=["/{id}"]') == ["/{id}"]
    assert ae.annotation_paths('value=["/a","/b"]') == ["/a", "/b"]
    assert ae.annotation_paths('"/{id}"') == ["/{id}"]
    assert ae.annotation_paths('path=["/a"]') == ["/a"]


def test_class_level_requestmapping_is_not_double_prefixed():
    """javap 把类级注解挂在该类某个方法记录上；当成方法级会拼出 /api/orders/api/orders/{id}。"""
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", "")),
                    _fn(CONTROLLER, "get", ("GetMapping", 'value=["/{id}"]')),
                    _fn(CONTROLLER, "delete", ("DeleteMapping", 'value=["/{id}"]'),
                        ("RequestMapping", 'value=["/api/orders"]'), ("Tag", 'name="订单"')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert set(doc["paths"]) == {"/api/orders", "/api/orders/{id}"}, doc["paths"]
    assert set(doc["paths"]["/api/orders/{id}"]) == {"get", "delete"}
    assert info["operations"] == 3 and info["ambiguous_classes"] == []


def test_multiple_paths_are_cartesian_not_first_only():
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", 'value=["/a","/b"]'),
                        ("RequestMapping", 'value=["/api/x","/api/y"]')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert set(doc["paths"]) == {"/api/x/a", "/api/x/b", "/api/y/a", "/api/y/b"}
    assert info["operations"] == 4


def test_empty_method_path_maps_to_class_base():
    decl = _fn_decl(_fn(CONTROLLER, "health", ("GetMapping", ""),
                        ("RequestMapping", 'value=["/api/health"]')))
    doc, _ = ae.synthesize_entry_contract(decl)
    assert set(doc["paths"]) == {"/api/health"}


def test_ambiguous_requestmapping_is_reported_not_guessed():
    decl = _fn_decl(_fn(CONTROLLER, "a", ("GetMapping", ""), ("RequestMapping", 'value=["/api/x"]')),
                    _fn(CONTROLLER, "b", ("GetMapping", ""), ("RequestMapping", 'value=["/api/y"]')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert info["ambiguous_classes"] and info["ambiguous_classes"][0]["class"] == CONTROLLER
    assert doc["paths"] == {}, "分不清类级基路径却仍造出了绑定"


def test_requestmapping_with_method_attribute_is_ambiguous():
    decl = _fn_decl(_fn(CONTROLLER, "a", ("GetMapping", ""),
                        ("RequestMapping", 'value=["/api/x"],method=[GET]')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert info["ambiguous_classes"] and doc["paths"] == {}


def test_controller_without_mapping_is_reported_not_filled_with_empty_binding():
    decl = _fn_decl(_fn(CONTROLLER, "helper", ("Transactional", "")))
    doc, info = ae.synthesize_entry_contract(decl)
    assert info["controllers_without_mapping"] == [CONTROLLER]
    assert doc["paths"] == {} and info["operations"] == 0


def test_path_collision_is_reported_not_silently_dropped():
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", "")),
                    _fn(CONTROLLER, "listAll", ("GetMapping", "")))
    doc, info = ae.synthesize_entry_contract(decl)
    assert info["path_collisions"] and info["path_collisions"][0]["path"] == "/"
    assert info["operations"] == 1, "碰撞后仍把被丢的操作计入操作数"


def test_non_controller_classes_are_not_invented_as_entries():
    decl = _fn_decl(_fn("com.x.service.OrderService", "create", ("PostMapping", 'value=["/x"]')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert doc["paths"] == {} and info["controllers"] == 0


# ---------------------------------------------------------------- B：消费者真吃（坑 79）

def test_synthesized_contract_is_consumed_by_relation_net_bind_entries():
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", "")),
                    _fn(CONTROLLER, "get", ("GetMapping", 'value=["/{id}"]')),
                    _fn(CONTROLLER, "create", ("PostMapping", "")),
                    _fn(CONTROLLER, "delete", ("DeleteMapping", 'value=["/{id}"]'),
                        ("RequestMapping", 'value=["/api/orders"]'), ("Tag", 'name="订单"')))
    doc, info = ae.synthesize_entry_contract(decl)
    nodes = {f"{CONTROLLER}#{m}": {"id": f"{CONTROLLER}#{m}", "type": "HTTP_ENTRY",
                                   "cls": CONTROLLER, "method": m}
             for m in ("list", "get", "create", "delete")}
    bindings, unbound = rn.bind_entries(doc, nodes)
    assert len(bindings) == info["operations"] == 4, (bindings, unbound)
    assert unbound == []
    assert bindings["GET /api/orders/{id}"] == f"{CONTROLLER}#get"


def test_synthesized_contract_carries_no_schema_and_is_not_harvested(tmp_path, monkeypatch):
    """合成契约只供入口绑定：喂给采集器会与 annotation 判据重复计数。"""
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", ""),
                        ("RequestMapping", 'value=["/api/orders"]')))
    doc, _ = ae.synthesize_entry_contract(decl)
    assert "components" not in doc
    seen = {}

    def _run(script, argv, out_path):
        seen[script] = argv
        return 1, "stub", None

    monkeypatch.setattr(ae, "run_engine", _run)
    d = {"docs": [], "migrations": [], "java_roots": [tmp_path], "test_roots": [],
         "openapi": None, "packages": set()}
    ae.pillar_oracle(d, tmp_path, "t")
    assert "--openapi" not in seen["oracle_harvest.py"], "合成契约被喂进判据采集（重复计数）"


# ---------------------------------------------------------------- 人工优先 + 降级声明

def _overrides(**kw):
    base = {"docs": None, "migrations": None, "java_roots": None, "test_roots": None,
            "openapi": None, "units": None, "edges": None, "relation_net": None, "rules": None,
            "layers": None, "members": None, "role_facts": None, "classes_dir": None,
            "invariants": None, "target": None, "entity_dir": None}
    base.update(kw)
    return base


def _stub_producer(monkeypatch, fn_decl, class_decl):
    def _run(script, argv, out_path, env=None):
        if out_path is None:
            return 0, "", None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        name = out_path.name
        data = {"fn_declaration.json": fn_decl, "file_declaration.json": {"files": []},
                "class_declaration.json": class_decl,
                "units.json": {"units": [], "counts": {"units": 0}}, "edges.json": {"edges": []},
                "relation_net.json": {"nodes": {}, "edges": [], "counts": {}},
                "members.json": {"members": []}}.get(name, {})
        out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return 0, "", data
    monkeypatch.setattr(ae, "run_producer", _run)


def _repo(tmp_path):
    repo = tmp_path / "sut"
    java = repo / "backend" / "src" / "main" / "java" / "com" / "x"
    (java / "entity").mkdir(parents=True)
    (java / "entity" / "T.java").write_text("package com.x.entity;\npublic class T {}\n", encoding="utf-8")
    mig = repo / "backend" / "src" / "main" / "resources" / "db" / "migration"
    mig.mkdir(parents=True)
    (mig / "V1__x.sql").write_text("CREATE TABLE orders (final_amount INT NOT NULL);\n", encoding="utf-8")
    cls = repo / "backend" / "target" / "classes" / "com" / "x"
    cls.mkdir(parents=True)
    (cls / "OrderController.class").write_bytes(b"\xca\xfe\xba\xbe stub")
    return repo


def _decls():
    decl = _fn_decl(_fn(CONTROLLER, "list", ("GetMapping", "")),
                    _fn(CONTROLLER, "delete", ("DeleteMapping", 'value=["/{id}"]'),
                        ("RequestMapping", 'value=["/api/orders"]')))
    return decl, CLASS_DECL


def test_manual_invariants_take_priority_over_derivation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    manual = tmp_path / "manual_invariants.json"
    manual.write_text(json.dumps({"invariants": [{"id": "HUMAN-1", "entity": "BanquetOrder",
                                                  "assert": {"type": "field_not_null",
                                                             "left": "final_amount"}}]},
                                 ensure_ascii=False), encoding="utf-8")
    oracles = [_oracle("ok-1")]
    bp, hp = _bridge_report(tmp_path, oracles), _harvested(tmp_path, oracles)

    def _boom(*a, **k):
        raise AssertionError("供了人工声明却仍跑推导")

    monkeypatch.setattr(ae, "derive_invariant_anchors", _boom)
    _, _, _, _, gaps, info = ae.produce_upstream(repo, ae.discover(repo), out,
                                                 _overrides(invariants=manual),
                                                 bridge_report=bp, harvested=hp)
    assert (out / "upstream" / "invariants.json").read_text(encoding="utf-8") == \
        manual.read_text(encoding="utf-8"), "人工声明被推导覆盖"
    assert info["invariant_anchors"]["source"] == "provided"
    assert info["invariant_anchors"]["human_declared"] is True
    assert not any("不变式" in g for g in gaps), gaps


def test_manual_openapi_takes_priority_over_synthesis(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    manual = tmp_path / "manual_openapi.json"
    manual.write_text(json.dumps({"openapi": "3.0.1", "paths": {"/human": {"get": {}}}}),
                      encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("供了人工 openapi 却仍跑注解合成")

    monkeypatch.setattr(ae, "synthesize_entry_contract", _boom)
    d = ae.discover(repo)
    d["openapi"] = manual
    _, _, _, _, gaps, info = ae.produce_upstream(repo, d, out, _overrides())
    assert (out / "upstream" / "openapi.json").read_text(encoding="utf-8") == \
        manual.read_text(encoding="utf-8"), "人工 openapi 被合成覆盖"
    assert info["entry_contract"]["source"] == "provided"
    assert not any("openapi" in g for g in gaps), gaps


def test_derivation_is_declared_as_downgrade_not_human_declaration(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    oracles = [_oracle("ok-1")]
    bp, hp = _bridge_report(tmp_path, oracles), _harvested(tmp_path, oracles)
    _, _, _, _, gaps, info = ae.produce_upstream(repo, ae.discover(repo), out, _overrides(),
                                                 bridge_report=bp, harvested=hp)
    inv_gap = [g for g in gaps if "不变式" in g]
    oa_gap = [g for g in gaps if "openapi" in g]
    assert len(inv_gap) == 1 and "推导" in inv_gap[0] and "非人工业务声明" in inv_gap[0], gaps
    assert "单字段技术约束" in inv_gap[0] and "跨字段业务口径仍需人工" in inv_gap[0], \
        "降级声明必须写明推导只覆盖单字段技术约束、跨字段业务口径仍是不可约人工输入"
    assert len(oa_gap) == 1 and "推导" in oa_gap[0], gaps
    assert info["invariant_anchors"]["human_declared"] is False
    assert info["entry_contract"]["source"] == "derived_from_mapping_annotations"
    doc = json.loads((out / "upstream" / "invariants.json").read_text(encoding="utf-8"))
    assert doc["derivation"]["derived"] is True and doc["derivation"]["human_declared"] is False
    assert "invariants" in doc, "形状必须是消费者读的 {'invariants': [...]}，不是裸列表"


def test_derived_gaps_keep_graph_pillars_incomplete(tmp_path, monkeypatch):
    """推导成功也不许把图支柱洗绿：推导 ≠ 人工确认（D8 / 硬约束 3）。"""
    p = ae.pillar(ae.GREEN, "零违反", [])
    q = ae._with_upstream_gaps(p, ["业务不变式锚表由已确认判据推导（1 条），非人工业务声明"])
    assert q["state"] == ae.INCOMPLETE


# ---------------------------------------------------------------- 反向：合法仓库不得误红

def test_repo_without_controllers_reports_gap_and_does_not_crash(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl = _fn_decl(_fn("com.x.service.OrderService", "create", ("Transactional", "")))
    _stub_producer(monkeypatch, decl, CLASS_DECL)
    _, _, _, _, gaps, info = ae.produce_upstream(repo, ae.discover(repo), out, _overrides())
    assert info["entry_contract"]["source"] == "unavailable"
    assert info["entry_contract"]["operations"] == 0
    assert any("openapi" in g and "无生产者" in g for g in gaps), gaps
    assert not (out / "upstream" / "openapi.json").exists(), "推不出却造了空契约充数"


def test_empty_bridge_reports_gap_and_writes_no_anchor_file(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    bp, hp = _bridge_report(tmp_path, []), _harvested(tmp_path, [])
    _, _, _, _, gaps, info = ae.produce_upstream(repo, ae.discover(repo), out, _overrides(),
                                                 bridge_report=bp, harvested=hp)
    assert any("不变式" in g and "人工声明" in g for g in gaps), gaps
    assert not (out / "upstream" / "invariants.json").exists(), "推不出却造了空锚表充数"
    assert info["invariant_anchors"]["invariants"] == 0


def test_no_bridge_report_keeps_original_irreducible_gap(tmp_path, monkeypatch):
    """推不出时（无桥报告 + 无 mapping 注解）原「无生产者」缺口文案必须保留。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    _stub_producer(monkeypatch, _fn_decl(), CLASS_DECL)
    _, _, _, _, gaps, _ = ae.produce_upstream(repo, ae.discover(repo), out, _overrides())
    assert any("openapi" in g and "无生产者" in g for g in gaps), gaps
    assert any("不变式" in g and "人工声明" in g and "无生产者" in g for g in gaps), gaps
    assert len(gaps) == 2, gaps


def test_legitimate_empty_path_and_trailing_slash_are_not_flagged_ambiguous():
    """反向假阳性探针：合法的空 path / 尾斜杠 / 多 path 不得被记成 ambiguous 或 collision。"""
    decl = _fn_decl(_fn(CONTROLLER, "root", ("GetMapping", ""),
                        ("RequestMapping", 'value=["/api/orders/"]')),
                    _fn(CONTROLLER, "slash", ("GetMapping", 'value=["/tail/"]')),
                    _fn(CONTROLLER, "multi", ("PostMapping", 'value=["/a","/b"]')))
    doc, info = ae.synthesize_entry_contract(decl)
    assert info["ambiguous_classes"] == [] and info["path_collisions"] == []
    assert info["controllers_without_mapping"] == []
    assert set(doc["paths"]) == {"/api/orders/", "/api/orders/tail/", "/api/orders/a", "/api/orders/b"}


# ---------------------------------------------------------------- 不合并 / 不冒充人工声明

def test_manual_and_derived_are_never_merged(tmp_path, monkeypatch):
    """两类判据人口不相交（跨字段业务口径 vs 单字段技术约束），合并会混淆口径。

    与 priority 守卫不同：这里**不**把 derive 打成炸弹，而是让它真跑 —— 合并才会被看见。
    """
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    manual = tmp_path / "manual_invariants.json"
    manual.write_text(json.dumps({"invariants": [{"id": "HUMAN-1", "entity": "BanquetOrder",
                                                  "assert": {"type": "field_lte_field",
                                                             "left": "deposit_cent",
                                                             "right": "final_price_cent"}}]},
                                 ensure_ascii=False), encoding="utf-8")
    oracles = [_oracle("ok-1")]
    bp, hp = _bridge_report(tmp_path, oracles), _harvested(tmp_path, oracles)
    ae.produce_upstream(repo, ae.discover(repo), out, _overrides(invariants=manual),
                        bridge_report=bp, harvested=hp)
    doc = json.loads((out / "upstream" / "invariants.json").read_text(encoding="utf-8"))
    ids = {i["id"] for i in doc["invariants"]}
    assert ids == {"HUMAN-1"}, f"人工声明被掺进推导锚: {ids}"
    assert "derivation" not in doc, "人工文件被掺进推导元数据"


def test_derived_anchors_never_claimed_as_human_business_declaration(tmp_path, monkeypatch):
    """端到端：推导锚不得被报成「人工业务不变式需求已满足」，缺口必须留在报告里。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    decl, cd = _decls()
    _stub_producer(monkeypatch, decl, cd)
    oracles = [_oracle("ok-1")]
    res = ob.bridge(oracles, [])

    def _engine(script, argv, out_path):
        if script == "oracle_harvest.py":
            data = {"oracles": oracles, "counts": {"by_source": {"ddl": 1}}}
        elif script == "oracle_bridge.py":
            data = res
        else:
            return 1, "stub：图引擎不在本守卫范围", None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return 0, "", data

    monkeypatch.setattr(ae, "run_engine", _engine)
    report = ae.audit(repo, out, _overrides())
    anchors = report["upstream"]["invariant_anchors"]
    assert anchors["source"] == "derived_from_confirmed_oracles"
    assert anchors["human_declared"] is False and anchors["derived"] is True
    gaps = report["upstream_gaps"]
    inv = [g for g in gaps if "不变式" in g]
    assert len(inv) == 1, f"人工业务不变式这条不可约缺口被推导抹掉了: {gaps}"
    assert "非人工业务声明" in inv[0] and "跨字段业务口径仍需人工" in inv[0]
    assert not any("已满足" in g or "已自动" in g or "无需人工" in g for g in gaps), \
        f"把推导冒充成人工需求已满足: {gaps}"
    for name in ("capability", "architecture", "emergence"):
        assert report["pillars"][name]["state"] == ae.INCOMPLETE, name
    assert report["exit_code"] == 2
    doc = json.loads((out / "upstream" / "invariants.json").read_text(encoding="utf-8"))
    assert doc["derivation"]["human_declared"] is False