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
    graph = json.loads((PROJECT_DIR / "units.json").read_text(encoding="utf-8"))
    edges = json.loads((PROJECT_DIR / "edges.json").read_text(encoding="utf-8"))["edges"]
    rcs = json.loads((PROJECT_DIR / "root_causes.json").read_text(encoding="utf-8"))

    # 中心性近似：入边条数 + 调用次数
    centrality: dict[str, int] = defaultdict(int)
    for e in edges:
        centrality[e["to_agent_id"]] += int(e.get("call_count") or 1)

    by_unit: dict[str, dict[str, Any]] = {}
    for u in graph["units"]:
        by_unit[u["unit_key"]] = {
            "unit_key": u["unit_key"],
            "layer": u["layer"],
            "symbol": u["symbol"],
            "centrality": u["centrality"],
            "logic": u["logic"],
            "priority": u["priority"],
            "status": u["status"],
            "observations": u["observations"],
            "agents": u["role_count"],
            # 义务三值：该单元有 role_count 条义务；被判定为成立的是 satisfied，
            # 被判定为违反的是 violated，其余 unknown（没被任何判定手段验过）。
            # 没被验过 ≠ 满足。
            # satisfied 来自**通过的用例**（用例文件声明了每个分组验的是哪类义务），
            # violated 来自失败的用例与违规结论。
            "obligations": {
                "satisfied": len(u.get("satisfied_categories") or []),
                "violated": max(len(u.get("violated_categories") or []),
                                len(u["root_cause_keys"])),
                "unknown": max(0, u["role_count"]
                               - len(u.get("satisfied_categories") or [])
                               - max(len(u.get("violated_categories") or []),
                                     len(u["root_cause_keys"]))),
            },
            "cases": u["observations"],
            "root_causes": u["root_cause_keys"],
        }

    units = list(by_unit.values())
    for u in units:
        u["root_causes"] = sorted(u["root_causes"])


    # 没有这一步，排序会被 Lombok 生成的 getter 刷屏 —— 中心性高但零分支零副作用，
    # 测它没有意义。优先级 = 中心性 × 逻辑密度。
    density: dict[str, dict[str, Any]] = {}
    fn = json.loads((PROJECT_DIR / "fn_declaration.json").read_text(encoding="utf-8"))
    for rec in fn.get("functions") or []:
        key = f"{rec.get('class')}#{rec.get('method')}"
        branches = int(rec.get("branch_count") or 0)
        effects = len(rec.get("side_effects") or [])
        guards = int(rec.get("null_guard_count") or 0)
        impure = 0 if rec.get("is_pure") else 5
        density[key] = {
            "branches": branches,
            "side_effects": effects,
            "null_guards": guards,
            "is_pure": bool(rec.get("is_pure")),
            "score": branches + effects * 3 + guards + impure,
        }
    # 文件/类单元的逻辑密度 = 其成员函数密度之和（实体与 DTO 自己没分支，
    # 但它们承载的方法有）。否则文件层会因「中心性高但自己零分支」被误判成低优先。
    member_density: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"branches": 0, "side_effects": 0, "null_guards": 0,
                 "is_pure": True, "score": 0})
    for key, d in density.items():
        cls = key.split("#")[0]
        agg = member_density[cls]
        for f in ("branches", "side_effects", "null_guards", "score"):
            agg[f] += d[f]
        agg["is_pure"] = agg["is_pure"] and d["is_pure"]

    for u in units:
        d = density.get(u["unit_key"]) or member_density.get(u["unit_key"])
        u["logic"] = d or {"branches": 0, "side_effects": 0, "null_guards": 0,
                           "is_pure": True, "score": 0}
        u["priority"] = u["centrality"] * (1 + u["logic"]["score"])

    # 最该先验的：中心性高 + 状态 UNKNOWN + 义务多
    candidates = sorted(
        [u for u in units if u["status"] == "UNKNOWN"],
        key=lambda u: (-u["priority"], -u["centrality"]))
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
    print("  最该先验的单元（优先级 = 中心性 × 逻辑密度）：")
    for u in payload["top_blind_spots"][:10]:
        lg = u["logic"]
        print(f"    优先 {u['priority']:>6}  中心性 {u['centrality']:>4}  "
              f"分支 {lg['branches']:>2} 副作用 {lg['side_effects']:>2}  "
              f"{u['layer']:<11}{u['symbol'][:40]}")


if __name__ == "__main__":
    main()
