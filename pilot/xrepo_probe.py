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
        triggered.append({"method": "POST", "path": "/api/orders", "status": r.status_code})
    except requests.RequestException as exc:
        triggered.append({"error": str(exc)[:120]})
    return triggered


def main() -> None:
    surfaces = load_surfaces()
    if not surfaces:
        print("没有面清单（surfaces/ 目录），跨仓库检查跳过")
        return
    graph = build_graph(surfaces)

    # 观测运行时调用：触发一次，再看对端计数
    before = observe_calls(_base(surfaces, "python-svc", "svc"))
    triggered = trigger_cross_repo(surfaces)
    time.sleep(0.5)
    after = observe_calls(_base(surfaces, "python-svc", "svc"))

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
        "observed_calls": {"before": before, "after": after},
        "findings": findings,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

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


if __name__ == "__main__":
    main()
