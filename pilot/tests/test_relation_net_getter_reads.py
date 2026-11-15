"""W20 验证读通道契约守卫（坑 78 双向：缺陷态必须红，合法输入不得误红）。

锁的正确行为：
  · 默认关闭：build() 不传 field_access 时产物与关闭前逐字节一致（无新键、无新足迹）；
  · 只发射字节码 getfield 验证的字段读；计算型 getter 保分裂；别名 getter 按真实目标；
  · 名字推导一律不用（getAliased 读 other → 发射 setOther，绝不发射 setAliased）；
  · 继承链解析；非 JPA 类不发射（作用域）；
  · 读只进 source_fields，绝不进 sinks（读-读不报候选这条现有不变式必须保持）；
  · 资源键用 setter 等价形（与 sinks 同桶），写-读候选方向正确、provenance 分通道；
  · 缺输入 / 形状不合格 → CLI rc=1 且不写产物（宁缺勿猜）；
  · 开启时 modeling_domain 必须声明通道；关闭时不得有声明（声明漂移双向红）；
  · 单因子：既有候选裁决级字段不因新通道改变。

全部合成夹具，不碰真实台账、不跑工具链。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PILOT / "engine"))

import frame_check as fc  # noqa: E402
import relation_net as rn  # noqa: E402

FA = {"classes": {
    "com.x.FooEntity": {"super": "", "methods": {
        "getName": {"reads": ["name"], "writes": []},
        "getComputed": {"reads": [], "writes": []},
        "getAliased": {"reads": ["other"], "writes": []},
        "setName": {"reads": [], "writes": ["name"]},
    }},
    "com.x.BarEntity": {"super": "com.x.FooEntity", "methods": {}},
    # 非 JPA 类也在字节码映射里 —— 作用域闸门必须挡住它
    "com.x.Plain": {"super": "", "methods": {
        "getName": {"reads": ["name"], "writes": []},
    }},
}}

CD = {"jpa": {"FooEntity": {"table": "foos", "columns": [
    {"field": "name", "column": "name"}, {"field": "other", "column": "other"}]},
    "BarEntity": {"table": "bars", "columns": [{"field": "name", "column": "name"}]}},
    "ddl": {"tables": {"foos": {"columns": {"name": {}, "other": {}}}}}}


def _fn(calls_by_method: dict[str, list[str]]) -> dict:
    return {"functions": [
        {"class": "com.x.AService", "method": m, "modifiers": "public",
         "internal_calls": calls, "side_effects": [], "branch_count": 0, "is_pure": False}
        for m, calls in calls_by_method.items()]}


def _build(calls_by_method: dict[str, list[str]], field_access=FA["classes"]) -> dict:
    return rn.build(_fn(calls_by_method), CD, {}, [], surfaces=None, field_access=field_access)


def _taint(net: dict, method: str) -> dict:
    return net["nodes"][f"com.x.AService#{method}"]["taint"]


# ---------------------------------------------------------------- 默认关闭 / 作用域 / 验证

def test_default_off_is_byte_identical():
    calls = {"read": ["com.x.FooEntity.getName"], "write": ["com.x.FooEntity.setName"]}
    a = rn.build(_fn(calls), CD, {}, [], surfaces=None)
    b = rn.build(_fn(calls), CD, {}, [], surfaces=None, field_access=None)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "getter_read_resources" not in a["counts"]
    assert _taint(a, "read")["source_fields"] == [], "关闭时 getter 读不得入足迹"


def test_verified_read_emitted_as_read_channel():
    net = _build({"read": ["com.x.FooEntity.getName"]})
    t = _taint(net, "read")
    assert "com.x.FooEntity.setName" in t["source_fields"], "验证读必须发射(setter 等价键)"
    assert t["sinks"] == [], "读绝不进 sinks"
    assert net["counts"]["getter_read_resources"] == 1


def test_computed_getter_keeps_split():
    net = _build({"computed": ["com.x.FooEntity.getComputed"]})
    assert _taint(net, "computed")["source_fields"] == [], "无 getfield → 保分裂,不猜"
    assert net["counts"]["getter_read_resources"] == 0


def test_aliased_getter_uses_bytecode_not_name():
    """错误合并守卫:getAliased 实际读 other —— 发射 setOther,绝不发射 setAliased。"""
    net = _build({"aliased": ["com.x.FooEntity.getAliased"]})
    sf = _taint(net, "aliased")["source_fields"]
    assert "com.x.FooEntity.setOther" in sf
    assert "com.x.FooEntity.setAliased" not in sf, "名字推导造假字段身份"


def test_inheritance_resolved():
    net = _build({"inherited": ["com.x.BarEntity.getName"]})
    assert "com.x.BarEntity.setName" in _taint(net, "inherited")["source_fields"]


def test_non_entity_class_not_emitted():
    net = _build({"plain": ["com.x.Plain.getName"]})
    assert _taint(net, "plain")["source_fields"] == [], "非 JPA 类不在通道作用域"


def test_reads_never_enter_sinks():
    calls = {"read": ["com.x.FooEntity.getName"], "write": ["com.x.FooEntity.setName"]}
    off = _build(calls, field_access=None)
    on = _build(calls)
    for nid, node in on["nodes"].items():
        if "taint" not in node:
            continue
        assert node["taint"]["sinks"] == off["nodes"][nid]["taint"]["sinks"], \
            "开关通道不得改变任何细胞的写足迹"


# ---------------------------------------------------------------- frame_check 集成不变式

def _units(net: dict) -> dict:
    return {"units": [{"unit_key": k, "obligation_source_layer": "function"}
                      for k in net["nodes"] if ":" not in k]}


def test_read_read_overlap_reports_no_candidate():
    """现有不变式:读-读重叠不报候选 —— 新通道不得破坏。"""
    net = _build({"read": ["com.x.FooEntity.getName"], "read2": ["com.x.FooEntity.getName"]})
    # read2 改到 BService 以形成两个细胞
    net["nodes"]["com.x.BService#read2"] = net["nodes"].pop("com.x.AService#read2")
    net["nodes"]["com.x.BService#read2"].update(cls="com.x.BService", method="read2")
    rep = fc.run(net, _units(net), [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)
    assert rep["counts"]["overlapping_pairs"] == 1, "读-读应重叠(足迹可见)"
    assert rep["counts"]["pairs_candidate"] == 0, "读-读绝不报候选"


def test_write_read_candidate_direction_and_provenance():
    net = _build({"read": ["com.x.FooEntity.getName"], "write": ["com.x.FooEntity.setName"]})
    rep = fc.run(net, _units(net), [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)
    assert rep["counts"]["pairs_candidate"] == 1
    c = rep["candidates"][0]
    assert c["evidence_strength"] == "definite"
    r = c["resources"][0]
    assert r["resource"] == "com.x.FooEntity.setName"
    a, b = c["cells"]
    writer = "com.x.AService#write"
    reader = "com.x.AService#read"
    assert r["a_writes"] == (a == writer) and r["b_writes"] == (b == writer)
    prov_reader = r["provenance"][reader]
    prov_writer = r["provenance"][writer]
    assert all(p["source"] == "taint.source_fields" for p in prov_reader), "读通道 provenance"
    assert all(p["source"] == "taint.sinks" for p in prov_writer), "写通道 provenance"
    if b == writer:
        assert c["frame_violation"] == ["mod(B) ∩ fp(A) ≠ ∅"]
    else:
        assert c["frame_violation"] == ["mod(A) ∩ fp(B) ≠ ∅"]


def test_single_factor_existing_verdict_unchanged():
    """两个写者已有候选;加入读者后既有候选裁决级字段一字不变。"""
    calls = {"w1": ["com.x.FooEntity.setName"], "w2": ["com.x.FooEntity.setName"]}
    before = fc.run(_build(calls, field_access=None), _units(_build(calls, field_access=None)),
                    [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)
    calls["r1"] = ["com.x.FooEntity.getName"]
    net_on = _build(calls)
    after = fc.run(net_on, _units(net_on), [], [], None, {"REPO_OP"}, {"L0_business_rule"}, None)
    old = {tuple(c["cells"]): c for c in before["candidates"]}
    assert old, "夹具必须先有既有候选(非空洞)"
    for c in after["candidates"]:
        o = old.get(tuple(c["cells"]))
        if o:
            for k in ("frame_violation", "evidence_strength", "delta0", "survives_filter"):
                assert c[k] == o[k], f"既有候选 {c['cells']} 的 {k} 被新通道改变"
    assert after["counts"]["cycle_obstructions"] == before["counts"]["cycle_obstructions"] == 0


# ---------------------------------------------------------------- 输入拒绝 / 声明同步

def _cli(tmp_path: Path, fa_path: Path | None) -> subprocess.CompletedProcess:
    art = tmp_path / "art"
    art.mkdir(exist_ok=True)
    (art / "fn_declaration.json").write_text(json.dumps(
        _fn({"read": ["com.x.FooEntity.getName"]})), encoding="utf-8")
    (art / "class_declaration.json").write_text(json.dumps(CD), encoding="utf-8")
    (art / "openapi.json").write_text("{}", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    argv = [sys.executable, str(PILOT / "engine" / "relation_net.py"),
            "--artifacts-dir", str(art), "--project-dir", str(proj),
            "--out", str(tmp_path / "net.json")]
    if fa_path is not None:
        argv += ["--getter-reads", str(fa_path)]
    return subprocess.run(argv, capture_output=True, text=True)


def test_missing_input_refused_no_output(tmp_path):
    p = _cli(tmp_path, tmp_path / "nonexistent.json")
    assert p.returncode == 1, "缺输入必须拒绝出数"
    assert not (tmp_path / "net.json").exists(), "拒绝时不得写产物"
    assert "拒绝" in p.stdout


def test_malformed_input_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"classes": {"com.x.A": {"no_methods": True}}}), encoding="utf-8")
    p = _cli(tmp_path, bad)
    assert p.returncode == 1
    assert not (tmp_path / "net.json").exists()


def test_validate_field_access_rejects_bad_shapes():
    for bad in ({}, {"classes": {}}, {"classes": []}, [1, 2],
                {"classes": {"com.x.A": {"methods": []}}},
                {"classes": {"com.x.A": {}}}):
        try:
            rn.validate_field_access(bad)
        except ValueError:
            continue
        raise AssertionError(f"形状不合格却通过: {bad}")


def test_declaration_sync_both_directions(tmp_path):
    assert "footprint_channels" not in rn.modeling_domain_declaration(False)
    dom = rn.modeling_domain_declaration(True)
    assert "entity_getter_reads" in dom["footprint_channels"], "开启必须声明通道"
    fa = tmp_path / "fa.json"
    fa.write_text(json.dumps(FA), encoding="utf-8")
    p_on = _cli(tmp_path, fa)
    assert p_on.returncode == 0
    on = json.loads((tmp_path / "net.json").read_text())
    assert "footprint_channels" in on["modeling_domain"]
    assert on["field_access_input"]["sha256"].startswith("sha256:")
    p_off = _cli(tmp_path, None)
    assert p_off.returncode == 0
    off = json.loads((tmp_path / "net.json").read_text())
    assert "footprint_channels" not in off["modeling_domain"], "关闭不得残留声明"
    assert "field_access_input" not in off


# ---------------------------------------------------------------- 检测力限定（坑66 纪律，V19 残留2）

def test_declaration_carries_detection_power_qualification():
    """firing-capable 措辞必须带限定:不是已检出违规 + 当前全是 consistent。
    锚定否定词与现状短语本身(坑102:子串/计数守卫锁不住极性)。"""
    note = rn.modeling_domain_declaration(True)["footprint_channels"][
        "entity_getter_reads"]["detection_power_note"]
    assert "firing-capable" in note
    assert "不是已检出违规" in note, "丢掉否定限定: firing-capable 会被读成已检出"
    assert "当前全是 consistent" in note, "丢掉现状限定: 读者不知道桶当前状态"
    assert "obstructions" in note


def test_cli_qualification_present_on_absent_off(tmp_path):
    fa = tmp_path / "fa.json"
    fa.write_text(json.dumps(FA), encoding="utf-8")
    on = _cli(tmp_path, fa)
    assert on.returncode == 0
    assert "firing-capable ≠ 已检出违规" in on.stdout, "CLI 开启时必须并排报限定"
    assert "当前全是 consistent" in on.stdout
    off = _cli(tmp_path, None)
    assert off.returncode == 0
    assert "getter 读通道" not in off.stdout, "关闭时不得出现通道措辞"