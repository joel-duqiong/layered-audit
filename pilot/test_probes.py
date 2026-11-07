"""可执行不变量探针 —— jiuyan 靶子
每个 probe = 一条业务/安全不变量。probe 失败 = 确证的缺陷（带确定性复现）。
不是"模型认为有 bug"，而是"断言真的 FAIL 了"。
"""
import os, uuid, datetime, pytest, requests

BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
S = requests.Session()
MARK = "ai_probe_"

def _now(days=30):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()

def _first_package():
    r = S.get(f"{BASE}/api/packages", params={"page": 1, "pageSize": 1}, timeout=10).json()
    return r["data"]["items"][0]

def _create_order(**over):
    body = {"customerName": MARK + uuid.uuid4().hex[:8], "customerPhone": "13800000001",
            "banquetName": MARK + "宴席", "banquetType": "婚宴", "address": "probe",
            "reservationTime": _now(), "tableCount": 10, "packageId": _first_package()["id"],
            "planTotalCent": 0, "useFinalPrice": True, "finalPriceCent": 0,
            "depositCent": 0, "status": "PENDING", "remark": "probe"}
    body.update(over)
    r = S.post(f"{BASE}/api/orders", json=body, timeout=10).json()
    assert r["success"], r
    return r["data"]

# ---------- 已现场确证会 FAIL 的三条 ----------

def test_price_is_not_client_controlled():
    """金额守恒：成交价必须由服务端按套餐现价重算，客户端不得自定价。"""
    pkg = _first_package()
    o = _create_order(planTotalCent=0, useFinalPrice=True, finalPriceCent=0)
    assert o["finalPriceCent"] == pkg["priceCent"], (
        f"成交价 {o['finalPriceCent']} 分应由服务端按套餐现价 {pkg['priceCent']} 分重算，"
        f"客户端可任意定价（planTotalCent/finalPriceCent 均由请求体直接落库）")

def test_snapshot_price_matches_charged_price():
    """快照一致：快照内价目合计 == 实收金额。"""
    pkg = _first_package()
    o = _create_order(planTotalCent=0, useFinalPrice=True, finalPriceCent=0)
    snap_total = o["packageSnapshot"]["priceCent"]
    assert o["finalPriceCent"] == snap_total, (
        f"快照价 {snap_total} 与实收 {o['finalPriceCent']} 不一致")

def test_order_is_not_world_readable():
    """授权不变量：订单必须仅本人/授权方可读写。系统当前无该不变量。"""
    o = _create_order()
    r = S.get(f"{BASE}/api/orders/{o['id']}", timeout=10)
    assert r.status_code in (401, 403), (
        f"无任何凭证即可读取订单 {o['id']}（HTTP {r.status_code}），全系统零鉴权")

def test_order_status_cannot_skip_states():
    """状态单调：PENDING -> DEPOSIT_PAID -> COMPLETED，不可跳级。"""
    o = _create_order(status="PENDING")
    r = S.patch(f"{BASE}/api/orders/{o['id']}/status", json={"status": "COMPLETED"}, timeout=10).json()
    assert not r["success"], "允许 PENDING 直接跳到 COMPLETED，无状态机约束"

def test_order_no_is_unique_under_concurrency():
    """唯一性：同一秒内并发下单不得产生重复订单号。"""
    import concurrent.futures as cf
    def one(_):
        return _create_order()["orderNo"]
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        nos = list(ex.map(one, range(20)))
    assert len(set(nos)) == len(nos), f"订单号碰撞: {len(nos)-len(set(nos))} 个重复，样本 {nos}"

# ---------- 待验证（尚未现场确证） ----------

def test_repeat_submit_is_idempotent():
    """幂等：同参数重复提交不产生第二单。"""
    o = _create_order()
    body = {"customerName": o["customerName"], "customerPhone": o["customerPhone"],
            "banquetName": o["banquetName"], "banquetType": o["banquetType"],
            "reservationTime": o["reservationTime"], "tableCount": o["tableCount"],
            "packageId": o["packageId"], "planTotalCent": o["planTotalCent"],
            "useFinalPrice": True, "finalPriceCent": o["finalPriceCent"],
            "depositCent": o["depositCent"], "status": "PENDING"}
    r2 = S.post(f"{BASE}/api/orders", json=body, timeout=10).json()
    assert r2["data"]["id"] == o["id"], "重复提交产生新订单，无幂等键"

def test_upload_rejects_non_image_bytes():
    """输入可信度：非图片字节必须被拒绝（服务端需校验魔数/解码），不能只看扩展名+客户端 contentType。"""
    r = S.post(f"{BASE}/api/uploads/product-image",
               files={"file": ("x.jpg", os.urandom(2048), "image/jpeg")}, timeout=10)
    assert r.status_code >= 400, (
        f"随机字节以 .jpg 上传被接受（HTTP {r.status_code}），服务端未校验内容，仅信客户端 contentType")

def test_page_size_is_bounded():
    """有界：pageSize 必须有上限，防止全表拉取。"""
    r = S.get(f"{BASE}/api/orders", params={"page": 1, "pageSize": 100000}, timeout=30).json()
    assert len(r["data"]["items"]) <= 200, f"pageSize=100000 返回 {len(r['data']['items'])} 条，无上限"
