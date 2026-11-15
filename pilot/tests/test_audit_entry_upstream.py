"""audit_entry 上游接线的契约守卫（W19）。

锁的正确行为：
  · 缺图产物时驱动生产者，产物只落 --out，**绝不写被测仓库**；
  · 缺字节码 → 显式缺口且**绝不编译**（禁 maven/javac）；
  · 不可约人工输入（openapi 无生产者 / invariants 人工声明）显式列为需供给，不静默跳过；
  · 自动派生的 backend 必须是**含 src/main/java 的模块根**（给错会让自调用边 61→2）；
  · 已供给图产物时**不驱动生产者**（默认行为不变）；
  · 上游缺口让图支柱保持 INCOMPLETE；无缺口时不改状态（反向）。

全部合成仓库 + 桩生产者：不跑 javap/maven/docker，不碰真实台账。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PILOT / "engine"))

import audit_entry as ae  # noqa: E402


def _repo(tmp_path: Path, with_classes: bool = True) -> Path:
    repo = tmp_path / "sut"
    java = repo / "backend" / "src" / "main" / "java" / "com" / "x"
    java.mkdir(parents=True)
    (java / "AService.java").write_text("package com.x;\npublic class AService {}\n", encoding="utf-8")
    mig = repo / "backend" / "src" / "main" / "resources" / "db" / "migration"
    mig.mkdir(parents=True)
    (mig / "V1__x.sql").write_text("CREATE TABLE t (\n  c INT NOT NULL\n);\n", encoding="utf-8")
    ent = java / "entity"
    ent.mkdir()
    (ent / "T.java").write_text("package com.x.entity;\npublic class T {}\n", encoding="utf-8")
    if with_classes:
        cls = repo / "backend" / "target" / "classes" / "com" / "x"
        cls.mkdir(parents=True)
        (cls / "AService.class").write_bytes(b"\xca\xfe\xba\xbe stub")
    return repo


def _fingerprint(repo: Path) -> dict:
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*")) if p.is_file()}


def _stub_producer(monkeypatch, calls, fail=()):
    def _run(script, argv, out_path, env=None):
        calls.append({"script": script.name, "argv": argv, "out": str(out_path), "env": env})
        if script.name in fail:
            return 1, "stub failure", None
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            name = out_path.name
            if name == "units.json":
                data = {"units": [{"unit_key": "com.x.AService#m"}], "counts": {"units": 1}}
            elif name == "edges.json":
                data = {"edges": []}
            elif name == "relation_net.json":
                data = {"nodes": {}, "edges": [], "counts": {}}
            elif name == "members.json":
                data = {"members": [{"class": "com.x.AService", "method": "m", "modifiers": "public"}]}
            else:
                data = {"functions": [], "files": [], "ddl": {"tables": {}, "indexes": []}, "jpa": {}}
            out_path.write_text(json.dumps(data), encoding="utf-8")
            return 0, "", data
        return 0, "", None
    monkeypatch.setattr(ae, "run_producer", _run)


def _overrides(**kw):
    base = {"docs": None, "migrations": None, "java_roots": None, "test_roots": None,
            "openapi": None, "units": None, "edges": None, "relation_net": None, "rules": None,
            "layers": None, "members": None, "role_facts": None, "classes_dir": None,
            "invariants": None, "target": None, "entity_dir": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 驱动与只读

def test_missing_artifacts_drive_producers_into_out_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = _fingerprint(repo)
    out = tmp_path / "out"
    out.mkdir()
    calls = []
    _stub_producer(monkeypatch, calls)
    d = ae.discover(repo)
    units, edges, net, members, gaps, info = ae.produce_upstream(repo, d, out, _overrides())
    assert units and edges and net and members
    assert all(str(p).startswith(str(out)) for p in (units, edges, net, members))
    assert _fingerprint(repo) == before, "上游生产者写进了被测仓库"
    names = [c["script"] for c in calls]
    for expected in ("fn_static.py", "file_static.py", "class_static.py",
                     "build_agent_graph.py", "relation_net.py", "member_declaration.py"):
        assert expected in names, f"未驱动 {expected}"
    assert info["binding_source"] == "auto_derived"
    # 合成仓库没有 openapi/不变式，这两条不可约缺口**必须**报；除此之外不许有别的缺口。
    assert len(gaps) == 2, gaps
    assert any("openapi" in g for g in gaps) and any("不变式" in g for g in gaps)


def test_extractors_are_called_in_reuse_mode_never_compile(tmp_path, monkeypatch):
    """抽取器必须带 --classes-dir（复用字节码），绝不触发编译路径。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    calls = []
    _stub_producer(monkeypatch, calls)
    ae.produce_upstream(repo, ae.discover(repo), out, _overrides())
    for c in calls:
        if c["script"] in ("fn_static.py", "file_static.py"):
            assert "--classes-dir" in c["argv"], c
            i = c["argv"].index("--classes-dir")
            assert Path(c["argv"][i + 1]).is_dir()


def test_missing_bytecode_reports_gap_and_never_compiles(tmp_path, monkeypatch):
    repo = _repo(tmp_path, with_classes=False)
    out = tmp_path / "out"
    out.mkdir()
    calls = []
    _stub_producer(monkeypatch, calls)
    units, edges, net, members, gaps, _ = ae.produce_upstream(
        repo, ae.discover(repo), out, _overrides())
    assert (units, edges, net, members) == (None, None, None, None)
    assert calls == [], "缺字节码却仍驱动了生产者（可能触发编译）"
    assert any("--classes-dir" in g and "不编译" in g for g in gaps)


def test_irreducible_inputs_are_listed_not_silently_skipped(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _stub_producer(monkeypatch, [])
    _, _, _, _, gaps, _ = ae.produce_upstream(repo, ae.discover(repo), out, _overrides())
    assert any("openapi" in g and "无生产者" in g for g in gaps), gaps
    assert any("不变式" in g and "人工声明" in g for g in gaps), gaps


def test_supplied_irreducible_inputs_clear_their_gaps(tmp_path, monkeypatch):
    """反向：供给了 openapi 与 invariants 就不该再报这两条缺口（不许误报）。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    oa = tmp_path / "openapi.json"
    oa.write_text(json.dumps({"paths": {}}), encoding="utf-8")
    inv = tmp_path / "invariants.json"
    inv.write_text(json.dumps({"invariants": []}), encoding="utf-8")
    _stub_producer(monkeypatch, [])
    d = ae.discover(repo)
    d["openapi"] = oa
    _, _, _, _, gaps, _ = ae.produce_upstream(repo, d, out, _overrides(invariants=inv))
    assert not any("openapi" in g for g in gaps), gaps
    assert not any("不变式" in g for g in gaps), gaps
    assert (out / "upstream" / "openapi.json").is_file()
    assert (out / "upstream" / "invariants.json").is_file()


def test_derived_backend_is_module_root_not_repo_root(tmp_path):
    """实测缺陷：backend 给成仓库根 → file 字段全 null → 自调用边 61 塌到 2。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    binding = ae.minimal_binding(repo, ae.discover(repo), out, None)
    doc = json.loads(binding.read_text(encoding="utf-8"))
    assert Path(doc["paths"]["backend"]) == repo / "backend"
    assert Path(doc["source_roots"]["java"]) == repo / "backend" / "src" / "main" / "java"
    assert doc["paths"]["java_package"] == "com/x"
    assert doc["source_roots"]["java_package"] == "com/x"


def test_producer_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _stub_producer(monkeypatch, [], fail=("fn_static.py",))
    units, edges, net, members, gaps, _ = ae.produce_upstream(
        repo, ae.discover(repo), out, _overrides())
    assert (units, edges, net, members) == (None, None, None, None)
    assert any("fn_static.py" in g and "rc=1" in g for g in gaps)


# ---------------------------------------------------------------- 默认行为不变 / 缺口传导

def test_supplied_artifacts_bypass_producers(tmp_path, monkeypatch):
    """已供给 units/edges/net 时不驱动任何生产者（既有行为一字不变）。"""
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    calls = []
    _stub_producer(monkeypatch, calls)
    supplied = {}
    for name in ("units.json", "edges.json", "relation_net.json"):
        p = tmp_path / name
        p.write_text(json.dumps({"units": [], "edges": [], "nodes": {}}), encoding="utf-8")
        supplied[name] = p
    monkeypatch.setattr(ae, "pillar_oracle", lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
    monkeypatch.setattr(ae, "pillar_capability", lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
    monkeypatch.setattr(ae, "pillar_arch", lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
    monkeypatch.setattr(ae, "pillar_emergence", lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
    report = ae.audit(repo, out, _overrides(units=supplied["units.json"], edges=supplied["edges.json"],
                                            relation_net=supplied["relation_net.json"]))
    assert calls == [], "供给了产物却仍驱动生产者"
    assert report["upstream"]["attempted"] is False
    assert report["upstream_gaps"] == []
    assert report["exit_code"] == 0


def test_upstream_gaps_keep_graph_pillars_incomplete():
    p = ae.pillar(ae.GREEN, "零违反", [])
    q = ae._with_upstream_gaps(p, ["openapi 无生产者"])
    assert q["state"] == ae.INCOMPLETE
    assert "openapi 无生产者" in q["reason"] and q["upstream_gaps"] == ["openapi 无生产者"]


def test_red_pillar_is_not_downgraded_by_upstream_gaps():
    """已判红的支柱不许被上游缺口降级成 INCOMPLETE（红是更强的结论）。"""
    p = ae.pillar(ae.RED, "确定违反 2", [])
    q = ae._with_upstream_gaps(p, ["缺 invariants"])
    assert q["state"] == ae.RED


def test_no_gaps_leaves_state_untouched():
    """反向：无缺口时不得改动状态或文案。"""
    p = ae.pillar(ae.GREEN, "零违反零 unknown", [])
    q = ae._with_upstream_gaps(p, [])
    assert q["state"] == ae.GREEN and q["reason"] == "零违反零 unknown"
    assert "upstream_gaps" not in q


def test_out_guard_rejects_repo_and_symlink_escape(tmp_path, monkeypatch):
    """只读被测仓库的守卫必须继续挡住仓库内与符号链接逃逸 —— 真调 main() 取退出码。"""
    repo = _repo(tmp_path)
    link = tmp_path / "escape"
    link.symlink_to(repo, target_is_directory=True)
    for target in (repo / "audit_out", link / "audit_out"):
        monkeypatch.setattr(sys, "argv",
                            ["audit_entry.py", "--repo", str(repo), "--out", str(target)])
        assert ae.main() == 2, f"未拒绝落在被测仓库内的 --out: {target}"
        assert not (repo / "audit_out").exists(), "拒绝之前已经写进了被测仓库"