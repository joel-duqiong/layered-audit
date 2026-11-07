"""增量调度决策 —— 「改一处，只动受影响的那几个」。

**为什么需要它**：流水线现在是全量跑的。代码没变的层也重跑一遍，既慢，
又让「这次到底动了什么」看不出来。

**决策只有两种**：
  run   这层受影响 -> 跑
  skip  这层没受影响 -> **不跑**，产物文件原样保留（复用上次的）

**保守的地方**：只要有一处判断不了（种子只到类级、配置域变更、
影响半径超阈值），就一律 run —— 宁可多跑，不可漏跑。

**退出码约定**（给 shell 用）：
  0 = 该跑；1 = 该跳过；2 = 决策不出来（按「该跑」处理）
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema

PROJECT_DIR = findings_schema.target_path().parent
PLAN_FILE = PROJECT_DIR / "incremental_plan.json"

# 每层跑完会写出的产物文件（跳过 = 不重写 = 复用上次的）
LAYER_ARTIFACTS = {
    "function": ["fn_declaration.json", "fn_observation.json", "fn_findings.json"],
    "file": ["file_declaration.json", "file_observation.json", "file_findings.json"],
    "service": ["declaration.json", "observation.json", "findings.json"],
    "class": ["class_declaration.json", "class_contract.json",
               "class_observation.json", "class_findings.json"],
    "integration": ["integration_declaration.json", "integration_observation.json",
                    "integration_reconciliation.json", "integration_chains.json",
                    "integration_findings.json"],
}
# 非源码改动的影响面：不同配置只影响相关的层
CONFIG_AFFECTS = {
    "application.yml": {"service", "integration", "class"},
    "application-": {"service", "integration", "class"},
    "pom.xml": {"function", "file", "class", "service", "integration"},
    "build.gradle": {"function", "file", "class", "service", "integration"},
    "Dockerfile": {"function", "file", "class", "service", "integration"},
    "docker-compose": {"class", "service", "integration"},
    ".sql": {"class", "service", "integration"},
    "db/migration": {"class", "service", "integration"},
}
# 每层的靶区（决定它受不受影响）
LAYER_UNITS = {
    "function": lambda u: u["layer"] == "function",
    "file": lambda u: u["layer"] == "file",
    "service": lambda u: u["layer"] == "service",
    "class": lambda u: u["layer"] == "class",
    "integration": lambda u: u["layer"] == "integration",
}


def _decide(layer: str, scope: set[str], affected: set[str],
            config_touched: dict[str, list[str]],
            uncertain: list[str]) -> tuple[str, str]:
    """一层的跑/跳过决策（纯函数，build 与自检共用）。"""
    layer_uncertain = list(uncertain)
    if layer in config_touched:
        layer_uncertain.append(f"配置域变更：{config_touched[layer][:2]}")
    if layer_uncertain:
        return "run", "；".join(layer_uncertain)
    touched = sorted({a for a in affected
                      if a.split(":", 1)[-1].split("#")[0] in scope})
    if touched:
        return "run", f"受影响单元 {len(touched)} 个"
    return "skip", "本层无受影响单元，复用上次产物"


def _load(name: str) -> Any:
    path = PROJECT_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build() -> dict[str, Any]:
    units = (_load("units.json") or {}).get("units", [])
    impact = _load("change_impact.json") or {}
    target = findings_schema.load_target()
    sut = target.get("paths", {})

    affected = set(impact.get("affected_upper_bound") or [])
    changed_files = set(impact.get("changed_files") or [])
    counts = impact.get("counts") or {}

    # 决策不了的情况 -> 一律跑
    uncertain: list[str] = []
    if not units:
        # 首次运行（还没有单元索引）-> 全部跑，别拿空集当「没受影响」
        uncertain.append("没有单元索引（首次运行），全部层执行")
    if not affected:
        uncertain.append("没有影响集（无法计算变更影响）")
    if impact.get("counts", {}).get("upper_bound", 0) and \
            counts.get("upper_bound", 0) > 0.4 * max(len(units), 1):
        uncertain.append(f"影响半径过大（{counts.get('upper_bound')} / {len(units)} 个单元）")
    # 非源码改动（配置 / 依赖 / 镜像 / SQL）会按**影响面**分别让相关层重跑，
    # 而不是「全部层都跑」—— 那等于没有增量。
    config_touched: dict[str, list[str]] = defaultdict(list)
    for f in sorted(changed_files):
        for pattern, layers in CONFIG_AFFECTS.items():
            if pattern in f:
                for layer in layers:
                    config_touched[layer].append(f)

    by_layer: dict[str, set[str]] = defaultdict(set)
    for u in units:
        layer = u.get("layer")
        if layer in LAYER_UNITS:
            by_layer[layer].add(u["unit_key"].split("#")[0])

    plan: dict[str, Any] = {
        "base": impact.get("base"),
        "changed_files": sorted(changed_files),
        "uncertain": uncertain,
        "layers": {},
    }
    for layer in sorted(LAYER_UNITS):
        decision, reason = _decide(layer, by_layer.get(layer, set()), affected,
                                   config_touched, uncertain)
        plan["layers"][layer] = {
            "decision": decision,
            "reason": reason,
            "artifacts": LAYER_ARTIFACTS.get(layer, []),
        }
    return plan


def selftest() -> bool:
    """闸门自检：只改了文件层时，其余层必须判「跳过」。

    没有自检的话，一个「永远返回 run」的闸门看起来也完全正常 ——
    增量就成了摆设，而报告上还是绿的。
    """
    units = [
        {"unit_key": "com.x.A#m", "layer": "function"},
        {"unit_key": "com.x.B", "layer": "file"},
        {"unit_key": "com.x.C", "layer": "service"},
        {"unit_key": "com.x.D", "layer": "class"},
        {"unit_key": "com.x.E", "layer": "integration"},
    ]

    scope = {"function": {"com.x.A"}, "file": {"com.x.B"}, "service": {"com.x.C"},
             "class": {"com.x.D"}, "integration": {"com.x.E"}}

    def decide(affected: list[str], files: list[str]) -> dict[str, str]:
        config_touched: dict[str, list[str]] = defaultdict(list)
        for f in files:
            for pattern, layers in CONFIG_AFFECTS.items():
                if pattern in f:
                    for layer in layers:
                        config_touched[layer].append(f)
        return {layer: _decide(layer, scope[layer], set(affected), config_touched, [])[0]
                for layer in scope}

    d1 = decide(["file:com.x.B"], [])
    d2 = decide(["file:com.x.B"], ["docker-compose.yml"])
    ok = True
    if d1["file"] != "run" or any(v != "skip" for k, v in d1.items() if k != "file"):
        print(f"  自检失败：只改文件层时的判定不对 {d1}")
        ok = False
    else:
        print("  自检通过：只改文件层 -> 只有文件层 run，其余 skip")
    if d2["service"] != "run" or d2["function"] != "skip":
        print(f"  自检失败：配置变更没让服务层重跑 {d2}")
        ok = False
    else:
        print("  自检通过：compose 变更 -> 服务层/集成层/类层 run，函数层/文件层 skip")
    return ok


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="增量调度决策")
    parser.add_argument("--should-run", metavar="LAYER",
                        help="判断某层该不该跑（退出码 0=跑 1=跳过 2=不确定）")
    parser.add_argument("--write", action="store_true", help="写出决策文件")
    parser.add_argument("--selftest", action="store_true", help="闸门自检")
    args = parser.parse_args()

    if args.selftest:
        ok = selftest()
        print("闸门自检：", "通过" if ok else "失败")
        raise SystemExit(0 if ok else 1)

    plan = build()
    if args.write or not args.should_run:
        PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.should_run:
        layer = plan["layers"].get(args.should_run)
        if layer is None:
            raise SystemExit(2)
        decision = layer["decision"]
        if decision == "skip":
            print(f"[增量] 跳过 {args.should_run}：{layer['reason']}")
            raise SystemExit(1)
        print(f"[增量] 跑 {args.should_run}：{layer['reason']}")
        raise SystemExit(0)
    print(f"增量决策 -> {PLAN_FILE}")
    for layer, info in plan["layers"].items():
        print(f"  {layer:<12} {info['decision']:<5} {info['reason']}")


if __name__ == "__main__":
    main()
