"""服务层 · 并发与重复写探针（concurrency_test / chaos_experiment）。

填两个零覆盖类别：
  service.data_consistency —— 缓存与DB不一致 / 事务边界错 / 并发写冲突
  service.resilience        —— 超时 / 重试 / 熔断 / 幂等 / 降级缺失

性质（应该成立的关系）：
  DC1 并发写不丢更新：两个 PUT 并发改不同字段，最终行必须同时含两个新值
  DC2 失败的事务不留残局：非法 body 被拒后，订单行必须与之前逐字段一致
  RS1 重复提交不产生重复业务对象：同一个 POST 发两次，只能有一个订单落库

探针数据带 ai_probe_conc_ 前缀，写完即删，零残留。
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import requests

from integration_chain import BASE, call, cleanup, data_of, order_body, owner_session
from integration_reconcile import PFX, psql_rows

HERE = Path(__file__).resolve().parent
OUT = HERE / "concurrency_findings.json"
TAG = "conc"


def _product_and_package() -> tuple[int, int]:
    """借一个现成的商品与套餐（用种子里已有的，不造新的，减少残留面）。"""
    prods = data_of(call(owner_session, "GET", "/api/products", params={"pageSize": 5})[1]) or {}
    pkgs = data_of(call(owner_session, "GET", "/api/packages", params={"pageSize": 5})[1]) or {}
    items = prods.get("items") or []
    pkg_items = pkgs.get("items") or []
    if not items or not pkg_items:
        raise RuntimeError("缺少可用的商品或套餐")
    pid = items[0]["id"]
    kid = pkg_items[0]["id"]
    kpid = (pkg_items[0].get("items") or [{}])[0].get("productId") or pid
    return kpid, kid


def create_order(body: dict[str, Any]) -> int:
    st, payload = call(owner_session, "POST", "/api/orders", body=body)
    if st != 200:
        raise RuntimeError(f"造订单失败 http={st} {payload}")
    return data_of(payload)["id"]


def read_row(order_id: int) -> dict[str, Any]:
    rows = psql_rows(
        "SELECT customer_phone, address, deposit_cent, final_price_cent, status, remark "
        f"FROM orders WHERE id = {order_id}")
    return rows[0] if rows else {}


def dc1_concurrent_write_lost_update(pid: int, kid: int) -> dict[str, Any]:
    """两个并发 PUT 改不同字段：后写的不能把先写的抹掉。"""
    base = order_body(f"{TAG}_dc1", pid, kid)
    order_id = create_order(base)

    body_a = copy.deepcopy(base)
    body_a["customerPhone"] = "13900000001"
    body_b = copy.deepcopy(base)
    body_b["address"] = f"{PFX}{TAG}_addr_concurrent"

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def put(name: str, body: dict[str, Any]) -> None:
        session = requests.Session()
        barrier.wait()
        resp = session.put(f"{BASE}/api/orders/{order_id}", json=body, timeout=20)
        results[name] = resp.status_code

    ta = threading.Thread(target=put, args=("A", body_a))
    tb = threading.Thread(target=put, args=("B", body_b))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    row = read_row(order_id)
    phone_ok = row.get("customer_phone") == "13900000001"
    address_ok = row.get("address") == f"{PFX}{TAG}_addr_concurrent"
    return {
        "property": "两个并发 PUT 分别改 customerPhone 与 address，最终行必须同时含两个新值",
        "ok": phone_ok and address_ok,
        "order_id": order_id,
        "http_status": results,
        "final_row": row,
        "phone_applied": phone_ok,
        "address_applied": address_ok,
        "lost_update": not (phone_ok and address_ok),
    }


def dc2_failed_tx_leaves_no_partial_write(pid: int, kid: int) -> dict[str, Any]:
    """非法 body 被拒后，订单行必须与拒绝前逐字段一致。"""
    base = order_body(f"{TAG}_dc2", pid, kid)
    order_id = create_order(base)
    before = read_row(order_id)

    bad = copy.deepcopy(base)
    bad["tableCount"] = 0          # @Min(1) -> 400
    bad["customerPhone"] = "13700000002"   # 顺带改一个合法字段，看它有没有被部分写入
    status, _ = call(owner_session, "PUT", f"/api/orders/{order_id}", body=bad)
    after = read_row(order_id)
    return {
        "property": "被拒的 PUT 不能在订单行留下任何部分写入",
        "ok": before == after,
        "order_id": order_id,
        "http_status": status,
        "before": before,
        "after": after,
        "partial_write": before != after,
    }


def rs1_post_not_idempotent(pid: int, kid: int) -> dict[str, Any]:
    """同一个 POST 重发两次（模拟客户端超时后重试），只能产生一个订单。"""
    body = order_body(f"{TAG}_rs1", pid, kid)
    body["customerName"] = f"{PFX}{TAG}_rs1_cust"
    st1, p1 = call(owner_session, "POST", "/api/orders", body=body)
    st2, p2 = call(owner_session, "POST", "/api/orders", body=body)
    id1 = (data_of(p1) or {}).get("id")
    id2 = (data_of(p2) or {}).get("id")
    rows = psql_rows(
        f"SELECT count(*) AS n FROM orders WHERE customer_name = '{PFX}{TAG}_rs1_cust'")
    count = rows[0]["n"] if rows else 0
    return {
        "property": "同一个 POST 重发两次，只能有一个订单落库（POST 必须幂等或带幂等键）",
        "ok": count == 1,
        "http_status": [st1, st2],
        "first_order_id": id1,
        "second_order_id": id2,
        "orders_in_db": count,
        "same_id": id1 == id2,
    }


def main() -> None:
    findings: list[dict[str, Any]] = []
    try:
        pid, kid = _product_and_package()
        dc1 = dc1_concurrent_write_lost_update(pid, kid)
        dc2 = dc2_failed_tx_leaves_no_partial_write(pid, kid)
        rs1 = rs1_post_not_idempotent(pid, kid)
    finally:
        cleanup()

    if dc1.get("lost_update"):
        findings.append({
            "id": "SVC-CONCURRENT-WRITE-LOST-UPDATE",
            "severity": "MEDIUM",
            "endpoint": "PUT /api/orders/{id}",
            "title": "并发编辑同一订单会静默丢掉对方的修改：没有版本号、没有冲突检测",
            "detail": (
                "订单编辑走 PUT /api/orders/{id}，服务层在事务里整行覆盖，没有乐观锁"
                "（无 version / ETag / If-Match），也没有任何冲突检测。\n"
                "两个客户端同时编辑同一订单、各改一个字段时，后提交的那个用自己那份"
                "完整快照覆盖了整行，先提交的人的修改直接消失 —— 没有报错、没有冲突提示，"
                "两边都以为自己改成功了。\n"
                f"实测：{json.dumps(dc1, ensure_ascii=False)}\n"
                "谈单场景下的具体后果：销售 A 改了成交价、销售 B 同时改了电话，"
                "A 的改价凭空消失，订单金额回到旧值，而 A 的界面显示他改成功了。"),
            "repro": "python3 concurrency_props.py",
            "evidence": {"probe": dc1},
        })
    if dc2.get("partial_write"):
        findings.append({
            "id": "SVC-FAILED-WRITE-LEAVES-PARTIAL-STATE",
            "severity": "HIGH",
            "endpoint": "PUT /api/orders/{id}",
            "title": "被拒的写请求在订单行留下了部分写入",
            "detail": f"实测：{json.dumps(dc2, ensure_ascii=False)}",
            "repro": "python3 concurrency_props.py",
            "evidence": {"probe": dc2},
        })
    elif dc2.get("ok"):
        findings.append({
            "id": "SVC-FAILED-WRITE-LEAVES-NO-PARTIAL-STATE",
            "severity": "INFO",
            "endpoint": "PUT /api/orders/{id}",
            "title": "被拒的写请求不会在订单行留下部分写入（事务边界有效）",
            "detail": (
                "同一个 PUT 里塞一个非法字段（tableCount=0，@Min(1) 拒绝）和一个合法字段的"
                "改动，服务返回 4xx 后，订单行与请求前逐字段一致 —— 没有出现「校验失败但"
                "另一部分已经写进去」的半截状态。\n"
                f"实测：{json.dumps(dc2, ensure_ascii=False)}"),
            "repro": "python3 concurrency_props.py",
            "evidence": {"probe": dc2},
        })
    if not rs1.get("ok"):
        findings.append({
            "id": "SVC-POST-NOT-IDEMPOTENT",
            "severity": "MEDIUM",
            "endpoint": "POST /api/orders",
            "title": "下单接口没有幂等：同一个请求重发一次就产生两个订单",
            "detail": (
                "POST /api/orders 不接受也不返回幂等键，没有按业务键去重，"
                "也没有唯一约束能挡住重复。客户端在超时后重试（这是所有 HTTP 客户端的"
                "标准行为），就会多出一张订单。\n"
                f"实测：{json.dumps(rs1, ensure_ascii=False)}\n"
                "在零鉴权的前提下，这个接口同时也是「随便重发就能灌订单」的放大器。"),
            "repro": "python3 concurrency_props.py",
            "evidence": {"probe": rs1},
        })

    payload = {"service": findings,
               "probes": {"dc1": dc1, "dc2": dc2, "rs1": rs1}}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"并发/重复写探针 -> {OUT}  {len(findings)} 条发现")
    for f in findings:
        print(f"  {f['severity']:<9}{f['id']:<44}{f['title'][:44]}")
    for key, probe in (("DC1", dc1), ("DC2", dc2), ("RS1", rs1)):
        print(f"  {key} ok={probe.get('ok')}")


if __name__ == "__main__":
    main()
