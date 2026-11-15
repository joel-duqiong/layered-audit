"""判据来源二分闸门 —— A 技术可自证 / B 业务预期，两个方向都必须有牙。

## 为什么需要这个判据

用关键词扫「断言业务预期但无 oracle」会把**技术可自证的安全缺陷**一起扫进去
（无鉴权、上传不嗅探、XFF 伪造、CORS 过宽）—— 它们不需要业务 oracle。
压掉它们是**漏报，比假阳性更危险**。所以闸门不能按「有没有 oracle」一刀切，
必须按**判据来源**分流。

## 可操作分类判据（三问，按序；声明即记录，机器可核）

    Q1 域替换：把被测系统换成任意 CRUD 应用，这条断言是否仍是缺陷？
    Q2 权威出处：这条「应该」能否指向通用权威（CWE / 语言规范 / 平台语义 /
       HTTP 规范），而不必引用本项目的业务量？
    Q3 混合裁决：若两者都像，问「撤销该业务规则后断言是否仍成立」——
       仍成立归 technical，不成立归 business。

    technical → 直接开单，**绝不压**（authority 必须非空，domain_swap=holds）
    business  → 必须有 confirmed oracle（domain_swap=fails）
    未声明    → 保守压住并标「未裁定」，不静默开单也不静默丢弃

## 本轮裁定结果（45 条 = 27 A + 7 B + 11 非缺陷）

B 类 7 条：
  refuted     FILE-VALIDATION-STATUS-SPLIT（AGENTS.md:153-156 定义 400/422 为不同类别，
              业务校验对照.md:14 与 :24 均记「一致」）、SVC-PAGINATION-COUNT-DISCARDED
              （AGENTS.md:128-140 的分页契约明文要求 total/totalPages，「纯开销」被否定）
  unconfirmed FILE-NULL-BODY-WRAPPED-AS-SUCCESS、CLASS-PUT-WIPES-UNSET-FIELD、
              CLASS-ENUM-CASE-SENSITIVE、SVC-POST-NOT-IDEMPOTENT、LATENCY
  confirmed   FILE-ERROR-BODY-SHAPE-INCONSISTENT（AGENTS.md:100「所有接口必须返回统一 JSON」）
              → **B 类有 confirmed oracle 照样开单**，这是闸门不过度压制的阳性对照
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

import class_contract_diff as ccd  # noqa: E402
import finding_meta  # noqa: E402
import oracle_gate  # noqa: E402

TECH = {"kind": "technical", "authority": "CWE-209", "domain_swap": "holds",
        "basis": "通用信息泄漏", "evidence": "X.java:1"}
BIZ_UNCONF = {"kind": "business", "domain_swap": "fails", "basis": "业务预期",
              "oracle": {"source": "inferred", "confirmation": "unconfirmed",
                         "reference": "无文档依据"}}
BIZ_CONF = {"kind": "business", "domain_swap": "fails", "basis": "明文契约",
            "oracle": {"source": "project_doc", "confirmation": "confirmed",
                       "reference": "AGENTS.md:100"}}
BIZ_REFUT = {"kind": "business", "domain_swap": "fails", "basis": "被文档否定",
             "oracle": {"source": "project_doc", "confirmation": "refuted",
                        "reference": "AGENTS.md:153"}}
F = {"id": "X", "severity": "HIGH", "title": "t", "kind": "defect"}


# ---------------------------------------------------------------- A 分流语义
def test_technical_is_filed_without_any_oracle():
    """A 类核心：技术可自证**不需要** oracle，必须开单。"""
    assert "oracle" not in TECH
    kept, held = oracle_gate.warrant_split(TECH, [F], rule_id="X")
    assert kept == [F] and held == []


def test_business_confirmed_is_filed():
    kept, held = oracle_gate.warrant_split(BIZ_CONF, [F], rule_id="X")
    assert kept == [F] and held == []


def test_business_unconfirmed_is_held():
    kept, held = oracle_gate.warrant_split(BIZ_UNCONF, [F], rule_id="X")
    assert kept == [] and held[0]["oracle"]["confirmation"] == "unconfirmed"


def test_business_refuted_is_held():
    kept, held = oracle_gate.warrant_split(BIZ_REFUT, [F], rule_id="X")
    assert kept == [] and held[0]["oracle"]["confirmation"] == "refuted"


def test_business_without_oracle_is_held():
    kept, held = oracle_gate.warrant_split({"kind": "business"}, [F], rule_id="X")
    assert kept == [] and held[0]["oracle"]["confirmation"] == "unconfirmed"


def test_missing_warrant_is_held_as_unadjudicated():
    """未声明判据来源 → 保守压住，并明确标「未裁定」而不是「已排除」。"""
    kept, held = oracle_gate.warrant_split(None, [F], rule_id="X")
    assert kept == []
    assert held[0]["oracle"]["source"] == "unwarranted"
    assert "未裁定" in held[0]["oracle"]["note"]


def test_unknown_warrant_kind_is_held():
    kept, held = oracle_gate.warrant_split({"kind": "大概是技术"}, [F], rule_id="X")
    assert kept == [] and held[0]["oracle"]["source"] == "unwarranted"


def test_empty_items_stay_empty():
    for w in (TECH, BIZ_UNCONF, None):
        kept, held = oracle_gate.warrant_split(w, [], rule_id="X")
        assert kept == [] and held == []


def test_held_carries_rule_id_and_finding():
    _, held = oracle_gate.warrant_split(BIZ_UNCONF, [F], rule_id="X")
    assert held[0]["rule_id"] == "X" and held[0]["finding"] is F


# ------------------------------------------------- B 判据声明完整性（机器执行）
def _file_cfg() -> dict:
    return json.loads((PILOT / "file_findings_config.json").read_text(encoding="utf-8"))


def _class_cd() -> dict:
    return json.loads((PILOT / "class_scenarios.json").read_text(encoding="utf-8"))["contract_diff"]


def _contract_warrants() -> dict:
    """静态读出 contract_diff.py 的 WARRANTS（不执行模块，避免读产物）。"""
    import ast
    tree = ast.parse((PILOT / "contract_diff.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "WARRANTS":
            return ast.literal_eval(node.value)
    pytest.fail("contract_diff.py 里找不到 WARRANTS 声明")


def _all_warrants() -> dict:
    out = {}
    for r in _file_cfg()["findings"]:
        if r.get("kind") == "defect":
            out[r["id"]] = r.get("warrant")
    out.update(_contract_warrants())
    cd = _class_cd()
    for key in ("put_wipes", "put_guarded"):
        blk = cd["write_footprint"].get(key)
        if blk and blk.get("kind", "defect") == "defect":
            out[blk["id"]] = blk.get("warrant")
    for r in cd.get("error_contract", []):
        if r.get("kind", "defect") == "defect":
            out[r["id"]] = r.get("warrant")
    return out


def test_every_defect_door_entry_declares_a_warrant():
    """无声明 = 未裁定，不得静默开单。这条守卫保证普查没有漏项。"""
    missing = [k for k, w in _all_warrants().items() if not w]
    assert missing == [], f"这些开单条目没有 warrant 声明：{missing}"


def test_warrant_kinds_are_valid():
    bad = {k: (w or {}).get("kind") for k, w in _all_warrants().items()
           if (w or {}).get("kind") not in oracle_gate.WARRANT_KINDS}
    assert bad == {}


def test_technical_warrants_name_an_authority_and_pass_domain_swap():
    """A 类必须写出通用权威出处，且域替换成立 —— 不许只写「technical」蒙混。"""
    bad = []
    for k, w in _all_warrants().items():
        if w["kind"] != "technical":
            continue
        if not (w.get("authority") or "").strip():
            bad.append((k, "authority 为空"))
        if w.get("domain_swap") != "holds":
            bad.append((k, "domain_swap 不是 holds"))
        if not (w.get("basis") or "").strip():
            bad.append((k, "basis 为空"))
        if not (w.get("evidence") or "").strip():
            bad.append((k, "缺 file:line 证据"))
    assert bad == []


def test_business_warrants_fail_domain_swap_and_carry_an_oracle():
    bad = []
    for k, w in _all_warrants().items():
        if w["kind"] != "business":
            continue
        if w.get("domain_swap") != "fails":
            bad.append((k, "domain_swap 不是 fails"))
        o = w.get("oracle") or {}
        if o.get("confirmation") not in ("confirmed", "unconfirmed", "refuted"):
            bad.append((k, f"oracle 确认状态非法：{o.get('confirmation')!r}"))
        if not (w.get("basis") or "").strip():
            bad.append((k, "basis 为空"))
    assert bad == []


def test_contract_diff_add_call_sites_are_all_covered():
    """门普查：add() 的每个 id 都必须在 WARRANTS 里，否则走「未裁定」保守分支。"""
    import re
    text = (PILOT / "contract_diff.py").read_text(encoding="utf-8")
    used = set(re.findall(r'add\(\s*"([A-Z_0-9]+)"', text))
    declared = set(_contract_warrants())
    assert used <= declared, f"这些 add() 的 id 没有 warrant：{sorted(used - declared)}"


@pytest.mark.parametrize("fid", sorted(
    k for k, w in _all_warrants().items() if w["kind"] == "technical"))
def test_every_real_technical_warrant_files(fid):
    """A 类逐条阳性对照：真实声明的每个 technical 都必须放行（防漏报）。"""
    kept, held = oracle_gate.warrant_split(_all_warrants()[fid], [dict(F, id=fid)],
                                           rule_id=fid)
    assert len(kept) == 1 and held == []


# ---------------------------------------------------------------- C 真实台账
def _ledger() -> list[dict]:
    return json.loads((PILOT / "all_findings.json").read_text(encoding="utf-8"))["findings"]


def test_business_without_confirmed_oracle_is_not_in_the_ledger():
    led = {f["id"] for f in _ledger()}
    offenders = [k for k, w in _all_warrants().items()
                 if w["kind"] == "business"
                 and (w.get("oracle") or {}).get("confirmation") != "confirmed"
                 and k in led]
    assert offenders == [], f"B 类无 confirmed oracle 却进了台账：{offenders}"


def test_business_with_confirmed_oracle_stays_in_the_ledger():
    """阳性对照：B 类有 confirmed oracle 必须照样开单，闸门不得过度压制。

    只查当前有观测触发的门；ENVELOPE_BREAK 在真实数据上不触发，
    由 test_contract_door_end_to_end 的合成夹具覆盖。
    """
    led = {f["id"] for f in _ledger()}
    firing_confirmed = {"FILE-ERROR-BODY-SHAPE-INCONSISTENT"}
    for k, w in _all_warrants().items():
        if (w["kind"] == "business"
                and (w.get("oracle") or {}).get("confirmation") == "confirmed"
                and k in firing_confirmed):
            assert k in led, f"{k} 有 confirmed oracle 却被压掉了"
    assert firing_confirmed <= led


def test_technical_defects_are_all_in_the_ledger():
    """A 类逐条回查真实台账：一个都不许少。"""
    led = {f["id"] for f in _ledger()}
    # LATENCY 等门当前无观测触发，不在台账里属正常；只查已知会触发的那些
    firing = {"NO_AUTHZ", "UNHANDLED_HTTP_EXCEPTION", "INTERNAL_LEAK",
              "FILE-CORS-INTRANET-WIDE", "FILE-XFF-CLIENT-IP-SPOOF",
              "FILE-BADREQUEST-ECHO", "FILE-DB-CONSTRAINT-LEAK",
              "FILE-PKG-MUTATE-BEFORE-VALIDATE", "FILE-STATIC-ROOT-NO-TRAILING-SLASH",
              "FILE-TRACEID-CLIENT-CONTROLLED", "FILE-UPLOAD-LIMIT-SPLIT-ACROSS-THREE-FILES",
              "FILE-DUPLICATE-HELPERS-ACROSS-SERVICES", "CLASS-ERROR-MESSAGE-MISMATCH"}
    assert sorted(firing - led) == [], f"A 类技术缺陷被误压：{sorted(firing - led)}"


def test_non_defect_kinds_are_not_gated():
    """positive / observation 不是开单，不得被闸门影响。"""
    led = {f["id"] for f in _ledger()}
    for r in _file_cfg()["findings"]:
        if r.get("kind") in ("positive", "observation"):
            assert r["id"] in led, f"{r['id']} 是 {r['kind']}，不该被闸门压掉"
    assert "CLASS-PUT-REQUIRED-FK-GUARDED" in led


# ---------------------------------------------------------------- D 门级端到端
def _file_sandbox(tmp_path: Path, *, mutation: str | None = None) -> Path:
    """合成夹具：不依赖任何 gitignore 产物，fresh clone 可跑。"""
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    art = root / "artifacts"
    art.mkdir()
    for rel in ("engine/file_diff.py", "engine/findings_schema.py", "engine/oracle_gate.py"):
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")
    cfg = {"target_name": "sandbox", "derived": {}, "findings": [
        {"id": "T-DEFECT", "severity": "HIGH", "kind": "defect", "layer": "file",
         "title": "技术缺陷", "detail": "d", "evidence": {},
         "root_cause_file": "X.java", "warrant": TECH},
        {"id": "B-UNCONF", "severity": "MEDIUM", "kind": "defect", "layer": "file",
         "title": "业务预期未确认", "detail": "d", "evidence": {},
         "root_cause_file": "X.java", "warrant": BIZ_UNCONF},
        {"id": "B-CONF", "severity": "LOW", "kind": "defect", "layer": "file",
         "title": "业务契约已确认", "detail": "d", "evidence": {},
         "root_cause_file": "X.java", "warrant": BIZ_CONF},
        {"id": "NO-WARRANT", "severity": "LOW", "kind": "defect", "layer": "file",
         "title": "未裁定", "detail": "d", "evidence": {}, "root_cause_file": "X.java"},
        {"id": "P-POS", "severity": "INFO", "kind": "positive", "layer": "file",
         "title": "正面结论", "detail": "d", "evidence": {}, "root_cause_file": "X.java"},
    ]}
    (root / "pilot" / "file_findings_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    (root / "pilot" / "target.json").write_text(json.dumps({
        "target": "sandbox", "contract": "../layers.yaml", "source_roots": {},
        "annotation_table": "finding_meta.py", "evidence_sources": [],
        "paths": {"artifacts_dir": str(art)}}), encoding="utf-8")
    (art / "file_declaration.json").write_text(
        json.dumps({"files": [], "meta": {"files": 0, "methods": 0}}), encoding="utf-8")
    (art / "file_observation.json").write_text(
        json.dumps({"results": [], "meta": {"scenarios": 0, "http_probes": 0}}),
        encoding="utf-8")
    if mutation:
        target = root / "pilot" / "engine" / "file_diff.py"
        text = target.read_text(encoding="utf-8")
        anchor, repl = FILE_MUTANTS[mutation]
        assert anchor in text, f"变异锚点未命中：{mutation}"
        mutated = text.replace(anchor, repl)
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    return root


FILE_MUTANTS = {
    # M6 拆掉闸门：所有 defect 无条件开单
    "no_gate": ("""        if out.get("kind") == "defect":
            kept, gated = oracle_gate.warrant_split(
                rec.get("warrant"), [out], rule_id=out.get("id", ""))
            findings.extend(kept)
            held.extend(gated)
        else:
            findings.append(out)""",
                "        findings.append(out)"),
    # M7 过度扩大：technical 也走 oracle（A 类被误压 = 漏报）
    "suppress_technical": ("""        if out.get("kind") == "defect":
            kept, gated = oracle_gate.warrant_split(
                rec.get("warrant"), [out], rule_id=out.get("id", ""))""",
                """        if out.get("kind") == "defect":
            kept, gated = oracle_gate.warrant_split(
                {"kind": "business", "oracle": (rec.get("warrant") or {}).get("oracle")},
                [out], rule_id=out.get("id", ""))"""),
}


def _run_file_cli(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    return subprocess.run([sys.executable, str(root / "pilot" / "engine" / "file_diff.py")],
                          cwd=str(root), env=env, capture_output=True, text=True)


def _assert_file_cli(root: Path) -> None:
    proc = _run_file_cli(root)
    assert proc.returncode == 0, proc.stderr
    art = root / "artifacts"
    fs = json.loads((art / "file_findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in fs}
    assert "T-DEFECT" in ids, "A 类技术缺陷被误压（漏报，比假阳性更危险）"
    assert "B-CONF" in ids, "B 类有 confirmed oracle 必须开单"
    assert "P-POS" in ids, "positive 不是开单，不得被闸门影响"
    assert "B-UNCONF" not in ids, "B 类无 confirmed oracle 不得开单"
    assert "NO-WARRANT" not in ids, "未声明判据来源不得静默开单"
    sp = art / "file_rule_suspects.json"
    assert sp.is_file(), "疑点清单必须落盘"
    sus = json.loads(sp.read_text(encoding="utf-8"))
    held = {h["finding"]["id"] for h in sus["unconfirmed"]}
    assert held == {"B-UNCONF", "NO-WARRANT"}
    assert sus["unconfirmed_count"] == len(sus["unconfirmed"])


def test_file_door_end_to_end(tmp_path):
    _assert_file_cli(_file_sandbox(tmp_path))


def _contract_sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    art = root / "pilot"
    for rel in ("contract_diff.py", "project.py", "engine/oracle_gate.py"):
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")
    (art / "declaration.json").write_text(json.dumps({
        "global": {"security_annotations": [], "security_on_classpath": False,
                   "error_codes": ["OK"]},
        "operations": [{"key": "GET /api/x", "method": "GET", "path": "/api/x",
                        "declared_status": [200], "response_props": []}]}),
        encoding="utf-8")
    (art / "observation.json").write_text(json.dumps({"observations": [
        {"key": "GET /api/x", "scenario": "valid", "status": 200, "latency_ms": 300,
         "request": "/api/x", "envelope_ok": True, "data_keys": [], "code": "OK"},
        {"key": "GET /api/x", "scenario": "bad", "status": 400, "latency_ms": 10,
         "request": "/api/x", "envelope_ok": False, "json": {}}]}),
        encoding="utf-8")
    (art / "target.json").write_text(json.dumps({
        "target": "sandbox", "contract": "../layers.yaml", "source_roots": {},
        "annotation_table": "finding_meta.py", "evidence_sources": [],
        "paths": {"artifacts_dir": str(art), "backend": str(root / "backend"),
                  "java_package": "com/example"}}), encoding="utf-8")
    return root


def test_contract_door_end_to_end(tmp_path):
    """A（NO_AUTHZ）开单 / B-confirmed（ENVELOPE_BREAK）开单 / B-unconfirmed（LATENCY）留痕。"""
    root = _contract_sandbox(tmp_path)
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    proc = subprocess.run([sys.executable, str(root / "pilot" / "contract_diff.py")],
                          cwd=str(root), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    fs = json.loads((root / "pilot" / "findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in fs}
    assert "NO_AUTHZ" in ids, "A 类零鉴权被误压"
    assert "ENVELOPE_BREAK" in ids, "B 类有 confirmed oracle 必须开单"
    assert "LATENCY" not in ids, "200ms 阈值无文档依据，不得开单"
    sus = json.loads((root / "pilot" / "service_rule_suspects.json").read_text(encoding="utf-8"))
    assert {h["finding"]["id"] for h in sus["unconfirmed"]} == {"LATENCY"}


def test_class_cd_doors_gate_only_business():
    """差分 C/D：A 类错误文案保留，B 类 PUT 语义与枚举大小写进疑点。"""
    paths = [PILOT / n for n in ("class_declaration.json", "class_contract.json",
                                 "class_observation.json")]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        pytest.skip(f"缺流水线观测产物 {missing}")
    cfg = _class_cd()
    out = ccd.run(*(str(p) for p in paths), cfg)
    ids = {f["id"] for f in out["findings"]}
    assert "CLASS-ERROR-MESSAGE-MISMATCH" in ids, "A 类错误文案漂移被误压"
    assert "CLASS-PUT-REQUIRED-FK-GUARDED" in ids, "positive 不得被闸门影响"
    assert "CLASS-PUT-WIPES-UNSET-FIELD" not in ids
    assert "CLASS-ENUM-CASE-SENSITIVE" not in ids
    held = {h.get("rule_id") for h in out["rule_suspects"]}
    assert {"CLASS-PUT-WIPES-UNSET-FIELD", "CLASS-ENUM-CASE-SENSITIVE"} <= held


# ---------------------------------------------------------------- E 双向变异
def _caught(guard, *args) -> bool:
    try:
        guard(*args)
    except AssertionError:
        return True
    return False


def _guard_business_held():
    kept, _ = oracle_gate.warrant_split(BIZ_UNCONF, [F], rule_id="X")
    assert kept == []


def _guard_technical_filed():
    kept, held = oracle_gate.warrant_split(TECH, [F], rule_id="X")
    assert kept == [F] and held == []


def _guard_business_confirmed_filed():
    kept, _ = oracle_gate.warrant_split(BIZ_CONF, [F], rule_id="X")
    assert kept == [F]


def _guard_unwarranted_held():
    kept, held = oracle_gate.warrant_split(None, [F], rule_id="X")
    assert kept == [] and held[0]["oracle"]["source"] == "unwarranted"


def _guard_authority_required():
    bad = [k for k, w in _all_warrants().items()
           if w["kind"] == "technical" and not (w.get("authority") or "").strip()]
    assert bad == []


def test_baseline_guards_are_green_before_mutating():
    for g in (_guard_business_held, _guard_technical_filed,
              _guard_business_confirmed_filed, _guard_unwarranted_held,
              _guard_authority_required):
        g()


def test_mutation_gate_always_files_is_caught(monkeypatch):
    """M1 闸门恒放行 → B 类无 oracle 也开单 → 必须红。"""
    monkeypatch.setattr(oracle_gate, "warrant_split",
                        lambda w, items, rule_id="": (list(items), []))
    assert _caught(_guard_business_held)


def test_mutation_technical_suppressed_is_caught(monkeypatch):
    """M2 **反向**：把 technical 也当 business 压 → A 类漏报 → 必须红。

    注意：不能只把 kind 换成 business 而留 oracle=None —— 那会被
    「业务预期缺 oracle → unconfirmed」正确接住，看似变异其实等价（坑 87）。
    这里显式塞一个 unconfirmed oracle，确保行为真的变了。
    """
    real = oracle_gate.warrant_split
    monkeypatch.setattr(oracle_gate, "warrant_split",
                        lambda w, items, rule_id="": real(
                            {"kind": "business",
                             "oracle": {"confirmation": "unconfirmed"}},
                            items, rule_id=rule_id))
    assert _caught(_guard_technical_filed)


def test_mutation_confirmed_business_suppressed_is_caught(monkeypatch):
    """M3 把 confirmed 的业务契约也压掉 → 过度压制 → 必须红。"""
    monkeypatch.setattr(oracle_gate, "confirmation", lambda o: "unconfirmed" if o else "")
    assert _caught(_guard_business_confirmed_filed)


def test_mutation_unwarranted_silently_files_is_caught(monkeypatch):
    """M4 未声明 warrant 静默开单 → 必须红。"""
    real = oracle_gate.warrant_split
    monkeypatch.setattr(oracle_gate, "warrant_split",
                        lambda w, items, rule_id="": (list(items), []) if not w
                        else real(w, items, rule_id=rule_id))
    assert _caught(_guard_unwarranted_held)


def test_mutation_empty_authority_accepted_is_caught(monkeypatch):
    """M5 technical 的 authority 被抽空仍算合法 → 判据退化 → 必须红。"""
    real = _all_warrants
    def wash():
        out = real()
        for w in out.values():
            if w["kind"] == "technical":
                w["authority"] = ""
        return out
    monkeypatch.setattr(sys.modules[__name__], "_all_warrants", wash)
    assert _caught(_guard_authority_required)


def test_file_level_mutation_no_gate_is_caught(tmp_path):
    """M6 文件级：拆掉 file_diff 闸门 → CLI 守卫必须红。"""
    _assert_file_cli(_file_sandbox(tmp_path / "ok"))
    assert _caught(_assert_file_cli, _file_sandbox(tmp_path / "m", mutation="no_gate"))


def test_file_level_mutation_suppressing_technical_is_caught(tmp_path):
    """M7 文件级**反向**：A 类技术缺陷被误压 → CLI 守卫必须红。"""
    _assert_file_cli(_file_sandbox(tmp_path / "ok"))
    assert _caught(_assert_file_cli,
                   _file_sandbox(tmp_path / "m", mutation="suppress_technical"))


def test_file_level_declaration_flip_reopens_the_defect(tmp_path):
    """M8 文件级：把 B 类声明改成 confirmed → 必须重新开单（证明不是按 id 拉黑）。"""
    root = _file_sandbox(tmp_path / "flip")
    cfgp = root / "pilot" / "file_findings_config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    for r in cfg["findings"]:
        if r["id"] == "B-UNCONF":
            r["warrant"] = BIZ_CONF
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    proc = _run_file_cli(root)
    assert proc.returncode == 0, proc.stderr
    ids = {f["id"] for f in json.loads(
        (root / "artifacts" / "file_findings.json").read_text(encoding="utf-8"))}
    assert "B-UNCONF" in ids, "改成 confirmed 后必须重新开单 —— 闸门不是 id 黑名单"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))