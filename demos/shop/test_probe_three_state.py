"""探针判据的三态 —— 「判据不适用」永远不许被当成「没违反」。

实测挖出来的洞：BI-QUOTA-LEDGER-PARITY 声明了 when currency==CNY。
注入 EUR 之后不变式引擎正确返回 ok=None / checked=0（不适用），
但 _judge 把它压成 violated=False + status=ok，于是裁决器判出
**假的 FALSIFIED_CHAIN**（「合起来也不违反」）。

与引擎里那个洞同族，只是漏在项目侧边界上。这组测试锁住边界。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
for p in (str(PILOT), str(PILOT / "engine"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import emergence  # noqa: E402
import emergence_probe as probe  # noqa: E402

INV_CFG = json.loads((HERE / "invariants.json").read_text(encoding="utf-8"))


class _FakeDb:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def rows(self, _sql: str) -> list[dict[str, Any]]:
        return [self.row]


def _executor(row: dict[str, Any]) -> probe.DemoExecutor:
    """只装配 _judge / run 需要的部分，不碰 DB 与 HTTP。"""
    ex = object.__new__(probe.DemoExecutor)
    ex.db = _FakeDb(row)
    ex.invariants = {i["id"]: i for i in INV_CFG["invariants"]}
    ex.entities = {e["name"]: e for e in INV_CFG["entities"]}
    ex.probe_invariants = set()
    ex.catalog = emergence.Catalog(chains=(), instances={"vip": {"profile": "vip"}})
    ex.scenario_log = {}
    ex.alive_log = {}
    ex._reset = lambda chain: None
    ex._apply = lambda chain, subset: ("vip", {"currency": row["currency"]})
    ex._place = lambda chain, instance, overrides: (200, {"id": int(row["id"])})
    return ex


def _chain() -> emergence.Chain:
    return emergence.Chain(
        id="T", title="t", hypothesis="h", severity="HIGH",
        target_invariant="BI-QUOTA-LEDGER-PARITY", root_cause_key="rc",
        location={}, injections=(), entity="order",
        flow={"customer": {"name": "ai_probe_t"},
              "orders": [{"quotedAmountCent": 900000, "currency": "CNY"}]},
        baseline_instance="default")


def _row(currency: str, occupancy: int, ledger: int) -> dict[str, Any]:
    return {"id": 1, "currency": currency, "customer_level": "VIP",
            "quoted_amount_cent": 900000, "settled_amount_cent": ledger,
            "expected_member_price_cent": 810000,
            "occupancy_amount_cent": occupancy, "ledger_amount_cent": ledger}


def test_not_applicable_is_not_reported_as_satisfied() -> None:
    # Arrange：EUR 行 —— 判据的 when 把它排除了，两条记录金额差得很远
    ex = _executor(_row("EUR", 900000, 745200))

    # Act
    obs = ex.run(_chain(), ())

    # Assert：不许是 ok，否则裁决器会当成「没违反」
    assert obs.status == "not_applicable"
    assert obs.violated is False
    assert "不适用" in (obs.detail or "")


def test_applicable_violation_still_reported() -> None:
    ex = _executor(_row("CNY", 900000, 810000))
    obs = ex.run(_chain(), ())
    assert obs.status == "ok"
    assert obs.violated is True
    assert obs.magnitude == 90000.0


def test_applicable_satisfied_still_reported() -> None:
    ex = _executor(_row("CNY", 900000, 900000))
    obs = ex.run(_chain(), ())
    assert obs.status == "ok"
    assert obs.violated is False


def test_missing_records_are_unknown_not_equal() -> None:
    """两条记录缺失（LEFT JOIN 取不到）时按三态判 unknown，不许当成相等。"""
    row = _row("CNY", 900000, 900000)
    row["occupancy_amount_cent"] = None
    row["ledger_amount_cent"] = None
    ex = _executor(row)
    obs = ex.run(_chain(), ())
    assert obs.status == "not_applicable"
    assert obs.violated is False
