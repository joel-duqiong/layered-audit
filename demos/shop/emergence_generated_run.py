"""物化链执行器（demo-shop，项目侧）—— 让**生成的候选**自己驱动执行。

与 emergence_candidate_run.py 的区别，正是要补的那个洞：
    candidate_run：遍历**人写的**注入目录，候选只拿来事后对一下槽位种类
    generated_run：遍历**候选**，值由物化器从被测系统自身枚举，人写的链不参与

取值来源（都不含「哪个值触发缺陷」的信息）：
    · 库里 shop_customer.level 的 DISTINCT 值
    · target.json / 注入目录里声明的装配实例清单（只取 instances，不取 chains）
    · value_sources.json 声明的请求取值域

模板 scenario_template.json 只说明「怎么下单、基线是什么」，没有 VIP / vip / EUR。

用法：
    python3 demos/shop/emergence_generated_run.py
"""
from __future__ import annotations

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
for p in (str(PILOT), str(PILOT / "engine"), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import emergence  # noqa: E402
import emergence_candidates as ec  # noqa: E402
import emergence_materialize as em  # noqa: E402
import emergence_probe as probe  # noqa: E402

NET_FILE = HERE / "artifacts" / "relation_net.json"
INVARIANTS_FILE = HERE / "invariants.json"
CATALOG_FILE = HERE / "emergence_injections.json"
SOURCES_FILE = HERE / "value_sources.json"
TEMPLATE_FILE = HERE / "scenario_template.json"
OUT = HERE / "artifacts" / "emergence_generated_run.json"
BUDGET = 12


def _reader(db: probe.Db):
    """项目侧取值读取器：库里该列的既有取值。引擎不碰 SQL，SQL 只在这里。"""
    def read(table: str, column: str) -> list[str]:
        assert probe.IDENT.match(table) and probe.IDENT.match(column), "非法表名/列名"
        rows = db.rows(f"SELECT DISTINCT {column} AS v FROM {table} ORDER BY 1")
        return [str(r["v"]) for r in rows if r.get("v") is not None]
    return read


def main() -> int:
    catalog = emergence.load_catalog(CATALOG_FILE)   # 只用 instances / database
    inv_cfg = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    template = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))

    net = ec.load_json(NET_FILE)
    invariants = inv_cfg.get("invariants") or []
    anchors = net.get("invariant_anchors") or ec.derive_anchors(net.get("nodes") or {}, invariants)
    metrics = ec.hub_metrics(net)
    candidates = ec.generate(net, anchors, metrics)
    for i, cand in enumerate(candidates):
        cand["id"] = f"C{i:04d}"
    mc = ec.monte_carlo(net, metrics, ec.SEED)
    scored = ec.score_candidates(candidates, net.get("nodes") or {}, metrics, mc, anchors)
    picked = ec.select(scored, BUDGET, ec.SEED)
    selected_ids = {c["id"] for c in picked["selected"]}
    selected = [c for c in scored if c["id"] in selected_ids]

    inv_by_id = {i["id"]: i for i in invariants}
    db = probe.Db(catalog.database)
    domains = em.build_domains(sources, template, catalog.instances, _reader(db))
    print("取值域（来自被测系统自身，不含答案）：")
    for d in domains:
        print(f"  {d.kind:<9}{d.slot or d.target:<16}取值 {list(d.values)}｜基线 {d.baseline}")

    runs: list[dict[str, Any]] = []
    try:
        for cand in selected:
            # 判据的 when 就是适用域：被 eq 约束的字段钉住，不许枚举出去
            pins = em.pins_from_when((inv_by_id.get(cand["invariant"]) or {}).get("when"))
            mat = em.materialize(cand, template, domains, pins=pins)
            if not mat.chain.injections:
                print(f"[{cand['id']}] 物化后没有注入（条件位全是基线值）—— 跳过")
                continue
            out = probe.run_chain(catalog, db, inv_cfg, mat.chain)
            runs.append({
                "candidate": cand["id"],
                "chain": mat.chain.id,
                "endpoint": cand["endpoint"],
                "invariant": cand["invariant"],
                "conditions": cand["conditions"],
                "injections": [{"id": i.id, "mutation": dict(i.mutation)}
                               for i in mat.chain.injections],
                "skipped_baseline_values": list(mat.skipped),
                "unmatched_conditions": list(mat.unmatched),
                "pinned_to_scope": list(mat.pinned),
                "unmatched_pins": list(mat.unmatched_pins),
                "verdict": out["verdict"],
                "core": out["core"],
                "alive": out["alive"],
                "owner_layer": out["owner_layer"],
                "rationale": out["rationale"],
                "warnings": out["warnings"],
                "observations": out["observations"],
            })
            print(f"[{cand['id']}] 注入 {len(mat.chain.injections)} 条 "
                  f"({'/'.join(i.id for i in mat.chain.injections)})")
            print(f"    裁决 = {out['verdict']}｜核心集 = {'+'.join(out['core']) or '—'}")
            for pin in mat.pinned:
                print(f"    钉在适用域：{pin['field']} = {pin['value']}（判据 when 声明）")
            for up in mat.unmatched_pins:
                print(f"    告警：适用域约束 {up} 对不上任何取值域 —— 约束没生效")
            for w in out["warnings"]:
                print(f"    告警：{w}")
    finally:
        left = probe.cleanup(db)
        print(f"残留校验：探针数据行 {left['rows']} 条（必须为 0）")

    emergent = [r for r in runs if str(r["verdict"]).startswith("EMERGENT")]
    fp = hashlib.sha256(json.dumps(
        [(r["candidate"], r["verdict"], r["core"]) for r in runs],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_from": "demos/shop/emergence_generated_run.py",
        "answer_key_used": False,
        "note": "值来自库内既有取值 / 声明的实例清单 / 声明的请求取值域；人写的注入目录只提供连接信息",
        "seed": ec.SEED,
        "counts": {"candidates": len(scored), "selected": len(selected),
                   "materialized": len(runs), "emergent": len(emergent)},
        "domains": [{"kind": d.kind, "slot": d.slot, "target": d.target,
                     "values": list(d.values), "baseline": d.baseline} for d in domains],
        "runs": runs,
        "residue_rows": left["rows"],
        "decision_fingerprint": fp,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  候选 {len(scored)} 条｜物化真跑 {len(runs)} 条｜涌现裁决 {len(emergent)} 条")
    print(f"  裁决指纹 {fp}｜产物 -> {OUT}")
    return 0 if left["rows"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
