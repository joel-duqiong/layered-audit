"""Agent 群的盲区地图 —— 「哪里没人验过、哪里值得投测试」。

这个项目真正的稀缺资源不是模型额度，是**被动态验证过的单元数**。
当前 92 个动态用例只覆盖 16/461 个函数 —— 绝大多数单元的 agent 状态是 UNKNOWN。
本脚本把这件事变成可排序、可决策的地图：

  · 按中心性（被调用次数）排序的「最该先验的单元」
  · 每个根因由多少个 agent 承载（跨层复现情况）
  · 义务的三值分布（satisfied / violated / unknown）

中心性用「入边数 + 调用次数」近似 —— 这是关系网上最便宜的强度信号。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import findings_schema

PROJECT_DIR = findings_schema.target_path().parent


def build() -> dict[str, Any]:
    agents = json.loads((PROJECT_DIR / "agents.json").read_text(encoding="utf-8"))
    edges = json.loads((PROJECT_DIR / "edges.json").read_text(encoding="utf-8"))["edges"]
    rcs = json.loads((PROJECT_DIR / "root_causes.json").read_text(encoding="utf-8"))

    # 中心性近似：入边条数 + 调用次数
    centrality: dict[str, int] = defaultdict(int)
    for e in edges:
        centrality[e["to_agent_id"]] += int(e.get("call_count") or 1)

    by_unit: dict[str, dict[str, Any]] = {}
    for a in agents["agents"]:
        key = a["unit"]["unit_key"]
        rec = by_unit.setdefault(key, {
            "unit_key": key,
            "layer": a["layer"],
            "symbol": a["unit"].get("symbol", ""),
            "centrality": 0,
            "agents": 0,
            "status": "UNKNOWN",
            "obligations": {"satisfied": 0, "violated": 0, "unknown": 0},
            "cases": 0,
            "root_causes": set(),
        })
        rec["agents"] += 1
        rec["centrality"] = max(rec["centrality"], centrality.get(a["agent_id"], 0))
        rec["cases"] += len(a["dynamic_life"].get("cases") or [])
        for ob in a["mission"]["obligations"]:
            rec["obligations"][ob["status"]] = rec["obligations"].get(ob["status"], 0) + 1
            if ob["status"] == "violated":
                rec["status"] = "FAIL"
        if rec["status"] != "FAIL":
            if all(ob["status"] != "unknown" for ob in a["mission"]["obligations"]):
                rec["status"] = "PASS"
            elif a["dynamic_life"].get("exercised"):
                # 有动态观测，但还有义务没被判定 —— 不等于「没问题」，也不等于「没查」
                rec["status"] = "OBSERVED"
            else:
                rec["status"] = "UNKNOWN"
        for k in a["conclusions"].get("root_cause_keys") or []:
            if k:
                rec["root_causes"].add(k)

    units = list(by_unit.values())
    for u in units:
        u["root_causes"] = sorted(u["root_causes"])

    # 最该先验的：中心性高 + 状态 UNKNOWN + 义务多
    candidates = sorted(
        [u for u in units if u["status"] == "UNKNOWN"],
        key=lambda u: (-u["centrality"], -sum(u["obligations"].values())))
    verified = [u for u in units if u["status"] in ("PASS", "FAIL")]
    observed = [u for u in units if u["status"] == "OBSERVED"]

    rc_carriers: dict[str, list[str]] = defaultdict(list)
    for u in units:
        for k in u["root_causes"]:
            rc_carriers[k].append(u["unit_key"])

    totals = {"satisfied": 0, "violated": 0, "unknown": 0}
    for u in units:
        for k, v in u["obligations"].items():
            totals[k] = totals.get(k, 0) + v

    return {
        "counts": {
            "units": len(units),
            "verified_units": len(verified),
            "observed_only_units": len(observed),
            "unknown_units": len(candidates),
            "verified_ratio": round(len(verified) / len(units), 4) if units else 0,
            "obligations": totals,
            "root_causes_with_multiple_carriers": sum(1 for v in rc_carriers.values() if len(v) > 1),
        },
        "top_blind_spots": [
            {k: v for k, v in u.items() if k != "root_causes"} | {"root_causes": u["root_causes"]}
            for u in candidates[:25]
        ],
        "root_cause_carriers": {k: sorted(v) for k, v in sorted(rc_carriers.items())},
    }


def main() -> None:
    payload = build()
    (PROJECT_DIR / "agent_coverage.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    c = payload["counts"]
    print(f"Agent 盲区地图 -> {PROJECT_DIR / 'agent_coverage.json'}")
    print(f"  单元 {c['units']}：已判定 {c['verified_units']}（{c['verified_ratio']:.2%}）"
          f" / 有观测但义务未定 {c['observed_only_units']} / 完全没查 {c['unknown_units']}")
    print(f"  义务三值：满足 {c['obligations'].get('satisfied', 0)} / "
          f"违反 {c['obligations'].get('violated', 0)} / "
          f"未知 {c['obligations'].get('unknown', 0)}")
    print("  最该先验的单元（中心性高但没验过）：")
    for u in payload["top_blind_spots"][:8]:
        print(f"    中心性 {u['centrality']:>4}  {u['layer']:<11}{u['symbol'][:44]:<46}"
              f"义务 {sum(u['obligations'].values())}")


if __name__ == "__main__":
    main()
