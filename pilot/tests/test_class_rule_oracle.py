"""类层「规则三层全缺」结论必须过 oracle 来源门禁（硬约束 5）。

## 为什么有这组测试

`business_verdict.py`（服务层）已经接上了 oracle 来源门禁，但类层走的是
`class_scenarios.json` 里另一份规则清单，**完全绕过了它**。后果是实测抓到的：

  `CLASS-RULE-MISSING-IN-ALL-LAYERS` 报 HIGH + ground_truth=verified，
  detail 里明写「定金不超过成交价」—— 而项目权威文档
  `/Users/june/tests/auto-api/docs/业务校验对照.md` 第 65 行写着
  「定金与成交价的大小关系：当前只要求各自 >= 0」，列在「当前未定义」标题下。

同一份文档第 62 行把「预约时间是否允许过去时间」也列为未定义。
也就是说：一条 HIGH 缺陷里 4 条规则，2 条被文档明确否定，却标着 verified。

**「三层都没有这条规则」是客观观测；「所以这是缺陷」需要业务预期。**
预期没被确认，就只能进待确认疑点，不能进缺陷台账。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PILOT))

import class_contract_diff as ccd  # noqa: E402

CONFIRMED = {"source": "project_doc", "confirmation": "confirmed",
             "reference": "业务校验对照.md P1"}
UNCONFIRMED = {"source": "inferred", "confirmation": "unconfirmed"}
REFUTED = {"source": "project_doc", "confirmation": "refuted",
           "reference": "业务校验对照.md 第 65 行：当前未定义"}


def _probe(label: str, oracle: dict | None = None, **kw) -> dict:
    p = {"label": label, "table": "orders", "constraint": None, "blocked": False}
    if oracle is not None:
        p["oracle"] = oracle
    p.update(kw)
    return p


def _ctx(probes: list[dict]) -> dict:
    return {
        "contract": {"reflection": [], "db_actual": {"tables": {}}},
        "obs": {"defense_audit": {"constraint_probes": probes}},
    }


def _missing_findings(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["id"] == "CLASS-RULE-MISSING-IN-ALL-LAYERS"]


# ---------------------------------------------------------------- 分桶

def test_confirmed_rule_is_filed():
    buckets = ccd.rule_oracle_buckets(
        _ctx([_probe("手机号格式", CONFIRMED)]), {})
    assert [r["rule"] for r in buckets["confirmed"]] == ["手机号格式"]
    assert buckets["unconfirmed"] == []
    assert buckets["refuted"] == []


def test_unconfirmed_rule_goes_to_suspects_not_findings():
    buckets = ccd.rule_oracle_buckets(
        _ctx([_probe("完成必有定金", UNCONFIRMED)]), {})
    assert buckets["confirmed"] == []
    assert [r["rule"] for r in buckets["unconfirmed"]] == ["完成必有定金"]


def test_refuted_rule_kept_as_observation_only():
    buckets = ccd.rule_oracle_buckets(
        _ctx([_probe("定金大于成交价", REFUTED)]), {})
    assert buckets["confirmed"] == []
    assert buckets["unconfirmed"] == []
    assert [r["rule"] for r in buckets["refuted"]] == ["定金大于成交价"]


def test_missing_oracle_defaults_to_unconfirmed():
    """没声明来源 = 未确认。宁可少开单，不可编造预期。"""
    buckets = ccd.rule_oracle_buckets(_ctx([_probe("某条规则")]), {})
    assert buckets["confirmed"] == []
    assert len(buckets["unconfirmed"]) == 1


def test_unknown_confirmation_value_defaults_to_unconfirmed():
    buckets = ccd.rule_oracle_buckets(
        _ctx([_probe("x", {"confirmation": "大概是吧"})]), {})
    assert buckets["confirmed"] == []
    assert len(buckets["unconfirmed"]) == 1


# ---------------------------------------------------------------- 开单

def test_only_confirmed_rules_produce_a_finding():
    probes = [_probe("手机号格式", CONFIRMED),
              _probe("定金大于成交价", REFUTED),
              _probe("预订过去时间", REFUTED),
              _probe("完成必有定金", UNCONFIRMED)]
    findings = ccd.diff_rules(_ctx(probes), {})
    hits = _missing_findings(findings)
    assert len(hits) == 1
    assert [r["rule"] for r in hits[0]["evidence"]["rules"]] == ["手机号格式"]
    assert "1 条" in hits[0]["title"]


def test_zero_confirmed_emits_no_finding_at_all():
    """全被否定/未确认时，不许开一张「0 条」的单，也不许开原来的单。"""
    probes = [_probe("定金大于成交价", REFUTED),
              _probe("完成必有定金", UNCONFIRMED)]
    assert _missing_findings(ccd.diff_rules(_ctx(probes), {})) == []


def test_refuted_rules_never_appear_in_any_finding():
    probes = [_probe("手机号格式", CONFIRMED),
              _probe("定金大于成交价", REFUTED)]
    assert "定金大于成交价" not in repr(ccd.diff_rules(_ctx(probes), {}))


# ---------------------------------------------------------------- 旧行为不回归

def test_blocked_probes_still_excluded():
    probes = [_probe("被 DB 挡住的", CONFIRMED, blocked=True)]
    assert ccd.rule_oracle_buckets(_ctx(probes), {})["confirmed"] == []


def test_probes_with_constraint_still_excluded():
    probes = [_probe("有 CHECK 的", CONFIRMED, constraint="ck_x")]
    assert ccd.rule_oracle_buckets(_ctx(probes), {})["confirmed"] == []


def test_rule_excludes_still_honored():
    probes = [_probe("已知不适用", CONFIRMED)]
    buckets = ccd.rule_oracle_buckets(
        _ctx(probes), {"rule_excludes": ["已知不适用"]})
    assert buckets["confirmed"] == []


def test_rule_notes_still_attached():
    probes = [_probe("手机号格式", CONFIRMED)]
    cfg = {"rule_notes": {"手机号格式": {"http": 200, "note": "落库为 not-a-phone"}}}
    rec = ccd.rule_oracle_buckets(_ctx(probes), cfg)["confirmed"][0]
    assert rec["api_http"] == 200
    assert rec["api_note"] == "落库为 not-a-phone"


def test_oracle_provenance_is_carried_into_the_record():
    """开单时必须能追溯到来源，否则人工复核无从下手。"""
    probes = [_probe("手机号格式", CONFIRMED)]
    rec = ccd.rule_oracle_buckets(_ctx(probes), {})["confirmed"][0]
    assert rec["oracle"]["confirmation"] == "confirmed"
    assert rec["oracle"]["reference"] == "业务校验对照.md P1"


# ---------------------------------------------------------------- run 的返回形状

def test_run_returns_findings_and_side_channels(tmp_path):
    """未确认/被否定的规则必须留痕，不能悄悄消失（对照 class_carrier_gap 的先例）。"""
    probes = [_probe("手机号格式", CONFIRMED),
              _probe("定金大于成交价", REFUTED),
              _probe("完成必有定金", UNCONFIRMED)]
    ctx = _ctx(probes)
    decl = tmp_path / "decl.json"
    contract = tmp_path / "contract.json"
    obs = tmp_path / "obs.json"
    decl.write_text('{"jpa": {}}', encoding="utf-8")
    contract.write_text('{"reflection": [], "db_actual": {"tables": {}}}',
                        encoding="utf-8")
    obs.write_text(json.dumps(ctx["obs"]), encoding="utf-8")

    out = ccd.run(str(decl), str(contract), str(obs), {})
    assert isinstance(out, dict)
    assert [r["rule"] for r in out["rule_refuted"]] == ["定金大于成交价"]
    assert [r["rule"] for r in out["rule_suspects"]] == ["完成必有定金"]
    assert len(_missing_findings(out["findings"])) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------- 配置侧声明

def test_oracle_can_be_declared_in_config_by_label():
    """oracle 是声明，属于项目配置；观测产物只装实测数据。"""
    probes = [_probe("手机号格式"), _probe("定金大于成交价")]
    cfg = {"rule_oracles": {"手机号格式": CONFIRMED, "定金大于成交价": REFUTED}}
    buckets = ccd.rule_oracle_buckets(_ctx(probes), cfg)
    assert [r["rule"] for r in buckets["confirmed"]] == ["手机号格式"]
    assert [r["rule"] for r in buckets["refuted"]] == ["定金大于成交价"]


def test_probe_level_oracle_wins_over_config():
    probes = [_probe("x", CONFIRMED)]
    cfg = {"rule_oracles": {"x": REFUTED}}
    assert len(ccd.rule_oracle_buckets(_ctx(probes), cfg)["confirmed"]) == 1
