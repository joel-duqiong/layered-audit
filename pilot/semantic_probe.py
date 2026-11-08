"""语义地图探针 —— 把「每个单元该干什么」编译成可执行断言并跑一遍。

**产出三个数字**（这才是这套东西的价值）：
  1. 有多少单元有使命（语义地图覆盖了多少单元）
  2. 有多少使命被验证为**达成**（断言通过）
  3. 有多少使命被**违反**（断言失败 = 这条使命对应的真实缺陷）

断言直接复用函数层已有的执行器（fn_dynamic 的 case 格式 + evaluate 判定），
**不新增执行器语义** —— 编译产物就是普通用例。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import project

sys.path.insert(0, project.engine_path())

from obligations import CompileError, compile_map, validate_case  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT_CASES = HERE / "semantic_cases.json"
OUT_REPORT = HERE / "semantic_map_report.json"
MAP_FILE = HERE / "semantic_map.json"
MODEL_MAP_FILE = HERE / "semantic_map_model.json"


def main() -> None:
    semantic = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    cases_file = project.project_dir() / project.path("fn_cases", "fn_cases.json")
    spec = json.loads(cases_file.read_text(encoding="utf-8"))
    ctx = {"fixtures": spec.get("fixtures", {}), "types": spec.get("types", {})}

    missions = semantic.get("missions", [])
    cases, failures = compile_map(missions, ctx)

    # 模型轨的候选：单独编译、单独统计。
    # 它们的 group 是 SEMANTIC_OBLIGATION_MODEL —— 函数层差分只认
    # SEMANTIC_OBLIGATION，所以模型轨**不会开出缺陷单**（只作线索）。
    model_missions: list[dict[str, Any]] = []
    if MODEL_MAP_FILE.exists():
        model_map = json.loads(MODEL_MAP_FILE.read_text(encoding="utf-8"))
        model_missions = model_map.get("missions", [])
    model_cases, model_failures = compile_map(model_missions, ctx)
    cases = cases + model_cases

    decl_path = project.artifact_dir() / "fn_declaration.json"
    declarations: dict[str, Any] = {}
    if decl_path.exists():
        for rec in json.loads(decl_path.read_text(encoding="utf-8")).get("functions") or []:
            declarations[f"{rec.get('class')}#{rec.get('method')}"] = rec

    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for case in cases:
        problems = validate_case(case, declarations)
        if problems:
            rejected.append({"id": case["id"], "problems": problems})
        else:
            valid.append(case)

    OUT_CASES.write_text(json.dumps({"cases": valid}, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    units_with_mission = {m.get("unit") for m in missions if m.get("unit")}
    report = {
        "missions_total": len(missions),
        "compiled_ok": len(valid),
        "compile_failed": failures,
        "rejected_by_validation": rejected,
        "units_with_mission": sorted(x for x in units_with_mission if x),
        "source_mix": {},
        "model_track": {
            "missions": len(model_missions),
            "compiled_ok": len(model_cases),
            "compile_failed": model_failures,
            "gate_eligible": False,
            "note": "模型轨只产出待验证线索：不进缺陷台账、不进门禁；"
                    "要进门禁得人工冻结到 semantic_map.json（决议 D3）",
        },
    }
    for m in missions + model_missions:
        report["source_mix"][m.get("source", "declared")] = \
            report["source_mix"].get(m.get("source", "declared"), 0) + 1
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"语义地图 -> {OUT_REPORT}")
    print(f"  职责 {len(missions)} 条，覆盖单元 {len(report['units_with_mission'])} 个")
    print(f"  编译通过 {len(valid)} / 编译失败 {len(failures)} / 校验不通过 {len(rejected)}")
    mt = report["model_track"]
    print(f"  模型轨：候选 {mt['missions']} 条 / 编译通过 {mt['compiled_ok']} "
          f"/ 编译失败 {len(mt['compile_failed'])}（只作线索，不进门禁）")
    print(f"  产出可执行用例 -> {OUT_CASES}")
    for f in failures:
        print(f"  编译失败 {f['id']}: {f['error']}")
    for r in rejected:
        print(f"  校验不通过 {r['id']}: {r['problems']}")


if __name__ == "__main__":
    main()
