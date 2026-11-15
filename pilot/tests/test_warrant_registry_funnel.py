"""台账级判据登记册 + 闸门覆盖清单 —— 裁定必须被执行，覆盖面必须被如实报。

## V18 查出的三处真矛盾（本组守卫锁的就是它们）

1. **文档里的裁定没被执行**:`SVC-PAGINATION-COUNT-DISCARDED` / `SVC-POST-NOT-IDEMPOTENT`
   在交付散文里被裁定为 B refuted / B unconfirmed，但产物里仍是 MEDIUM defect、
   `warrant: None`、`service_rule_suspects.json` 空(0/0)。
   **声称已裁定但实际未执行，比假阳性更危险 —— 它让人以为闸门盖住了。**
2. **机器普查只覆盖 24 条不是 45**:`warrant_split` 只接了 3 个生产者，
   而台账有 15 个生产者。闸门实际覆盖面远小于声称面。
3. **「CRITICAL/HIGH 一个没动」字面为假**:用 uid 比会被 `#N` 序号漂移污染，
   必须用稳定 ID `(layer, id)` 比。

## 修法

- `warrant_registry.json`(项目侧):给**生产者装不了闸门**的发现声明 warrant
  （那些生产者是探针，禁跑，无法在源头重生成）。
- `engine/normalize_findings.py` 在**唯一入台账漏斗**处消费登记册:
  business 非 confirmed 拦下并落 `ledger_rule_suspects.json`；technical 放行；
  **未登记的不受影响**（不误压），但进「未裁定」计数。
- `warrant_coverage` 清单入 `all_findings.json` 与报告第十节:
  每个生产者的闸门模式(warrant / oracle / none / unknown)、生产条数、defect 数、
  登记册覆盖、漏斗拦下、**未裁定** —— 已接与未接并排报。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PILOT))
sys.path.insert(0, str(PILOT / "engine"))

import normalize_findings as nf  # noqa: E402
import oracle_gate  # noqa: E402

TECH = {"kind": "technical", "authority": "CWE-209", "domain_swap": "holds",
        "basis": "b", "evidence": "X.java:1"}
BIZ_U = {"kind": "business", "domain_swap": "fails", "basis": "b",
         "oracle": {"source": "inferred", "confirmation": "unconfirmed", "reference": "无"}}
BIZ_R = {"kind": "business", "domain_swap": "fails", "basis": "b",
         "oracle": {"source": "project_doc", "confirmation": "refuted", "reference": "d.md:1"}}
BIZ_C = {"kind": "business", "domain_swap": "fails", "basis": "b",
         "oracle": {"source": "project_doc", "confirmation": "confirmed", "reference": "d.md:2"}}


def _reg() -> dict:
    return json.loads((PILOT / "warrant_registry.json").read_text(encoding="utf-8"))["warrants"]


def _target() -> dict:
    return json.loads((PILOT / "target.json").read_text(encoding="utf-8"))


def _ledger_doc() -> dict:
    return json.loads((PILOT / "all_findings.json").read_text(encoding="utf-8"))


def _ledger_ids() -> set:
    return {f["id"] for f in _ledger_doc()["findings"]}


def _suspects() -> dict:
    """所有 suspects 通道合并：id -> confirmation。"""
    out: dict[str, str] = {}
    for src in _target()["suspect_sources"]:
        p = PILOT / src["file"]
        if not p.is_file():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for bucket in ("unconfirmed", "refuted"):
            for h in doc.get(src[f"{bucket}_key"]) or []:
                rid = h.get("rule_id") or h.get("id") or h.get("rule")
                if rid:
                    out[rid] = bucket
    return out


# ------------------------------------------- A 声称已裁定 ⇒ 产物必须有裁定（最重要）
def test_every_adjudicated_business_claim_is_executed_in_the_artifacts():
    """登记册里每条非 confirmed 的 business 裁定，必须真的不在台账、且真的在疑点清单。

    这是「文档声称已裁定但产物无裁定」的机器化守卫：登记册与产物是两个独立文件，
    只写登记册不跑漏斗 → 本条红。
    """
    led = _ledger_ids()
    sus = _suspects()
    bad = []
    for rid, w in _reg().items():
        if w["kind"] != "business":
            continue
        conf = (w.get("oracle") or {}).get("confirmation")
        if conf == "confirmed":
            continue
        if rid in led:
            bad.append(f"{rid}: 裁定为 {conf} 却仍以 defect 在台账里")
        if sus.get(rid) != conf:
            bad.append(f"{rid}: 裁定为 {conf}，疑点清单里却是 {sus.get(rid)!r}")
    assert bad == [], "声称已裁定但产物未执行：\n" + "\n".join(bad)


def test_every_adjudicated_technical_claim_stays_in_the_ledger():
    """反向：登记册裁定为 technical 的，必须仍在台账（A 类漏报比假阳性危险）。"""
    led = _ledger_ids()
    producers = {e["producer"] for e in _ledger_doc()["warrant_coverage"]["by_producer"]}
    bad = [rid for rid, w in _reg().items()
           if w["kind"] == "technical" and w.get("producer")
           and _producer_file(w["producer"]) in producers and rid not in led]
    assert bad == [], f"A 类技术缺陷被误压：{bad}"


def _producer_file(script: str) -> str:
    for f, g in (_target().get("producer_gates") or {}).items():
        if g.get("script") == script:
            return f
    return ""


def test_registry_only_covers_ungated_producers():
    """登记册是给「装不了闸门的生产者」用的；已有门级闸门的不该再登记（坑 11 双载体）。"""
    gates = _target()["producer_gates"]
    bad = []
    for rid, w in _reg().items():
        f = _producer_file(w.get("producer", ""))
        if not f:
            bad.append(f"{rid}: producer {w.get('producer')!r} 不在 producer_gates 里")
        elif gates[f]["gate"] != "none":
            bad.append(f"{rid}: 生产者 {f} 已有 {gates[f]['gate']} 闸门，不该再走登记册")
    assert bad == []


def test_registry_warrants_are_well_formed():
    bad = []
    for rid, w in _reg().items():
        if w.get("kind") not in oracle_gate.WARRANT_KINDS:
            bad.append((rid, "kind 非法"))
        if not (w.get("basis") or "").strip():
            bad.append((rid, "basis 为空"))
        if not (w.get("evidence") or "").strip():
            bad.append((rid, "缺证据"))
        if w["kind"] == "technical":
            if not (w.get("authority") or "").strip():
                bad.append((rid, "authority 为空"))
            if w.get("domain_swap") != "holds":
                bad.append((rid, "domain_swap 应为 holds"))
        else:
            if w.get("domain_swap") != "fails":
                bad.append((rid, "domain_swap 应为 fails"))
            if (w.get("oracle") or {}).get("confirmation") not in (
                    "confirmed", "unconfirmed", "refuted"):
                bad.append((rid, "oracle 确认状态非法"))
    assert bad == []


# ------------------------------------------------------- B 覆盖清单不得只报已接
def test_coverage_lists_every_producer_in_the_ledger():
    doc = _ledger_doc()
    srcs = {f["source"]["file"] for f in doc["findings"]}
    listed = {e["producer"] for e in doc["warrant_coverage"]["by_producer"]}
    assert srcs <= listed, f"这些生产者没进覆盖清单：{sorted(srcs - listed)}"


def test_coverage_gate_modes_are_conserved():
    wc = _ledger_doc()["warrant_coverage"]
    assert sum(wc["gate_modes"].values()) == wc["producer_count"]
    assert len(wc["by_producer"]) == wc["producer_count"]
    assert sum(e["total"] for e in wc["by_producer"]) >= len(_ledger_doc()["findings"])


def test_unadjudicated_count_is_recomputable():
    """未裁定数必须能从清单逐行重算，不得是一个凭空写的数。"""
    wc = _ledger_doc()["warrant_coverage"]
    recomputed = sum(e["defects"] - e["registry_covered"]
                     for e in wc["by_producer"] if e["gate"] in ("none", "unknown"))
    assert wc["unadjudicated_defects"] == recomputed
    assert all(e["unadjudicated"] == (e["defects"] - e["registry_covered"]
                                      if e["gate"] in ("none", "unknown") else 0)
               for e in wc["by_producer"])


def test_ungated_producers_are_all_listed():
    wc = _ledger_doc()["warrant_coverage"]
    expect = sorted(e["producer"] for e in wc["by_producer"]
                    if e["gate"] in ("none", "unknown"))
    assert wc["ungated_producers"] == expect
    assert expect, "本目标存在未接闸门的生产者，清单不得为空"


def test_held_counts_match_the_ledger_suspects_file():
    wc = _ledger_doc()["warrant_coverage"]
    sus = json.loads((PILOT / "ledger_rule_suspects.json").read_text(encoding="utf-8"))
    assert wc["held_at_ledger"] == sus["unconfirmed_count"] + sus["refuted_count"]
    assert sum(e["held"] for e in wc["by_producer"]) == wc["held_at_ledger"]


def test_report_shows_ungated_producers_side_by_side():
    """报告第十节必须列出每个未接闸门的生产者与未裁定数，不得只报已接的那部分。"""
    md = (PILOT / "audit_report.md").read_text(encoding="utf-8")
    assert "## 十、判据闸门覆盖清单" in md
    sec = md.split("## 十、")[1]
    wc = _ledger_doc()["warrant_coverage"]
    for p in wc["ungated_producers"]:
        assert p in sec, f"未接闸门的生产者 {p} 没在报告里列出"
    assert f"未裁定 defect {wc['unadjudicated_defects']} 条" in md.split("## 一、")[0]


def test_unadjudicated_ids_are_identifiable():
    """未裁定的具体是哪几条必须可查，不得只给一个总数。"""
    doc = _ledger_doc()
    reg = _reg()
    gates = _target()["producer_gates"]
    ungated = {f for f, g in gates.items() if g["gate"] in ("none", "unknown")}
    rows = [f["id"] for f in doc["findings"]
            if f["kind"] == "defect" and f["source"]["file"] in ungated
            and f["id"] not in reg]
    assert len(rows) == doc["warrant_coverage"]["unadjudicated_defects"]
    assert len(set(rows)) == len(rows)


# --------------------------------------------------- C 漏斗闸门语义（合成，可移植）
def _rec(fid: str, kind: str = "defect", warrant=None) -> dict:
    r = {"id": fid, "layer": "service", "kind": kind, "severity": "MEDIUM",
         "title": f"{fid} 标题", "detail": "d", "uid": f"service:{fid}#0",
         "evidence_refs": [f"x.json#{fid}"], "repro_cmd": "python3 x.py",
         "location": {"file": "X.java", "line": 1}, "source": {"file": "x.json", "index": 0}}
    if warrant is not None:
        r["warrant"] = warrant
    return r


def test_funnel_technical_passes():
    kept, held = nf.apply_warrant_registry([_rec("T")], {"T": TECH})
    assert [r["id"] for r in kept] == ["T"] and held == []


def test_funnel_business_unconfirmed_held():
    kept, held = nf.apply_warrant_registry([_rec("U")], {"U": BIZ_U})
    assert kept == [] and held[0]["oracle"]["confirmation"] == "unconfirmed"
    assert held[0]["finding"]["evidence"]["evidence_refs"] == ["x.json#U"]


def test_funnel_business_refuted_held():
    kept, held = nf.apply_warrant_registry([_rec("R")], {"R": BIZ_R})
    assert kept == [] and held[0]["oracle"]["confirmation"] == "refuted"


def test_funnel_business_confirmed_passes():
    kept, held = nf.apply_warrant_registry([_rec("C")], {"C": BIZ_C})
    assert [r["id"] for r in kept] == ["C"] and held == []


def test_funnel_unregistered_passes_and_is_counted_unadjudicated():
    """未登记的不误压，但必须进未裁定计数 —— 不静默当成已澄清。"""
    recs = [_rec("N")]
    kept, held = nf.apply_warrant_registry(recs, {})
    assert [r["id"] for r in kept] == ["N"] and held == []
    wc = nf.warrant_coverage(recs, [], {}, [])
    assert wc["unadjudicated_defects"] == 1


def test_funnel_never_gates_non_defects():
    for kind in ("positive", "observation"):
        kept, held = nf.apply_warrant_registry([_rec("P", kind)], {"P": BIZ_U})
        assert [r["id"] for r in kept] == ["P"] and held == []


def test_missing_registry_file_warns_not_silent_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(nf, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(nf, "_target", lambda: {"warrant_registry": "nope.json"})
    reg, warns = nf.warrant_registry()
    assert reg == {} and warns and "不存在" in warns[0]


def test_undeclared_registry_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(nf, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(nf, "_target", lambda: {})
    reg, warns = nf.warrant_registry()
    assert reg == {} and warns and "未启用" in warns[0]


def test_unknown_producer_is_flagged_not_assumed_gated():
    recs = [_rec("X")]
    recs[0]["source"]["file"] = "never_declared.json"
    warns: list[str] = []
    wc = nf.warrant_coverage(recs, [], {}, warns)
    assert wc["by_producer"][0]["gate"] == "unknown"
    assert any("never_declared.json" in w for w in warns)
    assert wc["unadjudicated_defects"] == 1


# ---------------------------------------------------------------- D 双向变异
def _caught(guard, *args) -> bool:
    try:
        guard(*args)
    except AssertionError:
        return True
    return False


def _guard_funnel_executes_business_verdict():
    """指名守卫：登记册里的 business 非 confirmed 裁定必须被漏斗执行。"""
    recs = [_rec("U"), _rec("R")]
    kept, held = nf.apply_warrant_registry(recs, {"U": BIZ_U, "R": BIZ_R})
    assert kept == [], "business 非 confirmed 必须被拦下"
    assert {h["rule_id"] for h in held} == {"U", "R"}
    assert {h["oracle"]["confirmation"] for h in held} == {"unconfirmed", "refuted"}


def _guard_funnel_keeps_technical():
    """指名守卫（反向）：A 类技术缺陷必须放行。"""
    kept, held = nf.apply_warrant_registry([_rec("T")], {"T": TECH})
    assert [r["id"] for r in kept] == ["T"] and held == []


def _guard_funnel_keeps_confirmed_business():
    kept, _ = nf.apply_warrant_registry([_rec("C")], {"C": BIZ_C})
    assert [r["id"] for r in kept] == ["C"]


def _guard_coverage_lists_ungated():
    """指名守卫：未接闸门的生产者必须出现在清单里。"""
    recs = [_rec("X")]
    recs[0]["source"]["file"] = "ungated_probe.json"
    wc = nf.warrant_coverage(recs, [], {}, [])
    assert "ungated_probe.json" in wc["ungated_producers"]
    assert wc["by_producer"][0]["gate"] in ("none", "unknown")


def _guard_unadjudicated_recomputable():
    """指名守卫：未裁定数必须等于逐行重算，不得被洗成 0。"""
    recs = [_rec("X"), _rec("Y")]
    for r in recs:
        r["source"]["file"] = "ungated_probe.json"
    wc = nf.warrant_coverage(recs, [], {}, [])
    assert wc["unadjudicated_defects"] == 2
    assert wc["by_producer"][0]["unadjudicated"] == 2


def test_baseline_guards_are_green_before_mutating():
    for g in (_guard_funnel_executes_business_verdict, _guard_funnel_keeps_technical,
              _guard_funnel_keeps_confirmed_business, _guard_coverage_lists_ungated,
              _guard_unadjudicated_recomputable):
        g()


def test_mutation_funnel_always_passes_is_caught(monkeypatch):
    """M1 漏斗闸门恒放行 → 裁定只写在登记册里不被执行 → 指名守卫必须红。"""
    monkeypatch.setattr(nf, "apply_warrant_registry",
                        lambda records, registry: (list(records), []))
    assert _caught(_guard_funnel_executes_business_verdict)


def test_mutation_registry_ignored_is_caught(monkeypatch):
    """M2 漏斗忽略登记册（只认记录自带的 warrant）→ 裁定不执行 → 必须红。"""
    real = nf.apply_warrant_registry
    monkeypatch.setattr(nf, "apply_warrant_registry",
                        lambda records, registry: real(records, {}))
    assert _caught(_guard_funnel_executes_business_verdict)


def test_mutation_technical_suppressed_is_caught(monkeypatch):
    """M3 **反向**：漏斗把 technical 也当 business 压 → A 类漏报 → 必须红。"""
    monkeypatch.setattr(nf.oracle_gate, "warrant_split",
                        lambda w, items, rule_id="": (
                            [], [{"rule_id": rule_id,
                                  "oracle": {"confirmation": "unconfirmed"},
                                  "finding": i} for i in items]))
    assert _caught(_guard_funnel_keeps_technical)


def test_mutation_confirmed_business_suppressed_is_caught(monkeypatch):
    """M4 有 confirmed oracle 的业务契约也被压 → 过度压制 → 必须红。"""
    monkeypatch.setattr(nf.oracle_gate, "warrant_split",
                        lambda w, items, rule_id="": (
                            [], [{"rule_id": rule_id,
                                  "oracle": {"confirmation": "unconfirmed"},
                                  "finding": i} for i in items]))
    assert _caught(_guard_funnel_keeps_confirmed_business)


def test_mutation_coverage_hides_ungated_is_caught(monkeypatch):
    """M5 覆盖清单只报已接闸门的生产者 → 必须红。"""
    real = nf.warrant_coverage
    def wash(records, held, registry, warnings):
        out = real(records, held, registry, warnings)
        out["by_producer"] = [e for e in out["by_producer"] if e["gate"] not in ("none", "unknown")]
        out["ungated_producers"] = []
        return out
    monkeypatch.setattr(nf, "warrant_coverage", wash)
    assert _caught(_guard_coverage_lists_ungated)


def test_mutation_unadjudicated_zeroed_is_caught(monkeypatch):
    """M6 未裁定数恒 0（把未裁定洗成已澄清）→ 必须红。"""
    real = nf.warrant_coverage
    def wash(records, held, registry, warnings):
        out = real(records, held, registry, warnings)
        out["unadjudicated_defects"] = 0
        for e in out["by_producer"]:
            e["unadjudicated"] = 0
        return out
    monkeypatch.setattr(nf, "warrant_coverage", wash)
    assert _caught(_guard_unadjudicated_recomputable)


# ------------------------------------------------------- E 文件级 CLI（端到端）
def _cli_sandbox(tmp_path: Path, *, mutation: str | None = None) -> Path:
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    for rel in ("engine/normalize_findings.py", "engine/findings_schema.py",
                "engine/oracle_gate.py"):
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")
    tgt = _target()
    for name in ("finding_meta.py", "warrant_registry.json"):
        shutil.copyfile(PILOT / name, root / "pilot" / name)
    for src in tgt["evidence_sources"]:
        p = PILOT / src["file"]
        if p.is_file():
            shutil.copyfile(p, root / "pilot" / src["file"])
    (root / "pilot" / "target.json").write_text(
        json.dumps(tgt, ensure_ascii=False), encoding="utf-8")
    if mutation:
        target = root / "pilot" / "engine" / "normalize_findings.py"
        text = target.read_text(encoding="utf-8")
        anchor, repl = CLI_MUTANTS[mutation]
        assert anchor in text, f"变异锚点未命中：{mutation}"
        mutated = text.replace(anchor, repl)
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    return root


CLI_MUTANTS = {
    # M7 拆掉漏斗闸门：登记册裁定不被执行
    "no_funnel_gate": (
        "    records, ledger_held = apply_warrant_registry(records, registry)",
        "    ledger_held = []"),
    # M8 覆盖清单只留已接闸门的生产者
    "hide_ungated": (
        '        "by_producer": sorted(per.values(), key=lambda x: x["producer"]),',
        '        "by_producer": sorted((e for e in per.values() if e["gate"] != "none"),'
        ' key=lambda x: x["producer"]),'),
}


def _run_cli(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    return subprocess.run(
        [sys.executable, str(root / "pilot" / "engine" / "normalize_findings.py")],
        cwd=str(root), env=env, capture_output=True, text=True)


def _assert_cli_executes_verdicts(root: Path) -> None:
    proc = _run_cli(root)
    assert proc.returncode in (0, 2), proc.stderr
    doc = json.loads((root / "pilot" / "all_findings.json").read_text(encoding="utf-8"))
    led = {f["id"] for f in doc["findings"]}
    sus = json.loads((root / "pilot" / "ledger_rule_suspects.json").read_text(encoding="utf-8"))
    sus_ids = {h["rule_id"]: b for b in ("unconfirmed", "refuted") for h in sus[b]}
    for rid, w in _reg().items():
        if w["kind"] == "business":
            conf = (w.get("oracle") or {}).get("confirmation")
            if conf != "confirmed":
                assert rid not in led, f"{rid} 裁定未在漏斗执行"
                assert sus_ids.get(rid) == conf, f"{rid} 裁定未落盘"
        elif w["kind"] == "technical":
            assert rid in led, f"{rid} 是 A 类技术缺陷，被误压"
    wc = doc["warrant_coverage"]
    assert wc["ungated_producers"], "未接闸门的生产者必须被列出"
    assert wc["unadjudicated_defects"] == sum(
        e["defects"] - e["registry_covered"] for e in wc["by_producer"]
        if e["gate"] in ("none", "unknown"))


def test_cli_executes_every_registered_verdict(tmp_path):
    _assert_cli_executes_verdicts(_cli_sandbox(tmp_path))


def test_file_level_mutation_no_funnel_gate_is_caught(tmp_path):
    """M7 文件级：拆掉漏斗闸门 → 裁定回到「只写在文档里」→ 必须红。"""
    _assert_cli_executes_verdicts(_cli_sandbox(tmp_path / "ok"))
    assert _caught(_assert_cli_executes_verdicts,
                   _cli_sandbox(tmp_path / "m", mutation="no_funnel_gate"))


def test_file_level_mutation_hiding_ungated_is_caught(tmp_path):
    """M8 文件级：覆盖清单藏掉未接闸门的生产者 → 必须红。"""
    _assert_cli_executes_verdicts(_cli_sandbox(tmp_path / "ok"))
    assert _caught(_assert_cli_executes_verdicts,
                   _cli_sandbox(tmp_path / "m", mutation="hide_ungated"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))