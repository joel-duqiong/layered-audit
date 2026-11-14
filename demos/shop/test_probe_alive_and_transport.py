"""物化链的活性检查与传输层 —— 独立审查（2026-11-15）确认的两处缺陷。

1. `http_post` 只捕 HTTPError，不捕 URLError（两者不是父子关系，反向才是）。
   实例容器没起时 connection refused 直接炸进程，**绕过 D17 的 UNDECIDABLE 闸门** ——
   闸门只消费 executor 返回的 Observation，进程崩了就没有 Observation。
2. 落库校验跨两条序列化链比字符串：reader 走 to_jsonb -> JSON -> Python
   （numeric(10,2) 的 100.00 变成 '100.0'，boolean 的 true 变成 'True'），
   校验走 psql -tAc（'100.00' / 'true'）。注入明明落库却被判死 ->
   从分母剔除 -> 真 EMERGENT_CONJUNCTIVE 翻成假 FALSIFIED_CHAIN。
   比较必须交给 DB 做，让 Postgres 按列类型 coerce。
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
for p in (str(PILOT), str(PILOT / "engine"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import emergence  # noqa: E402
import emergence_probe as probe  # noqa: E402

INV_CFG = json.loads((HERE / "invariants.json").read_text(encoding="utf-8"))


class _CountingDb:
    """记录 SQL 的假 DB：落库校验必须把比较交给 SQL，不许在 Python 里比字符串。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.sqls: list[str] = []

    def exec(self, sql: str) -> str:
        self.sqls.append(sql)
        return self.answer

    def rows(self, _sql: str) -> list[dict[str, Any]]:
        return []


def _executor(db: Any) -> probe.DemoExecutor:
    ex = object.__new__(probe.DemoExecutor)
    ex.db = db
    ex.invariants = {i["id"]: i for i in INV_CFG["invariants"]}
    ex.entities = {e["name"]: e for e in INV_CFG["entities"]}
    ex.probe_invariants = set()
    ex.catalog = emergence.Catalog(chains=(), instances={"vip": {"profile": "vip"}})
    ex.scenario_log = {}
    ex.alive_log = {}
    ex._reset = lambda chain: None
    ex._apply = lambda chain, subset: ("vip", {})
    ex._place = lambda chain, instance, overrides: (200, {"id": 7})
    return ex


def _chain() -> emergence.Chain:
    return emergence.Chain(
        id="T", title="t", hypothesis="h", severity="HIGH",
        target_invariant="BI-QUOTA-LEDGER-PARITY", root_cause_key="rc",
        location={}, injections=(), entity="order",
        flow={"customer": {"name": "ai_probe_t", "level": "NORMAL"},
              "orders": [{"quotedAmountCent": 900000, "currency": "CNY"}]},
        baseline_instance="default")


def _injection(column: str, value: str) -> emergence.Injection:
    return emergence.Injection(
        id="GEN-DATA-T-C-V", defect="generated.data.c", layer="service",
        target_step="POST /api/orders",
        mutation={"kind": "data", "target": "customer", "set": {column: value}},
        expected_symptom="BI-QUOTA-LEDGER-PARITY", alive_check={"kind": "applied"},
        active=True)


# --------------------------------------------------------------- 传输层三态

def test_connection_refused_returns_error_not_exception() -> None:
    """URLError 不许穿透：容器没起时要走 error 分支，让引擎判 UNDECIDABLE。"""
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("connection refused")):
        status, payload = probe.http_post("http://127.0.0.1:1", "/api/orders", {})
    assert status == -1
    # 失败原因要进证据，不许只给一个空 payload
    assert "connection refused" in payload["transport_error"]
    assert "id" not in payload      # run() 靠这个走 error 分支


def test_timeout_returns_error_not_exception() -> None:
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        status, payload = probe.http_post("http://127.0.0.1:1", "/api/orders", {})
    assert status == -1
    assert "timed out" in payload["transport_error"]


def test_transport_failure_becomes_undecidable_not_satisfied() -> None:
    """端到端：传输失败 -> Observation(error) -> 引擎判 UNDECIDABLE，零开单。"""
    ex = _executor(_CountingDb("1"))
    ex._place = lambda chain, instance, overrides: (-1, None)
    obs = ex.run(_chain(), ())
    assert obs.status == "error"
    assert obs.violated is False


# ------------------------------------------------------- 落库校验交给 DB

def test_applied_check_compares_in_sql_not_python() -> None:
    """numeric 精度差异不许判死：'100.0'（reader）vs 100.00（库）由 Postgres coerce。"""
    db = _CountingDb("1")
    ex = _executor(db)
    res = ex._alive_applied(_chain(), _injection("credit_limit_cent", "100.0"))
    assert res.alive is True
    assert any("credit_limit_cent = '100.0'" in s for s in db.sqls), db.sqls
    # 不许把列值取回 Python 再 str() 比较
    assert not any(s.strip().upper().startswith("SELECT CREDIT_LIMIT_CENT FROM")
                   for s in db.sqls), db.sqls


def test_applied_check_fails_when_db_says_not_applied() -> None:
    db = _CountingDb("0")
    ex = _executor(db)
    res = ex._alive_applied(_chain(), _injection("level", "VIP"))
    assert res.alive is False
    assert "没落库" in (res.detail or "")


def test_applied_check_escapes_quote_in_value() -> None:
    """取值来自库内既有数据，可能含单引号 —— 不许拼出坏 SQL。"""
    db = _CountingDb("1")
    ex = _executor(db)
    ex._alive_applied(_chain(), _injection("level", "O'Brien"))
    assert any("'O''Brien'" in s for s in db.sqls), db.sqls
