"""疑点可见性 —— 撤掉假阳性不能让底下的真风险从报告里消失。

## 反向失败模式（本组守卫锁的就是它）

oracle 闸门把 5 条未经确认/被否定的结论压出了缺陷台账，留痕在各层
`*_rule_suspects.json` 与 `business_findings.json`。但 `audit_report.md`
对「待确认 / unconfirmed / 疑点 / 已证伪 / refuted」**0 命中** ——
价格篡改这个真实的规范层风险，从「CRITICAL 缺陷」变成了「同事完全看不见」。

与坑 66（覆盖 ≠ 检测力）同族：不是数字错了，是**该被看到的东西没被展示**。
北极星是「直接暴露代码有没有问题」，只在中间文件里的风险对使用者等于不存在。

## 三个口径，不得混算

    确认缺陷（kind=defect，进台账）
    待确认疑点（unconfirmed，**不知道是否成立，需业务方确认**，不在台账内）
    已证伪只留观测（refuted，文档明确否定，不在台账内）

unconfirmed **既不是缺陷，也不是已排除**。把它计入缺陷分子是编造预期；
把它写成「已排除」是吞掉真风险。两个方向都要有守卫。
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

import aggregate_findings as af  # noqa: E402
import findings_schema  # noqa: E402

UNCONF = {"source": "inferred", "confirmation": "unconfirmed",
          "reference": "无文档依据", "note": "推断预期，待业务确认"}
REFUT = {"source": "project_doc", "confirmation": "refuted",
         "reference": "doc.md:307", "text": "该字段可为空", "note": "文档明确否定"}

SOURCES = [
    {"layer": "function", "file": "fn_sus.json",
     "unconfirmed_key": "unconfirmed", "refuted_key": "refuted"},
    {"layer": "service", "file": "biz.json",
     "unconfirmed_key": "unconfirmed_suspects", "refuted_key": "refuted_observations"},
]


def _held(rid: str, oracle: dict, sev: str = "CRITICAL") -> dict:
    """A 形：闸门留痕。"""
    return {"rule_id": rid, "oracle": oracle,
            "finding": {"id": rid, "severity": sev, "title": f"{rid} 的预期",
                        "detail": "观测说明", "evidence": {"actual": 999999},
                        "repro": "python3 x.py"}}


def _bucket(rule: str, oracle: dict) -> dict:
    """B 形：规则桶。"""
    return {"rule": rule, "table": "orders", "db_blocked": False,
            "api_http": 200, "api_note": "落库无异常", "oracle": oracle}


def _finding_shape(fid: str, oracle: dict) -> dict:
    """C 形：服务层不变式结论。"""
    return {"id": fid, "severity": "HIGH", "endpoint": "POST /api/x",
            "title": f"{fid} 的预期", "detail": "实测违反 1 次",
            "repro": "python3 business_probe.py",
            "evidence": {"checked": 6, "violations": 1}, "oracle": oracle}


def _files(tmp_path: Path, *, unconfirmed=None, refuted=None,
           biz_unconfirmed=None, biz_refuted=None) -> Path:
    d = tmp_path / "art"
    d.mkdir(exist_ok=True)
    (d / "fn_sus.json").write_text(json.dumps({
        "unconfirmed": unconfirmed if unconfirmed is not None else [_held("FN-X", UNCONF)],
        "refuted": refuted if refuted is not None else [],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "biz.json").write_text(json.dumps({
        "unconfirmed_suspects": biz_unconfirmed if biz_unconfirmed is not None else [],
        "refuted_observations": biz_refuted if biz_refuted is not None else [_finding_shape("BI-Y", REFUT)],
    }, ensure_ascii=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------- A 收集器
def test_both_buckets_are_collected(tmp_path):
    d = _files(tmp_path, refuted=[_held("FN-Z", REFUT)])
    out = af.collect_suspects(SOURCES, d)
    assert out["counts"]["unconfirmed"] == 1
    assert out["counts"]["refuted"] == 2
    assert out["channel_status"] == "OK"


def test_all_three_upstream_shapes_normalize(tmp_path):
    d = _files(tmp_path,
               unconfirmed=[_held("A", UNCONF), _bucket("B 规则", UNCONF)],
               biz_unconfirmed=[_finding_shape("C", UNCONF)])
    out = af.collect_suspects(SOURCES, d)
    # B 形上游本来就没有 id，标识落在 expectation 上（不得编造 id）
    got = {(r["rule_id"] or r["expectation"]): r for r in out["unconfirmed"]}
    assert set(got) == {"A", "B 规则", "C"}
    assert got["A"]["observation"] == {"actual": 999999}
    assert got["B 规则"]["observation"]["api_http"] == 200
    assert got["B 规则"]["rule_id"] == ""
    assert got["C"]["observation"] == {"checked": 6, "violations": 1}
    assert all(r["expectation"] for r in got.values())


def test_unrecognized_shape_is_counted_not_dropped(tmp_path):
    d = _files(tmp_path, unconfirmed=[{"完全认不出": 1}, _held("A", UNCONF)])
    out = af.collect_suspects(SOURCES, d)
    assert len(out["unrecognized_records"]) == 1
    assert out["counts"]["unconfirmed"] == 1
    assert out["channel_status"] == "INCOMPLETE"
    assert any("形态认不出" in w for w in out["input_warnings"])


def test_missing_oracle_is_unrecognized(tmp_path):
    d = _files(tmp_path, unconfirmed=[{"rule_id": "A", "finding": {"id": "A"}}])
    out = af.collect_suspects(SOURCES, d)
    assert out["counts"]["unconfirmed"] == 0
    assert len(out["unrecognized_records"]) == 1


def test_missing_file_is_incomplete_not_zero(tmp_path):
    d = _files(tmp_path)
    (d / "fn_sus.json").unlink()
    out = af.collect_suspects(SOURCES, d)
    assert out["missing_sources"] == ["fn_sus.json"]
    assert out["channel_status"] == "INCOMPLETE"
    assert any("不得读成 0" in w for w in out["input_warnings"])


def test_missing_bucket_key_is_incomplete_not_zero(tmp_path):
    d = _files(tmp_path)
    (d / "fn_sus.json").write_text(json.dumps({"refuted": []}), encoding="utf-8")
    out = af.collect_suspects(SOURCES, d)
    assert out["missing_keys"] == [{"file": "fn_sus.json", "bucket": "unconfirmed",
                                   "key": "unconfirmed"}]
    assert out["channel_status"] == "INCOMPLETE"


def test_no_declared_sources_is_incomplete(tmp_path):
    out = af.collect_suspects([], tmp_path)
    assert out["channel_status"] == "INCOMPLETE"
    assert out["counts"]["unconfirmed"] == 0
    assert any("未测量" in w for w in out["input_warnings"])


def test_state_mismatch_is_flagged(tmp_path):
    d = _files(tmp_path, unconfirmed=[_held("A", REFUT)])
    out = af.collect_suspects(SOURCES, d)
    assert len(out["state_mismatches"]) == 1
    assert out["state_mismatches"][0]["rule_id"] == "A"
    assert out["channel_status"] == "INCOMPLETE"


def test_empty_buckets_are_legal_and_not_incomplete(tmp_path):
    """合法输入：两桶都空 → 测得 0，OK，零告警。不得误红。"""
    d = _files(tmp_path, unconfirmed=[], refuted=[], biz_refuted=[])
    out = af.collect_suspects(SOURCES, d)
    assert out["counts"]["unconfirmed"] == 0 and out["counts"]["refuted"] == 0
    assert all(v == {"unconfirmed": 0, "refuted": 0}
               for v in out["counts"]["by_layer"].values())
    assert out["channel_status"] == "OK"
    assert out["input_warnings"] == []


def test_by_layer_counts_are_conserved(tmp_path):
    d = _files(tmp_path, unconfirmed=[_held("A", UNCONF), _bucket("B", UNCONF)],
               refuted=[_held("Z", REFUT)], biz_unconfirmed=[_finding_shape("C", UNCONF)])
    out = af.collect_suspects(SOURCES, d)
    bl = out["counts"]["by_layer"]
    assert sum(v["unconfirmed"] for v in bl.values()) == out["counts"]["unconfirmed"]
    assert sum(v.get("refuted", 0) for v in bl.values()) == out["counts"]["refuted"]


def test_why_not_filed_keeps_the_three_state(tmp_path):
    d = _files(tmp_path)
    out = af.collect_suspects(SOURCES, d)
    u = out["unconfirmed"][0]["why_not_filed"]
    assert "需业务方确认" in u
    assert "既不是缺陷，也不是已排除" in u
    r = out["refuted"][0]["why_not_filed"]
    assert "只保留观测" in r


# ---------------------------------------------------------------- B 三口径
def _records() -> list[dict]:
    return json.loads((PILOT / "all_findings.json").read_text(encoding="utf-8"))["findings"]


def test_calibers_never_mixes_suspects_into_defects(tmp_path):
    d = _files(tmp_path, unconfirmed=[_held("A", UNCONF), _held("B", UNCONF)])
    sus = af.collect_suspects(SOURCES, d)
    rec = _records()
    cal = af.calibers(rec, sus)
    assert cal["by_kind"]["defect"] == sum(1 for r in rec if r["kind"] == "defect")
    assert cal["unconfirmed"] == 2
    assert sum(cal["by_kind"].values()) == len(rec)


def test_calibers_empty_suspects_is_zero_not_error():
    cal = af.calibers(_records(), {"counts": {"unconfirmed": 0, "refuted": 0}})
    assert cal["unconfirmed"] == 0 and cal["refuted"] == 0


def test_suspect_ids_are_not_in_the_ledger():
    """真实产物：被压住的结论不得同时出现在台账里（技术重挂那条除外）。"""
    ledger = {f["id"] for f in _records()}
    sus = json.loads((PILOT / "root_causes.json").read_text(encoding="utf-8"))["suspects"]
    overlap = {r["rule_id"] for b in ("unconfirmed", "refuted") for r in sus[b]} & ledger
    assert overlap == set(), f"这些结论既在疑点清单又在台账里：{overlap}"


# ---------------------------------------------------------------- C 报告渲染
def _payload(sus: dict) -> dict:
    rec = _records()
    cats = findings_schema.load_layer_categories()
    rcs = af.aggregate(rec, findings_schema.load_escalation_targets(),
                       findings_schema.load_out_of_scope())
    cov = af.coverage(rec, rcs, cats)
    cal = af.calibers(rec, sus)
    return {"summary": {"findings": len(rec), "root_causes": len(rcs),
                        "collapsed": len(rec) - len(rcs),
                        "violations": sum(1 for r in rcs if r["violations"]),
                        "by_kind": cal["by_kind"],
                        "unconfirmed_suspects": cal["unconfirmed"],
                        "refuted_observations": cal["refuted"],
                        "suspect_channel_status": sus["channel_status"]},
            "root_causes": rcs, "coverage": cov, "suspects": sus}


def _render(tmp_path: Path, sus: dict) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    old = af.PROJECT_DIR
    af.PROJECT_DIR = tmp_path
    try:
        af._write_md(_payload(sus), findings_schema.load_layer_categories())
    finally:
        af.PROJECT_DIR = old
    return (tmp_path / "audit_report.md").read_text(encoding="utf-8")


def _sus_both(tmp_path) -> dict:
    d = _files(tmp_path, unconfirmed=[_held("FN-PRICE-X", UNCONF)],
               refuted=[_held("CLASS-ADDR-X", REFUT)])
    return af.collect_suspects(SOURCES, d)


def test_report_has_both_sections(tmp_path):
    md = _render(tmp_path / "r", _sus_both(tmp_path))
    assert "## 八、待确认疑点" in md
    assert "## 九、已证伪" in md


def test_headline_reports_three_calibers_side_by_side(tmp_path):
    md = _render(tmp_path / "r", _sus_both(tmp_path))
    head = md.split("## 一、")[0]
    assert "确认缺陷" in head and "待确认疑点" in head and "已证伪只留观测" in head
    assert "不在台账" in head


def test_every_entry_carries_the_required_fields(tmp_path):
    md = _render(tmp_path / "r", _sus_both(tmp_path))
    for rid in ("FN-PRICE-X", "CLASS-ADDR-X"):
        block = md.split(f"`{rid}`")[1].split("###")[0]
        for field in ("**预期原文**", "**oracle 状态**", "**reference**", "**为何不进台账**"):
            assert field in block, f"{rid} 缺 {field}"


def test_unconfirmed_is_not_labelled_excluded(tmp_path):
    md = _render(tmp_path / "r", _sus_both(tmp_path))
    block = md.split("## 八、")[1].split("## 九、")[0]
    assert "已排除" not in block.replace("既不是缺陷，也不是已排除", "")
    assert "需业务方确认" in block


def test_refuted_severity_is_not_called_a_risk_rating(tmp_path):
    md = _render(tmp_path / "r", _sus_both(tmp_path))
    block = md.split("## 九、")[1]
    assert "原结论严重度（已撤销）" in block
    assert "**风险评级**" not in block


def test_empty_bucket_renders_zero_not_disappears(tmp_path):
    """合法输入：某桶为空时章节仍须在，并显式写 0 条。"""
    d = _files(tmp_path, unconfirmed=[], refuted=[], biz_refuted=[])
    md = _render(tmp_path / "r", af.collect_suspects(SOURCES, d))
    assert "## 八、待确认疑点" in md and "## 九、已证伪" in md
    assert md.count("本桶 0 条。") == 2
    assert "INCOMPLETE" not in md


def test_unmeasured_renders_incomplete_not_zero(tmp_path):
    md = _render(tmp_path / "r", af.collect_suspects([], tmp_path))
    assert "INCOMPLETE" in md
    assert "未测量" in md


def test_real_report_shows_the_price_risk():
    """真实产物：价格篡改风险必须在报告里可见（本轮的起点）。"""
    md = (PILOT / "audit_report.md").read_text(encoding="utf-8")
    assert "FN-PRICE-CLIENT-CONTROLLED" in md
    assert "## 八、待确认疑点" in md
    assert "CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING" in md
    assert "## 九、已证伪" in md


# ---------------------------------------------------------------- D 常设变异
def _caught(guard, *args) -> bool:
    try:
        guard(*args)
    except AssertionError:
        return True
    return False


def _guard_both_sections(tmp_path):
    md = _render(tmp_path / "g", _sus_both(tmp_path))
    assert "## 八、待确认疑点" in md
    assert "## 九、已证伪" in md


def _guard_defect_numerator(tmp_path):
    d = _files(tmp_path, unconfirmed=[_held("A", UNCONF), _held("B", UNCONF)])
    sus = af.collect_suspects(SOURCES, d)
    rec = _records()
    cal = af.calibers(rec, sus)
    assert cal["by_kind"]["defect"] == sum(1 for r in rec if r["kind"] == "defect")
    assert sum(cal["by_kind"].values()) == len(rec)


def _guard_not_excluded(tmp_path):
    md = _render(tmp_path / "g", _sus_both(tmp_path))
    block = md.split("## 八、")[1].split("## 九、")[0]
    assert "已排除" not in block.replace("既不是缺陷，也不是已排除", "")


def _guard_empty_bucket_ok(tmp_path):
    d = _files(tmp_path, unconfirmed=[], refuted=[], biz_refuted=[])
    out = af.collect_suspects(SOURCES, d)
    assert out["channel_status"] == "OK" and out["input_warnings"] == []
    md = _render(tmp_path / "g", out)
    assert "INCOMPLETE" not in md


def _guard_missing_file_incomplete(tmp_path):
    d = _files(tmp_path)
    (d / "fn_sus.json").unlink()
    out = af.collect_suspects(SOURCES, d)
    assert out["channel_status"] == "INCOMPLETE"


def test_baseline_guards_are_green_before_mutating(tmp_path):
    for g in (_guard_both_sections, _guard_defect_numerator, _guard_not_excluded,
              _guard_empty_bucket_ok, _guard_missing_file_incomplete):
        g(tmp_path)


def test_mutation_drop_unconfirmed_section_is_caught(monkeypatch, tmp_path):
    """M1 把待确认疑点章节从报告里去掉 → 必须红。"""
    real = af._suspect_section
    monkeypatch.setattr(af, "_suspect_section",
                        lambda a, sus, bucket, *a_, **k: None if bucket == "unconfirmed"
                        else real(a, sus, bucket, *a_, **k))
    assert _caught(_guard_both_sections, tmp_path)


def test_mutation_drop_refuted_section_is_caught(monkeypatch, tmp_path):
    """M2 把已证伪章节去掉 → 必须红（address 的留痕会消失）。"""
    real = af._suspect_section
    monkeypatch.setattr(af, "_suspect_section",
                        lambda a, sus, bucket, *a_, **k: None if bucket == "refuted"
                        else real(a, sus, bucket, *a_, **k))
    assert _caught(_guard_both_sections, tmp_path)


def test_mutation_suspects_counted_as_defects_is_caught(monkeypatch, tmp_path):
    """M3 把 unconfirmed 计入缺陷分子 → 必须红（反向：编造预期）。"""
    real = af.calibers
    def wash(records, suspects):
        out = real(records, suspects)
        out["by_kind"]["defect"] += out["unconfirmed"]
        return out
    monkeypatch.setattr(af, "calibers", wash)
    assert _caught(_guard_defect_numerator, tmp_path)


def test_mutation_unconfirmed_labelled_excluded_is_caught(monkeypatch, tmp_path):
    """M4 把 unconfirmed 写成「已排除」→ 必须红（反向：吞掉真风险）。"""
    monkeypatch.setattr(af, "WHY_NOT_FILED",
                        {**af.WHY_NOT_FILED, "unconfirmed": "该预期已排除，无需跟进"})
    assert _caught(_guard_not_excluded, tmp_path)


def test_mutation_empty_bucket_flagged_incomplete_is_caught(monkeypatch, tmp_path):
    """M5 合法空桶被误判 INCOMPLETE → 必须红（不得误红合法输入）。"""
    real = af.collect_suspects
    def over(tmp_sources, project_dir):
        out = real(tmp_sources, project_dir)
        if out["counts"]["unconfirmed"] == 0:
            out["channel_status"] = "INCOMPLETE"
            out["input_warnings"].append("误报")
        return out
    monkeypatch.setattr(af, "collect_suspects", over)
    assert _caught(_guard_empty_bucket_ok, tmp_path)


def test_mutation_missing_file_silently_zero_is_caught(monkeypatch, tmp_path):
    """M6 文件缺失静默当 0 → 必须红（未测量 ≠ 测得 0）。"""
    real = af.collect_suspects
    def wash(sources, project_dir):
        out = real(sources, project_dir)
        out["channel_status"] = "OK"
        out["input_warnings"] = []
        return out
    monkeypatch.setattr(af, "collect_suspects", wash)
    assert _caught(_guard_missing_file_incomplete, tmp_path)


# ---------------------------------------------------------------- E 文件级 CLI
CLI_MODULES = ("engine/aggregate_findings.py", "engine/findings_schema.py")

CLI_MUTANTS = {
    # M7 删掉待确认疑点章节
    "drop_unconfirmed": (
        '    _suspect_section(a, sus, "unconfirmed", "八、待确认疑点（规范层风险 · 需业务方确认）",',
        '    _skip = ("八、待确认疑点（规范层风险 · 需业务方确认）",'),
    # M8 删掉已证伪章节
    "drop_refuted": (
        '    _suspect_section(a, sus, "refuted", "九、已证伪（文档明确否定 · 只保留观测）",',
        '    _skip = ("九、已证伪（文档明确否定 · 只保留观测）",'),
    # M9 把疑点计入缺陷分子
    "count_as_defect": (
        '    by_kind = calibers(records, suspects)["by_kind"]',
        '    by_kind = calibers(records, suspects)["by_kind"]\n'
        '    by_kind["defect"] += suspects["counts"]["unconfirmed"]'),
}


def _cli_sandbox(tmp_path: Path, *, mutation: str | None = None,
                 empty: bool = False) -> Path:
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    for rel in CLI_MODULES:
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")
    shutil.copyfile(PILOT / "all_findings.json", root / "pilot" / "all_findings.json")
    art = root / "pilot"
    _files_into(art, empty=empty)
    (art / "target.json").write_text(json.dumps({
        "target": "sandbox", "contract": "../layers.yaml", "source_roots": {},
        "annotation_table": "finding_meta.py", "evidence_sources": [],
        "suspect_sources": SOURCES}), encoding="utf-8")
    if mutation:
        target = root / "pilot" / "engine" / "aggregate_findings.py"
        text = target.read_text(encoding="utf-8")
        anchor, repl = CLI_MUTANTS[mutation]
        assert anchor in text, f"变异锚点未命中：{mutation}"
        mutated = text.replace(anchor, repl)
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    return root


def _files_into(d: Path, *, empty: bool = False) -> None:
    (d / "fn_sus.json").write_text(json.dumps({
        "unconfirmed": [] if empty else [_held("FN-PRICE-X", UNCONF)],
        "refuted": [] if empty else [_held("CLASS-ADDR-X", REFUT)],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "biz.json").write_text(json.dumps({
        "unconfirmed_suspects": [], "refuted_observations": [],
    }, ensure_ascii=False), encoding="utf-8")


def _run_cli(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    return subprocess.run(
        [sys.executable, str(root / "pilot" / "engine" / "aggregate_findings.py")],
        cwd=str(root), env=env, capture_output=True, text=True)


def _assert_cli_visible(root: Path) -> None:
    proc = _run_cli(root)
    assert proc.returncode == 0, proc.stderr
    md = (root / "pilot" / "audit_report.md").read_text(encoding="utf-8")
    assert "## 八、待确认疑点" in md, "待确认疑点章节必须在报告里"
    assert "## 九、已证伪" in md, "已证伪章节必须在报告里"
    assert "FN-PRICE-X" in md and "CLASS-ADDR-X" in md
    head = md.split("## 一、")[0]
    assert "待确认疑点" in head and "已证伪只留观测" in head
    doc = json.loads((root / "pilot" / "root_causes.json").read_text(encoding="utf-8"))
    n_defect = sum(1 for f in json.loads(
        (root / "pilot" / "all_findings.json").read_text(encoding="utf-8"))["findings"]
        if f["kind"] == "defect")
    assert doc["summary"]["by_kind"]["defect"] == n_defect, "疑点不得计入缺陷分子"
    assert doc["summary"]["unconfirmed_suspects"] == 1
    assert doc["summary"]["refuted_observations"] == 1


def test_cli_report_shows_both_buckets(tmp_path):
    _assert_cli_visible(_cli_sandbox(tmp_path))


def test_cli_empty_buckets_are_legal(tmp_path):
    """合法输入：两桶都空 → rc=0，章节仍在，显式 0 条，不报 INCOMPLETE。"""
    root = _cli_sandbox(tmp_path, empty=True)
    proc = _run_cli(root)
    assert proc.returncode == 0, proc.stderr
    md = (root / "pilot" / "audit_report.md").read_text(encoding="utf-8")
    assert "## 八、待确认疑点" in md and "## 九、已证伪" in md
    assert md.count("本桶 0 条。") == 2
    assert "INCOMPLETE" not in md


def test_file_level_mutation_drop_unconfirmed_is_caught(tmp_path):
    """M7 文件级：subprocess 读磁盘原文，同进程 monkeypatch 杀不死它。"""
    _assert_cli_visible(_cli_sandbox(tmp_path / "ok"))
    assert _caught(_assert_cli_visible, _cli_sandbox(tmp_path / "m", mutation="drop_unconfirmed"))


def test_file_level_mutation_drop_refuted_is_caught(tmp_path):
    """M8 文件级：删掉已证伪章节 → 必须红。"""
    _assert_cli_visible(_cli_sandbox(tmp_path / "ok"))
    assert _caught(_assert_cli_visible, _cli_sandbox(tmp_path / "m", mutation="drop_refuted"))


def test_file_level_mutation_count_as_defect_is_caught(tmp_path):
    """M9 文件级：把疑点计入缺陷分子 → 必须红。"""
    _assert_cli_visible(_cli_sandbox(tmp_path / "ok"))
    assert _caught(_assert_cli_visible, _cli_sandbox(tmp_path / "m", mutation="count_as_defect"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))