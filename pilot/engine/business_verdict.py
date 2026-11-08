"""业务不变式的判定层（通用引擎，零项目字面量）。

项目侧负责两件事：**造出会违反不变式的业务数据**、**把产生的行读出来**。
本模块负责其余全部 —— 调用、读行、对每一行判每一条不变式、把违反开成 finding、
把恒成立的写成「正面结论」，并区分两种「没违反」：

  · 违规场景在入口就被拒（HTTP 4xx）⇒ 这条不变式是**结构性保证**的，
    不是碰巧没触发 —— 报告里要把这个区别写出来
  · 违规场景建单成功但判定没违反      ⇒ 真的没守住

用法：
    from engine.business_verdict import run
    result = run(
        invariants=invs,
        scenarios=scenarios,              # [{tag, targets, body}, ...]
        post_scenario=lambda sc: ...,     # -> (http_status, payload)
        read_rows=lambda ids: [...],      # -> list[dict]
        endpoint="POST /api/orders",
        repro="python3 <项目探针脚本>.py",
    )
"""
from __future__ import annotations

import json
from typing import Any, Callable

from engine.invariants import evaluate_all


def run(invariants: list[dict[str, Any]],
        scenarios: list[dict[str, Any]],
        post_scenario: Callable[[dict[str, Any]], tuple[int, Any]],
        read_rows: Callable[[list[int]], list[dict[str, Any]]],
        endpoint: str,
        repro: str) -> dict[str, Any]:
    """跑一遍业务不变式判定。

    post_scenario 抛异常时视为「建单失败」，按入口拒绝处理（不静默吞掉：
    拒绝的场景会进 rejected_scenarios 报告）。
    """
    order_ids: list[int] = []
    scenario_of: dict[int, str] = {}
    rejected: dict[str, int] = {}
    payload_of: dict[int, Any] = {}

    for sc in scenarios:
        try:
            status, payload = post_scenario(sc)
        except Exception as exc:                      # 建单失败 = 入口拒绝
            rejected[sc["tag"]] = -1
            payload_of[sc["tag"]] = {"error": str(exc)}
            continue
        if status != 200:
            # 建单被拒 —— 这本身就是证据：该不变式被系统在入口挡住了
            rejected[sc["tag"]] = status
            continue
        row_id = _order_id(payload)
        if row_id is None:
            continue
        order_ids.append(row_id)
        scenario_of[row_id] = sc["tag"]

    rows = read_rows(order_ids)
    for row in rows:
        row["_scenario"] = scenario_of.get(row.get("id"), "?")

    results = evaluate_all(rows, invariants)
    by_id = {r["id"]: r for r in results}
    findings: list[dict[str, Any]] = []
    for inv in invariants:
        res = by_id.get(inv["id"])
        if res is None or res["ok"]:
            continue
        samples = []
        for f in res["failure_samples"]:
            row = next((r for r in rows if r.get("id") == f.get("row_id")), {})
            samples.append({**f, "scenario": row.get("_scenario")})
        findings.append({
            "id": f"BI-{inv['id'][3:]}",
            "severity": inv["severity"],
            "endpoint": endpoint,
            "title": inv["claim"],
            "detail": (f"{inv['why']}\n"
                       f"不变式：{inv['id']}　判定原语：{inv['assert']['type']}\n"
                       f"实测 {res['checked']} 个业务场景，违反 {res['violations']} 次：\n"
                       + json.dumps(samples, ensure_ascii=False, indent=2)),
            "repro": repro,
            "evidence": {
                "invariant": inv,
                "checked": res["checked"],
                "violations": res["violations"],
                "samples": samples,
                "scenarios": [{"scenario": r.get("_scenario"), "order_id": r.get("id")}
                              for r in rows],
            },
        })

    inv_by_id = {i["id"]: i for i in invariants}
    tag_to_target = {t: sc["tag"] for sc in scenarios for t in sc["targets"]}
    positives: list[dict[str, Any]] = []
    for r in results:
        if not r["ok"]:
            continue
        inv = inv_by_id[r["id"]]
        # 如果这条不变式的违规场景在入口就被拒了，那它是结构性保证
        guard = tag_to_target.get(r["id"])
        guard_note = ""
        guard_evidence: dict[str, Any] = {}
        if guard and guard in rejected:
            guard_note = (f"\n特别地：专门构造的违规场景「{guard}」在入口就被拒"
                          f"（HTTP {rejected[guard]}）—— 这条不变式是被结构性保证的，"
                          f"不是碰巧没触发。")
            guard_evidence = {"guard_scenario": guard, "rejected_http": rejected[guard]}
        positives.append({
            "id": f"BI-{r['id'][3:]}",
            "severity": "INFO",
            "endpoint": endpoint,
            "title": r["claim"],
            "detail": (f"{r['why']}\n"
                       f"不变式：{r['id']}　判定原语：{inv['assert']['type']}\n"
                       f"在 {r['checked']} 个业务场景（含刻意违规的对照组）上恒成立 —— "
                       f"这条不变式是被系统守住的。{guard_note}"),
            "repro": repro,
            "evidence": {"invariant": inv, "checked": r["checked"], **guard_evidence},
        })

    return {
        "service": findings + positives,
        "results": results,
        "rejected_scenarios": rejected,
        "scenarios": [{"tag": s["tag"], "targets": s["targets"]} for s in scenarios],
    }


def _order_id(payload: Any) -> int | None:
    """从响应里取业务单据 id（data.id / id）。取不到就返回 None。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("id", "orderId"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None
