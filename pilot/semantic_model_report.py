"""模型轨报告：候选断言跑完之后，它们**值不值得晋升**。

三件事：
  1. 模型提了哪些候选、编译过没过（四道闸）
  2. 真跑的结果：哪些断言成立、哪些被违反
  3. **隔离性自证**：把模型写的自由文本换成垃圾，编译出的承重部分（断言）
     必须逐字节一致 —— 证明「模型的自然语言进不了判定」（决议 D2）

模型轨的结论是**线索**，不是缺陷单：它不进 all_findings、不进门禁。
要进门禁，必须人工把候选冻结进 semantic_map.json（决议 D3）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import project

sys.path.insert(0, project.engine_path())

from obligations import compile_map  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL_MAP_FILE = HERE / "semantic_map_model.json"
OBS_FILE = HERE / "fn_observation.json"
OUT = HERE / "semantic_model_report.json"
MODEL_GROUP = "SEMANTIC_OBLIGATION_MODEL"


def _load(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def isolation_check(missions: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    """把自由文本换成垃圾，编译出的断言必须一模一样。

    这是「模型只出断言草案、判定由确定性层做」的**可执行证明**：
    mission / label 随便写都不影响 check（承重字段）。
    """
    poisoned = []
    for m in missions:
        m2 = copy.deepcopy(m)
        m2["mission"] = "这段文字是垃圾，用来证明它不参与判定 ###"
        m2["label"] = "垃圾标签 🎯"
        poisoned.append(m2)
    real_cases, _ = compile_map(missions, ctx)
    poison_cases, _ = compile_map(poisoned, ctx)
    same = len(real_cases) == len(poison_cases) and all(
        json.dumps(a.get("check"), sort_keys=True, ensure_ascii=False)
        == json.dumps(b.get("check"), sort_keys=True, ensure_ascii=False)
        for a, b in zip(real_cases, poison_cases))
    return {
        "checked": len(real_cases),
        "assertions_identical_after_poisoning_text": same,
        "note": "承重字段只有 template/args/bound/stubs；mission 与 label 是自由文本",
    }


def main() -> None:
    model_map = _load(MODEL_MAP_FILE, {}) or {}
    missions = model_map.get("missions", [])
    cases_file = project.project_dir() / project.path("fn_cases", "fn_cases.json")
    spec = _load(cases_file, {}) or {}
    ctx = {"fixtures": spec.get("fixtures", {}), "types": spec.get("types", {})}

    obs = _load(OBS_FILE, {}) or {}
    by_case = {c.get("id"): c for c in obs.get("cases", [])}

    rows = []
    for m in missions:
        c = by_case.get(m.get("id"))
        rows.append({
            "id": m.get("id"),
            "unit": m.get("unit"),
            "template": m.get("template"),
            "mission": m.get("mission"),
            "compiled": c is not None,
            "verdict": (c or {}).get("verdict"),
            "reason": (c or {}).get("reason"),
            "gate_eligible": False,
        })

    violated = [r for r in rows if r.get("verdict") == "FAIL"]
    passed = [r for r in rows if r.get("verdict") == "PASS"]
    isolation = isolation_check(missions, ctx) if missions else {}

    payload = {
        "note": "模型轨报告：候选断言的真跑结果 + 隔离性自证。"
                "结论是线索，不进缺陷台账、不进门禁。",
        "candidates": len(missions),
        "executed": len([r for r in rows if r["compiled"]]),
        "passed": len(passed),
        "violated": len(violated),
        "promotion_candidates": [
            r for r in violated
            # 只推「模型断言被违反」的候选：那才是真缺陷的线索
        ],
        "isolation_check": isolation,
        "rows": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"模型轨报告 -> {OUT}")
    print(f"  候选 {len(missions)} 条｜跑起来 {payload['executed']} 条"
          f"｜成立 {len(passed)}｜被违反 {len(violated)}")
    if isolation:
        print(f"  隔离性自证：自由文本换成垃圾后断言一致 = "
              f"{isolation['assertions_identical_after_poisoning_text']}")
    for r in rows:
        print(f"    [{r['verdict'] or '未跑':4s}] {r['id'][:44]:46s} {r['template']}")


if __name__ == "__main__":
    main()
