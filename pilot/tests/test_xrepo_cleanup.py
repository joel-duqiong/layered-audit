"""跨仓库探针的零残留 —— 它用种子客户真下单，就必须自己删掉。

实测挖出来的漏洞：xrepo_probe 为了观测对端计数，用种子客户 demo-vip 真下一单，
从来不删。因为客户名没有 ai_probe_ 前缀，残留守卫看不见它 ——
「容器 0 / ai_ 行 0 / 上传 0」全绿，库里其实留着订单。

订单有了附属记录（额度占用 / 账本分录）之后，外键还会挡住直接删订单，
所以清理必须按外键顺序来。这组测试锁住顺序与范围。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PILOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("LAYERED_AUDIT_TARGET",
                      str(PILOT.parent / "demos" / "shop" / "target.json"))
sys.path.insert(0, str(PILOT))

import xrepo_probe  # noqa: E402


def test_cleanup_deletes_child_records_before_order() -> None:
    stmts = xrepo_probe.cleanup_statements(4321)
    tables = [s.split("FROM ")[1].split(" ")[0] for s in stmts]
    assert tables.index("shop_quota_occupancy") < tables.index("shop_order")
    assert tables.index("shop_ledger_entry") < tables.index("shop_order")


def test_cleanup_is_scoped_to_the_created_order_only() -> None:
    stmts = xrepo_probe.cleanup_statements(4321)
    assert all("4321" in s for s in stmts)
    assert all(s.startswith("DELETE FROM ") for s in stmts)


def test_cleanup_rejects_non_numeric_id() -> None:
    """id 来自 HTTP 响应，是外部数据 —— 不许直接拼进 SQL。"""
    for bad in ("1; DROP TABLE shop_order", "abc", "", "1 OR 1=1"):
        try:
            xrepo_probe.cleanup_statements(bad)
        except ValueError:
            continue
        raise AssertionError(f"非法 id 没被拒绝: {bad!r}")
