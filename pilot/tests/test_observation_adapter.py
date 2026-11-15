"""observation_adapter 回归守卫 —— 锁住「纯转录、不编造、证据可解析、三态不洗」。

全部用合成夹具（通用字段名），不依赖真实观测产物（并发 Agent 在写，会漂）：
  1. 逐字转录：值与键都来自源产物，不补字段、不填 null、不改写；
  2. reached 诚实：行未记录 → not_reached 台账，永不进包、永不标 reached；
  3. 证据：每条观测的 evidence_refs 非空且首条指针真的能在源产物解析到该行；
  4. scope 纪律：无绑定/解析不出 → 不产出、缺口可见，不猜；一包一 scope；
  5. filter 精确匹配，不得退化成子串（LESSONS 坑 1）；
  6. 空输入不崩溃；缺产物硬报错（不出半套数）；
  7. 三态端到端：缺字段 UNKNOWN、空串 FAIL、null FAIL，经真实求值器验证；
  8. 汇总合并：FAIL > UNKNOWN > PASS，unknown 绝不洗成 pass；异 scope 批不污染；
  9. CLI subprocess 端到端（LESSONS 坑 24）+ 逐字节确定性；
 10. 常设变异守卫（LESSONS 做法 26）：改坏实现必须被对应守卫咬住。
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import observation_adapter as oa  # noqa: E402
from engine import rule_assessment as ra  # noqa: E402

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def config(bindings, target="synthetic-target"):
    return {"target": target, "bindings": bindings}


def binding(**kw):
    base = {"artifact": "obs.json", "section": "rows", "scope": "alpha",
            "row_key": "row", "scope_evidence": ["scenarios.json#/probes/0/table"]}
    base.update(kw)
    return base


def write_artifact(tmp_path, data, name="obs.json"):
    (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def transcribe(tmp_path, data, bindings=None, target="synthetic-target"):
    write_artifact(tmp_path, data)
    cfg = config(bindings if bindings is not None else [binding()], target)
    return oa.transcribe(tmp_path, cfg)


def rule(rid="r1", scope="alpha", assert_spec=None, kind="document", when=None):
    r = {"id": rid, "claim": "synthetic claim", "scope": scope,
         "assert": assert_spec or {"type": "field_not_null", "left": "name"},
         "source": {"kind": kind, "reference": "doc.md:1", "confirmation": "confirmed",
                    "confirmed_by": "document:doc.md:1"}}
    if when is not None:
        r["when"] = when
    return r


def pack_of(packs, scope="alpha"):
    return next(p["pack"] for p in packs if p["pack"]["scope"] == scope)


def resolve_pointer(data, pointer):
    cur = data
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


# ---------------------------------------------------------------- 1. 逐字转录

class TestVerbatim:
    def test_row_values_and_keys_transcribed_verbatim(self, tmp_path):
        row = {"name": "", "count": 0, "ratio": 1.5, "flag": False,
               "nested": {"a": [1, 2]}, "nullable": None}
        packs, report = transcribe(tmp_path, {"rows": [{"row": row}]})
        out = pack_of(packs)["observations"][0]["row"]
        assert out == row
        assert set(out.keys()) == set(row.keys())

    def test_missing_fields_not_filled(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]})
        obs = pack_of(packs)["observations"][0]
        assert obs["row"] == {"name": "x"}
        verdict = ra.evaluate_observation(rule(assert_spec={"type": "field_not_null", "left": "absent"}),
                                          obs, "synthetic-target")
        assert verdict == "UNKNOWN"

    def test_mutating_source_changes_output(self, tmp_path):
        data = {"rows": [{"row": {"name": "before"}}]}
        packs1, _ = transcribe(tmp_path, data)
        data["rows"][0]["row"]["name"] = "after"
        packs2, _ = transcribe(tmp_path, data)
        assert pack_of(packs1)["observations"][0]["row"]["name"] == "before"
        assert pack_of(packs2)["observations"][0]["row"]["name"] == "after"

    def test_output_row_is_deep_copy(self, tmp_path):
        data = {"rows": [{"row": {"nested": {"a": 1}}}]}
        packs, _ = transcribe(tmp_path, data)
        pack_of(packs)["observations"][0]["row"]["nested"]["a"] = 999
        assert data["rows"][0]["row"]["nested"]["a"] == 1


# ---------------------------------------------------------------- 2. reached 诚实

class TestReached:
    def test_null_row_goes_to_not_reached_ledger_not_pack(self, tmp_path):
        packs, report = transcribe(tmp_path, {"rows": [{"row": None}, {"row": {"name": "x"}}]})
        obs = pack_of(packs)["observations"]
        assert len(obs) == 1 and obs[0]["row"] == {"name": "x"}
        batch = report["batches"][0]
        assert len(batch["not_reached"]) == 1
        assert batch["not_reached"][0]["pointer"] == "obs.json#/rows/0"
        assert report["totals"]["not_reached"] == 1

    def test_missing_row_key_not_emitted(self, tmp_path):
        packs, report = transcribe(tmp_path, {"rows": [{"label": "no row recorded"}]})
        assert packs == []
        assert report["totals"]["not_reached"] == 1

    def test_row_map_reached_key_false_not_emitted(self, tmp_path):
        b = binding(row_key=None, row_map={"status": "db_status"}, reached_key="accepted")
        del b["row_key"]
        packs, report = transcribe(tmp_path, {"rows": [
            {"accepted": False, "db_status": "X"}, {"accepted": True, "db_status": "Y"}]}, [b])
        obs = pack_of(packs)["observations"]
        assert [o["row"] for o in obs] == [{"status": "Y"}]
        assert report["totals"]["not_reached"] == 1

    def test_row_map_null_read_not_transcribed_as_null(self, tmp_path):
        b = binding(row_map={"status": "db_status"})
        del b["row_key"]
        packs, report = transcribe(tmp_path, {"rows": [{"db_status": None}]}, [b])
        assert packs == []
        assert report["totals"]["not_reached"] == 1

    def test_every_emitted_observation_reached_true(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}, {"row": None}]})
        assert all(o["reached"] is True for p in packs for o in p["pack"]["observations"])


# ---------------------------------------------------------------- 3. 证据可解析

class TestEvidence:
    def test_evidence_pointer_resolves_to_transcribed_row(self, tmp_path):
        row = {"name": "x", "count": 2}
        data = {"rows": [{"row": row}]}
        packs, _ = transcribe(tmp_path, data)
        obs = pack_of(packs)["observations"][0]
        assert obs["evidence_refs"]
        first = obs["evidence_refs"][0]
        artifact, pointer = first.split("#", 1)
        assert artifact == "obs.json"
        assert resolve_pointer(data, pointer) == obs["row"]

    def test_row_map_evidence_points_at_each_source_key(self, tmp_path):
        b = binding(row_map={"status": "db_status"})
        del b["row_key"]
        data = {"rows": [{"db_status": "Y"}]}
        packs, _ = transcribe(tmp_path, data, [b])
        obs = pack_of(packs)["observations"][0]
        assert resolve_pointer(data, obs["evidence_refs"][0].split("#", 1)[1]) == {"db_status": "Y"}
        assert any(r.endswith("/db_status") for r in obs["evidence_refs"])

    def test_binding_without_scope_evidence_rejected(self):
        b = binding()
        del b["scope_evidence"]
        with pytest.raises(ValueError, match="scope_evidence"):
            oa.validate_config(config([b]))

    def test_iterate_evidence_pointer_includes_index(self, tmp_path):
        b = binding(row_key=None, row_path="payload.items", iterate=True)
        del b["row_key"]
        data = {"rows": [{"payload": {"items": [{"name": "a"}, {"name": "b"}]}}]}
        packs, _ = transcribe(tmp_path, data, [b])
        obs = pack_of(packs)["observations"]
        assert len(obs) == 2
        for i, o in enumerate(obs):
            pointer = o["evidence_refs"][0].split("#", 1)[1]
            assert resolve_pointer(data, pointer) == {"name": "ab"[i]}


# ---------------------------------------------------------------- 4. scope 纪律

class TestScope:
    def test_unbound_section_reported_not_emitted(self, tmp_path):
        packs, report = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}],
                                              "unbound": [{"row": {"name": "y"}}]})
        assert len(pack_of(packs)["observations"]) == 1
        assert any(g["bucket"] == "section_not_bound" and g["section"] == "unbound"
                   for g in report["gaps"])

    def test_scope_key_missing_no_guess(self, tmp_path):
        b = binding(scope=None, scope_key="table")
        del b["scope"]
        packs, report = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]}, [b])
        assert packs == []
        assert any(g["bucket"] == "scope_unresolved" for g in report["gaps"])

    def test_scope_key_recorded_value_used(self, tmp_path):
        b = binding(scope=None, scope_key="table")
        del b["scope"]
        packs, _ = transcribe(tmp_path, {"rows": [{"table": "beta", "row": {"name": "x"}}]}, [b])
        assert pack_of(packs, "beta")["observations"][0]["scope"] == "beta"

    def test_label_prefix_scope(self, tmp_path):
        b = binding(scope=None, scope_label_prefix=True)
        del b["scope"]
        packs, report = transcribe(tmp_path, {"rows": [
            {"label": "beta.field_x note", "row": {"name": "x"}},
            {"label": "no-dot-token", "row": {"name": "y"}}]}, [b])
        assert len(pack_of(packs, "beta")["observations"]) == 1
        assert any(g["bucket"] == "scope_unresolved" for g in report["gaps"])

    def test_packs_split_by_scope_homogeneous(self, tmp_path):
        b = binding(scope=None, scope_key="table")
        del b["scope"]
        packs, _ = transcribe(tmp_path, {"rows": [
            {"table": "alpha", "row": {"name": "x"}},
            {"table": "beta", "row": {"name": "y"}}]}, [b])
        assert len(packs) == 2
        for p in packs:
            assert {o["scope"] for o in p["pack"]["observations"]} == {p["pack"]["scope"]}

    def test_target_mismatch_yields_unknown(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]}, target="t1")
        obs = pack_of(packs)["observations"][0]
        assert ra.evaluate_observation(rule(), obs, "t2") == "UNKNOWN"

    def test_scope_mismatch_yields_unknown(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]})
        obs = pack_of(packs)["observations"][0]
        assert ra.evaluate_observation(rule(scope="other"), obs, "synthetic-target") == "UNKNOWN"


# ---------------------------------------------------------------- 5. filter 精确匹配

class TestFilter:
    def test_filter_exact_not_substring(self, tmp_path):
        b = binding(filter={"key": ["GET /x/{id}"]})
        packs, report = transcribe(tmp_path, {"rows": [
            {"key": "GET /x/{id}", "row": {"name": "hit"}},
            {"key": "GET /x/{id}/sub", "row": {"name": "miss1"}},
            {"key": "GET /x/{id}2", "row": {"name": "miss2"}},
            {"key": "XGET /x/{id}", "row": {"name": "miss3"}}]}, [b])
        obs = pack_of(packs)["observations"]
        assert [o["row"]["name"] for o in obs] == ["hit"]
        assert report["batches"][0]["filtered_out"] == 3

    def test_filter_missing_field_does_not_match(self, tmp_path):
        b = binding(filter={"key": ["A"]})
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]}, [b])
        assert packs == []


# ---------------------------------------------------------------- 6. 空输入与硬报错

class TestEmptyAndErrors:
    def test_empty_artifact_no_crash(self, tmp_path):
        packs, report = transcribe(tmp_path, {})
        assert packs == []
        assert any(g["bucket"] == "section_missing" for g in report["gaps"])

    def test_empty_section_no_crash(self, tmp_path):
        packs, report = transcribe(tmp_path, {"rows": []})
        assert packs == []
        assert report["totals"]["observations"] == 0

    def test_empty_bindings_no_crash(self, tmp_path):
        write_artifact(tmp_path, {"rows": [{"row": {"name": "x"}}]})
        packs, report = oa.transcribe(tmp_path, config([]))
        assert packs == []
        assert report["totals"]["observations"] == 0

    def test_missing_artifact_hard_error(self, tmp_path):
        with pytest.raises(ValueError, match="不存在"):
            oa.transcribe(tmp_path, config([binding()]))

    def test_malformed_entries_reported_not_crash(self, tmp_path):
        packs, report = transcribe(tmp_path, {"rows": [
            "not-a-dict", {"row": "not-a-dict-row"}, {"row": {"name": "ok"}}]})
        assert len(pack_of(packs)["observations"]) == 1
        buckets = {g["bucket"] for g in report["gaps"]}
        assert "entry_malformed" in buckets and "row_malformed" in buckets

    def test_iterate_on_non_list_reported(self, tmp_path):
        b = binding(row_key=None, row_path="payload", iterate=True)
        del b["row_key"]
        packs, report = transcribe(tmp_path, {"rows": [{"payload": {"name": "x"}}]}, [b])
        assert packs == []
        assert any(g["bucket"] == "row_malformed" for g in report["gaps"])

    def test_map_key_section(self, tmp_path):
        b = binding(section="transition", map_key="matrix",
                    row_map={"status": "db_status"})
        del b["row_key"]
        data = {"transition": {"matrix": {"A->B": {"db_status": "B"}, "A->C": {"db_status": None}}}}
        packs, report = transcribe(tmp_path, data, [b])
        obs = pack_of(packs)["observations"]
        assert [o["row"] for o in obs] == [{"status": "B"}]
        assert "A->B" in obs[0]["id"]
        assert report["totals"]["not_reached"] == 1

    def test_duplicate_observation_ids_impossible_within_pack(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}] * 3})
        ids = [o["id"] for o in pack_of(packs)["observations"]]
        assert len(ids) == len(set(ids))

    def test_config_validation_rejects_bad_shapes(self):
        with pytest.raises(ValueError):
            oa.validate_config({"target": "", "bindings": []})
        with pytest.raises(ValueError):
            oa.validate_config(config([binding(row_key="row", row_path="a.b")]))
        with pytest.raises(ValueError):
            oa.validate_config(config([binding(scope="s", scope_key="table")]))
        with pytest.raises(ValueError):
            oa.validate_config(config([binding(), binding()]))


# ---------------------------------------------------------------- 7. 三态端到端

class TestThreeStateEndToEnd:
    def test_blank_string_fails_not_blank_with_evidence(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": ""}}]})
        pack = pack_of(packs)
        report = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": [rule(assert_spec={"type": "field_not_blank", "left": "name"})]},
                           pack)
        item = report["results"][0]
        assert item["observed_status"] == "FAIL" and item["status"] == "FAIL"
        assert item["observations"][0]["evidence_refs"]

    def test_null_fails_not_null(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": None}}]})
        report = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": [rule()]}, pack_of(packs))
        assert report["results"][0]["observed_status"] == "FAIL"

    def test_missing_field_unknown_not_pass(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"other": 1}}]})
        report = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": [rule()]}, pack_of(packs))
        assert report["results"][0]["observed_status"] == "UNKNOWN"

    def test_empty_pack_all_unknown(self, tmp_path):
        report = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": [rule()]}, {"observations": []})
        assert report["results"][0]["observed_status"] == "UNKNOWN"

    def test_when_not_satisfied_not_applicable(self, tmp_path):
        packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "", "mode": "off"}}]})
        r = rule(assert_spec={"type": "field_not_blank", "left": "name"},
                 when={"field": "mode", "op": "eq", "value": "on"})
        report = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": [r]}, pack_of(packs))
        assert report["results"][0]["observed_status"] == "NOT_APPLICABLE"


# ---------------------------------------------------------------- 8. 汇总合并

def make_run(scope, rows, rules, pack_file="p.json"):
    pack = {"schema": "observation-pack/1", "target": "synthetic-target", "scope": scope,
            "observations": [{"id": f"{scope}-{i}", "target": "synthetic-target", "scope": scope,
                              "reached": True, "row": row, "evidence_refs": ["obs.json#/rows/0"]}
                             for i, row in enumerate(rows)]}
    assessment = ra.assess({"schema": "business-rules/1", "target": "synthetic-target",
                            "rules": rules}, pack)
    return {"pack_file": pack_file, "pack": pack, "assessment": assessment}


class TestSummarize:
    def test_fail_wins_over_pass(self):
        rules = [rule(assert_spec={"type": "field_not_blank", "left": "name"})]
        runs = [make_run("alpha", [{"name": "ok"}], rules, "p1.json"),
                make_run("alpha", [{"name": ""}], rules, "p2.json")]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": rules}, runs)
        assert summary["rules"][0]["observed_status"] == "FAIL"
        assert summary["merged"]["observed_status_counts"]["FAIL"] == 1

    def test_unknown_not_washed_to_pass(self):
        rules = [rule()]
        runs = [make_run("alpha", [{"name": "ok"}], rules, "p1.json"),
                make_run("alpha", [{"other": 1}], rules, "p2.json")]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": rules}, runs)
        assert summary["rules"][0]["observed_status"] == "UNKNOWN"

    def test_other_scope_run_does_not_pollute(self):
        rules = [rule(scope="alpha")]
        runs = [make_run("alpha", [{"name": "ok"}], rules, "p1.json"),
                make_run("beta", [{"name": "ok"}], rules, "p2.json")]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": rules}, runs)
        assert summary["rules"][0]["observed_status"] == "PASS"
        assert len(summary["rules"][0]["per_run"]) == 1

    def test_no_observations_for_scope_stays_unknown(self):
        rules = [rule(scope="gamma")]
        runs = [make_run("alpha", [{"name": "ok"}], rules, "p1.json")]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": rules}, runs)
        assert summary["rules"][0]["observed_status"] == "UNKNOWN"
        assert summary["rules"][0]["no_observations_for_scope"] is True
        assert summary["merged"]["rules_without_observations"] == 1

    def test_fail_evidence_chain_complete(self):
        rules = [rule(assert_spec={"type": "field_not_blank", "left": "name"})]
        runs = [make_run("alpha", [{"name": ""}], rules)]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": rules}, runs)
        chain = summary["rules"][0]["fail_evidence"][0]
        assert chain["row"] == {"name": ""}
        assert chain["evidence_refs"]
        assert chain["rule_source"]["reference"] == "doc.md:1"
        assert chain["rule_assert"] == {"type": "field_not_blank", "left": "name"}
        assert chain["claim"] == "synthetic claim"

    def test_confirmation_gate_and_observed_visibility(self):
        # 求值器两 tier 契约（rule_assessment.py:183-198，owner 扩）：
        # confirmed 的 technical(ddl/annotation/openapi) 与 business(user/document) 都放行 status；
        # 未确认(needs_confirmation)无论 observed 是什么，status 强制 UNKNOWN。observed 始终可见。
        spec = {"type": "field_not_blank", "left": "name"}
        confirmed_ddl = [rule(assert_spec=spec, kind="ddl")]
        runs = [make_run("alpha", [{"name": ""}], confirmed_ddl)]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": confirmed_ddl}, runs)
        assert summary["rules"][0]["observed_status"] == "FAIL"
        assert summary["rules"][0]["status"] == "FAIL"

        unconfirmed = [rule(assert_spec=spec, kind="document")]
        unconfirmed[0]["source"]["confirmation"] = "needs_confirmation"
        runs = [make_run("alpha", [{"name": ""}], unconfirmed)]
        summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                "rules": unconfirmed}, runs)
        assert summary["rules"][0]["observed_status"] == "FAIL"
        assert summary["rules"][0]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- 9. CLI 端到端

class TestCli:
    def _setup(self, tmp_path):
        write_artifact(tmp_path, {"rows": [{"row": {"name": ""}}, {"row": None}]})
        bindings = tmp_path / "bindings.json"
        bindings.write_text(json.dumps(config([binding()])), encoding="utf-8")
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps({"schema": "business-rules/1", "target": "synthetic-target",
                                     "rules": [rule(assert_spec={"type": "field_not_blank",
                                                                 "left": "name"})]}),
                         encoding="utf-8")
        return bindings, rules

    def test_cli_transcribe_assess_summarize(self, tmp_path):
        bindings, rules = self._setup(tmp_path)
        out = tmp_path / "out"
        r1 = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                             "--bindings", str(bindings), "--artifacts-dir", str(tmp_path),
                             "--out-dir", str(out), "--rules", str(rules)],
                            capture_output=True, text=True)
        assert r1.returncode == 0, r1.stderr
        packs = sorted(out.glob("observations_*.json"))
        assert len(packs) == 1
        for pack in packs:
            stem = pack.name[len("observations_"):-len(".json")]
            r2 = subprocess.run([sys.executable, str(ENGINE / "rule_assessment.py"),
                                 "--rules", str(rules), "--observations", str(pack),
                                 "--out", str(out / f"assessment_{stem}.json")],
                                capture_output=True, text=True)
            assert r2.returncode == 0, r2.stderr
        r3 = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                             "--summarize", "--rules", str(rules), "--out-dir", str(out)],
                            capture_output=True, text=True)
        assert r3.returncode == 0, r3.stderr
        summary = json.loads((out / "summary.json").read_text())
        assert summary["merged"]["observed_status_counts"] == {"FAIL": 1}
        assert summary["rules"][0]["fail_evidence"][0]["row"] == {"name": ""}
        report = json.loads((out / "transcription_report.json").read_text())
        assert report["totals"]["not_reached"] == 1
        assert report["inputs"]["artifacts"]["obs.json"]["sha256"]
        assert report["repro_cmd"]

    def test_cli_missing_artifact_nonzero(self, tmp_path):
        bindings = tmp_path / "bindings.json"
        bindings.write_text(json.dumps(config([binding()])), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                            "--bindings", str(bindings), "--artifacts-dir", str(tmp_path),
                            "--out-dir", str(tmp_path / "out")],
                           capture_output=True, text=True)
        assert r.returncode != 0

    def test_cli_summarize_missing_assessment_nonzero(self, tmp_path):
        bindings, rules = self._setup(tmp_path)
        out = tmp_path / "out"
        r1 = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                             "--bindings", str(bindings), "--artifacts-dir", str(tmp_path),
                             "--out-dir", str(out)], capture_output=True, text=True)
        assert r1.returncode == 0, r1.stderr
        r2 = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                             "--summarize", "--rules", str(rules), "--out-dir", str(out)],
                            capture_output=True, text=True)
        assert r2.returncode != 0

    def test_determinism_byte_identical(self, tmp_path):
        bindings, rules = self._setup(tmp_path)
        outputs = []
        for name in ("out1", "out2"):
            out = tmp_path / name
            r = subprocess.run([sys.executable, str(ENGINE / "observation_adapter.py"),
                                "--bindings", str(bindings), "--artifacts-dir", str(tmp_path),
                                "--out-dir", str(out), "--rules", str(rules)],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            outputs.append({p.name: p.read_bytes() for p in sorted(out.glob("*.json"))
                            if p.name != "transcription_report.json"})
        assert outputs[0] == outputs[1]


# ---------------------------------------------------------------- 10. 常设变异守卫

def _caught(guard):
    try:
        guard()
    except AssertionError:
        return True
    return False


class TestMutationGuards:
    """每个变异改坏实现，对应守卫必须变红（同进程 monkeypatch，LESSONS 坑 2/做法 26）。"""

    def test_m1_inject_missing_field_is_caught(self, tmp_path, monkeypatch):
        original = oa._extract_row

        def fabricate(b, entry):
            row, reason = original(b, entry)
            if isinstance(row, dict):
                row["__fabricated__"] = None
            return row, reason

        monkeypatch.setattr(oa, "_extract_row", fabricate)

        def guard():
            packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]})
            assert pack_of(packs)["observations"][0]["row"] == {"name": "x"}

        assert _caught(guard)

    def test_m2_unreached_marked_reached_is_caught(self, tmp_path, monkeypatch):
        def fabricate(b, entry):
            return {}, None

        monkeypatch.setattr(oa, "_extract_row", fabricate)

        def guard():
            packs, report = transcribe(tmp_path, {"rows": [{"row": None}]})
            assert packs == []
            assert report["totals"]["not_reached"] == 1

        assert _caught(guard)

    def test_m3_strip_evidence_is_caught(self, tmp_path, monkeypatch):
        original = oa.transcribe

        def strip_evidence(artifacts_dir, cfg):
            packs, report = original(artifacts_dir, cfg)
            for p in packs:
                for obs in p["pack"]["observations"]:
                    obs["evidence_refs"] = []
            return packs, report

        monkeypatch.setattr(oa, "transcribe", strip_evidence)

        def guard():
            packs, _ = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]})
            obs = pack_of(packs)["observations"][0]
            assert obs["evidence_refs"]
            artifact, pointer = obs["evidence_refs"][0].split("#", 1)
            data = json.loads((tmp_path / artifact).read_text())
            assert resolve_pointer(data, pointer) == obs["row"]

        assert _caught(guard)

    def test_m4_scope_fallback_guess_is_caught(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oa, "_resolve_scope", lambda b, entry: "guessed-default")

        def guard():
            b = binding(scope=None, scope_key="table")
            del b["scope"]
            packs, report = transcribe(tmp_path, {"rows": [{"row": {"name": "x"}}]}, [b])
            assert packs == []
            assert any(g["bucket"] == "scope_unresolved" for g in report["gaps"])

        assert _caught(guard)

    def test_m5_substring_filter_is_caught(self, tmp_path, monkeypatch):
        def substring_filter(b, entry):
            filt = b.get("filter")
            if not filt:
                return True
            return all(any(v in str(entry.get(k, "")) for v in vals) for k, vals in filt.items())

        monkeypatch.setattr(oa, "_matches_filter", substring_filter)

        def guard():
            b = binding(filter={"key": ["GET /x/{id}"]})
            packs, _ = transcribe(tmp_path, {"rows": [
                {"key": "GET /x/{id}", "row": {"name": "hit"}},
                {"key": "GET /x/{id}/sub", "row": {"name": "miss"}}]}, [b])
            assert [o["row"]["name"] for o in pack_of(packs)["observations"]] == ["hit"]

        assert _caught(guard)

    def test_m6_merge_washes_unknown_to_pass_is_caught(self):
        monkey_merge = lambda statuses: "PASS" if "PASS" in statuses else oa._merge_status(statuses)
        original = oa._merge_status
        try:
            oa._merge_status = monkey_merge

            def guard():
                rules = [rule()]
                runs = [make_run("alpha", [{"name": "ok"}], rules, "p1.json"),
                        make_run("alpha", [{"other": 1}], rules, "p2.json")]
                summary = oa.summarize({"schema": "business-rules/1", "target": "synthetic-target",
                                        "rules": rules}, runs)
                assert summary["rules"][0]["observed_status"] == "UNKNOWN"

            assert _caught(guard)
        finally:
            oa._merge_status = original