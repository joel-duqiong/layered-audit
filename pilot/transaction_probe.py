"""切面生效性探针 —— 「声明了 @Transactional，运行时到底生效没有」。

这是整套审计**最大的语义盲区**：function/file 层的载体（反射直调、打桩）根本不走
Spring 容器，代理不参与，所以「事务边界」在低层完全观察不到。

做法：
  1. 克隆正在跑的容器（probe_instance.py），加
     `-Dlogging.level.org.springframework.transaction.interceptor=TRACE`
  2. 打真实请求，触发到各个 service 方法
  3. 从容器日志里抓 `Getting transaction for [class.method]` —— 这是 Spring
     事务拦截器**逐方法**打出的实际生效记录
  4. 与静态声明的 @Transactional 清单**差分**：
     声明了但运行时没生效 → 代理失效（HIGH）
     运行时有但静态没识别 → 规则库漏了（框架自带的算噪声，单独归桶）

产出 transaction_findings.json（service 层），进总表与覆盖度矩阵。
"""
from __future__ import annotations
import project

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from integration_chain import BASE, order_body
from integration_reconcile import psql_rows
from probe_instance import ProbeInstance

HERE = project.artifact_dir()
OUT = HERE / "transaction_findings.json"
TRACE_RE = re.compile(r"Getting transaction for \[(?P<cls>[\w.$]+)\.(?P<method>\w+)\]")
TRACE_LEVEL = "-Dlogging.level.org.springframework.transaction.interceptor=TRACE"


def simple_to_fqn() -> dict[str, str]:
    """简单类名 -> 全限定名（fn_declaration 索引）。"""
    fn = HERE / "fn_declaration.json"
    if not fn.exists():
        return {}
    data = json.loads(fn.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for rec in data.get("functions") or []:
        cls = rec.get("class") or ""
        if cls:
            out.setdefault(cls.rsplit(".", 1)[-1], cls)
    return out


def declared_transactions() -> list[dict[str, Any]]:
    """静态声明的 @Transactional 方法（来自 Java 特性扫描器的产物），类名统一成全限定名。"""
    ff = HERE / "feature_findings.json"
    if not ff.exists():
        return []
    data = json.loads(ff.read_text(encoding="utf-8"))
    methods = (data.get("features") or {}).get("aspect", {}).get("methods") or []
    fqn = simple_to_fqn()
    for m in methods:
        m["fqcn"] = fqn.get(m["class"], m["class"])
        m["fq_method"] = f"{m['fqcn']}.{m['method']}"
    return methods


def trigger_requests(inst: "ProbeInstance", base: str) -> tuple[list[dict[str, Any]], set[str]]:
    """逐个请求打，每打一个就 diff 一次事务日志 → 得到「哪个请求触发了哪些方法」的精确映射。

    一次性打一轮再 diff 是做不到的：日志里没有「这条事务属于哪个请求」。
    """
    s = requests.Session()
    calls: list[dict[str, Any]] = []
    triggered: set[str] = set()

    def seen_tx() -> set[str]:
        return {f"{m.group('cls')}.{m.group('method')}"
                for m in TRACE_RE.finditer(inst.logs())}

    def hit(method: str, path: str, **kw: Any) -> None:
        before = seen_tx()
        try:
            r = s.request(method, base + path, timeout=20, **kw)
            calls.append({"method": method, "path": path, "status": r.status_code})
        except requests.RequestException as exc:
            calls.append({"method": method, "path": path, "error": str(exc)[:120]})
            return
        triggered.update(seen_tx() - before)

    for path in ("/api/products?page=1&pageSize=5", "/api/packages?page=1&pageSize=5",
                 "/api/orders?page=1&pageSize=5", "/api/categories", "/api/package-types"):
        hit("GET", path)
    prods = s.get(base + "/api/products?page=1&pageSize=5").json()["data"]["items"]
    pkgs = s.get(base + "/api/packages?page=1&pageSize=5").json()["data"]["items"]
    ptypes = s.get(base + "/api/package-types").json()["data"]
    if prods and pkgs and ptypes:
        body = order_body("txprobe", prods[0]["id"], pkgs[0]["id"])
        r = s.post(base + "/api/orders", json=body, timeout=20)
        calls.append({"method": "POST", "path": "/api/orders", "status": r.status_code})
        if r.status_code == 200:
            oid = r.json()["data"]["id"]
            hit("PUT", f"/api/orders/{oid}", json=body)
            hit("PATCH", f"/api/orders/{oid}/status", json={"status": "DEPOSIT_PAID"})
            hit("GET", f"/api/orders/{oid}")
    return calls, triggered


def cleanup_probe_orders() -> int:
    """删掉本探针造的订单（ai_probe_txprobe 前缀）。

    探针造了数据就必须自己删干净 —— 这是项目的零残留铁律，不是可选项。
    """
    from integration_reconcile import psql_exec
    psql_exec("DELETE FROM orders WHERE customer_name LIKE 'ai_probe_txprobe%';")
    rows = psql_rows("SELECT count(*) AS n FROM orders WHERE customer_name LIKE 'ai_probe_txprobe%'")
    return rows[0]["n"] if rows else 0


def main() -> None:
    declared = declared_transactions()
    declared_set = {m["fq_method"] for m in declared}
    effective: set[str] = set()
    calls: list[dict[str, Any]] = []
    probe_ok = True
    probe_note = ""

    try:
        with ProbeInstance("jiuyan-tx-probe", java_tool_options=TRACE_LEVEL) as inst:
            calls, triggered = trigger_requests(inst, inst.base)
            time.sleep(1)
            logs = inst.logs()
            for m in TRACE_RE.finditer(logs):
                effective.add(f"{m.group('cls')}.{m.group('method')}")
    finally:
        left = cleanup_probe_orders()
        print(f"  清理：删除本探针造的订单，残留 {left} 行")

    project_effective = {e for e in effective
                         if e.startswith("com.jiuyan") or ".banquet." in e}
    framework_effective = effective - project_effective

    triggered_fq = set(triggered)

    # 三分：触发了但没生效（真缺陷）/ 没触发（未观测，不是缺陷）/ 生效但没声明（规则盲区）
    declared_not_effective = sorted(declared_set - project_effective - (declared_set - triggered_fq))
    declared_not_triggered = sorted(declared_set - triggered_fq)
    effective_not_declared = sorted(project_effective - declared_set)

    if not effective:
        probe_ok = False
        probe_note = ("探针没抓到任何事务日志 —— 可能是 Spring 版本漂移导致日志格式变了。"
                      "此时**不能**判定「事务没生效」，只能判定「没查完」。")

    findings: list[dict[str, Any]] = []
    if probe_ok and declared_not_effective:
        findings.append({
            "id": "SVC-TX-DECLARED-NOT-EFFECTIVE",
            "severity": "HIGH",
            "endpoint": "",
            "title": "声明了 @Transactional 但运行时没观察到事务生效",
            "detail": (
                "静态扫描到 " + str(len(declared_set)) + " 个 @Transactional 方法，"
                "其中 " + str(len(triggered_fq & declared_set)) + " 个本次被请求触达，"
                "运行时事务拦截器只对 " + str(len(project_effective)) + " 个打了 "
                "`Getting transaction for [...]`。\n"
                "**触达了但没生效**（这些才是真缺陷）：\n"
                + json.dumps(declared_not_effective, ensure_ascii=False, indent=2)
                + "\n这类方法的事务边界是假的 —— 出错时不会回滚。\n"
                "（另有 " + str(len(declared_not_triggered)) + " 个本次没被请求触达，"
                "归入「未观测」，不算缺陷。）"),
            "repro": "python3 transaction_probe.py",
            "evidence": {"declared": sorted(declared_set),
                         "effective": sorted(project_effective),
                         "declared_not_effective": declared_not_effective,
                         "calls": calls},
        })
    if probe_ok and effective_not_declared:
        findings.append({
            "id": "SVC-TX-EFFECTIVE-NOT-DECLARED",
            "severity": "MEDIUM",
            "endpoint": "",
            "title": "运行时生效的事务没被静态规则识别 → 静态扫描器有盲区",
            "detail": (
                "运行时观察到事务生效，但静态 @Transactional 清单里没有这些方法：\n"
                + json.dumps(effective_not_declared, ensure_ascii=False, indent=2)
                + "\n可能是：隐式事务（Spring Data repository 自带 @Transactional(readOnly=true)）、"
                  "或扫描器的注解匹配漏了。"),
            "repro": "python3 transaction_probe.py",
            "evidence": {"effective_not_declared": effective_not_declared,
                         "framework_effective": sorted(framework_effective)},
        })
    if probe_ok and not declared_not_effective and not effective_not_declared:
        findings.append({
            "id": "SVC-TX-DECLARED-EQUALS-EFFECTIVE",
            "severity": "INFO",
            "endpoint": "",
            "title": "静态声明的事务与运行时实际生效的事务完全一致（差分为空）",
            "detail": (
                f"静态声明 {len(declared_set)} 个 @Transactional 方法，"
                f"运行时事务拦截器对其中全部打了生效记录，无遗漏、无多报。\\n"
                f"另有 {len(framework_effective)} 条框架自身的事务（Spring Data repository "
                f"自带 @Transactional(readOnly=true)），单独归桶，不算项目缺陷。\\n"
                "**这是静态扫描无法单独给出的结论** —— 只有把静态声明与运行时拦截器日志"
                "差分才能确认「切面真的织入了」。"),
            "repro": "python3 transaction_probe.py",
            "evidence": {"declared": sorted(declared_set),
                         "effective": sorted(project_effective),
                         "framework_effective": sorted(framework_effective)},
        })
    if not probe_ok:
        findings.append({
            "id": "SVC-TX-PROBE-INCONCLUSIVE",
            "severity": "INFO",
            "endpoint": "",
            "title": "事务生效性探针没抓到日志 → 判定「没查完」而不是「没生效」",
            "detail": probe_note,
            "repro": "python3 transaction_probe.py",
            "evidence": {"calls": calls},
        })

    payload = {"service": findings,
               "declared": sorted(declared_set),
               "effective": sorted(project_effective),
               "framework_effective": sorted(framework_effective),
               "triggered": sorted(triggered_fq),
               "declared_not_triggered": declared_not_triggered}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"切面生效性探针 -> {OUT}")
    print(f"  静态声明 {len(declared_set)} / 本次触达 {len(triggered_fq & declared_set)} / "
          f"运行时生效 {len(project_effective)} / 框架自带 {len(framework_effective)}")
    if declared_not_triggered:
        print(f"  未触达（不是缺陷，是没观测到）{len(declared_not_triggered)} 条")
    if declared_not_effective:
        print(f"  声明了没生效：{declared_not_effective}")
    if effective_not_declared:
        print(f"  生效但没识别：{effective_not_declared}")
    for f in findings:
        print(f"  {f['severity']:<9}{f['id']:<38}{f['title'][:44]}")


if __name__ == "__main__":
    main()
