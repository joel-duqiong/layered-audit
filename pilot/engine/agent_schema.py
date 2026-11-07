"""Agent 群的数据契约（通用引擎）。

三个概念，**不要混**：

- **unit（单元）**：可寻址的代码实体 —— 方法 / 类 / 文件 / 服务 / 端到端流程。
  unit 是事实，不是 Agent。
- **agent_role（角色）**：判定手段的绑定，来自层契约的 category + oracle。
  角色数恒定（小），不随代码规模增长。
- **agent_instance（实例）**：(role, unit) 二元组 —— 「某个判定角色施加于某个单元」。
  实例数随代码规模线性增长，是 Agent 群的基本单位。

Agent **不是 finding**：finding 是结论，agent 是承载结论的主体。
一个单元可以挂多个角色（= 多个 Agent），一个 Agent 可以产出多条 finding。

本模块只定义结构与校验，不产出数据。数据由 build_agent_graph.py 构造。
"""
from __future__ import annotations

import re
from typing import Any

AGENT_ID_RE = re.compile(r"^(fn|file|cls|svc|flow):\S+$")
ID_PREFIX = {"method": "fn", "class": "cls", "file": "file", "service": "svc", "flow": "flow"}

FIDELITY = ("container", "hybrid", "stubbed", "static", "unknown")
OBLIGATION_STATUS = ("satisfied", "violated", "unknown", "not_exercised")
RELATION = ("call", "type_use", "file_use", "http", "runtime", "cross_repo")
RESOLUTION = ("static", "inferred", "observed", "unknown")
STABILITY = ("signature", "behavior", "semantic")


def agent_id(unit_kind: str, unit_key: str) -> str:
    """unit 的全局唯一标识。前缀标明单元种类（fn/cls/file/svc/flow）。"""
    return f"{ID_PREFIX.get(unit_kind, unit_kind)}:{unit_key}"


def validate_agent(inst: dict[str, Any]) -> list[str]:
    """校验一个 agent 实例。返回问题清单，空列表 = 通过。"""
    problems: list[str] = []
    for key in ("agent_id", "role_id", "layer", "unit", "mission", "static_life",
                "dynamic_life", "relations", "conclusions", "state"):
        if key not in inst:
            problems.append(f"缺少字段 {key}")
    if problems:
        return problems

    if not AGENT_ID_RE.match(str(inst["agent_id"])):
        problems.append(f"agent_id 格式非法: {inst['agent_id']}")
    if inst["layer"] not in ("function", "file", "class", "service", "integration"):
        problems.append(f"layer 非法: {inst['layer']}")

    unit = inst["unit"]
    if not unit.get("unit_key"):
        problems.append("unit.unit_key 为空")

    mission = inst["mission"]
    if not mission.get("statement"):
        problems.append("mission.statement 为空")
    if mission.get("source") not in ("declared", "derived", "model_hint"):
        problems.append(f"mission.source 非法: {mission.get('source')}")
    if mission.get("source") == "model_hint" and mission.get("gate_eligible"):
        problems.append("model_hint 的使命不得进门禁（gate_eligible 必须为 false）")

    for ob in mission.get("obligations", []):
        for key in ("obligation_id", "statement", "status", "source"):
            if key not in ob:
                problems.append(f"义务缺少字段 {key}")
        if ob.get("status") not in OBLIGATION_STATUS:
            problems.append(f"义务状态非法: {ob.get('status')}")
        if ob.get("source") == "model_hint" and ob.get("gate_eligible"):
            problems.append(f"义务 {ob.get('obligation_id')} 是 model_hint，不得进门禁")
        if ob.get("status") in ("satisfied", "violated") and not ob.get("evidence_refs"):
            problems.append(f"义务 {ob.get('obligation_id')} 判定为成立/违反但没有证据引用")

    dyn = inst["dynamic_life"]
    if dyn.get("fidelity") not in FIDELITY:
        problems.append(f"dynamic_life.fidelity 非法: {dyn.get('fidelity')}")

    conc = inst["conclusions"]
    for key in ("agent_status", "fulfillment", "coverage"):
        if key not in conc:
            problems.append(f"conclusions 缺少 {key}")
    status = conc.get("agent_status")
    if status not in ("PASS", "FAIL", "UNKNOWN", "STALE"):
        problems.append(f"agent_status 非法: {status}")
    if status == "PASS" and conc.get("fulfillment", {}).get("unknown", 0):
        problems.append("有未知义务时不能判 PASS（必须三值逻辑）")

    return problems


def validate_edge(edge: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("edge_id", "kind", "from_agent_id", "to_agent_id", "resolution", "stability"):
        if key not in edge:
            problems.append(f"边缺少字段 {key}")
    if problems:
        return problems
    if edge["kind"] not in RELATION:
        problems.append(f"边类型非法: {edge['kind']}")
    if edge["resolution"] not in RESOLUTION:
        problems.append(f"解析状态非法: {edge['resolution']}")
    if edge["stability"] not in STABILITY:
        problems.append(f"稳定性非法: {edge['stability']}")
    return problems
