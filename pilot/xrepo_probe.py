"""跨仓库探针（项目侧）。

做三件事：
  1. 读两个仓库的面清单，构建跨仓库图
  2. **真的触发一次跨仓库调用**，观测对端计数 -> 把边标成 CONFIRMED
  3. 找出契约漂移（只有一方声明）-> 那是缺陷，不生成链

跨仓库链的 owner：最早违反契约的那一跳（engine/surface.owner_of_chain）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

import project

sys.path.insert(0, project.engine_path())

from surface import build_graph, load_surfaces, mark_confirmed, owner_of_chain  # noqa: E402

# 产物跟着目标走：换绑定时落到目标自己的 artifacts 目录，不会覆盖别人的产物
OUT = project.artifact_dir() / "xrepo_report.json"


def _base(surfaces: dict[str, Any], repo_id: str, key: str) -> str:
    return (surfaces.get(repo_id, {}).get("base_urls") or {}).get(key, "")


def observe_calls(base: str) -> dict[str, int]:
    if not base:
        return {}
    try:
        r = requests.get(base + "/stats", timeout=10)
        return r.json().get("calls", {}) if r.status_code == 200 else {}
    except requests.RequestException:
        return {}


# 订单的附属记录，外键指向 shop_order —— 必须先删它们，否则订单删不掉
ORDER_RECORD_TABLES = ("shop_quota_occupancy", "shop_ledger_entry")


def cleanup_statements(order_id: Any) -> tuple[str, ...]:
    """按外键顺序生成清理语句。id 来自 HTTP 响应（外部数据），只接受纯数字。"""
    text = str(order_id)
    if not text.isdigit():
        raise ValueError(f"订单 id 不是纯数字，拒绝拼进 SQL: {text!r}")
    oid = int(text)
    return tuple(
        [f"DELETE FROM {t} WHERE order_id = {oid}" for t in ORDER_RECORD_TABLES]
        + [f"DELETE FROM shop_order WHERE id = {oid}"])


def cleanup_order(order_id: Any) -> list[dict[str, Any]]:
    """删掉本探针造的那一笔订单及其附属记录。

    探针用的是**种子客户**（没有 ai_probe_ 前缀），残留守卫按前缀查看不见它 ——
    所以这里必须自己收干净，不能指望守卫兜底。
    """
    import subprocess

    spec = (project.paths().get("db") or {})
    if not all(spec.get(k) for k in ("container", "user", "database")):
        return [{"sql": "", "ok": False,
                 "error": "target.json 的 paths.db 没配全，无法清理"}]
    log: list[dict[str, Any]] = []
    for sql in cleanup_statements(order_id):
        rc = subprocess.run(
            ["docker", "exec", spec["container"], "psql", "-U", spec["user"],
             "-d", spec["database"], "-tAc", sql], capture_output=True, text=True)
        log.append({"sql": sql, "ok": rc.returncode == 0,
                    "error": rc.stderr.strip()[-200:] if rc.returncode else ""})
    return log


def _order_entry(resp: Any) -> dict[str, Any]:
    """从下单响应里定位订单 id。定位不到就是 None —— 但 200 意味着订单已提交。

    响应形状漂移（非 JSON / 信封 {data:{id}} / list / 标量）都会走到 None。
    以前只捕 ValueError，list/标量的 `.get` 抛 AttributeError 会崩在清理之前。
    """
    entry: dict[str, Any] = {"status": resp.status_code}
    try:
        body = resp.json()
        entry["order_id"] = body.get("id") if isinstance(body, dict) else None
    except (ValueError, AttributeError):
        entry["order_id"] = None
    return entry


def residue_risks(triggered: list[dict[str, Any]],
                  cleanup_log: list[dict[str, Any]]) -> list[str]:
    """清理没做干净的理由。非空 -> 进程必须非 0 退出。

    为什么不能只打印：探针用的是**种子客户名**，残留守卫按 `ai_probe_` 前缀
    查不到这些行。清理失败若不改退出码，「零残留、机器守」就是假绿。
    """
    risks = [f"清理语句失败：{c.get('sql') or '(未执行)'} —— {c.get('error', '')}"
             for c in cleanup_log if not c.get("ok")]
    for t in triggered:
        if t.get("status") == 200 and t.get("order_id") is None:
            risks.append("下单返回 200（事务已提交）但定位不到订单 id，"
                         "订单可能留在库里，需人工核对")
    return risks


def trigger_cross_repo(surfaces: dict[str, Any]) -> list[dict[str, Any]]:
    """制造一次跨仓库调用（用 demo 的下单流程：Java 侧会调 python-svc 的 /fx）。"""
    triggered: list[dict[str, Any]] = []
    base = _base(surfaces, "demo-shop", "backend")
    if not base:
        return triggered
    s = requests.Session()

    def items_of(payload: Any) -> list:
        """两种响应形状都认：带信封的 {data:{items:[…]}} 和直接返回数组的。"""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("items") or []
        return []

    try:
        body = {
            # 客户名要是库里真实存在的（种子里的那个），否则 create 找不到客户会 500
            "customerName": os.environ.get("XREPO_CUSTOMER", "demo-vip"),
            "customerPhone": "13800000001",
            "banquetName": "ai_probe_xrepo", "banquetType": "粮油",
            "reservationTime": "2027-03-05T18:30:00+08:00", "tableCount": 1,
            "quotedAmountCent": 900000, "currency": "CNY",
        }
        r = s.post(base + "/api/orders", json=body, timeout=15)
        entry: dict[str, Any] = {"method": "POST", "path": "/api/orders"}
        entry.update(_order_entry(r))
        triggered.append(entry)
    except requests.RequestException as exc:
        triggered.append({"error": str(exc)[:120]})
    return triggered


def main() -> int:
    surfaces = load_surfaces()
    if not surfaces:
        print("没有面清单（surfaces/ 目录），跨仓库检查跳过")
        return 0
    graph = build_graph(surfaces)

    # 观测运行时调用：触发一次，再看对端计数
    before = observe_calls(_base(surfaces, "python-svc", "svc"))
    triggered = trigger_cross_repo(surfaces)
    time.sleep(0.5)
    after = observe_calls(_base(surfaces, "python-svc", "svc"))

    # 观测完立刻收干净：探针造的行不许留在库里（种子客户名，守卫按前缀查不到）
    cleanup_log: list[dict[str, Any]] = []
    for t in triggered:
        if t.get("order_id") is not None:
            cleanup_log += cleanup_order(t["order_id"])

    observed: set[tuple[str, str, str]] = set()
    for currency, n in after.items():
        if n > before.get(currency, 0):
            observed.add(("demo-shop", "GET", "/fx"))
    mark_confirmed(graph, observed)

    # 契约漂移 = 缺陷
    findings: list[dict[str, Any]] = []
    for drift in graph["drifts"]:
        findings.append({
            "id": "XREPO-CONTRACT-DRIFT",
            "severity": "HIGH",
            "endpoint": "",
            "title": f"跨仓库契约漂移：{drift['from']} 调用了 {drift['method']} {drift['path']}，"
                     f"但没有任何仓库声明提供这个面",
            "detail": json.dumps(drift, ensure_ascii=False),
            "repro": "python3 xrepo_probe.py",
            "evidence": {"drift": drift, "surfaces": sorted(surfaces)},
        })

    report = {
        "repos": sorted(surfaces),
        "edges": graph["edges"],
        "drifts": graph["drifts"],
        "triggered": triggered,
        "cleanup": cleanup_log,
        "observed_calls": {"before": before, "after": after},
        "findings": findings,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [c for c in cleanup_log if not c["ok"]]
    print(f"  探针订单清理：{len(cleanup_log) - len(failed)}/{len(cleanup_log)} 条语句成功")
    risks = residue_risks(triggered, cleanup_log)
    for r in risks:
        print(f"    残留风险：{r}")
    print(f"跨仓库图 -> {OUT}")
    print(f"  仓库 {len(surfaces)} 个：{', '.join(sorted(surfaces))}")
    for edge in graph["edges"]:
        print(f"  边 {edge['from']} -> {edge['to']}  {edge['method']} {edge['path']}  "
              f"[{edge['resolution']}]")
    for drift in graph["drifts"]:
        print(f"  漂移 {drift['from']} -> {drift['method']} {drift['path']}（无人提供）")
    if graph["edges"]:
        owner, reason = owner_of_chain(graph["edges"])
        print(f"  跨仓库归属：owner={owner}（{reason}）")
    print(f"  契约漂移缺陷 {len(findings)} 条")
    if risks:
        print("  探针未能自收干净 —— 判红（残留守卫按前缀看不见种子客户的数据）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
