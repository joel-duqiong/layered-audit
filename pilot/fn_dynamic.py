"""函数层 · 动态侧：在同一个 JVM 内直接反射调用被测函数。

载体 = JVM 直调（不是 HTTP、不是数据库）。仓库用动态代理打桩，
ObjectMapper 用真实实例，UploadService 落在临时目录——被测系统的数据零写入。

用例与断言由 Python 侧定义（oracle 与被测实现相互独立），Java 只负责执行。
产出 fn_observation.json：每个用例的实测行为 + 判定。
"""
from __future__ import annotations
import project

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, project.engine_path())

HERE = project.artifact_dir()          # 产物目录（可按目标隔离）
SCRIPT_DIR = Path(__file__).resolve().parent   # 脚本/源码目录（harness 源码在这）
WORK = project.work_dir()
CLASSES = WORK / "classes"
HARNESS_CLASSES = WORK / "harness"
CASES_FILE = WORK / "cases.json"
OUT_FILE = WORK / "observation_raw.json"
UPLOAD_DIR = WORK / "uploads"

ORDER_SVC = "com.jiuyan.banquet.service.OrderService"
UPLOAD_SVC = "com.jiuyan.banquet.service.UploadService"
CATEGORY_SVC = "com.jiuyan.banquet.service.CategoryService"
PRODUCT_SVC = "com.jiuyan.banquet.service.ProductService"
PACKAGE_SVC = "com.jiuyan.banquet.service.PackageService"

SHANGHAI = timezone(timedelta(hours=8))


# ----------------------------------------------------------------- arg helpers

def L(v): return {"$t": "long", "v": v}
def I(v): return {"$t": "int", "v": v}
def S(v): return {"$t": "str", "v": v}
def B(v): return {"$t": "bool", "v": v}
def N(): return {"$t": "null"}
def DT(v="2026-11-20T18:00:00+08:00"): return {"$t": "dt", "v": v}
def EN(v): return {"$t": "enum", "v": v}
def PKG(v=4): return {"$t": "pkg", "v": v}
def PKG_ITEMS(items): return {"$t": "pkgItems", "v": items}
def ADD_ITEMS(items): return {"$t": "addItems", "v": items}
def ORDER(): return {"$t": "order"}
def CATEGORY_REQ(name): return {"$t": "categoryReq", "v": name}


def MP(filename, content_type, content_bytes=b"", size=None):
    node = {"filename": filename, "contentType": content_type, "name": "file"}
    if size is not None:
        node["sizeBytes"] = size
    else:
        node["contentBase64"] = base64.b64encode(content_bytes).decode()
    return {"$t": "multipart", "v": node}


def apply_args(**over):
    """OrderService.apply 的 17 个入参，顺序与字节码一致。"""
    base = {
        "order": ORDER(), "customer_name": S("张三"), "customer_phone": S("13800000000"),
        "banquet_name": S("婚宴"), "banquet_type": S("WEDDING"), "address": S("某酒店"),
        "reservation_time": DT(), "table_count": I(10), "package_id": L(4),
        "package_items": PKG_ITEMS([]), "added_items": ADD_ITEMS([]),
        "plan_total_cent": L(46800), "use_final_price": B(False), "final_price_cent": N(),
        "deposit_cent": L(0), "status": EN("PENDING"), "remark": N(),
    }
    base.update(over)
    return [base["order"], base["customer_name"], base["customer_phone"], base["banquet_name"],
            base["banquet_type"], base["address"], base["reservation_time"], base["table_count"],
            base["package_id"], base["package_items"], base["added_items"], base["plan_total_cent"],
            base["use_final_price"], base["final_price_cent"], base["deposit_cent"], base["status"],
            base["remark"]]


def case(cid, group, cls, method, args, check, invariant, label=""):
    return {"id": cid, "group": group, "func": "reflect", "class": cls, "method": method,
            "args": args, "check": check, "invariant": invariant, "label": label}


# ----------------------------------------------------------------- cases

def build_cases() -> list[dict]:
    cases: list[dict] = []

    # ---- resolveFinalPrice：定价权的唯一决策点
    cases += [
        case("RFP-01", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(False), L(1), L(46800)], {"type": "returns_eq", "value": 46800},
             "不使用成交价时，价格必须等于套餐总价"),
        case("RFP-02", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(False), N(), L(46800)], {"type": "returns_eq", "value": 46800},
             "不使用成交价时忽略客户端传入的成交价"),
        case("RFP-03", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(True), N(), L(46800)], {"type": "throws_business", "code": "PRICE_INVALID",
                                       "status": 422},
             "成交价缺失必须以 422 拒绝"),
        case("RFP-04", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(True), L(-1), L(46800)], {"type": "throws_business", "code": "PRICE_INVALID",
                                         "status": 422},
             "负成交价必须以 422 拒绝"),
        case("RFP-05", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(True), L(1), L(46800)], {"type": "returns_ge", "value": 46800},
             "成交价不得低于套餐总价（客户端不能自降定价权）"),
        case("RFP-06", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(True), L(999999), L(46800)], {"type": "returns_le", "value": 46800},
             "成交价不得高于套餐总价"),
        case("RFP-07", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "resolveFinalPrice",
             [B(True), L(0), L(46800)], {"type": "returns_ge", "value": 1},
             "成交价不得为 0（0 元成交无任何业务依据）"),
    ]

    # ---- parseStatus：状态解析契约
    cases += [
        case("PST-01", "STATUS_PARSE", ORDER_SVC, "parseStatus", [N()],
             {"type": "returns_null"}, "空状态解析为 null（不过滤）"),
        case("PST-02", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("")],
             {"type": "returns_null"}, "空字符串解析为 null"),
        case("PST-03", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("   ")],
             {"type": "returns_null"}, "纯空白解析为 null"),
        case("PST-04", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("PENDING")],
             {"type": "returns_eq", "value": "PENDING"}, "合法状态原样返回"),
        case("PST-05", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("  PENDING  ")],
             {"type": "returns_eq", "value": "PENDING"}, "状态串需 trim"),
        case("PST-06", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("pending")],
             {"type": "throws_business", "code": "ORDER_STATUS_INVALID", "status": 422},
             "非法状态必须 422（不能漏成 500）"),
        case("PST-07", "STATUS_PARSE", ORDER_SVC, "parseStatus", [S("PENDING'; DROP--")],
             {"type": "throws_business", "code": "ORDER_STATUS_INVALID", "status": 422},
             "注入式状态串必须 422"),
    ]

    # ---- parseDateScope：用 Python 独立实现的时间区间作为 oracle
    for scope, rule in (("TODAY", "TODAY"), ("WEEK", "WEEK"), ("MONTH", "MONTH")):
        cases.append(case(f"PDS-{scope}", "DATE_SCOPE", ORDER_SVC, "parseDateScope",
                          [S(scope)], {"type": "date_range", "rule": rule},
                          f"{scope} 区间边界必须与独立实现一致"))
    cases += [
        case("PDS-null", "DATE_SCOPE", ORDER_SVC, "parseDateScope", [N()],
             {"type": "returns_null"}, "空区间不过滤"),
        case("PDS-lower", "DATE_SCOPE", ORDER_SVC, "parseDateScope", [S("today")],
             {"type": "date_range", "rule": "TODAY"}, "大小写不敏感"),
        case("PDS-trim", "DATE_SCOPE", ORDER_SVC, "parseDateScope", [S("  TODAY  ")],
             {"type": "date_range", "rule": "TODAY"}, "首尾空白需 trim"),
        case("PDS-bad", "DATE_SCOPE", ORDER_SVC, "parseDateScope", [S("YEAR")],
             {"type": "throws_business", "code": "VALIDATION_ERROR", "status": 400},
             "非法区间必须 400"),
    ]

    # ---- normalizePackageItems / normalizeAddedItems：明细归一化
    for prefix, method, group in (("NPI", "normalizePackageItems", "ITEM_QUANTITY_UNVALIDATED"),
                                   ("NAI", "normalizeAddedItems", "ITEM_QUANTITY_UNVALIDATED")):
        item_type = PKG_ITEMS if prefix == "NPI" else ADD_ITEMS
        cases += [
            case(f"{prefix}-01", group, ORDER_SVC, method, [item_type([])],
                 {"type": "map_size", "value": 0}, "空明细归一化为空表"),
            case(f"{prefix}-02", group, ORDER_SVC, method, [item_type([{"productId": 11, "quantity": 2}])],
                 {"type": "map_eq", "value": {"11": 2}}, "单条明细原样归一化"),
            case(f"{prefix}-03", group, ORDER_SVC, method,
                 [item_type([{"productId": 11, "quantity": 2}, {"productId": 11, "quantity": 3}])],
                 {"type": "throws_business", "code": "VALIDATION_ERROR", "status": 422},
                 "重复商品必须 422"),
            case(f"{prefix}-04", group, ORDER_SVC, method, [item_type([{"productId": None, "quantity": 2}])],
                 {"type": "map_keys_numeric"}, "productId 为空必须拒绝（DTO 声明 @NotNull）"),
            case(f"{prefix}-05", group, ORDER_SVC, method, [item_type([{"productId": 11, "quantity": None}])],
                 {"type": "map_values_ge", "value": 1}, "quantity 为空必须拒绝（DTO 声明 @NotNull）"),
            case(f"{prefix}-06", group, ORDER_SVC, method, [item_type([{"productId": 11, "quantity": 0}])],
                 {"type": "map_values_ge", "value": 1}, "quantity 必须 >= 1（DTO 声明 @Min(1)）"),
            case(f"{prefix}-07", group, ORDER_SVC, method, [item_type([{"productId": 11, "quantity": -3}])],
                 {"type": "map_values_ge", "value": 1}, "数量为负必须拒绝"),
        ]

    # ---- generateOrderNo：并发唯一性
    cases += [
        case("GON-01", "ORDER_NO_COLLISION", ORDER_SVC, "generateOrderNo", [],
             {"type": "returns_matches", "pattern": r"^DD\d{17}$"},
             "订单号格式必须是 DD + yyyyMMddHHmmss + 3 位随机"),
        {"id": "GON-02", "group": "ORDER_NO_COLLISION", "func": "generateOrderNo_burst",
         "n": 3000, "threads": 24,
         "target": "OrderService#generateOrderNo(burst)", "check": {"type": "burst_unique"},
         "invariant": "同一秒内生成的订单号必须互不相同（唯一槽位只有 1000 个/秒）"},
        {"id": "GON-03", "group": "ORDER_NO_COLLISION", "func": "generateOrderNo_conflict",
         "target": "OrderService#generateOrderNo(conflict)", "check": {"type": "order_no_attempts", "value": 5},
         "invariant": "订单号冲突重试 5 次后必须 409，而不是继续撞唯一键"},
    ]

    # ---- buildPackageSnapshot：快照语义
    cases += [
        case("BPS-01", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot", [N(), N()],
             {"type": "snapshot_empty"}, "无套餐时快照为空对象"),
        case("BPS-02", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot", [PKG(), N()],
             {"type": "snapshot_items_count", "value": 1}, "未传明细时回退套餐默认明细"),
        case("BPS-03", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot", [PKG(), PKG_ITEMS([])],
             {"type": "snapshot_items_count", "value": 0},
             "显式传空明细 = 用户不要明细，不得回退套餐默认明细"),
        case("BPS-04", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot",
             [PKG(), PKG_ITEMS([{"productId": 11, "quantity": 2}])],
             {"type": "snapshot_item_field", "index": 0, "field": "productId", "value": 11},
             "按请求明细生成快照"),
        case("BPS-05", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot",
             [PKG(), PKG_ITEMS([{"productId": 29, "quantity": 1}])],
             {"type": "throws_business", "code": "PRODUCT_NOT_FOUND", "status": 404},
             "套餐外商品（29 不在套餐 4 内）必须被拒绝"),
        case("BPS-06", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot",
             [PKG(), PKG_ITEMS([{"productId": 11, "quantity": 0}])],
             {"type": "snapshot_item_field", "index": 0, "field": "quantity", "value": 1},
             "快照里的数量必须 >= 1"),
        case("BPS-07", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot",
             [PKG(), PKG_ITEMS([{"productId": 999999, "quantity": 1}])],
             {"type": "throws_business", "code": "PRODUCT_NOT_FOUND", "status": 404},
             "不存在的商品必须 404"),
        case("BPS-08", "SNAPSHOT_SEMANTICS", ORDER_SVC, "buildPackageSnapshot", [PKG(), N()],
             {"type": "snapshot_has_fields",
              "fields": ["packageId", "packageName", "banquetType", "priceCent", "imageUrl", "items"]},
             "快照根字段必须齐全"),
    ]

    # ---- apply：订单落库前的全部编排
    cases += [
        case("APL-01", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply", apply_args(),
             {"type": "no_throw"}, "正常入参必须成功"),
        case("APL-02", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(customer_name=N()),
             {"type": "throws_business", "status": 422},
             "customerName 为空必须 422（DTO 声明 @NotBlank），不能是 500"),
        case("APL-03", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(customer_phone=N()),
             {"type": "throws_business", "status": 422},
             "customerPhone 为空必须 422，不能是 500"),
        case("APL-04", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(banquet_type=N()),
             {"type": "throws_business", "status": 422},
             "banquetType 为空必须 422，不能是 500"),
        case("APL-05", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply", apply_args(address=N()),
             {"type": "state_eq", "field": "address", "value": ""},
             "地址为空时归一化为空串"),
        case("APL-06", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply", apply_args(remark=N()),
             {"type": "state_eq", "field": "remark", "value": None},
             "备注为空时存 null"),
        case("APL-07", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(customer_name=S("  张三  ")),
             {"type": "state_eq", "field": "customerName", "value": "张三"},
             "文本字段必须 trim"),
        case("APL-08", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(package_id=L(999999)),
             {"type": "throws_business", "code": "PACKAGE_NOT_FOUND", "status": 404},
             "套餐不存在必须 404"),
        case("APL-09", "APPLY_NULL_CONTRACT", ORDER_SVC, "apply",
             apply_args(status=N()),
             {"type": "throws_business", "status": 422},
             "status 为空必须 422（DTO 声明 @NotNull）"),
        case("APL-10", "APPLY_AMOUNT_INVARIANTS", ORDER_SVC, "apply",
             apply_args(plan_total_cent=N()),
             {"type": "throws_business", "code": "PRICE_INVALID", "status": 422},
             "套餐总价缺失必须 422"),
        case("APL-11", "APPLY_AMOUNT_INVARIANTS", ORDER_SVC, "apply",
             apply_args(plan_total_cent=L(-1)),
             {"type": "throws_business", "code": "PRICE_INVALID", "status": 422},
             "负总价必须 422"),
        case("APL-12", "APPLY_AMOUNT_INVARIANTS", ORDER_SVC, "apply",
             apply_args(plan_total_cent=L(46800), deposit_cent=L(99999)),
             {"type": "throws_business", "status": 422},
             "定金不得大于套餐总价"),
        case("APL-13", "APPLY_AMOUNT_INVARIANTS", ORDER_SVC, "apply",
             apply_args(table_count=I(0)),
             {"type": "throws_business", "status": 422},
             "桌数必须 >= 1（DTO 声明 @Min(1)）"),
        case("APL-14", "APPLY_AMOUNT_INVARIANTS", ORDER_SVC, "apply",
             apply_args(reservation_time=N()),
             {"type": "throws_business", "status": 422},
             "预订时间缺失必须 422（DTO 声明 @NotNull）"),
        case("APL-15", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "apply",
             apply_args(use_final_price=B(True), final_price_cent=L(999999)),
             {"type": "state_le", "field": "finalPriceCent", "value": 46800},
             "落库的成交价不得高于套餐总价"),
        case("APL-16", "PRICE_CLIENT_CONTROLLED", ORDER_SVC, "apply",
             apply_args(use_final_price=B(False), final_price_cent=L(1)),
             {"type": "state_eq", "field": "finalPriceCent", "value": 46800},
             "不使用成交价时落库价格必须是套餐总价"),
        case("APL-17", "SNAPSHOT_SEMANTICS", ORDER_SVC, "apply",
             apply_args(package_items=PKG_ITEMS([])),
             {"type": "state_snapshot_items_count", "value": 0},
             "显式传空明细时，订单快照必须为空而不是套餐默认明细"),
    ]

    # ---- UploadService：文件落地
    cases += [
        case("UPL-01", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [N()], {"type": "throws_business", "code": "IMAGE_UPLOAD_FAILED", "status": 400},
             "空文件必须 400"),
        case("UPL-02", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [MP("evil.exe", "application/octet-stream", b"MZ\x90\x00payload")],
             {"type": "throws_business", "code": "IMAGE_UPLOAD_FAILED", "status": 400},
             "非图片扩展名必须 400"),
        case("UPL-03", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [MP("evil.exe", "image/png", b"MZ\x90\x00payload")],
             {"type": "upload_no_file_written"},
             "扩展名不在白名单时不得落盘（哪怕自称 image/png）"),
        case("UPL-04", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [MP("evil.png", "image/png", b"MZ\x90\x00\x03\x00\x00\x00this is a PE binary")],
             {"type": "upload_no_file_written"},
             "文件内容不是图片时不得落盘（内容必须嗅探，不能只看客户端自报的 contentType）"),
        case("UPL-05", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [MP("../../../../etc/passwd.png", "image/png", b"\x89PNG\r\n\x1a\n")],
             {"type": "upload_path_contained"},
             "文件名带路径穿越时，落盘路径必须仍在上传目录内"),
        case("UPL-06", "UPLOAD_NO_SIZE_LIMIT", UPLOAD_SVC, "uploadProductImage",
             [MP("big.png", "image/png", size=20 * 1024 * 1024)],
             {"type": "upload_size_le", "max_bytes": 5 * 1024 * 1024},
             "上传体积必须有上限（防止上传放大 DoS）"),
        case("UPL-07", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "uploadProductImage",
             [MP("plain.png", "image/png", b"just plain text, definitely not an image")],
             {"type": "upload_no_file_written"},
             "任意字节不得以图片扩展名落盘"),
        case("EXT-01", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "extension", [N()],
             {"type": "throws_business", "code": "IMAGE_UPLOAD_FAILED", "status": 400},
             "文件名为空必须 400"),
        case("EXT-02", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "extension", [S("a")],
             {"type": "throws_business", "code": "IMAGE_UPLOAD_FAILED", "status": 400},
             "无扩展名必须 400"),
        case("EXT-03", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "extension", [S("a.JPG")],
             {"type": "returns_eq", "value": "jpg"}, "扩展名需转小写"),
        case("EXT-04", "UPLOAD_CONTENT_NOT_VERIFIED", UPLOAD_SVC, "extension", [S("a.tar.gz")],
             {"type": "returns_eq", "value": "gz"}, "只取最后一段扩展名"),
    ]

    # ---- CategoryService.create：名称归一化
    cases += [
        case("CTG-01", "APPLY_NULL_CONTRACT", CATEGORY_SVC, "create",
             [CATEGORY_REQ(" 甜品 ")], {"type": "no_throw"}, "名称 trim 后正常创建"),
        case("CTG-02", "APPLY_NULL_CONTRACT", CATEGORY_SVC, "create",
             [CATEGORY_REQ(None)], {"type": "throws_business", "status": 422},
             "名称为 null 必须 422（DTO 声明 @NotBlank），不能是 500"),
    ]

    # ---- 分页归一化：两个 service 各一份，必须行为一致
    for svc, tag in ((PRODUCT_SVC, "PRD"), (PACKAGE_SVC, "PKG")):
        cases += [
            case(f"PG-{tag}-01", "PAGINATION", svc, "normalizePage", [I(0)],
                 {"type": "returns_eq", "value": 0}, "第 0 页归一化为 0（0 基）"),
            case(f"PG-{tag}-02", "PAGINATION", svc, "normalizePage", [I(-5)],
                 {"type": "returns_eq", "value": 0}, "负页码归一化为 0"),
            case(f"PG-{tag}-03", "PAGINATION", svc, "normalizePage", [I(3)],
                 {"type": "returns_eq", "value": 2}, "第 3 页归一化为 2"),
            case(f"PS-{tag}-01", "PAGINATION", svc, "normalizePageSize", [I(0)],
                 {"type": "returns_eq", "value": 1}, "每页 0 条归一化为 1"),
            case(f"PS-{tag}-02", "PAGINATION", svc, "normalizePageSize", [I(-1)],
                 {"type": "returns_eq", "value": 1}, "负每页条数归一化为 1"),
            case(f"PS-{tag}-03", "PAGINATION", svc, "normalizePageSize", [I(100)],
                 {"type": "returns_eq", "value": 100}, "每页 100 条保持不变"),
            case(f"PS-{tag}-04", "PAGINATION", svc, "normalizePageSize", [I(101)],
                 {"type": "returns_eq", "value": 100}, "每页上限 100"),
            case(f"PS-{tag}-05", "PAGINATION", svc, "normalizePageSize", [I(2147483647)],
                 {"type": "returns_eq", "value": 100}, "超大每页条数不得溢出"),
        ]
    return cases


# ----------------------------------------------------------------- oracle

def shanghai_today() -> datetime.date:
    return datetime.now(SHANGHAI).date()


def expected_range(rule: str) -> tuple[datetime.date, datetime.date]:
    today = shanghai_today()
    if rule == "TODAY":
        return today, today + timedelta(days=1)
    if rule == "WEEK":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if rule == "MONTH":
        start = today.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)
    raise ValueError(rule)


def parse_dt(value: str) -> datetime.datetime:
    return datetime.fromisoformat(value)


def _call_count(counts: dict, method: str) -> int:
    """按方法名汇总调用次数。

    harness 记的键是「仓储简单名#方法名」，断言里通常只写方法名，
    所以两种写法都要认（精确匹配优先，否则按后缀匹配）。
    """
    if method in counts:
        return counts[method]
    suffix = "#" + method
    hit = [v for k, v in counts.items() if k.endswith(suffix)]
    return sum(hit) if hit else 0


def _case_fingerprints(cases: list[dict]) -> dict[str, str]:
    """每条用例的指纹：用例定义 + 目标方法的声明事实 + 工具版本。

    目标方法的声明事实变了（签名/分支/副作用/调用集），这条用例的结论就可能过期。
    """
    import store

    decl = {}
    try:
        decl = json.loads((project.artifact_dir() / "fn_declaration.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        decl = {}
    raw_fns = decl.get("functions") or []
    fns = {}
    if isinstance(raw_fns, dict):
        fns = raw_fns
    else:
        for rec in raw_fns:
            key = f"{rec.get('class')}#{rec.get('method')}"
            fns[key] = rec
    import env_fingerprint

    env = store.env_fingerprint(env_fingerprint.collect())
    out: dict[str, str] = {}
    for case in cases:
        target = f"{case.get('class')}#{case.get('method')}"
        fact = fns.get(target, {})
        out[case["id"]] = store.sha(
            {"case": {k: v for k, v in case.items() if k != "label"},
             "fact": fact,
             "env": env,
             "tool": "fn_dynamic@2"})
    return out


def _stored_fingerprint(case_id: str) -> str | None:
    """上次跑这个分片时记下的指纹。"""
    import store

    path = store.shard_dir("fn_observation") / f"{case_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data.get("input_fingerprint")


def _merge_incremental(raw: list[dict], ran_cases: list[dict],
                       fingerprints: dict[str, str], stale: set[str]) -> list[dict]:
    """把重跑的分片写盘，再把没失效的旧分片合回来。

    少一个分片 → 退出码 2（没查完），不是绿。
    """
    import store

    fresh: dict[str, dict] = {}
    for obs in raw:
        fresh[obs["id"]] = obs
        store.write_shard("fn_observation", obs["id"], obs,
                          fingerprints.get(obs["id"], ""),
                          tool_version="fn_dynamic@2", source="incremental")
    merged, missing = store.merge("fn_observation", fresh, fingerprints)
    store.write_index("fn_observation",
                      {sid: {"input_fingerprint": fingerprints.get(sid, ""),
                              "generated_at": "",
                              "source": "incremental"}
                       for sid in merged}, tool_version="fn_dynamic@2")
    print(f"增量合并：本次重跑 {len(fresh)} 条，复用 {len(merged) - len(fresh)} 条，"
          f"缺失 {len(missing)} 条")
    if missing:
        print(f"  缺失分片 {len(missing)} 条 -> {missing[:8]}")
        raise SystemExit(2)
    return [merged[sid] for sid in sorted(merged)]


def evaluate(case_def: dict, obs: dict) -> tuple[bool, str]:
    ch = case_def["check"]
    kind = ch["type"]
    outcome = obs.get("outcome")
    detail = f"{obs.get('exception_class', '')}: {obs.get('exception_message', '')}".strip()

    needs_return = kind in {"returns_eq", "returns_null", "returns_ge", "returns_le",
                            "returns_gt", "returns_lt", "returns_matches", "map_size",
                            "map_eq", "map_keys_numeric", "map_values_ge", "date_range",
                            "snapshot_items_count", "snapshot_item_field", "snapshot_empty",
                            "snapshot_has_fields", "state_eq", "state_ge", "state_le",
                            "state_not_null", "state_snapshot_items_count", "no_throw"}
    needs_throw = kind in {"throws_business", "throws_any"}
    if needs_return and outcome != "returned":
        return False, f"期望正常返回，实际 {outcome} / {detail}"
    if needs_throw and outcome != "threw":
        return False, f"期望抛异常，实际返回 {obs.get('value')}"

    value = obs.get("value")
    if kind == "returns_eq":
        ok = value == ch["value"]
        return ok, f"期望 {ch['value']!r}，实际 {value!r}"
    if kind == "returns_non_null":
        ok = value is not None
        return ok, f"期望非 null，实际 {value!r}"
    if kind == "entity_field_null":
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"], "__缺失__")
        return got is None, f"期望 {ch['field']} 为 null，实际 {got!r}"
    if kind == "entity_field_not_null":
        # 「这个字段应该被写上」—— 断言**应有行为**，而不是现状。
        # 断言现状的用例永远 PASS，也就永远开不出单。
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"], "__缺失__")
        return got is not None, f"期望 {ch['field']} 非 null，实际 {got!r}"
    if kind == "returns_null":
        return value is None, f"期望 null，实际 {value!r}"
    if kind == "returns_ge":
        ok = isinstance(value, (int, float)) and value >= ch["value"]
        return ok, f"期望 >= {ch['value']}，实际 {value!r}"
    if kind == "returns_le":
        ok = isinstance(value, (int, float)) and value <= ch["value"]
        return ok, f"期望 <= {ch['value']}，实际 {value!r}"
    if kind == "returns_gt":
        ok = isinstance(value, (int, float)) and value > ch["value"]
        return ok, f"期望 > {ch['value']}，实际 {value!r}"
    if kind == "returns_lt":
        ok = isinstance(value, (int, float)) and value < ch["value"]
        return ok, f"期望 < {ch['value']}，实际 {value!r}"
    if kind == "returns_matches":
        ok = isinstance(value, str) and re.match(ch["pattern"], value) is not None
        return ok, f"期望匹配 {ch['pattern']}，实际 {value!r}"
    if kind == "throws_business":
        code = obs.get("business_error_code")
        status = obs.get("http_status")
        if "code" in ch and code != ch["code"]:
            return False, f"期望错误码 {ch['code']}，实际 {code}（抛出的是 {obs.get('exception_class')}）"
        if "status" in ch and status != ch["status"]:
            return False, (f"期望 HTTP {ch['status']}，实际 {status} / {code}"
                           f"（抛出的是 {obs.get('exception_class')}: {obs.get('exception_message')}）")
        return True, f"{code} / HTTP {status}"
    if kind == "throws_any":
        return True, f"{obs.get('exception_class')} / {obs.get('exception_message')}"
    if kind == "no_throw":
        return True, "无异常"
    if kind == "map_size":
        ok = isinstance(value, dict) and len(value) == ch["value"]
        return ok, f"期望 {ch['value']} 条，实际 {value!r}"
    if kind == "map_eq":
        ok = value == ch["value"]
        return ok, f"期望 {ch['value']!r}，实际 {value!r}"
    if kind == "map_keys_numeric":
        ok = isinstance(value, dict) and all(str(k).isdigit() for k in value.keys())
        return ok, f"实际 {value!r}"
    if kind == "map_values_ge":
        ok = isinstance(value, dict) and all(
            isinstance(v, (int, float)) and v >= ch["value"] for v in value.values())
        return ok, f"实际 {value!r}"
    if kind == "date_range":
        if not isinstance(value, dict) or "start" not in value or "end" not in value:
            return False, f"区间结构异常: {value!r}"
        start = parse_dt(value["start"]).date()
        end = parse_dt(value["end"]).date()
        exp_start, exp_end = expected_range(ch["rule"])
        ok = start == exp_start and end == exp_end
        return ok, f"期望 [{exp_start}, {exp_end})，实际 [{start}, {end})"
    if kind == "snapshot_empty":
        ok = isinstance(value, dict) and not value.get("items")
        return ok, f"期望空快照，实际 {value!r}"
    if kind == "snapshot_items_count":
        items = (value or {}).get("items") if isinstance(value, dict) else None
        ok = isinstance(items, list) and len(items) == ch["value"]
        return ok, f"期望 {ch['value']} 条明细，实际 {value!r}"
    if kind == "snapshot_item_field":
        items = (value or {}).get("items") if isinstance(value, dict) else None
        if not isinstance(items, list) or len(items) <= ch["index"]:
            return False, f"明细不足: {value!r}"
        got = items[ch["index"]].get(ch["field"])
        return got == ch["value"], f"期望 {ch['field']}={ch['value']!r}，实际 {got!r}"
    if kind == "snapshot_has_fields":
        missing = [f for f in ch["fields"] if not isinstance(value, dict) or f not in value]
        return not missing, f"缺失字段 {missing}"
    if kind == "state_eq":
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"])
        return got == ch["value"], f"期望 {ch['field']}={ch['value']!r}，实际 {got!r}"
    if kind == "state_ge":
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"])
        ok = isinstance(got, (int, float)) and got >= ch["value"]
        return ok, f"期望 {ch['field']} >= {ch['value']}，实际 {got!r}"
    if kind == "state_le":
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"])
        ok = isinstance(got, (int, float)) and got <= ch["value"]
        return ok, f"期望 {ch['field']} <= {ch['value']}，实际 {got!r}"
    if kind == "state_not_null":
        state = obs.get("entity_state") or {}
        got = state.get(ch["field"])
        return got is not None, f"{ch['field']} 实际 {got!r}"
    if kind == "state_snapshot_items_count":
        state = obs.get("entity_state") or {}
        snap = state.get("packageSnapshot") or {}
        items = snap.get("items")
        ok = isinstance(items, list) and len(items) == ch["value"]
        return ok, f"期望快照 {ch['value']} 条明细，实际 {snap!r}"
    if kind == "burst_unique":
        dups = obs.get("duplicates")
        return dups == 0, f"并发生成 {obs.get('generated')} 个订单号，重复 {dups} 个"
    if kind in ("call_count_eq", "call_count_at_least"):
        counts = obs.get("call_counts") or {}
        got = _call_count(counts, ch["method"])
        if kind == "call_count_eq":
            return got == ch["value"], f"{ch['method']} 调用 {got} 次，期望正好 {ch['value']} 次"
        return got >= ch["value"], f"{ch['method']} 调用 {got} 次，期望 ≥ {ch['value']}"
    if kind == "order_no_attempts":
        got = obs.get("order_no_exists_calls")
        return got == ch["value"], f"期望重试 {ch['value']} 次，实际 {got} 次"
    if kind == "upload_no_file_written":
        files = obs.get("upload_dir_state") or []
        return not files, f"落盘文件: {files}"
    if kind == "upload_path_contained":
        files = obs.get("upload_dir_state") or []
        ok = all("/" not in f.get("name", "") or ".." not in f.get("name", "") for f in files)
        return ok, f"落盘文件: {files}"
    if kind == "upload_size_le":
        files = obs.get("upload_dir_state") or []
        ok = all(f.get("size", 0) <= ch["max_bytes"] for f in files)
        return ok, f"落盘文件: {files}"
    return False, f"未知断言类型 {kind}"


# ----------------------------------------------------------------- runner

def ensure_generic_harness() -> list[str]:
    """编译通用 harness（不含任何项目字面量，用例与打桩来自用例文件）。"""
    import fn_static
    fn_static.ensure_compiled()
    deps = (WORK / "deps.txt").read_text().strip()
    generic_classes = WORK / "generic_classes"
    generic_classes.mkdir(parents=True, exist_ok=True)
    subprocess.run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(generic_classes),
                    "-cp", f"{fn_static.CLASSES}:{deps}",
                    str(SCRIPT_DIR / "fn_harness" / "GenericHarness.java")],
                   check=True, capture_output=True, text=True)
    return [str(generic_classes), str(fn_static.CLASSES), deps]


def ensure_harness() -> list[str]:
    import fn_static
    fn_static.ensure_compiled()
    deps = (WORK / "deps.txt").read_text().strip()
    HARNESS_CLASSES.mkdir(parents=True, exist_ok=True)
    subprocess.run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(HARNESS_CLASSES),
                    "-cp", f"{CLASSES}:{deps}", str(SCRIPT_DIR / "fn_harness" / "FunctionHarness.java")],
                   check=True, capture_output=True, text=True)
    return [f"{HARNESS_CLASSES}", f"{CLASSES}", deps]


def main() -> None:
    parser = argparse.ArgumentParser(description="函数层动态侧：JVM 内反射直调")
    parser.add_argument("--case", action="append", default=[], metavar="CASE_ID",
                        help="只跑指定用例 id，可重复；不带该参数则跑全量")
    parser.add_argument("--cases-file", default="",
                        help="用例文件（含打桩与类型别名）；给了就用通用 harness，"
                             "换项目只换这个文件")
    parser.add_argument("--extra-cases", default="",
                        help="额外的用例文件（如语义地图编译出来的职责断言），"
                             "与主用例合并执行")
    parser.add_argument("--incremental", action="store_true",
                        help="增量模式：只重跑指纹变了的用例，其余复用上次分片；"
                             "有分片缺失则退出码 2（没查完，不是绿）")
    args = parser.parse_args()

    cases_path = args.cases_file or project.path("fn_cases", "")
    if cases_path and not Path(cases_path).is_absolute():
        cases_path = str(project.project_dir() / cases_path)
    spec: dict | None = None
    if cases_path:
        spec = json.loads(Path(cases_path).read_text(encoding="utf-8"))
        cases = spec["cases"]
        classpath = ensure_generic_harness()
        main_class = "GenericHarness"
        CASES_FILE.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    else:
        cases = build_cases()
        classpath = ensure_harness()
        main_class = "FunctionHarness"
        CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    if args.extra_cases:
        extra_path = Path(args.extra_cases)
        if not extra_path.is_absolute():
            extra_path = project.project_dir() / extra_path
        extra = json.loads(extra_path.read_text(encoding="utf-8"))
        extra_cases = extra.get("cases", extra) if isinstance(extra, dict) else extra
        cases = list(cases) + list(extra_cases)
        print(f"合并额外用例 {len(extra_cases)} 条（语义地图职责断言）")
        # 用例文件必须重新写 —— 上面那份还是合并前的
        if spec is not None:
            CASES_FILE.write_text(
                json.dumps({**spec, "cases": cases}, ensure_ascii=False), encoding="utf-8")
        else:
            CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    if args.case:
        known = {c["id"] for c in cases}
        unknown = sorted(set(args.case) - known)
        if unknown:
            raise SystemExit(f"未知用例 id: {unknown}")
        cases = [c for c in cases if c["id"] in set(args.case)]
        CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 增量模式：先算指纹，只重跑失效的用例，其余复用上次分片
    stale: set[str] | None = None
    fingerprints: dict[str, str] = {}
    if args.incremental:
        fingerprints = _case_fingerprints(cases)
        stale = {cid for cid, fp in fingerprints.items()
                 if _stored_fingerprint(cid) != fp}
        print(f"增量模式：{len(cases)} 条用例，失效 {len(stale)} 条，需重跑 {len(stale)} 条")

    # all_cases 始终是**完整**的用例集合；cases_to_run 只是这次要跑的子集。
    # 增量合并后观测覆盖 all_cases，用例定义也必须按 all_cases 查 ——
    # 否则「复用回来的分片」会因为找不到用例定义而 KeyError。
    all_cases = list(cases)
    cases_to_run = list(cases)
    if stale is not None and stale:
        cases_to_run = [c for c in all_cases if c["id"] in stale]
        if spec is not None:
            CASES_FILE.write_text(
                json.dumps({**spec, "cases": cases_to_run}, ensure_ascii=False), encoding="utf-8")
        else:
            CASES_FILE.write_text(json.dumps(cases_to_run, ensure_ascii=False), encoding="utf-8")
            CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    raw: list[dict] = []
    if stale is None or stale:
        # 没有失效用例时**根本不启动 JVM**（增量模式的意义就在这）
        proc = subprocess.run(
            ["java", "-cp", os.pathsep.join(classpath), main_class,
             str(CASES_FILE), str(OUT_FILE), str(UPLOAD_DIR)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout[-3000:])
            print(proc.stderr[-3000:], file=sys.stderr)
            raise SystemExit(f"harness failed: {proc.returncode}")
        raw = json.loads(OUT_FILE.read_text())

    # 写分片 + 并集合并（增量模式下把复用���分片合回来）
    if stale is not None:
        raw = _merge_incremental(raw, cases_to_run, fingerprints, stale)

    by_id = {c["id"]: c for c in all_cases}
    results = []
    for obs in raw:
        cdef = by_id[obs["id"]]
        # 「参数类型对不上」是用例构造错误，不是断言失败，更不是缺陷。
        # 不拦下来的话，一个构造错的断言会以 FAIL 的面目混进缺陷台账。
        if "argument type mismatch" in str(obs.get("exception_message", "")):
            results.append({
                "id": cdef["id"], "group": cdef["group"], "func": cdef["func"],
                "target": obs.get("target", ""), "label": cdef.get("label", ""),
                "invariant": cdef["invariant"], "check": cdef["check"],
                "verdict": "INVALID", "reason": "用例构造错误：参数类型与方法签名对不上",
                "observation": obs,
            })
            continue
        ok, reason = evaluate(cdef, obs)
        results.append({
            "id": cdef["id"], "group": cdef["group"], "func": cdef["func"],
            "target": obs.get("target", ""), "label": cdef.get("label", ""),
            "invariant": cdef["invariant"], "check": cdef["check"],
            "verdict": "PASS" if ok else "FAIL", "reason": reason,
            "observation": obs,
        })

    payload = {
        "meta": {
            "carrier": "JVM 内反射直调（不经过 HTTP / 数据库）",
            "functions_under_test": len({r["target"] for r in results}),
            "cases": len(results),
            "passed": sum(1 for r in results if r["verdict"] == "PASS"),
            "failed": sum(1 for r in results if r["verdict"] == "FAIL"),
            "upload_sandbox": str(UPLOAD_DIR),
            "sut_data_written": 0,
        },
        "cases": results,
    }
    # 单用例重放不覆盖全量观测文件，避免把完整证据面打掉
    out = HERE / ("fn_observation_replay.json" if args.case else "fn_observation.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"函数层实测 -> {out}" + (f"（重放模式，{len(cases)} 条）" if args.case else ""))
    print(f"  用例 {len(results)} 条：通过 {payload['meta']['passed']}，失败 {payload['meta']['failed']}")
    for r in results:
        if r["verdict"] == "FAIL":
            print(f"    FAIL {r['id']:<12} {r['target']:<58} {r['reason'][:70]}")


if __name__ == "__main__":
    main()
