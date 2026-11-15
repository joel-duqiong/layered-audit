"""差分 A（列级可空性）的 field_notes 门 —— 硬约束 5 的第三扇开单门。

前两扇门已装闸门：`class_contract_diff.rule_oracle_buckets`（规则三层全缺）与
`class_rules.oracle_gate`（规则派发层）。第三扇是 `diff_nullability` 的 field_notes：
项目侧给特定列写的结论文案，只要 DB NOT NULL 而 DTO 无约束就开单，**从不问 oracle**。

实测抓到的假阳性（本组守卫锁的就是它）：

    CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING
    MEDIUM / confidence 0.92 / ground_truth=verified / kind=defect
    「orders.address 声明 NOT NULL，实际用空串绕过 —— 非空约束只挡了 NULL」

而 `/Users/june/jiuyan/docs/API.md:307` 明写「`address` 可为空；为空时后端保存为空
字符串，避免前端非必填地址导致订单生成失败」——带理由的有意设计。验收用例
`test_create_order_empty_address`（tests/auto-api/tests/test_orders.py:19-21）
断言的正是这个行为，即**存在一条 confirmed 的可执行 oracle 断言相反预期**。

空串既不是 NULL 也不违反 `nullable=false`：两条声明都被满足，约束没有被绕过。
「address 必须非空白」是比任何来源都更严的发明规则。原结论还编造了后果
「宴会执行单拿不到地址」——「执行单」在被测仓库全文 0 命中。

**「库里落空串」是客观观测；「所以这是缺陷」需要业务预期。**
观测保留（旁路留痕带完整 evidence），缺陷断言撤销。
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

import class_contract_diff as ccd  # noqa: E402
import class_rules  # noqa: E402

NOTNULL = "CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING"
LABEL = "address 缺省（DTO 无约束，DB NOT NULL）"

# class_diff.py 的完整依赖闭包（五个模块，全部只依赖标准库）
SANDBOX_MODULES = ("class_diff.py", "class_contract_diff.py", "class_rules.py",
                   "project.py", "file_carrier.py")
# class_contract_diff 的差分 C/D 闸门消费引擎模块，沙箱必须一并拷入（相对层级 engine/）
SANDBOX_ENGINE_MODULES = ("engine/oracle_gate.py",)

# 文件级变异（subprocess 守卫读磁盘原文，同进程 monkeypatch 杀不死它）
_MODULE_MUTANTS = {
    # M5 拆掉闸门 —— 复现本假阳性
    "gate": ("""            kept, gated = oracle_gate(
                {"id": note["id"], "params": {"oracle": note.get("oracle")}},
                [filed])
            findings.extend(kept)
            if held is not None:
                held.extend(gated)""",
             "            findings.append(filed)"),
    # M7 通过结论把被压住的列藏起来（台账看起来「不成立的都列出来了」）
    "hide_held": ("        gated_n = len(held or [])", "        gated_n = 0"),
}


# ---------------------------------------------------------------- 合成夹具
def _note(oracle: dict | None = None, **kw) -> dict:
    note = {
        "match_field": "address", "match_table": "orders",
        "probe_label": LABEL, "evidence_column": "address",
        "id": NOTNULL, "severity": "MEDIUM", "layer": "class",
        "title": "orders.address 声明 NOT NULL，实际用空串绕过",
        "detail": "接口返回 {{http}}，库里落 {{db_value_repr}}。",
        "evidence": {"db_value": "{{db_value}}", "http": "{{http}}"},
    }
    if oracle is not None:
        note["oracle"] = oracle
    note.update(kw)
    return note


def _ctx(notes_field: str = "address") -> dict:
    """DB NOT NULL + DTO 无约束 + 实测 200 落空串 —— 与真实观测同形。"""
    return {
        "decl": {"jpa": {}, "ddl": {"tables": {}, "indexes": {}}},
        "contract": {
            "reflection": [{"kind": "record", "simple_name": "CreateOrderRequest",
                            "components": [{"name": notes_field, "annotations": {}}]}],
            "db_actual": {"tables": {"orders": {"columns": {
                "address": {"nullable": False, "default": None}},
                "constraints": {}}}},
        },
        "obs": {
            "contract_boundary": [{"label": LABEL, "http": 200, "code": "OK",
                                   "db_row": {"address": ""}}],
            "defense_audit": {"constraint_probes": []},
        },
    }


def _cfg(*notes: dict) -> dict:
    return {"dto_target": {"CreateOrderRequest": ["orders", "BanquetOrder"]},
            "field_notes": list(notes)}


PRICE_LABEL = "finalPriceCent=null（DTO 可空，DB NOT NULL）"


def _ctx_two() -> dict:
    """两列：address（被否定）+ final_price_cent（无 oracle，照常开单）。

    阳性对照必须落在**另一列**上 —— 同一列只会匹配到第一条 note。
    """
    ctx = _ctx()
    cols = ctx["contract"]["db_actual"]["tables"]["orders"]["columns"]
    cols["final_price_cent"] = {"nullable": False, "default": None}
    ctx["contract"]["reflection"][0]["components"].append(
        {"name": "finalPriceCent", "annotations": {"Min": {"value": 0}}})
    ctx["obs"]["contract_boundary"].append(
        {"label": PRICE_LABEL, "http": 422, "code": "PRICE_INVALID",
         "message": "金额不能小于 0", "db_row": {"final_price_cent": 0}})
    return ctx


def _filed(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["id"] == NOTNULL]


REFUTED = {"source": "project_doc", "confirmation": "refuted",
           "reference": "/Users/june/jiuyan/docs/API.md:307"}
CONFIRMED = {"source": "project_doc", "confirmation": "confirmed",
             "reference": "doc P1"}
UNCONFIRMED = {"source": "inferred", "confirmation": "unconfirmed"}


# ---------------------------------------------------------------- A 闸门语义
def test_refuted_note_is_not_filed():
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(REFUTED)), held)
    assert _filed(out) == []
    assert len(held) == 1
    assert held[0]["oracle"]["confirmation"] == "refuted"
    assert held[0]["rule_id"] == NOTNULL


def test_unconfirmed_note_is_not_filed():
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(UNCONFIRMED)), held)
    assert _filed(out) == []
    assert len(held) == 1


def test_confirmed_note_is_filed():
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(CONFIRMED)), held)
    assert len(_filed(out)) == 1
    assert held == []


def test_note_without_oracle_is_filed_under_current_caliber():
    """锁当前口径：闸门只对**声明了 oracle** 的 note 生效，与派发层 oracle_gate 一致。

    这是一条**显式声明的宽松默认**，不是被忽略的洞：field_notes 门与派发层共用
    同一口径。若要把它收紧成「缺 oracle 一律 unconfirmed」（差分 B 的口径），
    那是一次有意的契约变更，必须同时裁定现存其余 note，不得静默改动。
    """
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(None)), held)
    assert len(_filed(out)) == 1
    assert held == []


def test_unknown_confirmation_value_is_conservative():
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note({"confirmation": "也许吧"})), held)
    assert _filed(out) == []
    assert len(held) == 1
    assert held[0]["oracle"]["confirmation"] == "unconfirmed"


def test_held_record_keeps_the_observation():
    """refuted 只保留观测 —— 观测本身（200 + 空串）必须完整留在留痕里。"""
    held: list[dict] = []
    ccd.diff_nullability(_ctx(), _cfg(_note(REFUTED)), held)
    ev = held[0]["finding"]["evidence"]
    assert ev["db_value"] == ""
    assert ev["http"] == 200


def test_gate_is_not_an_id_blacklist():
    """把同一条 note 的 oracle 改成 confirmed 必须重新开单。

    证明修的是**闸门**，不是按 id 硬编码拉黑这一条。
    """
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(CONFIRMED)), held)
    assert len(_filed(out)) == 1


def test_other_notes_are_not_over_suppressed():
    """阳性对照：同一份配置里另一列上没有 oracle 的 note 照常开单。"""
    other = _note(None, id="CLASS-OTHER", match_field="finalPriceCent",
                  probe_label=PRICE_LABEL)
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx_two(), _cfg(_note(REFUTED), other), held)
    assert _filed(out) == []
    assert [f["id"] for f in out if f["id"] == "CLASS-OTHER"] == ["CLASS-OTHER"]
    assert len(held) == 1


def test_run_routes_held_into_the_refuted_bypass(tmp_path):
    """被压下来的结论必须进 run() 的旁路，不能悄悄消失。"""
    decl, contract, obs = (tmp_path / n for n in
                           ("decl.json", "contract.json", "obs.json"))
    ctx = _ctx()
    decl.write_text(json.dumps(ctx["decl"]), encoding="utf-8")
    contract.write_text(json.dumps(ctx["contract"]), encoding="utf-8")
    obs.write_text(json.dumps(ctx["obs"]), encoding="utf-8")
    out = ccd.run(str(decl), str(contract), str(obs), _cfg(_note(REFUTED)))
    assert _filed(out["findings"]) == []
    assert [h["rule_id"] for h in out["rule_refuted"]] == [NOTNULL]
    assert out["rule_suspects"] == []


def test_run_routes_unconfirmed_into_the_suspect_bypass(tmp_path):
    decl, contract, obs = (tmp_path / n for n in
                           ("decl.json", "contract.json", "obs.json"))
    ctx = _ctx()
    decl.write_text(json.dumps(ctx["decl"]), encoding="utf-8")
    contract.write_text(json.dumps(ctx["contract"]), encoding="utf-8")
    obs.write_text(json.dumps(ctx["obs"]), encoding="utf-8")
    out = ccd.run(str(decl), str(contract), str(obs), _cfg(_note(UNCONFIRMED)))
    assert _filed(out["findings"]) == []
    assert [h["rule_id"] for h in out["rule_suspects"]] == [NOTNULL]
    assert out["rule_refuted"] == []


def _ctx_with_aligned() -> dict:
    """三列：address（被否定）+ final_price_cent（无 note）+ customer_name（三方一致）。"""
    ctx = _ctx_two()
    cols = ctx["contract"]["db_actual"]["tables"]["orders"]["columns"]
    cols["customer_name"] = {"nullable": False, "default": None}
    ctx["contract"]["reflection"][0]["components"].append(
        {"name": "customerName", "annotations": {"NotBlank": {}}})
    return ctx


def _aligned(findings: list[dict]) -> dict:
    hits = [f for f in findings if f["id"] == "CLASS-NULLABILITY-ALIGNED"]
    assert len(hits) == 1
    return hits[0]


def test_aligned_finding_does_not_hide_the_held_column():
    """压住一列后，通过结论不得再声称「不成立的只有下面单列出来的那几列」。

    否则台账里会出现「4 列不成立、只列 3 列」而读者无从知道第 4 列去哪了。
    """
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx_with_aligned(), _cfg(_note(REFUTED)), held)
    f = _aligned(out)
    assert "不成立的只有下面单列出来的那几列" not in f["detail"]
    assert "1 列的业务预期未确认或被文档否定" in f["detail"]
    assert f["evidence"]["expectation_held"] == 1
    assert f["evidence"]["aligned_columns"] == 1
    assert f["evidence"]["total_columns"] == 3


def test_aligned_finding_unchanged_when_nothing_is_held():
    """阳性对照：没有列被压住时，通过结论的文案与计数一字不变。"""
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx_with_aligned(), _cfg(), held)
    f = _aligned(out)
    assert "不成立的只有下面单列出来的那几列" in f["detail"]
    assert f["evidence"]["expectation_held"] == 0


# ---------------------------------------------------------------- B 真实配置声明
def _real_notes() -> list[dict]:
    cfg = json.loads((PILOT / "class_scenarios.json").read_text(encoding="utf-8"))
    return cfg["contract_diff"]["field_notes"]


def _address_note() -> dict:
    hits = [n for n in _real_notes() if n.get("id") == NOTNULL]
    assert len(hits) == 1, "address 的 field_note 必须唯一存在（撤销裁定要有载体）"
    return hits[0]


def test_real_address_note_declares_refuted():
    assert _address_note()["oracle"]["confirmation"] == "refuted"


def test_real_address_note_cites_the_refuting_line():
    oracle = _address_note()["oracle"]
    assert oracle["reference"].endswith("docs/API.md:307")
    assert "为空时后端保存为空字符串" in oracle["text"]


def test_real_address_note_cites_the_executable_oracle():
    """否定它的不只是散文，还有一条可执行验收用例 —— 必须被引用到。"""
    refs = [c["reference"] for c in _address_note()["oracle"]["corroborated_by"]]
    assert any(r.endswith("tests/test_orders.py:19") for r in refs)


def test_real_address_note_drops_the_fabricated_consequence():
    note = _address_note()
    assert "宴会执行单" not in note["detail"]
    assert "宴会执行单" not in note["title"]


def test_real_config_retracts_the_defect_on_real_observations():
    """真实配置 + 真实冻结观测：这条缺陷不得出现在 findings 里。

    观测产物（class_declaration/class_contract/class_observation）是流水线产物，
    fresh clone 里可能不存在；缺失时显式 skip，不静默当成通过。
    """
    paths = [PILOT / n for n in ("class_declaration.json", "class_contract.json",
                                 "class_observation.json")]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        pytest.skip(f"缺流水线观测产物 {missing}；本条在带产物的工作区跑")
    cfg = json.loads((PILOT / "class_scenarios.json").read_text(encoding="utf-8"))
    out = ccd.run(*(str(p) for p in paths), cfg["contract_diff"])
    assert _filed(out["findings"]) == []
    held = [h for h in out["rule_refuted"] if h.get("rule_id") == NOTNULL]
    assert len(held) == 1
    assert held[0]["finding"]["evidence"]["db_value"] == ""


@pytest.mark.parametrize("entry", ["primary", "corroborated"])
def test_every_cited_reference_resolves_to_the_original_text(entry):
    """引用的 file:line 必须能回原文逐字核到 —— 不许编造依据（坑 1 / 坑 50）。

    原文在被测仓库与验收套件里（只读）。文件不存在时显式 skip。
    """
    oracle = _address_note()["oracle"]
    items = ([{"reference": oracle["reference"], "text": oracle["text"]}]
             if entry == "primary" else oracle["corroborated_by"])
    checked = 0
    for item in items:
        ref = item["reference"]
        path, _, line = ref.rpartition(":")
        if not path or not line.isdigit():
            pytest.fail(f"reference 不是 file:line 形态：{ref}")
        src = Path(path)
        if not src.is_file():
            pytest.skip(f"原文不可达（只读被测系统不在本机）：{path}")
        actual = src.read_text(encoding="utf-8").splitlines()[int(line) - 1]
        assert item["text"] in actual, (
            f"{ref} 原文是 {actual!r}，不含引用的 {item['text']!r}")
        checked += 1
    assert checked > 0


# ---------------------------------------------------------------- C CLI 端到端
def _sandbox(tmp_path: Path, *, module_mutation: str | None = None,
             flip_confirmation: bool = False) -> Path:
    """建一个自包含沙箱：五个模块 + 最小项目绑定 + 真实 field_notes 声明。

    不依赖任何 gitignore 的流水线产物，也不写仓库一个字节。
    """
    root = tmp_path / "sb"
    (root / "pilot").mkdir(parents=True)
    artifacts = root / "artifacts"
    artifacts.mkdir()
    for name in SANDBOX_MODULES:
        shutil.copyfile(PILOT / name, root / "pilot" / name)
    (root / "pilot" / "engine").mkdir(exist_ok=True)
    for rel in SANDBOX_ENGINE_MODULES:
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)

    if module_mutation:
        target = root / "pilot" / "class_contract_diff.py"
        text = target.read_text(encoding="utf-8")
        anchor, replacement = _MODULE_MUTANTS[module_mutation]
        assert anchor in text, "变异锚点未命中 —— 必须显式失败，不得计入 killed"
        mutated = text.replace(anchor, replacement)
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")

    cfg = json.loads((PILOT / "class_scenarios.json").read_text(encoding="utf-8"))
    cd = cfg["contract_diff"]
    notes = cd["field_notes"]
    if flip_confirmation:
        notes = [dict(n, oracle=dict(n["oracle"], confirmation="confirmed"))
                 if n.get("id") == NOTNULL else n for n in notes]
    (root / "pilot" / "class_scenarios.json").write_text(json.dumps(
        {"contract_diff": {"dto_target": cd["dto_target"],
                           "server_derived": cd.get("server_derived", []),
                           "field_notes": notes}}, ensure_ascii=False),
        encoding="utf-8")
    (root / "pilot" / "target.json").write_text(json.dumps(
        {"target": "sandbox", "paths": {"artifacts_dir": str(artifacts),
                                       "backend": str(root / "backend")}}),
        encoding="utf-8")

    ctx = _ctx_with_aligned()
    (artifacts / "class_declaration.json").write_text(
        json.dumps(ctx["decl"]), encoding="utf-8")
    (artifacts / "class_contract.json").write_text(
        json.dumps(ctx["contract"]), encoding="utf-8")
    (artifacts / "class_observation.json").write_text(
        json.dumps(ctx["obs"]), encoding="utf-8")
    return root


def _run_cli(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    return subprocess.run([sys.executable, str(root / "pilot" / "class_diff.py")],
                          cwd=str(root), env=env, capture_output=True, text=True)


def _assert_cli_retracts(root: Path) -> None:
    """CLI 级守卫断言：磁盘上的代码跑出来的台账里没有这条缺陷，观测留在旁路。"""
    proc = _run_cli(root)
    assert proc.returncode == 0, proc.stderr
    artifacts = root / "artifacts"
    findings = json.loads((artifacts / "class_findings.json").read_text(encoding="utf-8"))
    assert _filed(findings) == [], "被文档否定的预期不得进缺陷台账"
    suspects = json.loads(
        (artifacts / "class_rule_suspects.json").read_text(encoding="utf-8"))
    held = [h for h in suspects["refuted"] if h.get("rule_id") == NOTNULL]
    assert len(held) == 1, "观测必须留痕，不能悄悄消失"
    assert held[0]["finding"]["evidence"]["db_value"] == ""
    assert suspects["refuted_count"] == len(suspects["refuted"])
    aligned = [f for f in findings if f["id"] == "CLASS-NULLABILITY-ALIGNED"]
    assert len(aligned) == 1
    assert aligned[0]["evidence"]["expectation_held"] == 1
    assert "不成立的只有下面单列出来的那几列" not in aligned[0]["detail"], (
        "通过结论不得把被压住的列藏起来")


def test_cli_retracts_the_defect_and_keeps_the_observation(tmp_path):
    _assert_cli_retracts(_sandbox(tmp_path))


def test_cli_keeps_other_field_notes(tmp_path):
    """阳性对照：CLI 路径上另一列的 note 照常开单（闸门没有过度压制）。"""
    root = _sandbox(tmp_path)
    assert _run_cli(root).returncode == 0
    findings = json.loads(
        (root / "artifacts" / "class_findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in findings}
    assert "CLASS-REQUIRED-CHECKED-IN-SERVICE-LAYER" in ids


# ------------------------------------------------- D 常设变异（守卫必须有牙）
def _caught(guard, *args) -> bool:
    """变异体下守卫是否变红。守卫无牙（恒过）→ 返回 False → 变异测试自己红。"""
    try:
        guard(*args)
    except AssertionError:
        return True
    return False


def _guard_refuted_not_filed() -> None:
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(REFUTED)), held)
    assert _filed(out) == []
    assert len(held) == 1


def _guard_no_oracle_still_filed() -> None:
    held: list[dict] = []
    out = ccd.diff_nullability(_ctx(), _cfg(_note(None)), held)
    assert len(_filed(out)) == 1


def _guard_run_keeps_bypass(tmp_path) -> None:
    decl, contract, obs = (tmp_path / n for n in
                           ("decl.json", "contract.json", "obs.json"))
    ctx = _ctx()
    decl.write_text(json.dumps(ctx["decl"]), encoding="utf-8")
    contract.write_text(json.dumps(ctx["contract"]), encoding="utf-8")
    obs.write_text(json.dumps(ctx["obs"]), encoding="utf-8")
    out = ccd.run(str(decl), str(contract), str(obs), _cfg(_note(REFUTED)))
    assert [h["rule_id"] for h in out["rule_refuted"]] == [NOTNULL]


def test_baseline_guards_are_green_before_mutating():
    """坑 38：基线不绿则整轮变异失去区分力。"""
    _guard_refuted_not_filed()
    _guard_no_oracle_still_filed()


def test_mutation_gate_removed_is_caught(monkeypatch):
    """M1 拆掉闸门（复现本缺陷）→ 守卫必须红。"""
    monkeypatch.setattr(ccd, "oracle_gate", lambda rule, findings: (findings, []))
    assert _caught(_guard_refuted_not_filed)


def test_mutation_refuted_washed_to_confirmed_is_caught(monkeypatch):
    """M2 把 refuted 洗成 confirmed → 守卫必须红。"""
    def wash(rule, findings):
        oracle = (rule.get("params") or {}).get("oracle") or {}
        return class_rules.oracle_gate(
            {"id": rule.get("id"),
             "params": {"oracle": dict(oracle, confirmation="confirmed")}},
            findings)
    monkeypatch.setattr(ccd, "oracle_gate", wash)
    assert _caught(_guard_refuted_not_filed)


def test_mutation_bypass_dropped_is_caught(monkeypatch, tmp_path):
    """M3 压下来但不留痕（观测悄悄消失）→ 守卫必须红。"""
    original = ccd.diff_nullability
    monkeypatch.setattr(ccd, "diff_nullability",
                        lambda ctx, cfg, held=None: original(ctx, cfg, None))
    assert _caught(_guard_run_keeps_bypass, tmp_path)


def test_mutation_gate_over_reaches_is_caught(monkeypatch):
    """M4 闸门过度扩大（没声明 oracle 也压）→ 守卫必须红。"""
    monkeypatch.setattr(ccd, "oracle_gate", lambda rule, findings: ([], [
        {"rule_id": rule.get("id"),
         "oracle": {"confirmation": "unconfirmed"}, "finding": f}
        for f in findings]))
    assert _caught(_guard_no_oracle_still_filed)


def test_file_level_mutation_reopens_the_defect(tmp_path):
    """M5 **文件级**变异：subprocess 守卫读磁盘原文，同进程 monkeypatch 杀不死它
    （坑 2 的镜像 / 做法 36 / 坑 62）。必须在沙箱里改文件再跑 CLI。

    判据：同一条 CLI 守卫断言，在 pristine 沙箱上通过、在变异沙箱上**必须红**。
    """
    pristine = _sandbox(tmp_path / "ok")
    _assert_cli_retracts(pristine)          # 基线绿，否则本轮作废（坑 38）
    mutated = _sandbox(tmp_path / "mut", module_mutation="gate")
    assert _caught(_assert_cli_retracts, mutated), (
        "文件级变异未被咬住：CLI 守卫无牙，或变异根本没改到文件")


def test_file_level_mutation_hiding_the_held_column_is_caught(tmp_path):
    """M7 文件级变异：通过结论把被压住的列藏起来 → CLI 守卫必须红。"""
    pristine = _sandbox(tmp_path / "ok")
    _assert_cli_retracts(pristine)
    mutated = _sandbox(tmp_path / "hide", module_mutation="hide_held")
    assert _caught(_assert_cli_retracts, mutated)


def test_file_level_declaration_flip_reopens_the_defect(tmp_path):
    """M6 文件级变异另一条路径：把声明里的 refuted 改成 confirmed。

    证明「撤销」来自 oracle 声明，而不是代码里按 id 拉黑。
    """
    pristine = _sandbox(tmp_path / "ok")
    _assert_cli_retracts(pristine)
    flipped = _sandbox(tmp_path / "flip", flip_confirmation=True)
    assert _caught(_assert_cli_retracts, flipped)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))