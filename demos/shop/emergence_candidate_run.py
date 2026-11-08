"""候选链执行器（demo-shop，项目侧）。

通用候选生成器（pilot/engine/emergence_candidates.py）只吃关系网与不变式，
不碰注入目录；本脚本把两边接上，并**真跑**一遍：

  1. 用生成器在 demo 的关系网上生成候选链（固定种子）
  2. 用召回比对把候选与注入目录里的已知链对齐（条件种类覆盖）
  3. 把候选提出的条件组合落到已知链的真实场景上，用同一个裁决器出结论
  4. 打印「候选抓到的真链」与裁决结论，末尾机器校验零残留

回答的问题是：**自动生成的候选链，能不能自己抓出那个真的涌现缺陷？**
—— 不是「已知链还在不在」（那是人工链的自我证明），而是「生成器独立提出的
入口 + 脊柱 + 写汇 + 条件种类，能不能复现同一个涌现结论」。

硬约束：
- **不改被测系统一行业务代码**：注入只改请求字段 / 数据行 / 装配上下文
- **零残留**：造的数据带 ai_probe_ 前缀，跑完自己删干净，末尾机器校验
- **确定性**：固定种子，同输入两次跑结论完全一致

用法：
    python3 emergence_candidate_run.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
os.environ.setdefault("LAYERED_AUDIT_TARGET", str(HERE / "target.json"))
sys.path.insert(0, str(PILOT))
sys.path.insert(0, str(PILOT / "engine"))
sys.path.insert(0, str(HERE))

import emergence  # noqa: E402
import emergence_candidates as ec  # noqa: E402
import emergence_probe as probe  # noqa: E402

NET_FILE = HERE / "artifacts" / "relation_net.json"
INVARIANTS_FILE = HERE / "invariants.json"
CATALOG_FILE = HERE / "emergence_injections.json"
OUT = HERE / "artifacts" / "emergence_candidate_run.json"
BUDGET = 12


def generate_candidates() -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """跑通用生成器（同一份代码、同一个种子）。"""
    net = ec.load_json(NET_FILE)
    invariants = ec.load_json(INVARIANTS_FILE).get("invariants") or []
    anchors = net.get("invariant_anchors") or ec.derive_anchors(net.get("nodes") or {},
                                                               invariants)
    metrics = ec.hub_metrics(net)
    candidates = ec.generate(net, anchors, metrics)
    for i, cand in enumerate(candidates):
        cand["id"] = f"C{i:04d}"
    mc = ec.monte_carlo(net, metrics, ec.SEED)
    scored = ec.score_candidates(candidates, net.get("nodes") or {}, metrics, mc, anchors)
    picked = ec.select(scored, BUDGET, ec.SEED)
    catalog_raw = ec.load_json(CATALOG_FILE)
    rec = ec.recall(scored, catalog_raw, anchors)
    return scored, rec, picked


def condition_kinds(candidate: dict[str, Any]) -> set[str]:
    kinds: set[str] = set()
    for cond in candidate.get("conditions") or []:
        head = str(cond).split(":", 1)[0]
        if head:
            kinds.add(head)
    return kinds


def slot_map(candidate: dict[str, Any], chain: emergence.Chain) -> list[dict[str, Any]]:
    """把候选提出的条件槽映射到真链的注入上（按 mutation.kind 对齐）。"""
    kinds = condition_kinds(candidate)
    out: list[dict[str, Any]] = []
    for inj in chain.injections:
        kind = str((inj.mutation or {}).get("kind", ""))
        out.append({
            "injection": inj.id,
            "mutation_kind": kind,
            "active": bool(inj.active),
            "covered": kind in kinds,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="候选链执行器（demo-shop）")
    ap.add_argument("--out", default="", help="产物路径")
    args = ap.parse_args()

    scored, rec, picked = generate_candidates()
    catalog = emergence.load_catalog(CATALOG_FILE)
    inv_cfg = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    db = probe.Db(catalog.database)

    by_chain = {k["chain"]: k for k in rec["known"]}
    chain_outs: list[dict[str, Any]] = []
    try:
        for chain in catalog.chains:
            known = by_chain.get(chain.id, {})
            cand_id = known.get("candidate")
            cand = next((c for c in scored if c.get("id") == cand_id), None)
            slots = slot_map(cand or {"conditions": []}, chain)
            active_slots = [s for s in slots if s["active"]]
            covered = bool(active_slots) and all(s["covered"] for s in active_slots)
            out = probe.run_chain(catalog, db, inv_cfg, chain)
            chain_outs.append({
                "chain": chain.id,
                "matched_candidate": cand_id,
                "candidate_entry": (cand or {}).get("entry"),
                "candidate_endpoint": (cand or {}).get("endpoint"),
                "candidate_invariant": (cand or {}).get("invariant"),
                "candidate_write_sink": (cand or {}).get("write_sink"),
                "candidate_conditions": (cand or {}).get("conditions"),
                "condition_coverage": known.get("condition_coverage"),
                "condition_slots": slots,
                "active_conditions_covered": covered,
                "verdict": out["verdict"],
                "owner_layer": out["owner_layer"],
                "owner_relation": out["owner_relation"],
                "category": out["category"],
                "core": out["core"],
                "ground_truth": out["ground_truth"],
                "alive": out["alive"],
                "warnings": out["warnings"],
            })
            flag = "✓" if covered else "✗"
            print(f"[{chain.id}] 候选 {cand_id or '（未命中）'} 条件覆盖 {flag}"
                  f"｜裁决 = {out['verdict']}｜owner={out['owner_layer']}"
                  f"({out['owner_relation']})｜核心集={'+'.join(out['core']) or '—'}")
            for s in slots:
                print(f"    条件槽 {s['mutation_kind']:<8} ← 注入 {s['injection']}"
                      f"{'' if s['active'] else '（inactive）'}"
                  f"：{'候选已提出' if s['covered'] else '候选没提出'}")
    finally:
        left = probe.cleanup(db)
        residue = left["rows"]
        print(f"残留校验：探针数据行 {residue} 条（必须为 0）"
              + (f" —— 残留 id: {left['ids']}" if left["ids"] else ""))

    emergent = [c for c in chain_outs if str(c["verdict"]).startswith("EMERGENT")]
    fp = hashlib.sha256(
        json.dumps([(c["chain"], c["matched_candidate"], c["verdict"], c["core"])
                    for c in chain_outs], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_from": "demos/shop/emergence_candidate_run.py",
        "target": "demo-shop",
        "seed": ec.SEED,
        "counts": {
            "candidates": len(scored),
            "selected": len(picked["selected"]),
            "unrun": len(picked["unrun"]),
            "known_chains": rec["total"],
            "matched_known": rec["matched"],
            "emergent_verdicts": len(emergent),
        },
        "recall": rec,
        "chains": chain_outs,
        "residue_rows": residue,
        "decision_fingerprint": fp,
        "candidate_fingerprint": ec.fingerprint(scored),
    }
    out_path = Path(args.out) if args.out else OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"  候选 {len(scored)} 条｜已知链命中 {rec['matched']}/{rec['total']}"
          f"｜涌现裁决 {len(emergent)} 条")
    print(f"  候选集指纹 {payload['candidate_fingerprint']}｜裁决指纹 {fp}")
    print(f"  产物 -> {out_path}")
    return 0 if residue == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
