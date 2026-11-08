"""文件层 · 容器探针插件：真实 HTTP / 真实 DB 的探针，参数全来自用例配置的 http 段。

为什么单独一个插件：整类实例化跑在 JVM 里（通用 harness），但「Tomcat 会不会挡掉畸形头」
「事务边界有没有把残局兜回来」这类问题只能在真实容器上问。容器、库、URL、SQL 都是项目知识，
所以插件是项目侧的；但插件里也没有项目字面量 —— 全部由用例文件的 http 段给出。
换项目 = 换用例文件。

探针类型（配置里用 kind 指定）：
  raw_header   裸 socket 发头（HTTP 库不让塞控制字符）
  http_get     普通 GET
  http_put     PUT（带 JSON 体）
  db_compare   写请求前后各查一次 DB，比对是否留下残局
  cors         带 Origin 头问 CORS 响应头
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

LOG_TAIL = 600
# docker 的日志条目时间戳只到毫秒，而 --since 收的是微秒 —— 同一毫秒里写下的日志
# 会被判成「早于窗口」而丢掉。窗口往前留 2 秒，躲开这个竞态（也躲开守护进程时钟漂移）。
LOG_LOOKBACK_S = 2.0


# --------------------------------------------------------------------- 基础动作

def log_window() -> str:
    """docker logs 只能按时间取窗（stdout/stderr 分流，没法按条取）。"""
    t = datetime.now(timezone.utc).timestamp() - LOG_LOOKBACK_S
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def container_logs(container: str, since: str) -> str:
    rc = subprocess.run(["docker", "logs", container, "--since", since],
                        capture_output=True, text=True)
    return rc.stdout + rc.stderr


def around(segment: str, marker: str, before: int = 300, after_lines: int = 0) -> str:
    """围绕标记截一段。

    取日志尾巴是碰运气：同一条窗口里别的日志排在后面，证据里就看不到要找的东西。
    围绕标记截，证据里一定看得见标记本身。

    默认只到标记那一行结束 —— 再往后就是容器里碰巧同时发生的别的事（实测遇到过
    OTel 导出报错混进证据），那部分每次跑都不一样，证据就不确定了。
    需要更多上下文就用 log_lines_after 显式要几行。
    """
    if not marker:
        return segment[-LOG_TAIL:]
    i = segment.rfind(marker)
    if i < 0:
        return segment[-LOG_TAIL:]
    start = max(0, i - before)
    nl = segment.find("\n", start)      # 切到行首，别把上一行劈成半句
    if nl >= 0 and nl < i:
        start = nl + 1
    end = segment.find("\n", i)
    if end < 0:
        end = len(segment)
    else:
        end += 1
    for _ in range(max(0, after_lines)):   # 要后面几行就整行整行地要
        nl = segment.find("\n", end)
        if nl < 0:
            end = len(segment)
            break
        end = nl + 1
    return segment[start:end]


def raw_http(cfg: dict[str, Any], path: str, headers: dict[str, str]) -> dict[str, Any]:
    """裸 socket：能塞进 HTTP 库不允许的控制字符（Tomcat 的边界就靠它测）。"""
    # 目标地址由用例文件给 —— 通用层不写死任何主机名/端口
    base_url = cfg.get("base_url")
    if not base_url:
        raise ValueError("HTTP 探针的用例必须给 base_url")
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}"]
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
    with socket.create_connection((host, port), timeout=10) as s:
        s.settimeout(3)
        s.sendall(raw)
        chunks: list[bytes] = []
        try:
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                if len(b"".join(chunks)) > 65536:
                    break
        except TimeoutError:
            pass   # keep-alive 连接不会自己关，拿到响应头即可
    data = b"".join(chunks)
    return {"raw_bytes": len(data), "raw_head": data[:400].decode("utf-8", "replace")}


def http_request(cfg: dict[str, Any], path: str, headers: dict[str, str] | None = None,
                 method: str = "GET", payload: Any = None,
                 base_url: str | None = None) -> dict[str, Any]:
    url = (base_url or cfg.get("base_url", "")).rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return {"status": resp.status, "headers": dict(resp.headers),
                    "body": body.decode(errors="replace"), "body_len": len(body)}
    except urllib.error.HTTPError as e:
        body = e.read()
        return {"status": e.code, "headers": dict(e.headers),
                "body": body.decode(errors="replace"), "body_len": len(body)}
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "headers": {}, "body": "", "body_len": 0,
                "error": f"{type(e).__name__}: {e}"}


def json_path(body: str, path: str) -> Any:
    """从 JSON 响应体里取一个字段（点分路径）。取不到就是 None。"""
    try:
        cur = json.loads(body)
    except Exception:
        return None
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def db_query(db: dict[str, Any], sql: str) -> str:
    rc = subprocess.run(
        ["docker", "exec", db["container"], "psql", "-U", db["user"], "-d", db["database"],
         "-tAc", sql], capture_output=True, text=True)
    return rc.stdout.strip()


# --------------------------------------------------------------------- 各类探针

def probe_raw_header(cfg: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    since = log_window()
    r = raw_http(cfg, probe.get("path", "/"), probe.get("headers", {}))
    segment = container_logs(cfg.get("container", ""), since)
    out: dict[str, Any] = {}
    if probe.get("status_line_as"):
        head = r.get("raw_head", "")
        out[probe["status_line_as"]] = head.splitlines()[0] if head else ""
    if probe.get("log_flag_as"):
        out[probe["log_flag_as"]] = probe.get("log_contains", "") in segment
    if probe.get("note"):
        out["note"] = probe["note"]
    return out


def probe_http(cfg: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    method = {"http_put": "PUT", "http_post": "POST"}.get(probe.get("kind", ""), "GET")
    headers = dict(probe.get("headers", {}))
    if method in ("PUT", "POST"):
        headers.setdefault("Content-Type", "application/json")
    need_log = bool(probe.get("log_contains"))
    since = log_window() if need_log else ""
    r = http_request(cfg, probe.get("path", "/"), headers, method, probe.get("payload"),
                     probe.get("base_url"))
    out: dict[str, Any] = {}
    if probe.get("status_as"):
        out[probe["status_as"]] = r["status"]
    if probe.get("body_as"):
        out[probe["body_as"]] = r["body"][: int(probe.get("body_limit", 400))]
    for name, path in (probe.get("json_path_as") or {}).items():
        out[name] = json_path(r.get("body", ""), path)
    if probe.get("body_len_as"):
        out[probe["body_len_as"]] = r["body_len"]
    for name, header in (probe.get("header_as") or {}).items():
        out[name] = r["headers"].get(header)
    for name, spec in (probe.get("flag_as") or {}).items():
        if "header" in spec:
            out[name] = r["headers"].get(spec["header"]) == spec.get("equals")
        elif "status" in spec:
            out[name] = r["status"] == spec["status"]
        else:
            out[name] = None
    if need_log:
        segment = container_logs(cfg.get("container", ""), since)
        if probe.get("log_flag_as"):
            out[probe["log_flag_as"]] = probe["log_contains"] in segment
        if probe.get("log_segment_as"):
            out[probe["log_segment_as"]] = around(
                segment, probe["log_contains"],
                after_lines=int(probe.get("log_lines_after", 0)))
    return out


def probe_db_compare(cfg: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    db = cfg.get("db", {})
    compares = probe.get("compare_as") or {}
    before = {name: db_query(db, probe[spec["before_sql"]]) for name, spec in compares.items()}
    r = http_request(cfg, probe.get("path", "/"), {"Content-Type": "application/json"},
                     "PUT", probe.get("payload"))
    out: dict[str, Any] = {}
    if probe.get("status_as"):
        out[probe["status_as"]] = r["status"]
    if probe.get("body_as"):
        out[probe["body_as"]] = r["body"][: int(probe.get("body_limit", 400))]
    for name, spec in compares.items():
        after = db_query(db, probe[spec["after_sql"]])
        out[spec["before"]] = before[name]
        out[spec["after"]] = after
        out[name] = before[name] == after
    return out


def probe_cors(cfg: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, origin in (probe.get("origins") or {}).items():
        r = http_request(cfg, probe.get("path", "/"), {"Origin": origin})
        out[name] = r["headers"].get("Access-Control-Allow-Origin")
    return out


PROBES = {
    "raw_header": probe_raw_header,
    "http_get": probe_http,
    "http_put": probe_http,
    "http_post": probe_http,
    "db_compare": probe_db_compare,
    "cors": probe_cors,
}


def cleanup_baselines(cfg: dict[str, Any], cleanups: list[dict[str, Any]]) -> dict[str, str]:
    """清理前先记下每张表的主键水位：清理只删「自己造的那一段」。"""
    db = cfg.get("db", {})
    out: dict[str, str] = {}
    for c in cleanups:
        table = c.get("table", "")
        if not table:
            continue
        idc = c.get("id_column", "id")
        out[table] = db_query(db, f"SELECT coalesce(max({idc}),0) FROM {table}")
    return out


def run_cleanup(cfg: dict[str, Any], cleanups: list[dict[str, Any]],
                baselines: dict[str, str]) -> list[dict[str, Any]]:
    """删掉探针造的行。SQL 在用例配置里（项目知识），这里只负责执行与计数。"""
    db = cfg.get("db", {})
    results: list[dict[str, Any]] = []
    for c in cleanups:
        table = c.get("table", "")
        idc = c.get("id_column", "id")
        sql = (c.get("sql", "")
               .replace("{table}", table)
               .replace("{id_column}", idc)
               .replace("{max_id_before}", baselines.get(table, "0")))
        if not sql:
            continue
        try:
            out = db_query(db, sql)
            m = re.search(r"DELETE\s+(\d+)", out)
            n = int(m.group(1)) if m else 0
        except Exception as exc:  # noqa: BLE001
            n = -1
            results.append({"table": table, "sql": sql, "deleted": n,
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        results.append({"table": table, "sql": sql, "deleted": n})
    return results


def run(cfg: dict[str, Any]) -> dict[str, dict]:
    """跑完配置里的全部探针，返回 {探针 id: 原始观测}。

    探针造的行由配置里的 cleanup 段自己删 —— 探针必须自己收干净，不靠人记得。
    """
    out: dict[str, dict] = {}
    cleanups = cfg.get("cleanup") or []
    baselines = cleanup_baselines(cfg, cleanups)
    try:
        for probe in cfg.get("probes", []):
            fn = PROBES.get(probe.get("kind", ""))
            if fn is None:
                out[probe.get("id", "?")] = {"probe_unsupported": probe.get("kind", "")}
                continue
            out[probe.get("id", "?")] = fn(cfg, probe)
    finally:
        if cleanups:
            out["_cleanup"] = {"results": run_cleanup(cfg, cleanups, baselines),
                               "baselines": baselines}
    return out
