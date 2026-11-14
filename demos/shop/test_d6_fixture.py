"""D6 必须真的被埋进实现，而不是只写在 README 和注释里。

历史问题（2026-11-15 实测推翻）：README D6 与 OrderService.create 的注释都声明
「额度按报价占住 900000、账按实付记 810000，差 90000 无对应账目」，但实现里
QuotaService.withinLimit 只返回 boolean、LedgerService.record 只 set 订单字段 ——
**没有占用记录，也没有独立账本**。于是「占用额 == 账本额」这条判据没有实现依据，
建立在它之上的 EMERGENT_CONJUNCTIVE 是编造出来的真值表。

这组测试锁住夹具契约：两条记录各自落地、单独看都对、只有 VIP ∧ 会员装配才对不上。
需要两个 demo 容器在跑（8090 default / 8091 vip）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emergence_probe as ep  # noqa: E402

QUOTED = 900000


@pytest.fixture()
def rig():
    catalog = ep.emergence.load_catalog(ep.CATALOG_FILE)
    db = ep.Db(catalog.database)
    yield catalog, db
    ep.cleanup(db)


def _place(catalog, db, level: str, instance: str) -> int:
    name = "ai_probe_d6_cust"
    ep._delete_probe_orders(db, f"customer_name = '{name}'")
    db.exec(f"DELETE FROM shop_customer WHERE name = '{name}'")
    db.exec(f"INSERT INTO shop_customer(name, level, credit_limit_cent) "
            f"VALUES ('{name}', '{level}', 1000000)")
    status, payload = ep.http_post(
        str(catalog.instances[instance]["base_url"]), "/api/orders",
        {"customerName": name, "quotedAmountCent": QUOTED, "currency": "CNY"})
    assert status == 200, f"下单失败 http={status} {payload}"
    return int(payload["id"])


def _amounts(db, order_id: int) -> tuple[int | None, int | None]:
    occ = db.rows(f"SELECT amount_cent FROM shop_quota_occupancy WHERE order_id = {order_id}")
    led = db.rows(f"SELECT amount_cent FROM shop_ledger_entry WHERE order_id = {order_id}")
    return (occ[0]["amount_cent"] if occ else None,
            led[0]["amount_cent"] if led else None)


def test_occupancy_is_recorded_on_the_quoted_basis(rig) -> None:
    """额度占用必须真的落一条记录，且按报价口径 —— 不是只比较一下。"""
    catalog, db = rig
    order_id = _place(catalog, db, "NORMAL", "default")

    occupancy, _ledger = _amounts(db, order_id)

    assert occupancy == QUOTED


def test_ledger_is_recorded_on_the_settled_basis(rig) -> None:
    """账本必须是独立的一笔记录，且按实付口径 —— 不是只写订单字段。"""
    catalog, db = rig
    order_id = _place(catalog, db, "VIP", "vip")

    _occupancy, ledger = _amounts(db, order_id)

    assert ledger == QUOTED * 9 // 10


def test_normal_customer_keeps_both_records_aligned(rig) -> None:
    """普通客户：两条记录一致 —— 单条件不触发。"""
    catalog, db = rig
    order_id = _place(catalog, db, "NORMAL", "vip")

    occupancy, ledger = _amounts(db, order_id)

    assert occupancy == ledger == QUOTED


def test_vip_on_standard_deployment_keeps_both_records_aligned(rig) -> None:
    """VIP 客户 + 标准装配：拿不到折扣，两条记录仍一致 —— 另一个单条件也不触发。"""
    catalog, db = rig
    order_id = _place(catalog, db, "VIP", "default")

    occupancy, ledger = _amounts(db, order_id)

    assert occupancy == ledger == QUOTED


def test_only_the_conjunction_diverges(rig) -> None:
    """VIP 客户 + 会员装配：占用 900000、账本 810000，差 90000 无对应账目。"""
    catalog, db = rig
    order_id = _place(catalog, db, "VIP", "vip")

    occupancy, ledger = _amounts(db, order_id)

    assert occupancy == QUOTED
    assert ledger == QUOTED * 9 // 10
    assert occupancy - ledger == 90000


def test_cleanup_leaves_no_probe_rows_in_either_record(rig) -> None:
    """零残留必须覆盖两张新表，否则残留守卫会漏。"""
    catalog, db = rig
    _place(catalog, db, "VIP", "vip")

    left = ep.cleanup(db)

    assert left["rows"] == 0
    for table in ("shop_quota_occupancy", "shop_ledger_entry"):
        assert db.exec(f"SELECT count(*) FROM {table}") == "0"
