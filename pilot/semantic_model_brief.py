"""模型任务书生成器（项目侧）。

给「模型轨」准备输入：**只给事实与封闭的模板目录，不给任何结论**。

设计依据（DESIGN §1.2 / §1.3）：
  · 承重字段只有 template_id / polarity / slots / when / assertions；
    `statement`（自然语言）不参与任何判定 —— 所以任务书里可以给模型
    「用自然语言描述职责」的余地，但**不能**给它结论。
  · 模型只能从**封闭的模板目录**里选（engine/obligations.py 的 TEMPLATES）；
    编译器有四道闸：模板存在 / 符号可解析 / 断言类型执行器认识 / 方法签名对得上。
  · 模型轨产出的是**候选**（source=model_hint, gate_eligible=false），
    判定一律由确定性层做。

产出：semantic_model_brief.json
  · units：待提职责的单元 + 它们的代码事实
  · catalog：可选的模板目录（含每种模板要的参数形状与极性）
  · rules：给模型的硬约束
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import project

sys.path.insert(0, project.engine_path())

from obligations import SUPPORTED_CHECK_TYPES, TEMPLATES  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "semantic_model_brief.json"
TOP_N = 12

# 每种模板的参数形状 —— 模型照着填，填错了编译器会拒
ARG_SHAPES = {
    "returns_le_arg": "args = [入参原值..., 上界值]；bound = {kind: arg, index: <上界所在的 args 下标>}。"
                      "含义：把上界那个入参换成越界值，期望返回值不超过上界。",
    "returns_ge_arg": "args = [入参原值..., 下界值]；bound = {kind: arg, index: <下界下标>}。"
                      "含义：把下界那个入参换成越界值，期望返回值不小于下界。",
    "returns_eq_const": "args = [入参原值...]；bound = {kind: const, value: <期望的确定值>}。",
    "throws_business_code": "args = [非法入参...]；bound = {kind: const, value: <期望的业务错误码>}。",
    "returns_null": "args = [空/未指定入参...]；bound 不需要。期望返回 null。",
    "not_null": "args = [入参原值...]；bound 不需要。期望返回非 null。",
}

RULES = [
    "只能从 catalog.templates 里挑模板 —— 目录外的模板名会被编译器拒绝。",
    "只能填 template / args / bound / stubs 这几个承重字段；"
    "mission 与 label 是自由文本，**不参与判定**，写错也不会让结论变。",
    "不要下结论：你的产物是「待验证的候选」，判定由确定性层执行断言后给出。",
    "args 里每个元素用用例文件的值规格：{\"$t\": \"long|bool|str|entity|fixture\", \"v\": ...}；"
    "实体用 {\"$t\": \"entity\", \"class\": ..., \"fields\": {...}}。",
    "只提你有把握的：事实不足就不提 —— 提一条编造的断言比不提更糟。",
    "**目标方法会调仓储/客户端时，必须在 stubs 里给它打桩** —— 不打桩的话调用会打到"
    "没连库的仓储，返回全是 null，你的断言会变成无意义的假失败。",
    "**入参优先用 fixtures 里现成的 id**（见 fixtures 目录）—— 自己编的 id 在库里不存在，"
    "断言同样会假失败。形状照抄 worked_example。",
    "每个单元的 `entity_schemas` 是**它会碰到的实体的字段结构** —— "
    "造实体要给**全字段**（缺一个字段，MapStruct 映射出来的响应就是 null）。",
    "每个单元的 `stub_examples` 是**它要调的仓储/客户端在别处怎么打桩** —— "
    "**照抄这个形状**。打桩形状错了断言会假失败：findById 要返回 Optional"
    "（给裸实体会让 .orElseThrow 炸掉 → 返回 null → 「期望非 null」永远失败）。",
    "每个单元的 `existing_cases_same_class` 是**同一个类上已经跑得通的用例** —— "
    "照抄它们的**形状**（入参用什么 fixture、仓储怎么打桩），不要照抄内容。"
    "打桩不全的断言会假失败（返回全是 null），这是上一轮 12 条候选里 10 条报废的原因。",
    "bound 只在模板需要时给（returns_le_arg / returns_ge_arg / returns_eq_const / "
    "throws_business_code）；not_null 与 returns_null **不要给 bound**（写了 {} 也行，"
    "编译器会当成没有）。",
]


def _arg_specs(fn: dict[str, Any]) -> list[dict[str, Any]]:
    """给模型的入参形状提示：参数类型 + 该方法里出现过的常量。"""
    out = []
    for i, p in enumerate(fn.get("params") or []):
        out.append({"index": i, "type": p})
    return out


def _entity_schemas(fn: dict[str, Any],
                     schemas: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """这个方法会碰到的实体的字段结构。

    造实体要**全字段**：少一个字段，MapStruct 映射出来的响应对象就是 null ——
    断言会看到「期望非 null，实际 None」，而真因是打桩的实体不完整。
    """
    out = []
    for call in sorted(set(fn.get("internal_calls") or [])):
        fq = call.rsplit(".", 1)[0]
        simple = fq.rsplit(".", 1)[-1]
        cls = fq if fq in schemas else (simple if simple in schemas else "")
        if cls and cls not in [o["class"] for o in out]:
            out.append({"class": cls, "fields": schemas[cls]})
    return out[:3]


def _stub_examples(fn: dict[str, Any],
                   stub_examples: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """这个方法会调的仓储/客户端，各自在别处是怎么打桩的。

    打桩形状错了，断言就会假失败：findById 返回裸实体（不是 Optional）时
    .orElseThrow 会炸 → 方法返回 null → 「期望非 null」永远失败。
    """
    calls = set(fn.get("internal_calls") or []) | set(fn.get("external_calls") or [])
    out: list[dict[str, Any]] = []
    for call in sorted(calls):
        iface = call.rsplit(".", 1)[0]
        if iface in stub_examples:
            out.extend(stub_examples[iface][:2])
    return out[:4]


def build() -> dict[str, Any]:
    units = json.loads((project.artifact_dir() / "units.json").read_text(encoding="utf-8"))["units"]
    cases_file = project.project_dir() / project.path("fn_cases", "fn_cases.json")
    spec = json.loads(cases_file.read_text(encoding="utf-8"))
    fns = json.loads((project.artifact_dir() / "fn_declaration.json").read_text(encoding="utf-8"))
    semantic = json.loads((HERE / "semantic_map.json").read_text(encoding="utf-8"))
    have = {m.get("unit") for m in semantic.get("missions", [])}

    by_key = {f"{f.get('class')}#{f.get('method')}": f for f in fns.get("functions") or []}

    # 只挑**有写副作用**的单元：业务规则（该拒绝什么、该守住什么不变式）
    # 住在会改状态的方法里；纯读方法的「职责」多半只是「返回查询结果」，
    # 提不出有信息量的断言。
    # 「副作用」把仓储读取也算进去了 —— 真正的写信号是**调用了写方法**
    # （save / insert / update / delete / set*）。业务规则住在写方法里。
    _WRITES = ("save", "insert", "update", "delete", "set")
    fn_all = {f"{f.get('class')}#{f.get('method')}": f
              for f in (json.loads((project.artifact_dir() / "fn_declaration.json")
                                   .read_text(encoding="utf-8")).get("functions") or [])}

    def _is_writer(u: dict[str, Any]) -> bool:
        fn = fn_all.get(u.get("unit_key") or "", {})
        return any(str(c).rsplit(".", 1)[-1].startswith(_WRITES)
                   for c in (fn.get("internal_calls") or []))

    cands = [u for u in units
             if u.get("layer") == "function"
             and u.get("unit_key") not in have
             and u.get("status") == "UNKNOWN"
             and (u.get("logic") or {}).get("score", 0) > 0
             and _is_writer(u)]
    cands.sort(key=lambda u: -u.get("priority", 0))
    picked = cands[:TOP_N]

    # 错误码目录 —— 「拒绝语义」是这个代码库里**可枚举**的业务语义：
    # 方法抛出 BusinessException(ErrorCode.X, "消息")，错误码与消息都是有限的。
    # 模型可以据此回答「这个方法应该拒绝什么、给哪个码」。
    svc_decl = json.loads((project.artifact_dir() / "declaration.json").read_text(encoding="utf-8"))
    error_codes = list((svc_decl.get("global") or {}).get("error_codes") or [])

    # 实体的字段结构 —— 模型要造一个「像样的实体」，缺字段的话
    # MapStruct 映射出来的响应就是 null（又是一次假失败）。
    # 用**文件层静态分析的字段表**（完整的 Java 字段），不用 DDL 的列
    # （DDL 只含表列，实体还有继承来的 id/is_deleted 等）。
    file_decl = json.loads((project.artifact_dir() / "file_declaration.json").read_text(encoding="utf-8"))
    entity_schemas: dict[str, list[dict[str, Any]]] = {}
    for rec in file_decl.get("files") or []:
        cls = rec.get("class") or ""
        simple = cls.rsplit(".", 1)[-1]
        fields = [{"field": name, "java_type": (info or {}).get("type")}
                  for name, info in (rec.get("fields") or {}).items()]
        if fields:
            entity_schemas[cls] = fields
            entity_schemas.setdefault(simple, fields)

    # 同一方法上**已有的、跑得通的用例** —— 最好的接地材料：
    # 模型可以照抄形状（打桩怎么写、入参用哪个 fixture），但不能照抄内容。
    existing: dict[str, list[dict[str, Any]]] = {}
    by_class: dict[str, list[dict[str, Any]]] = {}
    for c in spec.get("cases", []) or []:
        rec = {
            "id": c.get("id"),
            "method": c.get("method"),
            "args": c.get("args"),
            "stubs": c.get("stubs"),
            "check": c.get("check"),
            "invariant": c.get("invariant"),
        }
        target = f"{c.get('class')}#{c.get('method')}"
        existing.setdefault(target, []).append(rec)
        by_class.setdefault(c.get("class") or "", []).append(rec)

    # **同一个仓储/客户端接口在别处是怎么打桩的** —— 最关键的一层接地材料。
    # 模型不知道 findById 要返回 Optional（给裸实体会让 .orElseThrow 炸掉 → 返回 null
    # → 断言变成假失败），而现成的用例里有正确的形状。
    stub_examples: dict[str, list[dict[str, Any]]] = {}
    for c in spec.get("cases", []) or []:
        for iface, shape in (c.get("stubs") or {}).items():
            stub_examples.setdefault(iface, []).append({
                "case_id": c.get("id"),
                "method": c.get("method"),
                "stub": {iface: shape},
            })
    for c in spec.get("cases", []) or []:
        for iface in (c.get("stubs") or {}):
            pass

    out_units = []
    for u in picked:
        fn = by_key.get(u["unit_key"], {})
        out_units.append({
            "unit": u["unit_key"],
            "class": u.get("class"),
            "method": u.get("method"),
            "why_this_one": {
                "centrality": u.get("centrality"),
                "logic_score": (u.get("logic") or {}).get("score"),
                "branches": (u.get("logic") or {}).get("branches"),
                "side_effects": (u.get("logic") or {}).get("side_effects"),
                "null_guards": (u.get("logic") or {}).get("null_guards"),
            },
            "signature": {
                "returns": fn.get("returns"),
                "params": fn.get("params") or [],
                "arity": fn.get("arity"),
                "arg_slots": _arg_specs(fn),
            },
            # 同一方法上的用例（盲区单元通常是空的）；
            # 真正有用的是**同一个类的其它方法**上跑通过的用例 —— 那里有仓储的打桩形状。
            "existing_cases_same_class": by_class.get(u.get("class") or "", [])[:3],
            # 这个方法要调的仓储/客户端，各自在别处是怎么打桩的
            "stub_examples": _stub_examples(fn, stub_examples),
            # 这个方法会碰到的实体，它们的字段结构（造实体要用全字段）
            "entity_schemas": _entity_schemas(fn, entity_schemas),
            # 这个方法里出现的错误消息（业务拒绝的语义线索）
            "error_messages": [c.get("value") for c in (fn.get("constants") or [])
                               if isinstance(c, dict) and c.get("kind") == "String"
                               and ("不存在" in str(c.get("value")) or "已被" in str(c.get("value"))
                                    or "不能" in str(c.get("value")) or "超出" in str(c.get("value"))
                                    or "不是" in str(c.get("value")))][:4],
            "existing_cases_on_this_method": existing.get(u["unit_key"], [])[:2],
            "facts": {
                "internal_calls": fn.get("internal_calls") or [],
                "external_calls": fn.get("external_calls") or [],
                "side_effects": fn.get("side_effects") or [],
                "entity_field_writes": fn.get("entity_field_writes") or [],
                "constants": fn.get("constants") or [],
                "caught_exceptions": fn.get("caught_exceptions") or [],
                "throws_business_exception": fn.get("throws_business_exception"),
                "is_pure": fn.get("is_pure"),
            },
        })

    # 可用的 fixture 目录 —— 模型要用真实的 id，否则调用打到了没连库的仓储，
    # 返回全是 null，断言就变成「期望非 null，实际 None」的假失败。
    fixtures = spec.get("fixtures", {}) if isinstance(spec, dict) else {}
    fixture_catalogue = {
        name: {
            "fields": list((f or {}).keys()),
            "sample": (f or {}).get("value") if isinstance(f, dict) else None,
        }
        for name, f in fixtures.items()
    } if isinstance(fixtures, dict) else {}

    catalog = {
        "templates": [
            {
                "id": tid,
                "label": tpl["label"],
                "check_type": tpl["check"]["type"],
                "bound_from": tpl["bound_from"],
                "polarity": tpl.get("negation"),
                "arg_shape": ARG_SHAPES.get(tid, ""),
            }
            for tid, tpl in sorted(TEMPLATES.items())
        ],
        "supported_check_types": sorted(SUPPORTED_CHECK_TYPES),
    }

    payload = {
        "note": ("模型轨的任务书。**只有事实与封闭的模板目录，没有结论。**"
                 "模型产出的是候选（source=model_hint, gate_eligible=false），"
                 "判定由确定性层做。"),
        "generated_from": "semantic_model_brief.py",
        "units": out_units,
        "fixtures": fixture_catalogue,
        "error_codes": error_codes,
        "worked_example": {
            "note": "一个带打桩的用例长什么样（形状照抄，不要照抄内容）",
            "case": {
                "id": "OB-MODEL-EXAMPLE-1",
                "group": "SEMANTIC_OBLIGATION_MODEL",
                "func": "reflect",
                "class": "<目标类全名>",
                "method": "<方法名>",
                "args": [{"$t": "fixture", "v": "<fixture 名>"}],
                "check": {"type": "returns_non_null"},
                "stubs": {"<被打桩的接口全名>": {"<方法名>": {"$optional": {"$t": "entity", "class": "<实体全名>", "fields": {}}}}},
                "invariant": "<自由文本>",
                "label": "<自由文本>",
            },
        },
        "catalog": catalog,
        "rules": RULES + [
        "**这一轮的任务形态**：优先回答「**这个方法在什么输入下应该拒绝、拒绝时给哪个错误码**」"
        "（用 throws_business_code 模板，错误码从 error_codes 里选）—— "
        "「应该拒绝什么」是这个代码库里可枚举的业务语义，比「应该返回什么」更贴近职责。"
        "只有在你确实有把握时才用 returns_* 模板。",
            "**harness 的默认行为**（不写就按这个来）："
            "仓储/客户端方法**不打桩时返回 Optional.empty()** —— "
            "所以任何 `xxxRepository.findById(id).orElseThrow(...)` 的方法在默认打桩下"
            "会抛异常，你的断言会看到「返回 null」而假失败。"
            "要让方法走通，仓储方法必须显式打桩，且 Optional 形状要用 "
            "**{\"$optional\": {...}}** 包一层。"
            "（实体本身用 {\"$t\": \"entity\", \"class\": ..., \"fields\": {...}}。）",
        ],
        "output_contract": {
            "file": "semantic_map_model.json",
            "shape": {
                "missions": [{
                    "id": "OB-MODEL-<单元简名>-<序号>",
                    "unit": "<unit_key>",
                    "class": "<类全名>",
                    "method": "<方法名>",
                    "group": "SEMANTIC_OBLIGATION_MODEL",
                    "label": "<自由文本，不参与判定>",
                    "mission": "<自由文本，不参与判定>",
                    "template": "<catalog.templates 里的 id>",
                    "args": [],
                    "bound": {},
                    "stubs": {},
                    "source": "model_hint",
                    "gate_eligible": False,
                }],
            },
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    p = build()
    print(f"模型任务书 -> {OUT}")
    print(f"  待提职责的单元 {len(p['units'])} 个（从盲区地图里按中心性 × 逻辑密度选的）")
    print(f"  可选模板 {len(p['catalog']['templates'])} 个："
          f"{', '.join(t['id'] for t in p['catalog']['templates'])}")
    for u in p["units"]:
        print(f"    {u['unit'][:62]:64s} 分支{u['why_this_one']['branches']} "
              f"副作用{u['why_this_one']['side_effects']}")


if __name__ == "__main__":
    main()
