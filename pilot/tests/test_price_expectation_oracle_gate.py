"""函数层 / 集成层的 oracle 闸门 —— 一条未经确认的业务预期撑起 5 条台账缺陷。

## 裁定（本组守卫锁的就是它）

`FN-PRICE-CLIENT-CONTROLLED`（CRITICAL / confidence 0.97 / ground_truth=verified）
断言「成交价由客户端说了算，resolveFinalPrice 完全不与套餐价比对」是缺陷。

**规范违背不成立**：
  - `OrderService.java:149-157` 逐字实现 `API.md:337-338`
    （useFinalPrice=false → planTotalCent；true → 请求中的 finalPriceCent，只挡 null/<0）
  - `业务校验对照.md:20` 在「## 已确认一致的规则」表里明确记 `resolveFinalPrice`「一致」
  - `AGENTS.md:425-427` 规定勾选成交价就用成交价；:419-421 明确「不要做老板确认功能」
  - `tests/auto-api/tests/test_orders.py:14-16` 是 confirmed 的**可执行** oracle：
    finalPriceCent=5000 / planTotalCent=8888 → 断言保存 5000，即成交价低于方案合计是正确行为

**「成交价须 ≤ 套餐价」这条预期唯一的声明**在 `invariants.json` 的
`BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE`：`source=inferred` / `confirmation=unconfirmed` /
`reference=「无文档依据（现有文档只规定金额 >= 0）」`。服务层已正确压进
`business_findings.json` 的 `unconfirmed_suspects`；函数层两扇门与集成层一扇门
此前完全绕过它，直接开 CRITICAL/HIGH defect。

**与 address 那条的关键区别**：address 的非空白预期被文档**明确否定** → `refuted`；
本条文档只确认了实现符合规范、从未要求上界 → `unconfirmed`。
所以它是**规范层风险**（价格篡改真实存在），进**待确认疑点清单**，不是「只保留观测」就完事。

**极性翻转（D4）**：原 `related` 把反驳它的验收用例写成
「既有测试把保留客户端成交价写成了断言——缺陷被固化成契约」，即把反证当佐证。

**技术可自证的必须留下**：`FN-NUMERIC-BIGINT-PRECISION-LOSS`（IEEE754，int64 分值
超出 JS 安全整数）不依赖任何业务预期，改挂自己的根因并 own，不得被闸门误伤。
"""
from __future__ import annotations

import ast
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

import class_rules  # noqa: E402
import finding_meta  # noqa: E402
import fn_diff  # noqa: E402
import oracle_gate  # noqa: E402

RETRACTED = "price.final_price.upper_bound_unvalidated"
TECHNICAL = "numeric.amount_exceeds_js_safe_integer"
FN_ID = "FN-PRICE-CLIENT-CONTROLLED"
SEM_ID = "FN-SEMANTIC-PRICE-UPPER-BOUND"
INT_IDS = ("PRICE_CLIENT_CONTROLLED_XL", "INT-THREE-PRICES-NO-DETAIL-TOTAL")

UNCONFIRMED = {"source": "inferred", "confirmation": "unconfirmed",
               "reference": "无文档依据"}
CONFIRMED = {"source": "project_doc", "confirmation": "confirmed",
             "reference": "doc P1"}
REFUTED = {"source": "project_doc", "confirmation": "refuted",
           "reference": "doc 第 65 行"}

F = {"id": "X", "severity": "HIGH", "title": "t"}


# ---------------------------------------------------------------- A 通用闸门
def test_confirmed_is_filed():
    kept, held = oracle_gate.split(CONFIRMED, [F], rule_id="r")
    assert kept == [F] and held == []


def test_unconfirmed_is_held():
    kept, held = oracle_gate.split(UNCONFIRMED, [F], rule_id="r")
    assert kept == [] and len(held) == 1
    assert held[0]["oracle"]["confirmation"] == "unconfirmed"
    assert held[0]["rule_id"] == "r"


def test_refuted_is_held():
    kept, held = oracle_gate.split(REFUTED, [F], rule_id="r")
    assert kept == [] and held[0]["oracle"]["confirmation"] == "refuted"


def test_no_oracle_is_filed_under_declared_caliber():
    """显式口径：闸门只对声明了 oracle 的条目生效，技术可自证的不被误伤。"""
    kept, held = oracle_gate.split(None, [F], rule_id="r")
    assert kept == [F] and held == []


def test_illegal_confirmation_is_conservative():
    kept, held = oracle_gate.split({"confirmation": "也许吧"}, [F])
    assert kept == [] and held[0]["oracle"]["confirmation"] == "unconfirmed"


def test_held_keeps_the_original_finding():
    _, held = oracle_gate.split(UNCONFIRMED, [F], rule_id="r")
    assert held[0]["finding"] is F


def test_buckets_split_by_state():
    held = (oracle_gate.split(UNCONFIRMED, [F])[1]
            + oracle_gate.split(REFUTED, [dict(F, id="Y")])[1])
    b = oracle_gate.buckets(held)
    assert [h["finding"]["id"] for h in b["unconfirmed"]] == ["X"]
    assert [h["finding"]["id"] for h in b["refuted"]] == ["Y"]


def test_split_does_not_alias_the_input_list():
    items = [F]
    kept, _ = oracle_gate.split(CONFIRMED, items)
    kept.append(dict(F, id="Z"))
    assert items == [F]


@pytest.mark.parametrize("oracle", [None, CONFIRMED, UNCONFIRMED, REFUTED,
                                    {"confirmation": "非法值"}, {}])
def test_semantics_agree_with_the_class_layer_gate(oracle):
    """契约守卫：同一个三态语义不得在多个载体里漂移（坑 11）。

    类层派发层 `class_rules.oracle_gate` 与本引擎必须逐例同判。
    """
    rule = {"id": "r", "params": {"oracle": oracle} if oracle is not None else {}}
    kept_a, held_a = class_rules.oracle_gate(rule, [F])
    kept_b, held_b = oracle_gate.split(oracle, [F], rule_id="r")
    assert kept_a == kept_b
    assert [h["oracle"]["confirmation"] for h in held_a] == \
           [h["oracle"]["confirmation"] for h in held_b]
    assert [h["rule_id"] for h in held_a] == [h["rule_id"] for h in held_b]


# ---------------------------------------------------------------- B 函数层两扇门
def _case(cid: str, group: str, verdict: str = "FAIL") -> dict:
    return {"id": cid, "group": group, "verdict": verdict,
            "target": "com.example.Svc#m", "invariant": "断言",
            "reason": "期望 <= 46800，实际 999999", "observation": {}}


def _groups(oracle: dict | None) -> dict:
    meta = {"id": FN_ID, "severity": "CRITICAL", "title": "t", "function": "Svc.m",
            "root_cause": "rc", "api_reachable": True, "boundary_protected": False,
            "boundary_note": "n", "cwe": None, "related": []}
    if oracle is not None:
        meta["oracle"] = oracle
    return {"PRICE_CLIENT_CONTROLLED": meta}


def _sem(oracle: dict | None) -> dict:
    mission = {"id": "OB-X", "unit": "com.example.Svc#m", "mission": "使命",
               "template": "returns_le_arg", "source": "declared",
               "finding": {"id": SEM_ID, "severity": "HIGH", "title": "t"}}
    if oracle is not None:
        mission["oracle"] = oracle
    return {"missions": [mission]}


def test_group_door_holds_unconfirmed():
    held: list[dict] = []
    out = fn_diff.build_findings([_case("APL-15", "PRICE_CLIENT_CONTROLLED")],
                                 _groups(UNCONFIRMED), {}, held)
    assert out == []
    assert [h["finding"]["id"] for h in held] == [FN_ID]
    assert held[0]["finding"]["evidence"][0]["actual"] == "期望 <= 46800，实际 999999"


def test_group_door_files_without_oracle():
    """阳性对照：技术可自证的分组（无 oracle）照常开单。"""
    held: list[dict] = []
    out = fn_diff.build_findings([_case("APL-15", "PRICE_CLIENT_CONTROLLED")],
                                 _groups(None), {}, held)
    assert [f["id"] for f in out] == [FN_ID] and held == []


def test_group_door_files_confirmed():
    held: list[dict] = []
    out = fn_diff.build_findings([_case("APL-15", "PRICE_CLIENT_CONTROLLED")],
                                 _groups(CONFIRMED), {}, held)
    assert [f["id"] for f in out] == [FN_ID] and held == []


def test_semantic_door_holds_unconfirmed():
    held: list[dict] = []
    out = fn_diff.semantic_findings([_case("OB-X", "SEMANTIC_OBLIGATION")],
                                    _sem(UNCONFIRMED), held)
    assert out == []
    assert [h["finding"]["id"] for h in held] == [SEM_ID]


def test_semantic_door_files_without_oracle():
    held: list[dict] = []
    out = fn_diff.semantic_findings([_case("OB-X", "SEMANTIC_OBLIGATION")],
                                    _sem(None), held)
    assert [f["id"] for f in out] == [SEM_ID] and held == []


def test_declared_source_is_not_a_confirmed_oracle():
    """`source=declared` 只表示人写下来了，不等于业务方确认过。"""
    held: list[dict] = []
    sem = _sem(UNCONFIRMED)
    assert sem["missions"][0]["source"] == "declared"
    assert fn_diff.semantic_findings([_case("OB-X", "SEMANTIC_OBLIGATION")],
                                     sem, held) == []


def test_pass_cases_never_file():
    held: list[dict] = []
    out = fn_diff.build_findings([_case("APL-15", "PRICE_CLIENT_CONTROLLED", "PASS")],
                                 _groups(None), {}, held)
    assert out == [] and held == []


# ---------------------------------------------------------------- C 真实声明
def _fn_groups() -> dict:
    return json.loads((PILOT / "fn_groups.json").read_text(encoding="utf-8"))["groups"]


def _group_meta() -> dict:
    hits = [g for g in _fn_groups().values() if g.get("id") == FN_ID]
    assert len(hits) == 1
    return hits[0]


def _mission() -> dict:
    sem = json.loads((PILOT / "semantic_map.json").read_text(encoding="utf-8"))
    hits = [m for m in sem["missions"] if m.get("finding", {}).get("id") == SEM_ID]
    assert len(hits) == 1
    return hits[0]


def _invariant() -> dict:
    inv = json.loads((PILOT / "invariants.json").read_text(encoding="utf-8"))
    hits = [i for i in inv["invariants"]
            if i["id"] == "BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE"]
    assert len(hits) == 1
    return hits[0]


def test_real_group_declares_unconfirmed_not_refuted():
    """规范层风险 ≠ 被文档否定：必须是 unconfirmed，进待确认疑点而不是只留观测。"""
    assert _group_meta()["oracle"]["confirmation"] == "unconfirmed"


def test_real_mission_declares_unconfirmed():
    assert _mission()["oracle"]["confirmation"] == "unconfirmed"


def test_all_three_layers_agree_on_the_same_expectation():
    """同一条预期在函数层/语义地图/服务层必须同一确认状态（口径不得分裂）。"""
    states = {_group_meta()["oracle"]["confirmation"],
              _mission()["oracle"]["confirmation"],
              _invariant()["oracle"]["confirmation"]}
    assert states == {"unconfirmed"}


def test_polarity_inversion_removed():
    """反驳它的验收用例不得再被写成「缺陷被固化成契约」。"""
    related = _group_meta()["related"]
    text = json.dumps(related, ensure_ascii=False)
    assert "缺陷被固化成契约" not in text
    hit = [r for r in related if r[1] == "test_create_order_use_final_price"]
    assert len(hit) == 1
    assert "相反的预期" in hit[0][2]


def _integration_oracles() -> dict:
    """静态读出 integration_diff.py 的 ORACLES 声明（不执行模块，避免读产物）。"""
    tree = ast.parse((PILOT / "integration_diff.py").read_text(encoding="utf-8"))
    literals: dict[str, dict] = {}
    keys: list[str] = []
    for node in tree.body:
        # ORACLES 带类型标注（AnnAssign），_PRICE_EXPECTATION_ORACLE 不带（Assign）
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        else:
            continue
        name = getattr(target, "id", "")
        if name == "_PRICE_EXPECTATION_ORACLE":
            literals[name] = ast.literal_eval(value)
        elif name == "ORACLES":
            keys = [k.value for k in value.keys]
            assert all(v.id == "_PRICE_EXPECTATION_ORACLE" for v in value.values)
    assert literals and keys, "ORACLES 声明形态变了 —— 守卫必须显式失败，不得静默放过"
    return {k: literals["_PRICE_EXPECTATION_ORACLE"] for k in keys}


def test_integration_door_declares_both_citers():
    oracles = _integration_oracles()
    assert set(INT_IDS) <= set(oracles)
    assert all(o["confirmation"] == "unconfirmed" for o in oracles.values())


@pytest.mark.parametrize("source", ["group", "mission", "integration"])
def test_every_cited_reference_resolves_to_the_original_text(source):
    """引用的 file:line 必须能回原文逐字核到（坑 1 / 坑 50）。原文只读，不可达则显式 skip。"""
    if source == "group":
        items = _group_meta()["oracle"]["corroborated_by"]
    elif source == "mission":
        items = _mission()["oracle"]["corroborated_by"]
    else:
        items = _integration_oracles()[INT_IDS[0]]["corroborated_by"]
    checked = 0
    for item in items:
        path, _, line = item["reference"].rpartition(":")
        if not path or not line.isdigit():
            pytest.fail(f"reference 不是 file:line 形态：{item['reference']}")
        src = Path(path)
        if not src.is_file():
            pytest.skip(f"原文不可达（只读被测系统不在本机）：{path}")
        actual = src.read_text(encoding="utf-8").splitlines()[int(line) - 1]
        assert item["text"] in actual, (
            f"{item['reference']} 原文是 {actual!r}，不含引用的 {item['text']!r}")
        checked += 1
    assert checked > 0


# ------------------------------------------- D 技术可自证 vs 业务预期（分类判据）
def test_technical_finding_survives_and_owns_its_root_cause():
    """IEEE754 精度丢失不依赖业务预期：必须仍是 defect，且自己 own 一个根因。"""
    meta = finding_meta.FUNCTION["FN-NUMERIC-BIGINT-PRECISION-LOSS"]
    assert meta.kind == "defect"
    assert meta.root_cause_key == TECHNICAL
    assert meta.relation == "own"


def test_no_defect_still_cites_the_retracted_root_cause():
    """撤销一条业务预期根因后，不得留下引用它的 defect（否则根因无 own 归属）。

    判据：仍挂该根因的每个条目，其开单门必须已声明非 confirmed 的 oracle。
    """
    doors: dict[str, str] = {}
    for g in _fn_groups().values():
        doors[g["id"]] = (g.get("oracle") or {}).get("confirmation", "")
    sem = json.loads((PILOT / "semantic_map.json").read_text(encoding="utf-8"))
    for m in sem["missions"]:
        if m.get("finding"):
            doors[m["finding"]["id"]] = (m.get("oracle") or {}).get("confirmation", "")
    for i in json.loads((PILOT / "invariants.json").read_text(encoding="utf-8"))["invariants"]:
        doors[i["id"]] = (i.get("oracle") or {}).get("confirmation", "")
    for fid, o in _integration_oracles().items():
        doors[fid] = o["confirmation"]

    offenders = []
    for layer, table in finding_meta.META.items():
        for fid, meta in table.items():
            if meta.root_cause_key == RETRACTED and meta.kind == "defect":
                if doors.get(fid) not in ("unconfirmed", "refuted"):
                    offenders.append(f"{layer}:{fid}（门上的确认状态={doors.get(fid)!r}）")
    assert offenders == [], f"这些条目仍以 defect 引用已撤销的根因：{offenders}"


def test_new_root_cause_has_exactly_one_owner():
    owners = [fid for table in finding_meta.META.values()
              for fid, m in table.items()
              if m.root_cause_key == TECHNICAL and m.relation == "own"]
    assert owners == ["FN-NUMERIC-BIGINT-PRECISION-LOSS"]


def _assert_no_duplicated_attribution(text: str) -> None:
    """归属/根因由 finding_meta 单一声明，探针文案不得复述（坑 11 多载体漂移）。"""
    assert RETRACTED not in text, "探针文案仍把条目归给已撤销的根因"
    assert "不重复开单" not in text, "探针文案不得自行声明归属关系"


def test_probe_does_not_duplicate_attribution():
    _assert_no_duplicated_attribution(
        (PILOT / "numeric_props.py").read_text(encoding="utf-8"))


def test_mutation_probe_readds_attribution_is_caught():
    """M9 探针文案重新复述已撤销的根因 → 守卫必须红。"""
    pristine = (PILOT / "numeric_props.py").read_text(encoding="utf-8")
    _assert_no_duplicated_attribution(pristine)
    mutated = pristine.replace(
        "归属与根因由 finding_meta.py 单一声明，",
        f"归属：根因是「金额没有上界」（与 {RETRACTED} 同一根因），这里只量化后果，不重复开单。")
    assert mutated != pristine, "变异锚点未命中"
    assert _caught(_assert_no_duplicated_attribution, mutated)


# ---------------------------------------------------------------- E 端到端沙箱
FN_MODULES = ("engine/fn_diff.py", "engine/findings_schema.py", "engine/oracle_gate.py")


def _fn_sandbox(tmp_path: Path, *, module_mutation: str | None = None,
                flip: bool = False) -> Path:
    """自包含沙箱：真实 fn_groups/semantic_map 声明 + 合成观测，不依赖 gitignore 产物。"""
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    art = root / "artifacts"
    art.mkdir()
    for rel in FN_MODULES:
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")

    if module_mutation == "group_gate":
        target = root / "pilot" / "engine" / "fn_diff.py"
        text = target.read_text(encoding="utf-8")
        anchor = """        kept, gated = oracle_gate.split(meta.get("oracle"), [filed],
                                        rule_id=meta["id"])
        findings.extend(kept)
        if held is not None:
            held.extend(gated)"""
        assert anchor in text, "变异锚点未命中 —— 必须显式失败，不得计入 killed"
        mutated = text.replace(anchor, "        findings.append(filed)")
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    elif module_mutation == "suspects_write":
        target = root / "pilot" / "engine" / "fn_diff.py"
        text = target.read_text(encoding="utf-8")
        anchor = '(art / "fn_rule_suspects.json").write_text('
        assert anchor in text, "变异锚点未命中"
        # 写到别的文件名：fn_rule_suspects.json 不落盘 = 观测悄悄消失
        mutated = text.replace(anchor, '(art / "fn_rule_suspects.disabled").write_text(')
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")

    groups = json.loads((PILOT / "fn_groups.json").read_text(encoding="utf-8"))
    if flip:
        for g in groups["groups"].values():
            if g.get("id") == FN_ID:
                g["oracle"] = dict(g["oracle"], confirmation="confirmed")
    (root / "pilot" / "fn_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(PILOT / "semantic_map.json", root / "pilot" / "semantic_map.json")
    (root / "pilot" / "target.json").write_text(json.dumps({
        "target": "sandbox", "contract": "../layers.yaml", "source_roots": {},
        "annotation_table": "finding_meta.py", "evidence_sources": [],
        "paths": {"artifacts_dir": str(art)}}), encoding="utf-8")

    cases = [_case("APL-15", "PRICE_CLIENT_CONTROLLED"),
             _case("OB-RESOLVE-PRICE-UPPER-BOUND", "SEMANTIC_OBLIGATION"),
             _case("RACE-01", "ORDER_NO_COLLISION")]
    (art / "fn_observation.json").write_text(
        json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    (art / "fn_declaration.json").write_text(
        json.dumps({"meta": {"functions": 3}}), encoding="utf-8")
    return root


def _run_fn_cli(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    return subprocess.run([sys.executable, str(root / "pilot" / "engine" / "fn_diff.py")],
                          cwd=str(root), env=env, capture_output=True, text=True)


def _assert_fn_cli_retracts(root: Path) -> None:
    proc = _run_fn_cli(root)
    assert proc.returncode == 0, proc.stderr
    art = root / "artifacts"
    payload = json.loads((art / "fn_findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in payload["findings"]}
    assert FN_ID not in ids, "未经确认的业务预期不得进缺陷台账"
    assert SEM_ID not in ids
    assert "FN-ORDER-NO-COLLISION" in ids, "技术可自证的结论不得被误伤"
    assert payload["meta"]["expectation_held"] == 2
    suspects_path = art / "fn_rule_suspects.json"
    assert suspects_path.is_file(), "疑点清单必须落盘：被压下来的观测不能悄悄消失"
    suspects = json.loads(suspects_path.read_text(encoding="utf-8"))
    assert suspects["unconfirmed_count"] == 2
    assert suspects["unconfirmed_count"] == len(suspects["unconfirmed"])
    held = {h["finding"]["id"]: h for h in suspects["unconfirmed"]}
    assert set(held) == {FN_ID, SEM_ID}
    assert held[FN_ID]["finding"]["evidence"][0]["actual"] == "期望 <= 46800，实际 999999"


def test_fn_cli_retracts_and_keeps_observations(tmp_path):
    _assert_fn_cli_retracts(_fn_sandbox(tmp_path))


def test_fn_cli_writes_suspects_even_when_empty(tmp_path):
    """「没有疑点」与「没查」必须可区分：无条件落盘。"""
    root = _fn_sandbox(tmp_path)
    groups = json.loads((root / "pilot" / "fn_groups.json").read_text(encoding="utf-8"))
    for g in groups["groups"].values():
        g.pop("oracle", None)
    (root / "pilot" / "fn_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    sem = json.loads((root / "pilot" / "semantic_map.json").read_text(encoding="utf-8"))
    for m in sem["missions"]:
        m.pop("oracle", None)
    (root / "pilot" / "semantic_map.json").write_text(
        json.dumps(sem, ensure_ascii=False), encoding="utf-8")
    assert _run_fn_cli(root).returncode == 0
    suspects = json.loads(
        (root / "artifacts" / "fn_rule_suspects.json").read_text(encoding="utf-8"))
    assert suspects["unconfirmed_count"] == 0 and suspects["refuted_count"] == 0


# ------------------------------------------- F 集成层端到端（需流水线产物）
INT_ARTIFACTS = ("integration_declaration.json", "integration_observation.json",
                 "integration_chains.json", "integration_reconciliation.json")


def _int_sandbox(tmp_path: Path, *, oracles_empty: bool = False) -> Path:
    root = tmp_path / "sb"
    (root / "pilot" / "engine").mkdir(parents=True)
    art = root / "pilot"
    for rel in ("integration_diff.py", "project.py", "engine/oracle_gate.py"):
        shutil.copyfile(PILOT / rel, root / "pilot" / rel)
    for name in INT_ARTIFACTS:
        shutil.copyfile(PILOT / name, art / name)
    # 上游函数层的疑点清单（流水线里函数层先跑）：合成一份，只含被压住的那条
    (art / "fn_rule_suspects.json").write_text(json.dumps({
        "unconfirmed": [{"rule_id": FN_ID, "oracle": UNCONFIRMED,
                         "finding": {"id": FN_ID}}],
        "unconfirmed_count": 1, "refuted": [], "refuted_count": 0,
    }, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(PILOT.parent / "layers.yaml", root / "layers.yaml")
    (art / "target.json").write_text(json.dumps({
        "target": "sandbox", "contract": "../layers.yaml", "source_roots": {},
        "annotation_table": "finding_meta.py", "evidence_sources": [],
        "paths": {"artifacts_dir": str(art)}}), encoding="utf-8")
    if oracles_empty:
        target = root / "pilot" / "integration_diff.py"
        text = target.read_text(encoding="utf-8")
        anchor = ('    "PRICE_CLIENT_CONTROLLED_XL": _PRICE_EXPECTATION_ORACLE,\n'
                  '    "INT-THREE-PRICES-NO-DETAIL-TOTAL": _PRICE_EXPECTATION_ORACLE,')
        assert anchor in text, "变异锚点未命中"
        # 把两个 id 换成不存在的键：这两条结论不再被任何 oracle 声明覆盖
        mutated = text.replace(
            anchor,
            '    "DISABLED_XL": _PRICE_EXPECTATION_ORACLE,\n'
            '    "DISABLED_THREE": _PRICE_EXPECTATION_ORACLE,')
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    return root


def _int_artifacts_available() -> list[str]:
    return [n for n in INT_ARTIFACTS if not (PILOT / n).is_file()]


def _assert_int_cli_retracts(root: Path) -> None:
    env = dict(os.environ, LAYERED_AUDIT_TARGET=str(root / "pilot" / "target.json"))
    proc = subprocess.run([sys.executable, str(root / "pilot" / "integration_diff.py")],
                          cwd=str(root), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((root / "pilot" / "integration_findings.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in doc["findings"]}
    assert not (set(INT_IDS) & ids), "未经确认的业务预期不得进集成层缺陷台账"
    assert doc["summary"]["expectation_held"] == 2
    suspects_path = root / "pilot" / "integration_rule_suspects.json"
    assert suspects_path.is_file(), "疑点清单必须落盘：被压下来的观测不能悄悄消失"
    suspects = json.loads(suspects_path.read_text(encoding="utf-8"))
    assert {h["finding"]["id"] for h in suspects["unconfirmed"]} == set(INT_IDS)
    for p in doc["passed_checks"]:
        if p["check"] == "RECON_CORROBORATES_orders_final_price_cent":
            assert "根因已在function层开单" not in p["desc"]
            assert p["evidence"]["root_cause_filed"] is False
        if p["check"] == "CHAIN_CORROBORATES_CHAIN-C":
            assert "证实了低层缺陷" not in p["desc"]
            assert FN_ID in p["evidence"]["cited_expectation_unconfirmed"]


def test_int_cli_retracts_and_fixes_corroboration(tmp_path):
    missing = _int_artifacts_available()
    if missing:
        pytest.skip(f"缺流水线产物 {missing}；本条在带产物的工作区跑")
    _assert_int_cli_retracts(_int_sandbox(tmp_path))


# ---------------------------------------------------------------- G 常设变异
def _caught(guard, *args) -> bool:
    try:
        guard(*args)
    except AssertionError:
        return True
    return False


def _guard_unconfirmed_held() -> None:
    kept, held = oracle_gate.split(UNCONFIRMED, [F], rule_id="r")
    assert kept == [] and len(held) == 1


def _guard_no_oracle_filed() -> None:
    kept, _ = oracle_gate.split(None, [F], rule_id="r")
    assert kept == [F]


def _guard_group_door() -> None:
    held: list[dict] = []
    out = fn_diff.build_findings([_case("APL-15", "PRICE_CLIENT_CONTROLLED")],
                                 _groups(UNCONFIRMED), {}, held)
    assert out == [] and len(held) == 1


def test_baseline_guards_are_green_before_mutating():
    """坑 38：基线不绿则整轮变异失去区分力。"""
    _guard_unconfirmed_held()
    _guard_no_oracle_filed()
    _guard_group_door()


def test_mutation_gate_always_passes_is_caught(monkeypatch):
    """M1 拆掉闸门 → 守卫必须红。"""
    monkeypatch.setattr(oracle_gate, "split", lambda o, items, rule_id="": (list(items), []))
    assert _caught(_guard_unconfirmed_held)
    assert _caught(_guard_group_door)


def test_mutation_unconfirmed_washed_to_confirmed_is_caught(monkeypatch):
    """M2 把 unconfirmed 洗成 confirmed → 守卫必须红。"""
    monkeypatch.setattr(oracle_gate, "confirmation",
                        lambda o: "confirmed" if o else "")
    assert _caught(_guard_unconfirmed_held)


def test_mutation_gate_over_reaches_is_caught(monkeypatch):
    """M3 闸门过度扩大（无 oracle 也压）→ 技术缺陷被误伤，守卫必须红。"""
    monkeypatch.setattr(oracle_gate, "split", lambda o, items, rule_id="": ([], [
        {"rule_id": rule_id, "oracle": {"confirmation": "unconfirmed"}, "finding": i}
        for i in items]))
    assert _caught(_guard_no_oracle_filed)


def test_mutation_group_door_bypassed_is_caught(monkeypatch):
    """M4 分组门不消费 oracle（复现本缺陷）→ 守卫必须红。"""
    monkeypatch.setattr(fn_diff.oracle_gate, "split",
                        lambda o, items, rule_id="": (list(items), []))
    assert _caught(_guard_group_door)


def test_file_level_mutation_group_gate_reopens_the_defect(tmp_path):
    """M5 文件级变异：subprocess 守卫读磁盘原文，同进程 monkeypatch 杀不死它。"""
    pristine = _fn_sandbox(tmp_path / "ok")
    _assert_fn_cli_retracts(pristine)
    mutated = _fn_sandbox(tmp_path / "mut", module_mutation="group_gate")
    assert _caught(_assert_fn_cli_retracts, mutated), (
        "文件级变异未被咬住：CLI 守卫无牙，或变异没改到文件")


def test_file_level_declaration_flip_reopens_the_defect(tmp_path):
    """M6 文件级把声明 unconfirmed→confirmed → 必须重新开单（证明不是按 id 拉黑）。"""
    pristine = _fn_sandbox(tmp_path / "ok")
    _assert_fn_cli_retracts(pristine)
    flipped = _fn_sandbox(tmp_path / "flip", flip=True)
    assert _caught(_assert_fn_cli_retracts, flipped)


def test_file_level_mutation_suspects_not_written_is_caught(tmp_path):
    """M7 压下来但不落盘（观测悄悄消失）→ 守卫必须红。"""
    pristine = _fn_sandbox(tmp_path / "ok")
    _assert_fn_cli_retracts(pristine)
    mutated = _fn_sandbox(tmp_path / "nosus", module_mutation="suspects_write")
    assert _caught(_assert_fn_cli_retracts, mutated)


def test_file_level_mutation_integration_oracles_emptied_is_caught(tmp_path):
    """M8 集成层文件级变异：清空 ORACLES → 两条集成症状重新开单，守卫必须红。"""
    missing = _int_artifacts_available()
    if missing:
        pytest.skip(f"缺流水线产物 {missing}")
    pristine = _int_sandbox(tmp_path / "ok")
    _assert_int_cli_retracts(pristine)
    mutated = _int_sandbox(tmp_path / "mut", oracles_empty=True)
    assert _caught(_assert_int_cli_retracts, mutated)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))