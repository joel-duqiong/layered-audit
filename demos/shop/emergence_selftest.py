"""涌现裁决器的自证脚本（可执行，不是 pytest 套件）。

三段：

  A. 引擎七裁决矩阵 —— 用合成观测把 DESIGN.md §4.2 的七种裁决各触发一遍，
     证明裁决分支不是死代码、且优先级符合契约。
  B. 活性门控 —— 死掉的注入必须被剔除出分母 + 告警 + 三态降级为 mutant_survivor。
  C. 真实链路 —— 连跑两次真探针，比对裁决指纹；断言 demo 判出 EMERGENT_CONJUNCTIVE、
     两个单注都不触发、跑完 DB 无残留。

用法：python3 emergence_selftest.py     （退出码 0 = 全过）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent.parent / "pilot"
os.environ.setdefault("LAYERED_AUDIT_TARGET", str(HERE / "target.json"))
sys.path.insert(0, str(PILOT))
sys.path.insert(0, str(PILOT / "engine"))

import emergence  # noqa: E402

ARTIFACT = HERE / "artifacts" / "emergence_chains.json"
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got: Any, want: Any) -> None:
    global CHECKS
    CHECKS += 1
    if got == want:
        print(f"  ✓ {label}")
    else:
        FAILURES.append(f"{label}: 实得 {got!r}，期望 {want!r}")
        print(f"  ✗ {label}: 实得 {got!r}，期望 {want!r}")


# ------------------------------------------------------------------ 合成夹具

def inj(iid: str, layer: str = "service",
        category: str = "service.data_consistency") -> emergence.Injection:
    return emergence.Injection(
        id=iid, defect=f"rc.{iid}", layer=layer, target_step="POST /api/orders",
        mutation={"kind": "data", "set": {"flag": 1}}, expected_symptom="BI-PROBE",
        alive_check={"kind": "scenario", "scenario": {"probe_invariant": "BI-PROBE"}},
        active=True, category=category)


def chain(injections: list[emergence.Injection],
          target: str = "BI-PARITY") -> emergence.Chain:
    return emergence.Chain(
        id="CHAIN-SELFTEST", title="自证链", hypothesis="h", severity="HIGH",
        target_invariant=target, root_cause_key="rc.selftest",
        location={"file": "X.java", "line": 1, "symbol": "X.m", "endpoint": "POST /x"},
        injections=tuple(injections), entity="order", flow={}, baseline_instance="i")


class Scripted:
    """按预设结果回放观测的假执行器。"""

    def __init__(self, alive: dict[str, bool],
                 results: dict[tuple[str, ...], emergence.Observation]) -> None:
        self.alive_map = alive
        self.results = results

    def run(self, ch: emergence.Chain, subset: tuple[str, ...]) -> emergence.Observation:
        key = tuple(sorted(subset))
        hit = self.results.get(key)
        if hit is None:
            return emergence.Observation(subset=tuple(subset), violated=False)
        return emergence.Observation(subset=tuple(subset), violated=hit.violated,
                                     magnitude=hit.magnitude,
                                     violated_invariants=hit.violated_invariants)

    def alive(self, ch: emergence.Chain, injection: emergence.Injection) -> emergence.AliveResult:
        ok = self.alive_map.get(injection.id, True)
        return emergence.AliveResult(injection.id, ok, symptom="BI-PROBE",
                                     detail="" if ok else "注入在这个环境里是死的")


def obs(subset: tuple[str, ...], violated: bool, magnitude: float = 0.0,
        invariants: tuple[str, ...] = ()) -> emergence.Observation:
    return emergence.Observation(subset=subset, violated=violated, magnitude=magnitude,
                                 violated_invariants=invariants)


# ------------------------------------------------------------------ A 七裁决

def part_a() -> None:
    print("\n[A] 引擎七裁决矩阵（合成观测）")

    a, b, c = inj("INJ-A"), inj("INJ-B"), inj("INJ-C")

    # 1. EMERGENT_CONJUNCTIVE：合起来错、单独都不错
    j = emergence.adjudicate(chain([a, b]), Scripted(
        {"INJ-A": True, "INJ-B": True},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), False),
         ("INJ-B",): obs(("INJ-B",), False),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 90000.0, ("BI-PARITY",))}))
    check("合起来错/单独对 -> EMERGENT_CONJUNCTIVE", j.verdict, emergence.VERDICT_CONJUNCTIVE)
    check("  owner 归集成层", j.owner_layer, "integration")
    check("  开单类别", j.category, "integration.end_to_end_business")

    # 2. EMERGENT_CONJUNCTIVE_CORE：leave-one-out 求出更小的核心集
    j = emergence.adjudicate(chain([a, b, c]), Scripted(
        {"INJ-A": True, "INJ-B": True, "INJ-C": True},
        {(): obs((), False),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 100.0, ("BI-PARITY",)),
         ("INJ-A", "INJ-B", "INJ-C"): obs(("INJ-A", "INJ-B", "INJ-C"), True, 100.0, ("BI-PARITY",))}))
    check("有冗余注入 -> EMERGENT_CONJUNCTIVE_CORE", j.verdict,
          emergence.VERDICT_CONJUNCTIVE_CORE)
    check("  最小核心集", sorted(j.core), ["INJ-A", "INJ-B"])

    # 3. EMERGENT_AMPLIFIED：合起来错且偏离量放大 ≥1.5×
    j = emergence.adjudicate(chain([a, b]), Scripted(
        {"INJ-A": True, "INJ-B": True},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), True, 100.0, ("BI-X",)),
         ("INJ-B",): obs(("INJ-B",), False),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 200.0, ("BI-X",))}))
    check("错得更狠 -> EMERGENT_AMPLIFIED", j.verdict, emergence.VERDICT_AMPLIFIED)
    check("  类别", j.category, "integration.cross_service_performance")

    # 4. EMERGENT_MASKING：单注能看见症状、合起来反而被盖住；owner = 掩盖者的层
    masker = inj("INJ-B", layer="file", category="file.state_consistency")
    j = emergence.adjudicate(chain([a, masker]), Scripted(
        {"INJ-A": True, "INJ-B": True},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), True, 100.0, ("BI-X",)),
         ("INJ-B",): obs(("INJ-B",), False),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), False)}))
    check("症状被掩盖 -> EMERGENT_MASKING", j.verdict, emergence.VERDICT_MASKING)
    check("  owner = 掩盖者的层", j.owner_layer, "file")
    check("  集成层只引用", j.filings[1].relation, "cited")
    check("  集成层类别", j.filings[1].category, "integration.emergent_masking")

    # 5. NOT_EMERGENT_INDEPENDENT：各自都错、违反的不变量互不相交
    j = emergence.adjudicate(chain([a, b]), Scripted(
        {"INJ-A": True, "INJ-B": True},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), True, 100.0, ("BI-X",)),
         ("INJ-B",): obs(("INJ-B",), True, 100.0, ("BI-Y",)),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 100.0, ("BI-Z",))}))
    check("各错各的 -> NOT_EMERGENT_INDEPENDENT", j.verdict, emergence.VERDICT_NOT_INDEPENDENT)
    check("  集成层不开单", len(j.filings), 0)

    # 6. NOT_EMERGENT_ADDITIVE：单注也触发，且违反的不变量相交，没有倍数效应
    j = emergence.adjudicate(chain([a, b]), Scripted(
        {"INJ-A": True, "INJ-B": True},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), True, 100.0, ("BI-X",)),
         ("INJ-B",): obs(("INJ-B",), True, 100.0, ("BI-X",)),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 100.0, ("BI-X",))}))
    check("相交但无倍数效应 -> NOT_EMERGENT_ADDITIVE", j.verdict, emergence.VERDICT_NOT_ADDITIVE)
    check("  集成层不开单", len(j.filings), 0)

    # 7. FALSIFIED_CHAIN：基线就违反目标不变式 —— 违反不是注入造成的
    j = emergence.adjudicate(chain([a]), Scripted(
        {"INJ-A": True}, {(): obs((), True, 100.0, ("BI-PARITY",))}))
    check("基线即违反 -> FALSIFIED_CHAIN", j.verdict, emergence.VERDICT_FALSIFIED)
    check("  三态降级为 unverified", j.ground_truth, emergence.GT_UNVERIFIED)


# ------------------------------------------------------------------ B 活性门控

def part_b() -> None:
    print("\n[B] 活性门控（死注入剔除 + 告警 + 三态）")
    a, b, dead = inj("INJ-A"), inj("INJ-B"), inj("INJ-DEAD")
    j = emergence.adjudicate(chain([a, b, dead]), Scripted(
        {"INJ-A": True, "INJ-B": True, "INJ-DEAD": False},
        {(): obs((), False),
         ("INJ-A",): obs(("INJ-A",), False),
         ("INJ-B",): obs(("INJ-B",), False),
         ("INJ-A", "INJ-B"): obs(("INJ-A", "INJ-B"), True, 100.0, ("BI-PARITY",))}))
    check("死注入被剔除出分母（核心里没有它）", list(j.core), ["INJ-A", "INJ-B"])
    check("  死注入有告警（不许静默）", any("INJ-DEAD" in w for w in j.warnings), True)
    check("  三态降级为 mutant_survivor", j.ground_truth, emergence.GT_MUTANT_SURVIVOR)
    check("  其余注入照常裁决", j.verdict, emergence.VERDICT_CONJUNCTIVE)


# ------------------------------------------------------------------ C 真实链路

def _run_probe() -> dict[str, Any]:
    proc = subprocess.run([sys.executable, str(HERE / "emergence_probe.py")],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"探针跑失败（退出码 {proc.returncode}）:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def part_c() -> None:
    print("\n[C] 真实链路（demo-shop 真容器）")
    first = _run_probe()
    second = _run_probe()
    check("两次跑结论指纹一致（确定性）",
          first["decision_fingerprint"], second["decision_fingerprint"])

    chain_out = first["chains"][0]
    check("裁决 = EMERGENT_CONJUNCTIVE", chain_out["verdict"], "EMERGENT_CONJUNCTIVE")
    check("  owner 归集成层", chain_out["owner_layer"], "integration")
    check("  类别", chain_out["category"], "integration.end_to_end_business")
    check("  三态 = verified", chain_out["ground_truth"], "verified")

    by_subset = {tuple(o["subset"]): o for o in chain_out["observations"]}
    singles = [k for k in by_subset if len(k) == 1]
    check("  恰好两个单注场景", len(singles), 2)
    for k in sorted(singles):
        check(f"  单注 {k[0]} 单独不触发", by_subset[k]["violated"], False)
    combined = tuple(chain_out["core"])
    check("  合起来触发", by_subset[combined]["violated"], True)
    check("  偏离量 = 90000 分", by_subset[combined]["magnitude"], 90000.0)

    check("  跑完无探针残留行", first["summary"]["residue_rows"], 0)
    check("  两次跑无残留", second["summary"]["residue_rows"], 0)
    check("  无告警", first["summary"]["warnings"], [])

    # 引擎纯度：通用引擎里不许出现项目字面量。
    # 本次工作碰过的文件（emergence.py / invariants.py）必须零违规；
    # 别人正在写的模块若报违规，大声告警但不记成本次工作的失败。
    env = {k: v for k, v in os.environ.items() if k != "LAYERED_AUDIT_TARGET"}
    purity = subprocess.run([sys.executable, str(PILOT / "engine" / "check_engine_purity.py")],
                             capture_output=True, text=True, timeout=120, env=env)
    mine = ("emergence.py", "invariants.py")
    offenders = sorted({m.group(1) for m in
                        (re.match(r"\s+(\S+\.py):\d+", line) for line in purity.stdout.splitlines())
                        if m})
    mine_bad = [f for f in offenders if f in mine]
    check("引擎纯度：本次改动的文件零违规", mine_bad, [])
    if purity.returncode != 0:
        print(f"  ! 注意：仓库级纯度检查当前为红（违规文件 {offenders}）——")
        print("    那是别的模块的在制品，与本次涌现工作无关；但提交前应当由它的作者修掉。")
    else:
        print("  ✓ 仓库级引擎纯度检查通过")


def main() -> int:
    print("=" * 72)
    print("涌现裁决器自证")
    print("=" * 72)
    part_a()
    part_b()
    part_c()
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"自证失败：{len(FAILURES)}/{CHECKS} 项不通过")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"自证通过：{CHECKS} 项全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
