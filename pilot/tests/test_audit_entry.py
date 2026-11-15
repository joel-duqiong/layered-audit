"""audit_entry 的契约守卫：零配置发现、三态、退出码、缺输入不静默跳过、只读。

全部用合成仓库与 monkeypatch 的引擎返回，不碰真实台账、不跑流水线、不写数据库。
守卫锁的是正确行为（D8：unknown/缺输入不得变 GREEN，INCOMPLETE 不得当 0），
不是引擎当前输出的字符串（LESSONS 坑 2）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import audit_entry as ae


# ---------------------------------------------------------------- 工具

def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fake_engine(monkeypatch, table):
    def _run(script, argv, out_path):
        rc, err, data = table[script]
        if out_path is not None and data is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data), encoding="utf-8")
        return rc, err, data
    monkeypatch.setattr(ae, "run_engine", _run)


def _disc(**kw):
    base = {"docs": [], "migrations": [], "java_roots": [], "test_roots": [],
            "openapi": None, "packages": set()}
    base.update(kw)
    return base


def _counts(**kw):
    base = {"numerator": 0, "denominator": 0, "unmappable": 0, "excluded": 0,
            "oracles_implemented": 0, "oracles_total": 0, "unverified_edges": 0,
            "oracles_unlinked": 0, "field_write_edges_enforcement_unknown": 0, "unknown": 0,
            "findings": 0, "declared_type_findings": 0, "evaluations": 0,
            "unknown_evaluations": 0, "declared_type_unknown_evaluations": 0, "rules_unmatched": 0,
            "pairs_decidable": 0, "pairs_total": 0, "pairs_unknown": 0, "cells_footprint_unknown": 0,
            "cycle_obstructions": 0, "candidates_delta0": {"consistent": 0, "unknown": 0}}
    base.update(kw)
    return base


def _fingerprint(repo: Path) -> dict:
    """仓库内容指纹：相对路径 -> sha256。文件集与内容任一变化都会被捕获。"""
    return {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- 自动发现

class TestDiscovery:
    def test_finds_java_migration_docs_openapi(self, tmp_path):
        repo = tmp_path / "repo"
        _touch(repo / "backend/src/main/java/com/x/Foo.java", "class Foo {}")
        _touch(repo / "backend/src/main/resources/db/migration/V1__init.sql", "CREATE TABLE t (id INT);")
        _touch(repo / "docs/prd.md", "# PRD")
        _touch(repo / "README.md", "# readme")
        _touch(repo / "openapi.json", "{}")
        d = ae.discover(repo)
        assert d["java_roots"] == [repo / "backend/src/main/java"]
        assert d["migrations"] == [repo / "backend/src/main/resources/db/migration/V1__init.sql"]
        assert repo / "docs/prd.md" in d["docs"] and repo / "README.md" in d["docs"]
        assert d["openapi"] == repo / "openapi.json"

    def test_prunes_node_modules_and_target(self, tmp_path):
        repo = tmp_path / "repo"
        _touch(repo / "node_modules/src/main/java/Evil.java", "class Evil {}")
        _touch(repo / "node_modules/openapi.json", "{}")
        _touch(repo / "backend/target/src/main/java/Gen.java", "class Gen {}")
        _touch(repo / "backend/src/main/java/Real.java", "class Real {}")
        d = ae.discover(repo)
        assert d["java_roots"] == [repo / "backend/src/main/java"]
        assert d["openapi"] is None

    def test_empty_repo_no_crash(self, tmp_path):
        d = ae.discover(tmp_path)
        assert d["java_roots"] == [] and d["docs"] == [] and d["migrations"] == []
        assert d["openapi"] is None and d["packages"] == set()

    def test_detects_layered_packages(self, tmp_path):
        repo = tmp_path / "repo"
        for role in ("controller", "service", "repository"):
            _touch(repo / f"src/main/java/com/x/{role}/C.java", "class C {}")
        d = ae.discover(repo)
        assert {"controller", "service", "repository"} <= d["packages"]


# ---------------------------------------------------------------- 默认架构规则

class TestDefaultArchConfig:
    def test_no_layout_returns_none(self):
        assert ae.default_arch_config(set(), False) is None

    def test_layout_builds_unconfirmed_convention_rules(self):
        cfg = ae.default_arch_config({"controller", "service", "repository"}, False)
        assert cfg["schema"] == "arch-rules/1"
        ids = {r["id"] for r in cfg["rules"]}
        assert {"ARCH-D1", "ARCH-D2"} <= ids
        for r in cfg["rules"]:
            assert r["source"]["confirmed_by"] == ""
        assert any(r["source"]["kind"] == "convention" for r in cfg["rules"])

    def test_visibility_only_with_members(self):
        without = ae.default_arch_config({"controller", "service", "repository"}, False)
        with_m = ae.default_arch_config({"controller", "service", "repository"}, True)
        assert not any(r["type"] == "visibility" for r in without["rules"])
        assert any(r["type"] == "visibility" for r in with_m["rules"])

    def test_mapper_rule_only_when_mapper_present(self):
        cfg = ae.default_arch_config({"controller", "service", "repository", "mapper"}, False)
        assert any(r["id"] == "ARCH-D3" for r in cfg["rules"])
        cfg2 = ae.default_arch_config({"controller", "service", "repository"}, False)
        assert not any(r["id"] == "ARCH-D3" for r in cfg2["rules"])


# ---------------------------------------------------------------- 退出码三态

class TestExitCode:
    def test_all_green_is_zero(self):
        assert ae.aggregate_exit([ae.GREEN] * 4) == 0

    def test_any_red_is_one(self):
        assert ae.aggregate_exit([ae.GREEN, ae.RED, ae.INCOMPLETE]) == 1

    def test_incomplete_without_red_is_two(self):
        assert ae.aggregate_exit([ae.GREEN, ae.INCOMPLETE]) == 2

    def test_incomplete_never_zero(self):
        assert ae.aggregate_exit([ae.INCOMPLETE]) != 0
        assert ae.aggregate_exit([ae.GREEN, ae.INCOMPLETE]) != 0

    def test_empty_states_is_zero(self):
        assert ae.aggregate_exit([]) == 0


# ---------------------------------------------------------------- oracle 支柱

class TestOraclePillar:
    def test_no_input_incomplete_not_skip(self, tmp_path):
        p = ae.pillar_oracle(_disc(), tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "无可采集输入" in p["reason"]

    def test_all_executable_is_green(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        oa = _touch(tmp_path / "o.json", "{}")
        _fake_engine(monkeypatch, {
            "oracle_harvest.py": (0, "", {"oracles": [], "counts": {"by_source": {"document": 2, "openapi": 1}}}),
            "oracle_bridge.py": (0, "", {"counts": _counts(numerator=3, denominator=3),
                                         "untranscribed_count": 0})})
        d = _disc(docs=[doc], openapi=oa)
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.GREEN and p["numerator"] == 3

    def test_unmappable_is_incomplete(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        oa = _touch(tmp_path / "o.json", "{}")
        _fake_engine(monkeypatch, {
            "oracle_harvest.py": (0, "", {"oracles": [], "counts": {"by_source": {"document": 2, "openapi": 1}}}),
            "oracle_bridge.py": (0, "", {"counts": _counts(numerator=1, denominator=3, unmappable=2),
                                         "untranscribed_count": 0})})
        d = _disc(docs=[doc], openapi=oa)
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "不可映射" in p["reason"]

    def test_engine_failure_not_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {
            "oracle_harvest.py": (0, "", {"oracles": []}),
            "oracle_bridge.py": (1, "boom", None)})
        d = _disc(docs=[Path("prd.md")])
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "boom" in p["reason"]


# ---------------------------------------------------------------- capability 支柱

class TestCapabilityPillar:
    def test_missing_units_incomplete(self, tmp_path):
        p = ae.pillar_capability(_disc(java_roots=[Path("src")]), tmp_path, None, Path("net"), Path("h"))
        assert p["state"] == ae.INCOMPLETE and "units.json" in p["reason"]

    def test_unverified_edges_is_red(self, tmp_path, monkeypatch):
        h = _touch(tmp_path / "h.json", "{}")
        _fake_engine(monkeypatch, {"capability_map.py": (0, "", {"counts": _counts(
            unverified_edges=3, oracles_implemented=1, oracles_total=2)})})
        p = ae.pillar_capability(_disc(java_roots=[Path("src")]), tmp_path, Path("u"), Path("net"), h)
        assert p["state"] == ae.RED

    def test_unlinked_is_incomplete_not_green(self, tmp_path, monkeypatch):
        h = _touch(tmp_path / "h.json", "{}")
        _fake_engine(monkeypatch, {"capability_map.py": (0, "", {"counts": _counts(
            oracles_unlinked=4, oracles_implemented=1, oracles_total=5)})})
        p = ae.pillar_capability(_disc(java_roots=[Path("src")]), tmp_path, Path("u"), Path("net"), h)
        assert p["state"] == ae.INCOMPLETE

    def test_complete_is_green(self, tmp_path, monkeypatch):
        h = _touch(tmp_path / "h.json", "{}")
        _fake_engine(monkeypatch, {"capability_map.py": (0, "", {"counts": _counts(
            oracles_implemented=5, oracles_total=5)})})
        p = ae.pillar_capability(_disc(java_roots=[Path("src")], test_roots=[Path("test")]),
                                 tmp_path, Path("u"), Path("net"), h)
        assert p["state"] == ae.GREEN


# ---------------------------------------------------------------- architecture 支柱

class TestArchPillar:
    def _args(self, tmp_path):
        return (_disc(java_roots=[Path("src")]), tmp_path, Path("u"), Path("e"),
                Path("rules"), Path("layers"), None, None)

    def test_missing_units_incomplete(self, tmp_path):
        p = ae.pillar_arch(_disc(), tmp_path, None, Path("e"), Path("r"), Path("l"), None, None)
        assert p["state"] == ae.INCOMPLETE and "units.json" in p["reason"]

    def test_findings_is_red(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", {"counts": _counts(
            findings=2, evaluations=10), "input_warnings": []})})
        p = ae.pillar_arch(*self._args(tmp_path))
        assert p["state"] == ae.RED

    def test_unknown_is_incomplete_not_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", {"counts": _counts(
            findings=0, unknown_evaluations=5, evaluations=10), "input_warnings": []})})
        p = ae.pillar_arch(*self._args(tmp_path))
        assert p["state"] == ae.INCOMPLETE

    def test_clean_is_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", {"counts": _counts(
            findings=0, evaluations=10), "input_warnings": []})})
        p = ae.pillar_arch(*self._args(tmp_path))
        assert p["state"] == ae.GREEN

    def test_engine_failure_not_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (1, "refused", None)})
        p = ae.pillar_arch(*self._args(tmp_path))
        assert p["state"] == ae.INCOMPLETE and "refused" in p["reason"]

    def test_embedded_default_without_members_flags_visibility_gap(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", {"counts": _counts(
            findings=0, evaluations=10), "input_warnings": []})})
        d = _disc(java_roots=[Path("src")], packages={"controller", "service", "repository"})
        p = ae.pillar_arch(d, tmp_path, Path("u"), Path("e"), None, Path("l"), None, None)
        assert p["rules_source"] == "embedded_generic_convention"
        assert p["rules_confirmed"] is False
        assert p["state"] == ae.INCOMPLETE and p["visibility_unchecked"] is True

    def test_no_layout_no_rules_incomplete(self, tmp_path):
        p = ae.pillar_arch(_disc(), tmp_path, Path("u"), Path("e"), None, Path("l"), None, None)
        assert p["state"] == ae.INCOMPLETE and "分层" in p["reason"]


# ---------------------------------------------------------------- emergence 支柱

class TestEmergencePillar:
    def _net(self, tmp_path):
        return _touch(tmp_path / "net.json", json.dumps({"nodes": {}}))

    def test_missing_net_incomplete(self, tmp_path):
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), None, None)
        assert p["state"] == ae.INCOMPLETE and "relation_net.json" in p["reason"]

    def test_overlap_unknown_is_incomplete_not_red(self, tmp_path, monkeypatch):
        # jiuyan 实测形态：确定足迹重叠(旁条件失效)但 δ⁰ 全 unknown、零环障碍。
        # 这是待查线索，不是确定缺陷 —— 三态纪律对称面：unknown 不得变 RED/「确定违反」。
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10,
                              candidates_delta0={"consistent": 0, "unknown": 1}),
            "candidates": [{"frame_violation": ["mod(A) ∩ fp(B) ≠ ∅"],
                            "evidence_strength": "definite", "delta0": "unknown"}]})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.INCOMPLETE
        assert not _claims_definite_violation(p["reason"])
        assert "已证实不一致 0 / 一致性未知 1" in p["reason"]

    def test_proven_obstruction_is_red(self, tmp_path, monkeypatch):
        # 唯一可判 RED 的通道：环一致性障碍(约束集不相交)=已证实不一致。
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10, cycle_obstructions=2,
                              candidates_delta0={"consistent": 0, "unknown": 1}),
            "candidates": [{"frame_violation": ["x"], "evidence_strength": "definite"}]})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.RED
        assert "已证实不一致 2 / 一致性未知 1" in p["reason"]

    def test_headline_side_by_side_and_no_defect_claim_when_zero_proven(self, tmp_path, monkeypatch):
        # 头条必须并排报「已证实不一致 N / 一致性未知 M」；0 已证实时不得出现「确定违反」。
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=8385, pairs_total=148785,
                              candidates_delta0={"consistent": 0, "unknown": 9}),
            "candidates": [{"frame_violation": ["x"], "evidence_strength": "definite"}] * 9})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert "已证实不一致 0 / 一致性未知 9" in p["reason"]
        assert not _claims_definite_violation(p["reason"])
        assert p["state"] == ae.INCOMPLETE
        assert p["proven_inconsistent"] == 0 and p["delta0_unknown"] == 9
        assert p["side_condition_leads"] == 9

    def test_zero_proven_many_unknown_exit_two_not_one(self, tmp_path, monkeypatch):
        # 0 已证实 + 若干 unknown → 支柱 INCOMPLETE，聚合退出码 2(没查完)，绝不是 1(有红)。
        # 必须供给 units/net，否则 pillar_emergence 走缺输入早退，测不到候选判定逻辑（坑2）。
        for name in ("pillar_oracle", "pillar_capability", "pillar_arch"):
            monkeypatch.setattr(ae, name, lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
        units = _touch(tmp_path / "units.json", json.dumps({"units": []}))
        net = _touch(tmp_path / "net.json", json.dumps({"nodes": {}}))
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10,
                              candidates_delta0={"consistent": 0, "unknown": 9}),
            "candidates": [{"frame_violation": ["x"], "evidence_strength": "definite"}] * 9})})
        report = ae.audit(tmp_path, tmp_path / "out", {"units": units, "relation_net": net})
        assert report["pillars"]["emergence"]["state"] == ae.INCOMPLETE
        assert report["exit_code"] == 2

    def test_may_write_unknown_not_red(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10),
            "candidates": [{"frame_violation": [], "evidence_strength": "may_write_unknown"}]})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.INCOMPLETE

    def test_unknown_pairs_not_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10, pairs_unknown=3),
            "candidates": []})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.INCOMPLETE

    def test_clean_is_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=5), "candidates": []})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.GREEN

    def test_business_channel_incomplete_not_green(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=5), "candidates": [],
            "variety_deficit": {"business_oracle_channel": "INCOMPLETE"}})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), self._net(tmp_path), None)
        assert p["state"] == ae.INCOMPLETE and "未测量" in p["reason"]

    def test_plumbing_detection_handles_dict_nodes(self, tmp_path, monkeypatch):
        net = _touch(tmp_path / "net.json", json.dumps(
            {"nodes": {"n1": {"type": "REPO_OP"}}}))
        seen = {}

        def _run(script, argv, out_path):
            seen["argv"] = argv
            return 0, "", {"counts": _counts(pairs_decidable=1, pairs_total=1), "candidates": []}
        monkeypatch.setattr(ae, "run_engine", _run)
        ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
        assert "REPO_OP" in seen["argv"]


# ---------------------------------------------------------------- 端到端（真实引擎，合成仓库）

class TestEndToEnd:
    def test_empty_repo_exit_two_all_incomplete(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"
        report = ae.audit(repo, out, {})
        assert report["exit_code"] == 2
        assert all(p["state"] == ae.INCOMPLETE for p in report["pillars"].values())

    def test_read_only_repo_unchanged(self, tmp_path):
        repo = tmp_path / "repo"
        _touch(repo / "backend/src/main/java/com/x/Foo.java",
               "import jakarta.validation.constraints.NotNull; class Foo { @NotNull String name; }")
        _touch(repo / "backend/src/main/resources/db/migration/V1__init.sql",
               "CREATE TABLE t (id INT NOT NULL);")
        _touch(repo / "docs/prd.md", "# PRD\n- name 必填\n")
        before = _fingerprint(repo)
        out = tmp_path / "out"
        report = ae.audit(repo, out, {})
        after = _fingerprint(repo)
        assert before == after, "被测仓库被写入（文件集或内容指纹变化）"
        assert report["exit_code"] in (0, 1, 2)
        assert report["pillars"]["oracle"]["state"] in (ae.GREEN, ae.RED, ae.INCOMPLETE)
        assert report["pillars"]["capability"]["state"] == ae.INCOMPLETE

    def test_main_exit_code_empty_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"
        proc = subprocess.run([sys.executable, str(Path(ae.__file__)),
                               "--repo", str(repo), "--out", str(out)],
                              capture_output=True, text=True)
        assert proc.returncode == 2
        assert (out / "audit_report.json").is_file()

    def test_main_bad_repo_path_exit_two(self, tmp_path):
        proc = subprocess.run([sys.executable, str(Path(ae.__file__)),
                               "--repo", str(tmp_path / "nope"), "--out", str(tmp_path / "o")],
                              capture_output=True, text=True)
        assert proc.returncode == 2


# ---------------------------------------------------------------- 聚合层守卫

class TestAggregation:
    def test_incomplete_pillar_forces_exit_two(self, tmp_path, monkeypatch):
        for name in ("pillar_oracle", "pillar_capability", "pillar_arch", "pillar_emergence"):
            monkeypatch.setattr(ae, name, lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
        monkeypatch.setattr(ae, "pillar_emergence",
                            lambda *a, **k: ae.pillar(ae.INCOMPLETE, "缺输入", []))
        report = ae.audit(tmp_path, tmp_path / "out", {})
        assert report["exit_code"] == 2

    def test_red_pillar_forces_exit_one(self, tmp_path, monkeypatch):
        for name in ("pillar_oracle", "pillar_capability", "pillar_arch", "pillar_emergence"):
            monkeypatch.setattr(ae, name, lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
        monkeypatch.setattr(ae, "pillar_arch",
                            lambda *a, **k: ae.pillar(ae.RED, "违反", []))
        report = ae.audit(tmp_path, tmp_path / "out", {})
        assert report["exit_code"] == 1


# ---------------------------------------------------------------- 红线：绝不写被测仓库（git 指纹）

class TestReadOnlyGit:
    def test_git_status_clean_after_audit(self, tmp_path):
        repo = tmp_path / "repo"
        _touch(repo / "src/main/java/com/x/Foo.java", "class Foo { String name; }")
        _touch(repo / "docs/prd.md", "# PRD\n- name 必填\n")
        _touch(repo / "db/migration/V1__init.sql", "CREATE TABLE t (id INT NOT NULL);")
        env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                   "-c", "user.name=t", *args],
                                  capture_output=True, text=True, env=env)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "baseline")
        before = git("status", "--porcelain").stdout
        assert before == "", "基线本身不干净"
        ae.audit(repo, tmp_path / "out", {})
        after = git("status", "--porcelain").stdout
        assert after == "", f"审计后被测仓库出现写入: {after!r}"


# ---------------------------------------------------------------- 契约接缝：不偷偷读 target.json

class TestNoTargetBinding:
    def test_oracle_runs_with_bogus_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_AUDIT_TARGET", str(tmp_path / "NOPE.json"))
        repo = tmp_path / "repo"
        _touch(repo / "src/main/java/com/x/Foo.java", "class Foo { @NotNull String n; }")
        _touch(repo / "db/migration/V1__init.sql", "CREATE TABLE t (id INT NOT NULL);")
        _touch(repo / "docs/prd.md", "# PRD\n- n 必填\n")
        p = ae.pillar_oracle(ae.discover(repo), tmp_path / "out", "t")
        for leak in ("项目绑定配置", "FileNotFoundError", "target.json", "NOPE.json"):
            assert leak not in p["reason"], f"oracle 支柱偷偷依赖 target: {leak}"
        assert p.get("harvested_path"), "harvest 未成功产出"

    def test_arch_default_runs_with_bogus_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAYERED_AUDIT_TARGET", str(tmp_path / "NOPE.json"))
        units = _touch(tmp_path / "u.json", json.dumps({"units": []}))
        edges = _touch(tmp_path / "e.json", json.dumps({"edges": []}))
        d = _disc(java_roots=[tmp_path], packages={"controller", "service", "repository"})
        p = ae.pillar_arch(d, tmp_path / "out", units, edges, None, None, None, None)
        for leak in ("项目绑定配置", "FileNotFoundError", "NOPE.json"):
            assert leak not in p["reason"], f"arch 支柱偷偷依赖 target: {leak}"
        assert p["counts"], "arch_rules 未真正出数（可能因 target 缺失崩溃）"


# ---------------------------------------------------------------- 缺输入显式报缺口，不猜路径

class TestInputGaps:
    def test_repo_only_java_reports_doc_and_openapi_gaps(self, tmp_path):
        repo = tmp_path / "repo"
        _touch(repo / "src/main/java/com/x/Foo.java", "class Foo {}")
        d = ae.discover(repo)
        assert d["migrations"] == [] and d["docs"] == [] and d["openapi"] is None
        p = ae.pillar_oracle(d, tmp_path / "out", "t")
        assert p["state"] == ae.INCOMPLETE
        assert "未发现文档" in p["reason"] and "未发现 OpenAPI" in p["reason"]

    def test_missing_upstream_names_what_is_missing(self, tmp_path):
        p = ae.pillar_capability(_disc(java_roots=[Path("s")]), tmp_path, None, None, None)
        assert p["state"] == ae.INCOMPLETE
        assert "units.json" in p["reason"] and "relation_net.json" in p["reason"]


# ---------------------------------------------------------------- 供给上游 → 激活支柱（非缺输入 INCOMPLETE）

class TestSupplyUpstream:
    def test_capability_missing_then_supplied(self, tmp_path, monkeypatch):
        missing = ae.pillar_capability(_disc(java_roots=[Path("s")]), tmp_path, None, None, None)
        assert missing["state"] == ae.INCOMPLETE and "缺上游" in missing["reason"]
        h = _touch(tmp_path / "h.json", "{}")
        _fake_engine(monkeypatch, {"capability_map.py": (0, "", {"counts": _counts(
            oracles_implemented=5, oracles_total=5)})})
        supplied = ae.pillar_capability(_disc(java_roots=[Path("s")], test_roots=[Path("t")]),
                                        tmp_path, Path("u"), Path("net"), h)
        assert "缺上游" not in supplied["reason"]
        assert supplied["state"] == ae.GREEN

    def test_emergence_missing_then_supplied(self, tmp_path, monkeypatch):
        missing = ae.pillar_emergence(_disc(), tmp_path, Path("u"), None, None)
        assert missing["state"] == ae.INCOMPLETE and "缺上游" in missing["reason"]
        net = _touch(tmp_path / "net.json", json.dumps({"nodes": {}}))
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=5), "candidates": []})})
        supplied = ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
        assert "缺上游" not in supplied["reason"]
        assert supplied["state"] == ae.GREEN


# ---------------------------------------------------------------- 常设机械化变异测试（守卫有牙）

def _assert(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


def _caught(fn) -> bool:
    try:
        fn()
        return False
    except AssertionError:
        return True


def _claims_definite_violation(reason: str) -> bool:
    # 头条把「旁条件失效/一致性未知」说成确定缺陷的两种措辞都算违规：
    # 字面「确定违反」与旧头条「确定 frame rule 违反候选」。
    return "确定违反" in reason or "确定 frame rule 违反" in reason


class TestMutationGuards:
    """每个守卫配一个变异：把正确行为改坏，断言守卫会红。永久绿测试证明守卫非同义反复。"""

    def test_mutation_incomplete_as_zero_is_caught(self, monkeypatch):
        monkeypatch.setattr(ae, "aggregate_exit", lambda states: 1 if ae.RED in states else 0)
        assert _caught(lambda: _assert(ae.aggregate_exit([ae.INCOMPLETE]) != 0)), \
            "守卫没咬住 INCOMPLETE→0"

    def test_mutation_exit_two_as_zero_at_audit_level(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ae, "aggregate_exit", lambda states: 1 if ae.RED in states else 0)
        for name in ("pillar_oracle", "pillar_capability", "pillar_arch"):
            monkeypatch.setattr(ae, name, lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
        monkeypatch.setattr(ae, "pillar_emergence",
                            lambda *a, **k: ae.pillar(ae.INCOMPLETE, "缺输入", []))
        report = ae.audit(tmp_path, tmp_path / "out", {})
        assert _caught(lambda: _assert(report["exit_code"] == 2)), "audit 层没咬住 2→0"

    def test_mutation_missing_input_silent_skip_is_caught(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ae, "pillar_oracle", lambda d, out, target: ae.pillar(ae.GREEN, "skip", []))
        assert _caught(lambda: _assert(
            ae.pillar_oracle(_disc(), tmp_path, "t")["state"] == ae.INCOMPLETE)), \
            "守卫没咬住缺输入静默跳过"

    def test_mutation_engine_failure_fake_green_is_caught(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ae, "failed", lambda *a, **k: ae.pillar(ae.GREEN, "ok", []))
        _fake_engine(monkeypatch, {"oracle_harvest.py": (0, "", {"oracles": []}),
                                   "oracle_bridge.py": (1, "boom", None)})
        assert _caught(lambda: _assert(
            ae.pillar_oracle(_disc(docs=[Path("p.md")]), tmp_path, "t")["state"] == ae.INCOMPLETE)), \
            "守卫没咬住引擎失败假绿"

    def test_mutation_arch_unknown_washed_green_is_caught(self, tmp_path, monkeypatch):
        real = ae.pillar_arch
        def wash(*a, **k):
            p = real(*a, **k)
            if p["state"] == ae.INCOMPLETE and "unknown" in p["reason"]:
                p["state"] = ae.GREEN
            return p
        monkeypatch.setattr(ae, "pillar_arch", wash)
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", {
            "counts": _counts(unknown_evaluations=5, evaluations=10), "input_warnings": []})})
        assert _caught(lambda: _assert(
            ae.pillar_arch(_disc(java_roots=[Path("s")]), tmp_path, Path("u"), Path("e"),
                           Path("r"), Path("l"), None, None)["state"] == ae.INCOMPLETE)), \
            "守卫没咬住 arch unknown 洗绿"

    def test_mutation_may_write_as_definite_red_is_caught(self, tmp_path, monkeypatch):
        real = ae.pillar_emergence
        def wash(*a, **k):
            p = real(*a, **k)
            if "may-write" in p["reason"]:
                p["state"] = ae.RED
            return p
        monkeypatch.setattr(ae, "pillar_emergence", wash)
        net = _touch(tmp_path / "net.json", json.dumps({"nodes": {}}))
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10),
            "candidates": [{"frame_violation": [], "evidence_strength": "may_write_unknown"}]})})
        assert _caught(lambda: _assert(
            ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)["state"] == ae.INCOMPLETE)), \
            "守卫没咬住 may-write 当确定红"

    def test_mutation_unknown_overlap_as_definite_red_is_caught(self, tmp_path, monkeypatch):
        # 变异：把「0 已证实 + unknown 重叠」洗回旧错误行为(RED + 「确定违反」头条)。
        # 守卫必须咬住两个面：状态不得 RED、头条不得出现「确定违反」。
        real = ae.pillar_emergence

        def wash(*a, **k):
            p = real(*a, **k)
            if p.get("proven_inconsistent") == 0 and p.get("side_condition_leads"):
                p["state"] = ae.RED
                p["reason"] = f"确定 frame rule 违反候选 {p['side_condition_leads']} 个"
            return p
        monkeypatch.setattr(ae, "pillar_emergence", wash)
        net = _touch(tmp_path / "net.json", json.dumps({"nodes": {}}))
        _fake_engine(monkeypatch, {"frame_check.py": (0, "", {
            "counts": _counts(pairs_decidable=5, pairs_total=10,
                              candidates_delta0={"consistent": 0, "unknown": 9}),
            "candidates": [{"frame_violation": ["x"], "evidence_strength": "definite"}] * 9})})
        p = ae.pillar_emergence(_disc(), tmp_path, Path("u"), net, None)
        assert _caught(lambda: _assert(p["state"] == ae.INCOMPLETE)), \
            "守卫没咬住 unknown 重叠洗成确定红"
        assert _caught(lambda: _assert(not _claims_definite_violation(p["reason"]))), \
            "守卫没咬住「确定违反」头条"


# ---------------------------------------------------------------- 红线1：供给但读不到 → INCOMPLETE

class TestInputReadability:
    def _greenable(self, monkeypatch, by_source):
        _fake_engine(monkeypatch, {
            "oracle_harvest.py": (0, "", {"oracles": [], "counts": {"by_source": by_source}}),
            "oracle_bridge.py": (0, "", {"counts": _counts(numerator=2, denominator=2),
                                         "untranscribed_count": 0})})

    def test_nonexistent_openapi_incomplete_not_green(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        self._greenable(monkeypatch, {"document": 1, "openapi": 1})
        d = _disc(docs=[doc], openapi=tmp_path / "NOPE.json")
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "OpenAPI 供给但不可读" in p["reason"]

    def test_nonexistent_doc_incomplete(self, tmp_path, monkeypatch):
        self._greenable(monkeypatch, {"document": 1})
        d = _disc(docs=[tmp_path / "NOPE.md"])
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "文档供给但不可读" in p["reason"]

    def test_supplied_openapi_zero_consumption_incomplete(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        oa = _touch(tmp_path / "o.json", "{}")
        self._greenable(monkeypatch, {"document": 1})  # openapi 读到了但 0 产出
        d = _disc(docs=[doc], openapi=oa)
        p = ae.pillar_oracle(d, tmp_path, "t")
        assert p["state"] == ae.INCOMPLETE and "未产出 L2" in p["reason"]

    def test_readable_consumed_openapi_green(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        oa = _touch(tmp_path / "o.json", "{}")
        self._greenable(monkeypatch, {"document": 1, "openapi": 1})
        d = _disc(docs=[doc], openapi=oa)
        assert ae.pillar_oracle(d, tmp_path, "t")["state"] == ae.GREEN


# ---------------------------------------------------------------- 红线2：arch 分子剔除 not_applicable

class TestArchApplicableDomain:
    def _data(self):
        return {"counts": _counts(evaluations=6736, unknown_evaluations=59, findings=0, rules_unmatched=0),
                "input_warnings": [],
                "rules": [{"counts": {"compliant": 408, "unknown": 59,
                                      "not_applicable": 6269, "violation": 0}}]}

    def test_numerator_excludes_not_applicable(self, tmp_path, monkeypatch):
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", self._data())})
        p = ae.pillar_arch(_disc(java_roots=[Path("s")]), tmp_path, Path("u"), Path("e"),
                           Path("r"), Path("l"), None, None)
        assert p["numerator"] == 408, "分子必须只算 compliant"
        assert p["denominator"] == 467, "分母必须是适用域(compliant+unknown+violation)"
        assert p["not_applicable"] == 6269 and p["total_evaluations"] == 6736
        assert p["applicable_over_total"] < 0.07, "真实适用域约 6.9%，不得报成 99.1%"

    def test_idle_rules_reported_and_incomplete(self, tmp_path, monkeypatch):
        data = self._data()
        data["counts"]["rules_unmatched"] = 5
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", data)})
        p = ae.pillar_arch(_disc(java_roots=[Path("s")]), tmp_path, Path("u"), Path("e"),
                           Path("r"), Path("l"), None, None)
        assert p["idle_rules"] == 5 and p["state"] == ae.INCOMPLETE


# ---------------------------------------------------------------- 问题3：--out 越界拒绝

class TestOutBoundary:
    def _run(self, repo, out):
        return subprocess.run([sys.executable, str(Path(ae.__file__)),
                               "--repo", str(repo), "--out", str(out)],
                              capture_output=True, text=True)

    def test_out_inside_repo_refused(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        proc = self._run(repo, repo / "sub")
        assert proc.returncode == 2 and "拒绝" in proc.stderr
        assert not (repo / "sub").exists(), "越界 --out 仍写入了仓库"

    def test_out_equal_repo_refused(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        proc = self._run(repo, repo)
        assert proc.returncode == 2 and "拒绝" in proc.stderr

    def test_out_outside_repo_allowed(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        proc = self._run(repo, tmp_path / "out")
        assert proc.returncode == 2 and "拒绝" not in proc.stderr  # 空仓库 exit 2，但非越界拒绝


# ---------------------------------------------------------------- 问题5：相对路径解析

class TestRelativePaths:
    def test_relative_docs_resolved(self, tmp_path):
        work = tmp_path / "work"
        repo = work / "repo"
        _touch(repo / "src/main/java/com/x/Foo.java", "class Foo {}")
        _touch(work / "prd.md", "# PRD\n- name 必填\n")
        proc = subprocess.run([sys.executable, str(Path(ae.__file__)),
                               "--repo", "repo", "--out", "out", "--docs", "prd.md"],
                              capture_output=True, text=True, cwd=str(work))
        assert "FileNotFoundError" not in proc.stdout + proc.stderr
        assert (work / "out" / "audit_report.json").is_file()


# ---------------------------------------------------------------- 问题4：默认配置可用仓库自有 members

class TestDefaultConfigMembers:
    def test_default_config_declares_member_and_fact_source(self):
        cfg = ae.default_arch_config({"controller", "service", "repository"}, True)
        assert cfg["member_source"]["records"] == "members"
        assert cfg["role_fact_source"]["records"] == "facts"

    def test_arch_runs_with_repo_members_standard_shape(self, tmp_path):
        units = _touch(tmp_path / "u.json", json.dumps({"units": []}))
        edges = _touch(tmp_path / "e.json", json.dumps({"edges": []}))
        members = _touch(tmp_path / "m.json", json.dumps(
            {"members": [{"class": "com.x.controller.C", "method": "m", "modifiers": "private"}]}))
        d = _disc(java_roots=[tmp_path], packages={"controller", "service", "repository"})
        p = ae.pillar_arch(d, tmp_path / "out", units, edges, None, None, members, None)
        assert p["counts"], "默认配置应能让仓库自有 members 跑起来"
        assert p["visibility_unchecked"] is False

    def test_arch_members_wrong_shape_incomplete_not_green(self, tmp_path):
        units = _touch(tmp_path / "u.json", json.dumps({"units": []}))
        edges = _touch(tmp_path / "e.json", json.dumps({"edges": []}))
        members = _touch(tmp_path / "m.json", json.dumps({"functions": []}))  # records 键不匹配
        d = _disc(java_roots=[tmp_path], packages={"controller", "service", "repository"})
        p = ae.pillar_arch(d, tmp_path / "out", units, edges, None, None, members, None)
        assert p["state"] == ae.INCOMPLETE


# ---------------------------------------------------------------- M6/M7 变异守卫

class TestMutationGuardsRedLines:
    def test_mutation_nonexistent_openapi_washed_green_caught(self, tmp_path, monkeypatch):
        doc = _touch(tmp_path / "prd.md", "# PRD")
        _fake_engine(monkeypatch, {
            "oracle_harvest.py": (0, "", {"oracles": [], "counts": {"by_source": {"document": 1, "openapi": 1}}}),
            "oracle_bridge.py": (0, "", {"counts": _counts(numerator=2, denominator=2), "untranscribed_count": 0})})
        monkeypatch.setattr(ae, "_unreadable_inputs", lambda d: [])  # 变异：去掉可读性校验
        d = _disc(docs=[doc], openapi=tmp_path / "NOPE.json")
        assert _caught(lambda: _assert(
            ae.pillar_oracle(d, tmp_path, "t")["state"] == ae.INCOMPLETE)), \
            "M6 没咬住不存在 OpenAPI 洗绿"

    def test_mutation_not_applicable_in_numerator_caught(self, tmp_path, monkeypatch):
        real = ae.pillar_arch
        def wash(*a, **k):
            p = real(*a, **k)
            p["numerator"] = p["counts"]["evaluations"] - p["counts"]["unknown_evaluations"]  # 旧错误公式
            return p
        monkeypatch.setattr(ae, "pillar_arch", wash)
        data = {"counts": _counts(evaluations=6736, unknown_evaluations=59, findings=0, rules_unmatched=0),
                "input_warnings": [],
                "rules": [{"counts": {"compliant": 408, "unknown": 59, "not_applicable": 6269, "violation": 0}}]}
        _fake_engine(monkeypatch, {"arch_rules.py": (0, "", data)})
        p = ae.pillar_arch(_disc(java_roots=[Path("s")]), tmp_path, Path("u"), Path("e"),
                           Path("r"), Path("l"), None, None)
        assert _caught(lambda: _assert(p["numerator"] == 408)), "M7 没咬住 not_applicable 进分子"