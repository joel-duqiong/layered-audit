"""义务编译器 —— 把「这个单元该干什么」编译成**可执行断言**。

**为什么要有这一步**：一个 Agent 的「使命」如果只是自然语言，它就没法被验证，
也就没法进门禁。所以：**职责 = 模板 + 槽位 + 极性**，编译器把它变成一条
**能真的跑起来的断言**（复用函数层已有的执行器，不新增执行器语义）。

分工（这条边界很重要）：
  人 / 模型  ->  提出职责草案：选模板 + 填槽位 + 极性
  编译器    ->  把槽位解析成真实值，校验断言自洽，产出可执行 case
  确定性层  ->  执行并判定
  模型产出  ->  gate_eligible=false（永远是线索，不直接进门禁）

**编译器不做的事**：不发明规则、不写期望值数字 —— 期望值要么来自既有声明
（实体/表/快照），要么来自人工冻结。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 封闭的断言模板目录：模型只能从这里选，不能自创 kind
TEMPLATES: dict[str, dict[str, Any]] = {
    "returns_le_arg": {
        "label": "返回值不得超过某个上界",
        "check": {"type": "returns_le"},
        "bound_from": "arg",
        "negation": "le",
    },
    "returns_ge_arg": {
        "label": "返回值不得小于某个下界",
        "check": {"type": "returns_ge"},
        "bound_from": "arg",
        "negation": "ge",
    },
    "returns_eq_const": {
        "label": "返回值必须等于某个确定值",
        "check": {"type": "returns_eq"},
        "bound_from": "const",
    },
    "throws_business_code": {
        "label": "非法输入必须以指定业务错误码拒绝",
        "check": {"type": "throws_business"},
        "bound_from": "const",
    },
    "returns_null": {
        "label": "空/未指定输入必须返回 null（不过滤）",
        "check": {"type": "returns_null"},
        "bound_from": "none",
    },
    "not_null": {
        "label": "必须返回非 null",
        "check": {"type": "returns_non_null"},
        "bound_from": "none",
    },
}

SUPPORTED_CHECK_TYPES = {
    "returns_eq", "returns_ge", "returns_le", "returns_gt", "returns_lt", "returns_null",
    "returns_non_null", "throws_business", "state_eq", "state_ge", "state_le", "state_not_null",
    "snapshot_empty", "snapshot_items_count", "map_size", "map_eq", "map_keys_numeric",
    "map_values_ge", "date_range", "upload_no_file_written", "upload_path_contained",
    "upload_size_le", "burst_unique", "call_count_eq", "call_count_at_least",
    "entity_field_null",
}


class CompileError(Exception):
    """编译失败 —— 带着原因，不要静默丢弃。"""


def _resolve_bound(spec: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """把槽位解析成真实值。槽位只能来自既有声明，不能凭空来。"""
    kind = spec.get("kind")
    if kind == "const":
        if "value" not in spec:
            raise CompileError(f"常量槽位缺 value: {spec}")
        return spec["value"]
    if kind == "fixture":
        fx = ctx.get("fixtures", {}).get(str(spec.get("id")))
        if fx is None:
            raise CompileError(f"样本不存在: {spec.get('id')}")
        if "field" not in spec:
            return fx
        value = fx.get(spec["field"]) if isinstance(fx, dict) else None
        if value is None:
            raise CompileError(f"样本字段取不到值: {spec}")
        return value
    if kind == "arg":
        idx = spec.get("index")
        if idx is None:
            raise CompileError(f"参数槽位缺 index: {spec}")
        return spec.get("value")
    raise CompileError(f"未知的槽位类型: {kind}")


def compile_mission(mission: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """把一条职责编译成可执行 case。失败抛 CompileError（带原因）。"""
    template_id = mission.get("template")
    template = TEMPLATES.get(template_id)
    if template is None:
        raise CompileError(f"未知模板: {template_id}")

    args = list(mission.get("args") or [])
    bound_spec = mission.get("bound")
    bound_value = None
    if bound_spec is not None:
        if bound_spec.get("kind") == "arg":
            # 上界就是某个入参：当前值就是上界（可用 limit 改写期望值）
            idx = bound_spec.get("index")
            if idx is None or idx >= len(args):
                raise CompileError(f"上界入参下标越界: {bound_spec}")
            bound_value = args[idx]
        else:
            bound_value = _resolve_bound(bound_spec, ctx)

    check = dict(template["check"])
    if template["bound_from"] == "const" and bound_value is not None:
        check["value"] = bound_value
    if template["bound_from"] == "arg" and bound_value is not None:
        # 上界就是某个入参：把这个入参换成一个「越界」的值，期望被拒或被限住
        idx = bound_spec["index"]
        if "probe_value" in bound_spec:
            args[idx] = bound_spec["probe_value"]
        check["value"] = bound_spec.get("limit", bound_value)
    if check.get("type") not in SUPPORTED_CHECK_TYPES:
        raise CompileError(f"断言类型执行器不支持: {check.get('type')}")

    case_id = mission.get("id") or f"OB-{template_id}-{mission.get('unit', '')}"
    return {
        "id": case_id,
        "group": mission.get("group", "SEMANTIC_OBLIGATION"),
        "func": "reflect",
        "class": mission.get("class", ""),
        "method": mission.get("method", ""),
        "args": args,
        "check": check,
        "invariant": mission.get("mission", ""),
        "label": mission.get("label", ""),
        "stubs": mission.get("stubs", {}),
    }


def compile_map(missions: list[dict[str, Any]],
                ctx: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """编译一批职责 -> (可执行 case, 编译失败的记录)。"""
    cases: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for mission in missions:
        try:
            cases.append(compile_mission(mission, ctx))
        except CompileError as exc:
            failures.append({"id": mission.get("id"), "template": mission.get("template"),
                             "error": str(exc)})
    return cases, failures


def validate_case(case: dict[str, Any], declarations: dict[str, Any]) -> list[str]:
    """校验编译出的 case 真能跑：方法存在、参数个数对得上、断言类型执行器认识。"""
    problems: list[str] = []
    target = f"{case.get('class')}#{case.get('method')}"
    fact = declarations.get(target)
    if fact is None:
        problems.append(f"目标方法不存在: {target}")
    else:
        arity = len(fact.get("params") or [])
        if arity != len(case.get("args") or []):
            problems.append(f"参数个数不符: 声明 {arity} / 用例 {len(case.get('args') or [])}")
    if case.get("check", {}).get("type") not in SUPPORTED_CHECK_TYPES:
        problems.append(f"断言类型不认识: {case.get('check', {}).get('type')}")
    if not case.get("class") or not case.get("method"):
        problems.append("case 缺 class/method")
    return problems
