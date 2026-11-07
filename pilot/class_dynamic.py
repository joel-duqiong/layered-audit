"""类层动态实测 —— 薄驱动。

**通用骨架**在 class_probe.py（声明式探针引擎）；**项目特定的声明**在项目侧的
class_scenarios.json（造什么临时实体、查什么表、走哪个端点、哪些字段带前缀、
什么状态机、什么 SQL 探针）。换项目只换那个文件，这个驱动一行不改。

输出的 class_observation.json 结构与原来完全一致，class_diff.py 不需要改协议。

DB 通道（容器名 / 库名 / 用户）来自 target.json 的 paths.db；HTTP base_url 来自
paths.base_url —— 本文件不含任何项目字面量。
"""
from __future__ import annotations

import json
from pathlib import Path

import project
from class_probe import Api, ClassProbe, Database


def load_config() -> dict:
    """读项目侧的类层探针声明。"""
    rel = project.path("class_scenarios", "class_scenarios.json")
    path = project.project_dir() / rel
    return json.loads(path.read_text(encoding="utf-8"))


def db_channel() -> Database:
    """DB 通道 —— 容器 / 库 / 用户全部来自项目绑定。"""
    spec = project.paths().get("db") or {}
    container = spec.get("container", "")
    user = spec.get("user", "")
    database = spec.get("database", "")
    missing = [k for k, v in (("container", container), ("user", user),
                              ("database", database)) if not v]
    if missing:
        raise RuntimeError(f"target.json 的 paths.db 缺 {missing} —— 无法连库")
    return Database(container=container, user=user, database=database)


def main() -> None:
    cfg = load_config()
    probe = ClassProbe(cfg, db_channel(), Api(project.base_url()))
    res = probe.run()

    out = Path(project.artifact_dir()) / "class_observation.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    if res.get("_errors"):
        print("探针失败（不影响其余探针）：")
        for e in res["_errors"]:
            print(f"  {e['probe']}: {e['error']}")
    failed = [c for c in res.get("_cleanup", []) if not c.get("ok")]
    print(f"清理：{len(res.get('_cleanup', []))} 条，失败 {len(failed)} 条")
    for c in failed:
        print(f"  清理失败 {c['table']}/{c['id']}: {c.get('error')}")

    tr = res.get("transition") or {}
    if tr.get("matrix"):
        print("状态迁移矩阵（实测允许?）:")
        for k, v in tr["matrix"].items():
            print(f"  {k:26} http={v['http']} db={v['db_status']}")
    iu = res.get("index_usage") or {}
    if iu:
        print("索引可用性:")
        for k, v in iu.items():
            print(f"  {k:22} 默认={v['node'][:34]:34} "
                  f"禁用seqscan后={v['forced_seqscan_off_node'][:34]:34} "
                  f"索引可用={v['index_usable']}")
    sdu = res.get("softdelete_unique")
    if sdu:
        print("软删+唯一:", {k: v for k, v in sdu.items() if k != "recreate_body"})
    sc = res.get("scale_experiment") or {}
    if sc:
        print(f"等比放大实验(影子表 {sc.get('rows')} 行):")
        for k, v in sc.items():
            if k == "rows":
                continue
            print(f"  {k:18} {v['exec_ms']:>8} ms  cond={v['index_cond']}  "
                  f"{v['rows_removed_by_filter']}")
    bi = res.get("db_invariants") or {}
    if bi:
        print("DB 不变量:", {k: v for k, v in bi.items() if k != "table_sizes"})
    da = res.get("defense_audit") or {}
    if da.get("constraint_probes"):
        print("DB 防线盘点:")
        for d in da["constraint_probes"]:
            tag = d.get("constraint") or d.get("label")
            print(f"  {'拦住' if d['blocked'] else '没拦住'}  {tag}")
    cb = res.get("contract_boundary") or []
    if cb:
        print("契约边界实测:")
        for d in cb:
            print(f"  http={d['http']:4} {d['label']}")
    nl = res.get("nullability") or []
    if nl:
        print("可空性穿透:")
        for d in nl:
            print(f"  http={d['http']:4} {d['label']}  -> {d['db_row']}")
    wf = res.get("write_footprint") or []
    if wf:
        print("列级写入足迹:")
        for w in wf:
            for st in w["steps"]:
                print(f"  {w['entity']:9} {st['action']:32} -> {st['changed']}")
    ip = res.get("instance_pair") or {}
    if ip:
        print("装配差异:")
        for d in ip.get("divergence", []):
            print(f"  {d['field']}: {d['values']}")
    cs = res.get("class_shape")
    if cs:
        print("类形状:", json.dumps(cs, ensure_ascii=False)[:2000])
    print(f"实测 -> {out}")


if __name__ == "__main__":
    main()
