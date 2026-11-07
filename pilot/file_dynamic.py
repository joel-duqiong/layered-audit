"""文件层 · 动态侧：整类实例化实测 + 真实容器探针。

载体分工：
  - 文件内部编排（PackageService / WebConfig / TraceIdFilter / GlobalExceptionHandler）
    -> JVM 内整类实例化，仓库打桩可编排失败，中间态快照 + 日志捕获
  - 过滤器的边界行为（Tomcat 会不会挡掉恶意头）
    -> 真实容器 HTTP 探针（含裸 socket 发畸形头）

每个用例的判据都写在源码证据上（见 notes），不是「跑出来啥就是啥」。
产出 file_observation.json。
"""
from __future__ import annotations
import project

import json
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


BACKEND = project.backend_root()
PILOT = Path(__file__).resolve().parent   # harness 源码在脚本目录，不在产物目录
WORK = project.work_dir() / "file"
HARNESS = WORK / "harness"
CLASSES = project.work_dir() / "classes"
DEPS = PILOT / "file_deps.txt"
BASE_URL = "http://127.0.0.1:8080"
CONTAINER = "jiuyan-backend"

MARK_XFF = "9.9.9.9"
MARK_TRACE = "FILELAYER-TRACE-77"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


# --------------------------------------------------------------------- 用例定义

SCENARIOS = [
    {"id": "PKG-UPDATE-SUBSET", "checks": [
        ("after_items_count", "eq", 1,
         "PUT 只提交 1 个商品：PackageService.applyItems 的 removeIf 会把未提交的明细删掉（全量替换语义，PUT 语义下不算缺陷）"),
        ("result", "eq", "ok", "更新本身成功"),
    ]},
    {"id": "PKG-UPDATE-MISSING-PRODUCT", "checks": [
        ("error_type", "eq", "BusinessException",
         "不存在的商品 id 走 findProduct -> BusinessException(PRODUCT_NOT_FOUND)"),
        ("http_status", "eq", 404, "对外是 404 而不是 500"),
        ("after_items_count", "eq", 0,
         "★ 异常抛出时实体明细已被 removeIf 清空：删除发生在存在性校验之前（中间态已改坏）"),
    ]},
    {"id": "PKG-CREATE-MISSING-PRODUCT", "checks": [
        ("error_type", "eq", "BusinessException", "create 路径同样在校验失败时抛业务异常"),
        ("http_status", "eq", 404, "对外 404"),
    ]},
    {"id": "PKG-NULL-BANQUET-TYPE", "checks": [
        ("error_type", "eq", "NullPointerException",
         "★ applyBase 里 banquetType.trim() 未判空：空值是 NPE（500）而不是业务异常（422）"),
        ("is_business_exception", "eq", False, "非业务异常，会落到全局兜底 500"),
    ]},
    {"id": "PKG-EMPTY-ITEMS", "checks": [
        ("error_type", "eq", "BusinessException", "明细为空抛业务异常"),
        ("http_status", "eq", 422, "对外 422"),
    ]},
    {"id": "PKG-DUPLICATE-ITEMS", "checks": [
        ("error_type", "eq", "BusinessException", "重复明细抛业务异常"),
        ("http_status", "eq", 422, "对外 422"),
    ]},
    {"id": "PKG-QUANTITY-ZERO", "checks": [
        ("result", "eq", "ok",
         "★ 数量 0 被直接写进实体（Java 层不校验数量），只能靠 DB 的 chk_package_items_quantity 兜底"),
        ("after_quantity_zero", "eq", True, "★ 明细数量为 0 的条目已进入实体"),
    ]},
    {"id": "PKG-DUP-DB-ITEMS", "checks": [
        ("error_type", "eq", "IllegalStateException",
         "同套餐同商品出现两条明细时 Collectors.toMap 抛重复键（500）——但 DB 有 uk_package_items_package_product 唯一约束，构造不出这种数据"),
    ]},
    {"id": "PKG-UPDATE-MISSING-PACKAGE", "checks": [
        ("error_type", "eq", "BusinessException", "套餐不存在抛业务异常"),
        ("http_status", "eq", 404, "对外 404"),
    ]},
    {"id": "CORS-ORIGIN-MATRIX", "checks": [
        ("evil_allowed", "eq", False, "外网站点不得被放行"),
        ("intranet_any_port_allowed", "eq", True,
         "★ 内网任意主机 + 任意端口都被放行（http://192.168.*.*:[*] 之类的通配）"),
        ("localhost_any_port_allowed", "eq", True, "本机任意端口放行（开发期合理）"),
        ("https_evil_allowed", "eq", False, "https 外网不得被放行"),
    ]},
    {"id": "CORS-CREDENTIALS", "checks": [
        ("allow_credentials", "eq", False, "★ allowCredentials=false：跨站拿不到 Cookie（正向结论）"),
    ]},
    {"id": "UPLOADS-RESOURCE-LOCATION", "checks": [
        ("existing_dir_to_uri_ends_with_slash", "eq", True,
         "目录存在时 toUri() 带尾斜杠，静态映射拼相对路径才正确"),
        ("missing_dir_to_uri_ends_with_slash", "eq", False,
         "★ 目录不存在时 toUri() 不带尾斜杠 -> Spring 会把 /uploads/x.jpg 拼成 <root>x.jpg，全部 404"),
        ("relative_without_slash_ok", "eq", False, "★ 无尾斜杠时相对路径被拼坏"),
        ("relative_with_slash_ok", "eq", True, "有尾斜杠时相对路径正确"),
    ]},
    {"id": "UPLOADS-SAME-ROOT", "checks": [
        ("same_root", "eq", True,
         "★ 静态映射根目录与 UploadService 的上传根目录同源（推翻 manifest 里「非同源」的假设）"),
    ]},
    {"id": "TRACE-XFF-SPOOF", "checks": [
        ("logged_client_ip", "eq", MARK_XFF,
         "★ 审计日志里的 clientIp 直接取 X-Forwarded-For 首段，客户端可任意伪造"),
        ("remote_addr", "eq", "127.0.0.1", "真实对端地址是 127.0.0.1，与日志记录不符"),
    ]},
    {"id": "TRACE-ID-VERBATIM", "checks": [
        ("response_trace_id", "eq", MARK_TRACE,
         "★ 客户端提供的 traceId 原样回写到响应头与日志，服务端不做任何校验"),
        ("log_contains_raw_trace_id", "eq", True, "★ 客户端值原样进入日志行"),
    ]},
    {"id": "TRACE-MDC-CLEANUP", "checks": [
        ("mdc_after", "eq", None, "MDC 在 finally 里清理（正向结论）"),
        ("trace_context_after", "eq", None, "ThreadLocal 在 finally 里清理（正向结论）"),
    ]},
    {"id": "ERR-DATA-INTEGRITY-LEAK", "checks": [
        ("http_status", "eq", 409, "唯一键冲突返回 409"),
        ("body_contains_constraint_name", "eq", True,
         "★ 响应体里带出数据库约束名 uk_orders_order_no（内部实现细节泄漏）"),
    ]},
    {"id": "ERR-BADREQUEST-ECHO", "checks": [
        ("http_status", "eq", 400, "请求体解析失败返回 400"),
        ("body_contains_raw_fragment", "eq", True,
         "★ 响应体里回显了原始请求体片段（攻击者提交什么就回什么）"),
    ]},
    {"id": "ERR-CLIENT-ABORT-ENVELOPE", "checks": [
        ("http_status", "eq", 499, "客户端中断返回 499"),
        ("body_is_null", "eq", True,
         "★ 499 走的是 ResponseEntity<Void>，响应体为空：前端拿不到 success/code/message/data 信封"),
    ]},
    {"id": "ERR-UNKNOWN-NO-LEAK", "checks": [
        ("http_status", "eq", 500, "未知异常返回 500"),
        ("body_contains_internal_detail", "eq", False,
         "★ 兜底 handler 不泄漏内部堆栈（正向结论）"),
    ]},
    {"id": "ADVICE-ENVELOPE", "checks": [
        ("success_envelope_has_fields", "eq", True, "成功响应被 ApiResponseAdvice 包成统一信封"),
        ("trace_id_present", "eq", True,
         "★ 信封里的 traceId 字段：有过滤器的真实请求里一定在（实测容器响应头与响应体都带 traceId）"),
        ("already_envelope_unchanged", "eq", True, "已经是 ApiResponse 的 body 不会被二次包装"),
        ("null_body_result_is_null", "eq", True,
         "空 body 不应被包成 data:null 的成功信封（内容类型为空时不会发生）"),
    ]},
]


# --------------------------------------------------------------------- harness

def build_harness() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(HARNESS),
         "-cp", f"{DEPS.read_text().strip()}:{CLASSES}",
         str(PILOT / "file_harness/FileHarness.java")])


def run_harness() -> dict[str, dict]:
    spec = WORK / "scenarios.json"
    spec.write_text(json.dumps([{"id": s["id"]} for s in SCENARIOS], ensure_ascii=False))
    out = WORK / "observation.json"
    run(["java", "-cp", f"{HARNESS}:{CLASSES}:{DEPS.read_text().strip()}",
         "FileHarness", str(spec), str(out)])
    return {r["id"]: r for r in json.loads(out.read_text())}


# --------------------------------------------------------------------- 派生量

def derive(obs: dict[str, dict]) -> None:
    """把实测原始数据折成可判定的量（判据在 SCENARIOS 里）。"""
    for r in obs.values():
        if r.get("error_type") and "." in r["error_type"]:
            r["error_type"] = r["error_type"].rsplit(".", 1)[1]

    o = obs.get("PKG-UPDATE-SUBSET")
    if o:
        o["after_items_count"] = len(o.get("after_items", []))

    o = obs.get("PKG-UPDATE-MISSING-PRODUCT")
    if o:
        o["after_items_count"] = len(o.get("after_items", []))

    o = obs.get("PKG-QUANTITY-ZERO")
    if o:
        o["after_quantity_zero"] = any(i.get("quantity") == 0 for i in o.get("after_items", []))

    o = obs.get("CORS-ORIGIN-MATRIX")
    if o:
        probes = {p["origin"]: p["allowed"] for p in o.get("probes", [])}
        o["evil_allowed"] = bool(probes.get("http://evil.example.com"))
        o["https_evil_allowed"] = bool(probes.get("https://evil.example.com"))
        o["localhost_any_port_allowed"] = bool(probes.get("http://localhost:9999"))
        o["intranet_any_port_allowed"] = bool(
            probes.get("http://192.168.1.5:9999") and probes.get("http://10.0.0.7:3000"))

    o = obs.get("UPLOADS-RESOURCE-LOCATION")
    if o:
        o["existing_dir_to_uri_ends_with_slash"] = o.get("existing_dir_to_uri", "").endswith("/")
        o["missing_dir_to_uri_ends_with_slash"] = o.get("missing_dir_to_uri", "").endswith("/")
        o["relative_with_slash_ok"] = "/exists/seed/banquet-table.png" in o.get("relative_with_slash", "")
        o["relative_without_slash_ok"] = "/missing/seed/banquet-table.png" in o.get("relative_without_slash", "")

    o = obs.get("ADVICE-ENVELOPE")
    if o:
        body = o.get("success_envelope_json", "")
        o["success_envelope_has_fields"] = all(k in body for k in
                                                ('"success"', '"code"', '"message"', '"data"'))
        o["trace_id_present"] = '"traceId"' in body


# --------------------------------------------------------------------- 容器探针

def container_logs(since: str = "60s") -> str:
    rc = subprocess.run(["docker", "logs", CONTAINER, "--since", since],
                        capture_output=True, text=True)
    return rc.stdout + rc.stderr


def log_window() -> str:
    """取一个时间戳窗口内的容器日志（docker logs 会分流 stdout/stderr，只能按时间取窗）。"""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return ts


def raw_http(path: str, headers: dict[str, str], host: str = "127.0.0.1", port: int = 8080) -> dict:
    """裸 socket 发请求：能塞进 HTTP 库不允许的控制字符。"""
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}"]
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
    with socket.create_connection((host, port), timeout=10) as s:
        s.settimeout(3)
        s.sendall(raw)
        chunks = []
        try:
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                if len(b"".join(chunks)) > 65536:
                    break
        except TimeoutError:
            pass  # keep-alive 连接不会自己关，拿到响应头即可
    data = b"".join(chunks)
    return {"raw_bytes": len(data), "raw_head": data[:400].decode("utf-8", "replace")}


def http_get(path: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(BASE_URL + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": resp.status, "headers": dict(resp.headers), "body_len": len(resp.read())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "headers": dict(e.headers), "body_len": len(e.read())}
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "error": f"{type(e).__name__}: {e}"}


def db_query(sql: str) -> str:
    rc = subprocess.run(
        ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan", "-tAc", sql],
        capture_output=True, text=True)
    return rc.stdout.strip()


def http_put(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE_URL + path, data=data, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": resp.status, "body": resp.read().decode(errors="replace")[:400]}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode(errors="replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "error": f"{type(e).__name__}: {e}"}


def http_probes() -> dict[str, dict]:
    out: dict[str, dict] = {}

    # 1) 恶意 traceId 头：Tomcat 会不会挡
    ts = log_window()
    probe = raw_http("/api/health", {"X-Trace-Id": "abc\r\nFORGED-LOG-LINE-9X"})
    segment = container_logs(ts)
    out["HTTP-TRACEID-CRLF"] = {
        "status_line": probe["raw_head"].splitlines()[0] if probe["raw_head"] else "",
        "forged_line_in_logs": "FORGED-LOG-LINE-9X" in segment,
        "note": "Tomcat 若拒绝含控制字符的头，容器日志里不会出现伪造行",
    }

    # 1b) 非 CRLF 的换行类字符（U+2028）：连接器不一定拦
    ts2 = log_window()
    probe2 = raw_http("/api/health", {"X-Trace-Id": "abc\u2028FORGED-U2028-LINE"})
    segment2 = container_logs(ts2)
    out["HTTP-TRACEID-U2028"] = {
        "status_line": probe2["raw_head"].splitlines()[0] if probe2["raw_head"] else "",
        "u2028_in_logs": "FORGED-U2028-LINE" in segment2,
    }

    # 2) 正常但自定义的 traceId：是否原样回显
    r = http_get("/api/health", {"X-Trace-Id": MARK_TRACE})
    out["HTTP-TRACEID-ECHO"] = {
        "status": r["status"],
        "response_trace_id": r["headers"].get("X-Trace-Id"),
        "verbatim": r["headers"].get("X-Trace-Id") == MARK_TRACE,
    }

    # 3) X-Forwarded-For 伪造：审计日志里的 IP
    ts = log_window()
    http_get("/api/health", {"X-Forwarded-For": MARK_XFF})
    segment = container_logs(ts)
    out["HTTP-XFF-SPOOF"] = {
        "spoofed_ip_in_logs": f"clientIp={MARK_XFF}" in segment,
        "log_segment": segment[-600:],
    }

    # 4) 静态映射在真实部署里确实能出图（正向结论）
    r = http_get("/uploads/seed/banquet-table.png")
    out["HTTP-UPLOADS-STATIC"] = {
        "status": r["status"],
        "body_len": r.get("body_len"),
        "works": r["status"] == 200,
    }

    # 5) 跨文件一致性：DTO 文件声明的约束，在真实接口上是否真的生效
    base = {"name": "升学答谢宴", "banquetType": "升学宴", "priceCent": 46800,
            "imageUrl": "/uploads/seed/packages/package-04.jpg",
            "description": "适合升学答谢，热菜主食汤品齐全。", "isActive": True}
    q0 = http_put("/api/packages/4", {**base, "items": [{"productId": 1, "quantity": 0}]})
    out["HTTP-PKG-QTY-ZERO"] = {"status": q0["status"], "body": q0["body"][:300]}
    qn = http_put("/api/packages/4", {**base, "items": [{"productId": 1, "quantity": -1}]})
    out["HTTP-PKG-QTY-NEGATIVE"] = {"status": qn["status"], "body": qn["body"][:300]}

    # 6) 服务文件的编排顺序缺陷是否被事务边界兜住：改到一半失败，DB 会不会留残局
    items_before = db_query("SELECT id||'|'||product_id||'|'||quantity FROM package_items "
                             "WHERE package_id=4 ORDER BY id")
    name_before = db_query("SELECT name FROM packages WHERE id=4")
    miss = http_put("/api/packages/4", {**base, "items": [{"productId": 1, "quantity": 1},
                                                          {"productId": 999999, "quantity": 1}]})
    items_after = db_query("SELECT id||'|'||product_id||'|'||quantity FROM package_items "
                            "WHERE package_id=4 ORDER BY id")
    name_after = db_query("SELECT name FROM packages WHERE id=4")
    out["HTTP-PKG-ROLLBACK"] = {
        "status": miss["status"],
        "body": miss["body"][:300],
        "items_unchanged": items_before == items_after,
        "items_before": items_before,
        "items_after": items_after,
        "name_unchanged": name_before == name_after,
        "name_before": name_before,
        "name_after": name_after,
    }

    # 7) CORS 在真实容器里对内网任意主机是否放行
    r1 = http_get("/api/packages", {"Origin": "http://192.168.66.66:1234"})
    r2 = http_get("/api/packages", {"Origin": "http://localhost:65535"})
    r3 = http_get("/api/packages", {"Origin": "http://evil.example.com"})
    out["HTTP-CORS-REAL"] = {
        "intranet_acao": r1["headers"].get("Access-Control-Allow-Origin"),
        "localhost_any_port_acao": r2["headers"].get("Access-Control-Allow-Origin"),
        "evil_acao": r3["headers"].get("Access-Control-Allow-Origin"),
    }
    return out


# --------------------------------------------------------------------- 判定

OPS = {
    "eq": lambda a, e: a == e,
    "ne": lambda a, e: a != e,
    "in": lambda a, e: e in (a if isinstance(a, (list, tuple, dict)) else [a]),
}


def main() -> None:
    build_harness()
    obs = run_harness()
    derive(obs)
    http = http_probes()

    results = []
    for spec in SCENARIOS:
        sid = spec["id"]
        o = obs.get(sid, {})
        checks = []
        for name, op, expect, note in spec["checks"]:
            actual = o.get(name)
            ok = OPS[op](actual, expect)
            checks.append({"check": name, "expected": expect, "actual": actual,
                           "pass": bool(ok), "note": note})
        results.append({"id": sid, "checks": checks,
                        "raw": {k: v for k, v in o.items() if k not in ("logs",)}})

    http_checks = [
        {"check": "forged_line_in_logs", "expected": False, "actual": http["HTTP-TRACEID-CRLF"]["forged_line_in_logs"],
         "pass": http["HTTP-TRACEID-CRLF"]["forged_line_in_logs"] is False,
         "note": "含 CRLF 的 traceId 是否能伪造出日志行（若 Tomcat 拦截则为假，边界已挡）"},
        {"check": "verbatim_echo", "expected": True, "actual": http["HTTP-TRACEID-ECHO"]["verbatim"],
         "pass": http["HTTP-TRACEID-ECHO"]["verbatim"] is True,
         "note": "客户端 traceId 是否原样回显到响应头"},
        {"check": "spoofed_ip_in_logs", "expected": True, "actual": http["HTTP-XFF-SPOOF"]["spoofed_ip_in_logs"],
         "pass": http["HTTP-XFF-SPOOF"]["spoofed_ip_in_logs"] is True,
         "note": "X-Forwarded-For 伪造的 IP 是否进入真实容器审计日志"},
        {"check": "uploads_static_works", "expected": True, "actual": http["HTTP-UPLOADS-STATIC"]["works"],
         "pass": http["HTTP-UPLOADS-STATIC"]["works"] is True,
         "note": "真实部署里 /uploads/** 静态映射是否可用（正向结论）"},
        {"check": "qty_zero_rejected", "expected": 400, "actual": http["HTTP-PKG-QTY-ZERO"]["status"],
         "pass": http["HTTP-PKG-QTY-ZERO"]["status"] == 400,
         "note": "源码证据：DTO @Min(1) -> MethodArgumentNotValidException -> handleValidation -> BAD_REQUEST(400)。"
                 "数量=0 在跨文件校验链上被挡住了（正向结论）"},
        {"check": "qty_negative_rejected", "expected": 400, "actual": http["HTTP-PKG-QTY-NEGATIVE"]["status"],
         "pass": http["HTTP-PKG-QTY-NEGATIVE"]["status"] == 400,
         "note": "数量=-1 同上，被 @Min(1) 挡住"},
        {"check": "validation_status_split", "expected": True,
         "actual": http["HTTP-PKG-QTY-ZERO"]["status"] != obs.get("PKG-EMPTY-ITEMS", {}).get("http_status"),
         "pass": http["HTTP-PKG-QTY-ZERO"]["status"] != obs.get("PKG-EMPTY-ITEMS", {}).get("http_status"),
         "note": "同属「参数非法」：DTO 层校验返 400，服务文件自校验返 422 —— 状态码按校验责任分裂"},
        {"check": "missing_product_status", "expected": 404, "actual": http["HTTP-PKG-ROLLBACK"]["status"],
         "pass": http["HTTP-PKG-ROLLBACK"]["status"] == 404,
         "note": "明细引用不存在商品时的状态码"},
        {"check": "rollback_items_unchanged", "expected": True, "actual": http["HTTP-PKG-ROLLBACK"]["items_unchanged"],
         "pass": http["HTTP-PKG-ROLLBACK"]["items_unchanged"] is True,
         "note": "applyItems 先删后校验导致的中途失败，事务边界是否把 DB 兜回来"},
        {"check": "rollback_name_unchanged", "expected": True, "actual": http["HTTP-PKG-ROLLBACK"]["name_unchanged"],
         "pass": http["HTTP-PKG-ROLLBACK"]["name_unchanged"] is True,
         "note": "applyBase 先改后失败的字段是否也被回滚"},
        {"check": "cors_intranet_reflected", "expected": "http://192.168.66.66:1234",
         "actual": http["HTTP-CORS-REAL"]["intranet_acao"],
         "pass": http["HTTP-CORS-REAL"]["intranet_acao"] == "http://192.168.66.66:1234",
         "note": "真实容器是否把内网任意主机:任意端口回显成允许源"},
        {"check": "cors_evil_blocked", "expected": None, "actual": http["HTTP-CORS-REAL"]["evil_acao"],
         "pass": http["HTTP-CORS-REAL"]["evil_acao"] is None,
         "note": "外网域名是否被挡（正向结论）"},
    ]
    results.append({"id": "HTTP-PROBES", "checks": http_checks, "raw": http})

    payload = {
        "meta": {
            "harness": "JVM 内整类实例化 + 仓库打桩 + 日志捕获；容器探针走真实 HTTP",
            "scenarios": len(SCENARIOS),
            "http_probes": len(http_checks),
            "upload_sandbox": str(WORK / "uploads"),
        },
        "results": results,
    }
    out = PILOT / "file_observation.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    total = sum(len(r["checks"]) for r in results)
    failed = [(r["id"], c) for r in results for c in r["checks"] if not c["pass"]]
    print(f"文件层动态 -> {out}")
    print(f"  用例 {len(results)} 个 / 判定 {total} 条 / 不符 {len(failed)} 条")
    for sid, c in failed:
        print(f"  !! {sid}.{c['check']}: 期望 {c['expected']} 实际 {c['actual']}")


if __name__ == "__main__":
    main()
