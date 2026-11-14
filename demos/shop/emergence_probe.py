"""涌现探针（项目侧）。

对 demo-shop 真实跑「条件注入 → 下单 → 查库 → 判不变式」的实验，
把观测交给通用裁决引擎 pilot/engine/emergence.py 出结论。

三条硬约束：
- **不改被测系统一行业务代码**：注入只改请求字段 / 数据行 / 装配上下文（换实例）
- **零残留**：探针造的数据带 ai_probe_ 前缀，跑完自己删干净，末尾机器校验
- **确定性**：同一输入两次跑，裁决结论完全一致（不引随机、不引模型）

用法：
    python3 emergence_probe.py                      # 裁决全部链，产物落 artifacts/
    python3 emergence_probe.py --chain <chain_id>   # 只裁决一条链
    python3 emergence_probe.py --alive-check <inj>  # 只跑某条注入的活性检查
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
os.environ.setdefault("LAYERED_AUDIT_TARGET", str(HERE / "target.json"))
sys.path.insert(0, str(PILOT))
sys.path.insert(0, str(PILOT / "engine"))

import emergence  # noqa: E402
from invariants import deviation, evaluate_all  # noqa: E402

CATALOG_FILE = HERE / "emergence_injections.json"
INVARIANTS_FILE = HERE / "invariants.json"
ARTIFACTS = HERE / "artifacts"
OUT = ARTIFACTS / "emergence_chains.json"
PFX = "ai_probe_"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ------------------------------------------------------------------ 基础设施

class Db:
    """demo_shop 的 psql 通道（配置来自注入目录，不写死）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.container = cfg["container"]
        self.user = cfg["user"]
        self.name = cfg["database"]

    def _run(self, args: list[str]) -> str:
        proc = subprocess.run(
            ["docker", "exec", self.container, "psql", "-U", self.user, "-d", self.name, *args],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"psql 失败: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def exec(self, sql: str) -> str:
        return self._run(["-v", "ON_ERROR_STOP=1", "-tAc", sql]).strip()

    def rows(self, query: str) -> list[dict[str, Any]]:
        out = self._run(["-tA", "-c", f"SELECT to_jsonb(_r) FROM ({query}) _r"])
        return [json.loads(line) for line in out.splitlines() if line.strip()]


# 订单的两条附属记录（额度占用 / 账本分录）。外键指向订单，必须先删它们。
ORDER_RECORD_TABLES = ("shop_quota_occupancy", "shop_ledger_entry")


def _delete_probe_orders(db: Db, order_predicate: str) -> None:
    """按同一个谓词删掉订单及其附属记录 —— 顺序错了外键会挡住。"""
    for table in ORDER_RECORD_TABLES:
        db.exec(f"DELETE FROM {table} WHERE order_id IN "
                f"(SELECT id FROM shop_order WHERE {order_predicate})")
    db.exec(f"DELETE FROM shop_order WHERE {order_predicate}")


def http_post(base_url: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(
        base_url + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


# ------------------------------------------------------------------ 执行器

class DemoExecutor:
    """项目侧的实验执行器：施加条件 → 跑真实 HTTP → 读库 → 判不变式。"""

    def __init__(self, catalog: emergence.Catalog, inv_cfg: dict[str, Any],
                 db: Db) -> None:
        self.catalog = catalog
        self.db = db
        self.invariants = {i["id"]: i for i in inv_cfg["invariants"]}
        self.entities = {e["name"]: e for e in inv_cfg["entities"]}
        self.probe_invariants = {
            inj.alive_check.get("scenario", {}).get("probe_invariant")
            for inj in catalog.chains[0].injections
            if inj.alive_check.get("kind") == "scenario"
        } - {None}
        self.alive_log: dict[str, dict[str, Any]] = {}
        self.scenario_log: dict[tuple[str, ...], dict[str, Any]] = {}

    # -- 条件施加 --

    def _reset(self, chain: emergence.Chain) -> None:
        """把探针数据恢复到基线：删旧订单、按基线重建探针客户。"""
        cust = chain.flow["customer"]
        name = cust["name"]
        assert IDENT.match(name), f"探针客户名不合法: {name}"
        _delete_probe_orders(self.db, f"customer_name = '{name}'")
        self.db.exec(f"DELETE FROM shop_customer WHERE name = '{name}'")
        self.db.exec(
            f"INSERT INTO shop_customer(name, level, credit_limit_cent) VALUES "
            f"('{name}', '{cust['level']}', {int(cust['creditLimitCent'])})")

    def _apply(self, chain: emergence.Chain, subset: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
        """按注入集合施加条件，返回 (服务实例名, 请求字段覆盖)。"""
        instance = chain.baseline_instance
        overrides: dict[str, Any] = {}
        cust_name = chain.flow["customer"]["name"]
        for inj in chain.injections:
            if inj.id not in subset:
                continue
            mutation = inj.mutation
            kind = mutation.get("kind")
            if kind == "data":
                if mutation.get("target") != "customer":
                    raise ValueError(f"{inj.id}: 不认识的数据目标 {mutation.get('target')}")
                sets = []
                for column, value in (mutation.get("set") or {}).items():
                    assert IDENT.match(column), f"{inj.id}: 非法列名 {column}"
                    sets.append(f"{column} = '{value}'")
                self.db.exec(f"UPDATE shop_customer SET {', '.join(sets)} "
                             f"WHERE name = '{cust_name}'")
            elif kind == "instance":
                instance = str(mutation["instance"])
            elif kind == "request":
                overrides.update(mutation.get("set") or {})
            else:
                raise ValueError(f"{inj.id}: 不认识的注入类型 {kind}")
        return instance, overrides

    # -- 一次场景 --

    def _order_body(self, chain: emergence.Chain, overrides: dict[str, Any]) -> dict[str, Any]:
        body = dict(chain.flow["orders"][0])
        body.update(overrides)
        body["customerName"] = chain.flow["customer"]["name"]
        return body

    def _place(self, chain: emergence.Chain, instance: str,
               overrides: dict[str, Any]) -> tuple[int, Any]:
        inst = self.catalog.instances[instance]
        body = self._order_body(chain, overrides)
        status, payload = http_post(str(inst["base_url"]), "/api/orders", body)
        return status, payload

    def _judge(self, chain: emergence.Chain, order_id: int) -> tuple[bool, float, list[str], dict]:
        """读库 → 跑该链所在实体的全部不变式 → 返回 (目标是否被违反, 偏离量, 违反的不变量, 细节)。"""
        entity = self.entities[chain.entity]
        sql = entity["read_sql"].format(ids=str(order_id))
        rows = self.db.rows(sql)
        entity_invs = [i for i in self.invariants.values() if i.get("entity") == chain.entity]
        results = evaluate_all(rows, entity_invs)
        by_id = {r["id"]: r for r in results}
        target = by_id.get(chain.target_invariant)
        violated = bool(target and not target["ok"] and target["violations"] > 0)
        magnitude = 0.0
        if target:
            for sample in target["failure_samples"]:
                magnitude = max(magnitude, deviation(sample))
        # 目标不变式永远计入；其余「探针不变式」只用于活性检查，不进违反集
        excluded = {i for i in self.probe_invariants if i != chain.target_invariant}
        violated_ids = sorted(r["id"] for r in results
                              if not r["ok"] and r["violations"] > 0
                              and r["id"] not in excluded)
        detail = {r["id"]: {"violations": r["violations"], "checked": r["checked"],
                            "samples": r["failure_samples"]} for r in results}
        return violated, magnitude, violated_ids, {"rows": rows, "invariants": detail}

    # -- 引擎协议 --

    def run(self, chain: emergence.Chain, subset: tuple[str, ...]) -> emergence.Observation:
        self._reset(chain)
        instance, overrides = self._apply(chain, subset)
        status, payload = self._place(chain, instance, overrides)
        if status != 200 or not isinstance(payload, dict) or "id" not in payload:
            detail = f"下单失败 http={status} 响应={payload}"
            self.scenario_log[subset] = {"subset": list(subset), "instance": instance,
                                         "status": status, "error": detail}
            return emergence.Observation(subset=subset, violated=False, status="error",
                                         detail=detail)
        order_id = int(payload["id"])
        violated, magnitude, violated_ids, detail = self._judge(chain, order_id)
        row = detail["rows"][0] if detail["rows"] else {}
        evidence = {
            "instance": instance,
            "profile": self.catalog.instances[instance].get("profile", ""),
            "request": self._order_body(chain, overrides),
            "http_status": status,
            "response": payload,
            "row": row,
            "target_invariant": chain.target_invariant,
            "violated": violated,
            "magnitude": magnitude,
            "violated_invariants": violated_ids,
            "invariants": detail["invariants"],
        }
        self.scenario_log[subset] = evidence
        return emergence.Observation(subset=subset, violated=violated,
                                     magnitude=magnitude,
                                     violated_invariants=tuple(violated_ids),
                                     evidence=evidence)

    def alive(self, chain: emergence.Chain, injection: emergence.Injection) -> emergence.AliveResult:
        """活性检查：证明这条注入代表的条件在当前环境里是活的。

        语义（见 EMERGENCE_RESULT.md）：单独施加该注入后，在它自己的层上应当
        能看到症状 —— 看得到 = 活；看不到 = 死（环境把它 kill 了），该注入会被
        从分母剔除并告警。
        """
        check = injection.alive_check
        if check.get("kind") != "scenario":
            return emergence.AliveResult(injection.id, False,
                                         detail=f"未知的活性检查类型 {check.get('kind')}")
        scenario = check["scenario"]
        probe_id = scenario["probe_invariant"]
        probe = self.invariants.get(probe_id)
        if probe is None:
            return emergence.AliveResult(injection.id, False,
                                         detail=f"探针不变式 {probe_id} 不存在")

        # 活性检查有自己的场景：按 scenario 描述施加条件，再判探针不变式
        chain_probe = emergence.Chain(
            id=chain.id, title=chain.title, hypothesis=chain.hypothesis,
            severity=chain.severity, target_invariant=probe_id,
            root_cause_key=chain.root_cause_key, location=chain.location,
            injections=chain.injections, claim=chain.claim, cwe=chain.cwe,
            cites=chain.cites, entity=chain.entity, flow=chain.flow,
            baseline_instance=chain.baseline_instance, baseline=chain.baseline,
            notes=chain.notes)

        self._reset(chain_probe)
        cust_name = chain_probe.flow["customer"]["name"]
        self.db.exec(f"UPDATE shop_customer SET level = '{scenario['customer_level']}' "
                     f"WHERE name = '{cust_name}'")
        body = dict(chain_probe.flow["orders"][0])
        body["quotedAmountCent"] = int(scenario["quote"])
        body["currency"] = scenario.get("currency", "CNY")
        body["customerName"] = cust_name
        instance = scenario["instance"]
        inst = self.catalog.instances[instance]
        status, payload = http_post(str(inst["base_url"]), "/api/orders", body)
        if status != 200 or not isinstance(payload, dict) or "id" not in payload:
            detail = f"活性检查下单失败 http={status} 响应={payload}"
            self.alive_log[injection.id] = {"status": status, "error": detail}
            return emergence.AliveResult(injection.id, False, symptom=probe_id, detail=detail)

        order_id = int(payload["id"])
        rows = self.db.rows(self.entities[chain_probe.entity]["read_sql"].format(ids=str(order_id)))
        results = evaluate_all(rows, [probe])
        outcome = "violated" if not results[0]["ok"] else "satisfied"
        expect = scenario["expect"]
        alive = outcome == expect
        evidence = {
            "instance": instance,
            "profile": inst.get("profile", ""),
            "request": body,
            "http_status": status,
            "response": payload,
            "row": rows[0] if rows else {},
            "probe_invariant": probe_id,
            "outcome": outcome,
            "expect": expect,
        }
        self.alive_log[injection.id] = evidence
        return emergence.AliveResult(
            injection.id, alive, symptom=probe_id, evidence=evidence,
            detail=(f"探针不变式 {probe_id} 实测 {outcome}，期望 {expect}"
                    if alive else f"探针不变式 {probe_id} 实测 {outcome}，期望 {expect} —— 注入是死的"))


# ------------------------------------------------------------------ 产物

def _fmt(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_steps(executor: DemoExecutor, judgement: emergence.Judgement,
                chain: emergence.Chain, active_ids: list[str]) -> list[dict[str, Any]]:
    """把实验过程整理成 chain 的 steps（结构对齐 pilot/integration_chains.json）。"""
    steps: list[dict[str, Any]] = []
    n = 0
    for inj in chain.injections:
        if not inj.active:
            continue
        ev = executor.alive_log.get(inj.id, {})
        n += 1
        steps.append({
            "n": n,
            "method": "ALIVE-CHECK",
            "path": f"--alive-check {inj.id}",
            "status": 200 if ev.get("outcome") else 0,
            "evidence": (
                f"注入 {inj.id}（{inj.layer} 层，缺陷 {inj.defect}）活性检查："
                f"实例 {ev.get('instance', '?')}（profile={ev.get('profile', '?')}），"
                f"请求 {_fmt(ev.get('request', {}))}，HTTP {ev.get('http_status', '?')}，"
                f"响应 {_fmt(ev.get('response', {}))}，"
                f"探针不变式 {ev.get('probe_invariant', '?')} 实测 {ev.get('outcome', '?')}"
                f"（期望 {ev.get('expect', '?')}）→ "
                f"{'活' if judgement.alive.get(inj.id) else '死（已从分母剔除）'}"),
        })
    combined = tuple(active_ids)
    for obs in judgement.observations:
        ev = obs.evidence
        if not ev:
            continue
        subset = tuple(obs.subset)
        if subset == ():
            label = "基线（不注入任何条件）"
        elif subset == combined:
            label = f"合起来注入 {'+'.join(subset)}"
        elif len(subset) == 1:
            label = f"只注入 {subset[0]}"
        else:
            label = f"leave-one-out：{'+'.join(subset)}"
        n += 1
        row = ev.get("row", {})
        verdict = "违反" if obs.violated else "成立"
        steps.append({
            "n": n,
            "method": "POST",
            "path": "/api/orders",
            "status": ev.get("http_status"),
            "evidence": (
                f"{label}｜实例 {ev.get('instance')}（profile={ev.get('profile')}）｜"
                f"请求 {_fmt(ev.get('request', {}))}｜HTTP {ev.get('http_status')}｜"
                f"响应 {_fmt(ev.get('response', {}))}｜"
                f"DB 行 quoted_amount_cent={row.get('quoted_amount_cent')} "
                f"settled_amount_cent={row.get('settled_amount_cent')} "
                f"customer_level={row.get('customer_level')}｜"
                f"目标不变式 {ev.get('target_invariant')}：{verdict}"
                f"（偏离量 {ev.get('magnitude', 0):g}）"),
        })
    return steps


def _decision_fingerprint(chains: list[dict[str, Any]]) -> str:
    """裁决结论的指纹 —— 同一输入两次跑必须一致（时间戳/自增 id 不参与）。"""
    payload = []
    for ch in chains:
        payload.append({
            "id": ch["id"],
            "verdict": ch["verdict"],
            "owner": ch["owner"],
            "category": ch["category"],
            "core": ch["core"],
            "ground_truth": ch["ground_truth"],
            "observations": [{"subset": o["subset"], "violated": o["violated"],
                              "magnitude": o["magnitude"]} for o in ch["observations"]],
        })
    blob = _fmt(payload).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def run_chain(catalog: emergence.Catalog, db: Db, inv_cfg: dict[str, Any],
              chain: emergence.Chain) -> dict[str, Any]:
    executor = DemoExecutor(catalog, inv_cfg, db)
    judgement = emergence.adjudicate(chain, executor)
    active_ids = [i.id for i in chain.injections if i.active and judgement.alive.get(i.id)]

    steps = build_steps(executor, judgement, chain, active_ids)

    observations = [{
        "subset": list(o.subset),
        "violated": o.violated,
        "magnitude": o.magnitude,
        "violated_invariants": list(o.violated_invariants),
        "status": o.status,
        "detail": o.detail,
    } for o in judgement.observations]

    # 敏感性分析：把 inactive 的注入单独激活会怎样（不参与正式裁决）
    sensitivity: list[dict[str, Any]] = []
    for inj in chain.injections:
        if inj.active:
            continue
        counterfactual = emergence.Chain(
            id=chain.id, title=chain.title, hypothesis=chain.hypothesis,
            severity=chain.severity, target_invariant=chain.target_invariant,
            root_cause_key=chain.root_cause_key, location=chain.location,
            injections=tuple(emergence.Injection(
                id=i.id, defect=i.defect, layer=i.layer, target_step=i.target_step,
                mutation=i.mutation, expected_symptom=i.expected_symptom,
                alive_check=i.alive_check, active=(i.id == inj.id), category=i.category)
                for i in chain.injections),
            claim=chain.claim, cwe=chain.cwe, cites=chain.cites, entity=chain.entity,
            flow=chain.flow, baseline_instance=chain.baseline_instance,
            baseline=chain.baseline, notes=chain.notes)
        cf_executor = DemoExecutor(catalog, inv_cfg, db)
        cf = emergence.adjudicate(counterfactual, cf_executor)
        single = next((o for o in cf.observations if o.subset == (inj.id,)), None)
        sensitivity.append({
            "injection": inj.id,
            "note": inj.alive_check.get("why", "") or inj.expected_symptom,
            "f_single": None if single is None else single.violated,
            "magnitude_single": None if single is None else single.magnitude,
            "counterfactual_verdict": cf.verdict,
            "counterfactual_rationale": cf.rationale,
        })

    chain_out: dict[str, Any] = {
        "id": chain.id,
        "title": chain.title,
        "hypothesis": chain.hypothesis,
        "severity": chain.severity,
        "owner": (f"{judgement.owner_layer}（{judgement.owner_relation}）"
                  if judgement.owner_layer else "不开单"),
        "cites": list(chain.cites),
        "verdict": judgement.verdict,
        "steps": steps,
        "repro": f"python3 demos/shop/emergence_probe.py --chain {chain.id}",
        "rationale": judgement.rationale,
        "ground_truth": judgement.ground_truth,
        "owner_layer": judgement.owner_layer,
        "owner_relation": judgement.owner_relation,
        "category": judgement.category,
        "core": list(judgement.core),
        "warnings": list(judgement.warnings),
        "alive": dict(judgement.alive),
        "observations": observations,
        "sensitivity": sensitivity,
        "findings": [],
    }

    for filing in judgement.filings:
        detail = (f"{chain.hypothesis}\n\n"
                  f"裁决：{judgement.verdict}（{judgement.rationale}）\n"
                  f"活性检查：" + "；".join(
                      f"{k}={'活' if v else '死'}" for k, v in sorted(judgement.alive.items())) + "\n"
                  f"最小核心集：{'+'.join(judgement.core)}\n\n"
                  f"证据步骤：\n" + "\n".join(
                      f"  {s['n']}. [{s['method']} {s['path']}] {s['evidence']}"
                      for s in steps))
        finding = emergence.build_finding(
            judgement, chain, filing,
            source_ref="emergence_chains.json",
            repro_cmd=f"python3 demos/shop/emergence_probe.py --chain {chain.id}",
            evidence_refs=[f"emergence_chains.json#{chain.id}"],
            detail=detail)
        chain_out["findings"].append(finding)
    return chain_out


def cleanup(db: Db) -> dict[str, Any]:
    """删干净探针数据，返回残留情况（rows 必须为 0，ids 便于定位）。

    谓词用 starts_with 而不是 LIKE。理由：探针前缀本身以 `_` 结尾，而 LIKE 里
    `_` 是「单字符通配」—— 想按字面量匹配就得写 `\\_`，于是图案变成
    「ai_probe_ + 字面下划线」，要求名字里出现**两个**下划线，删一行都删不掉，
    而计数照样返回 0：「没有残留」变成假象，还很难发现。
    starts_with 是纯前缀比较，没有通配语义，写错会立刻暴露成残留行。
    """
    _delete_probe_orders(db, f"starts_with(customer_name, '{PFX}')")
    db.exec(f"DELETE FROM shop_customer WHERE starts_with(name, '{PFX}')")
    left = db.rows(
        f"SELECT id FROM shop_order WHERE starts_with(customer_name, '{PFX}') "
        f"UNION ALL SELECT id FROM shop_customer WHERE starts_with(name, '{PFX}') "
        f"UNION ALL SELECT id FROM shop_quota_occupancy WHERE starts_with(customer_name, '{PFX}') "
        f"UNION ALL SELECT id FROM shop_ledger_entry WHERE starts_with(customer_name, '{PFX}')")
    return {"rows": len(left), "ids": [r["id"] for r in left]}


def main() -> int:
    ap = argparse.ArgumentParser(description="涌现探针（demo-shop）")
    ap.add_argument("--chain", default="", help="只裁决指定链")
    ap.add_argument("--alive-check", default="", help="只跑某条注入的活性检查")
    args = ap.parse_args()

    catalog = emergence.load_catalog(CATALOG_FILE)
    inv_cfg = json.loads(INVARIANTS_FILE.read_text(encoding="utf-8"))
    db = Db(catalog.database)

    if args.alive_check:
        chain = next(c for c in catalog.chains
                     if any(i.id == args.alive_check for i in c.injections))
        inj = next(i for i in chain.injections if i.id == args.alive_check)
        executor = DemoExecutor(catalog, inv_cfg, db)
        res = executor.alive(chain, inj)
        print(_fmt(res.evidence))
        print(f"{args.alive_check}: {'活' if res.alive else '死'} —— {res.detail}")
        left = cleanup(db)
        print(f"残留校验：探针数据行 {left['rows']} 条（必须为 0）")
        return 0 if res.alive else 1

    chains = [c for c in catalog.chains if not args.chain or c.id == args.chain]
    if not chains:
        print(f"没有这条链: {args.chain}")
        return 2

    chain_outs: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    try:
        for chain in chains:
            out = run_chain(catalog, db, inv_cfg, chain)
            chain_outs.append(out)
            findings.extend(out["findings"])
            print(f"[{chain.id}] 裁决 = {out['verdict']}"
                  f"｜owner={out['owner_layer']}({out['owner_relation']})"
                  f"｜category={out['category'] or '—'}"
                  f"｜核心集={'+'.join(out['core']) or '—'}")
            for w in out["warnings"]:
                print(f"  ⚠ {w}")
    finally:
        left = cleanup(db)
        residue = left["rows"]
        print(f"残留校验：探针数据行 {residue} 条（必须为 0）"
              + (f" —— 残留 id: {left['ids']}" if left["ids"] else ""))

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_from": "demos/shop/emergence_probe.py",
        "target": "demo-shop",
        "catalog": "demos/shop/emergence_injections.json",
        "invariants": "demos/shop/invariants.json",
        "engine": "pilot/engine/emergence.py",
        "summary": {
            "chains": len(chain_outs),
            "verdicts": {c["id"]: c["verdict"] for c in chain_outs},
            "findings": len(findings),
            "warnings": [w for c in chain_outs for w in c["warnings"]],
            "residue_rows": residue,
        },
        "decision_fingerprint": _decision_fingerprint(chain_outs),
        "chains": chain_outs,
        "findings": findings,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"产物 -> {OUT}")
    print(f"裁决指纹 {payload['decision_fingerprint']}")
    return 0 if residue == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
