"""执行预算 —— 把层契约里的 `agents.constraint` 真正接上。

契约原文（layers.yaml）：
    max_agents: 12            一轮最多同时进行的「判定作业」数
    max_tokens_per_round: 2000000   每轮 token 预算（只约束将来的模型轨）
    diversity_source: bound_oracle  多样性来自绑定的判定手段，不是 prompt 人设

**语义边界（必须写死，否则预算一开就会把确定性工作也卡掉）**：
- `max_tokens_per_round` 只约束**模型轨**；确定性轨（探测 + 判定）token 恒为 0，不受它约束。
- `max_agents` 约束**一轮里被 admit 的作业数**（不是 Agent 实例总数 —— 实例总数随代码规模
  线性增长，限制总数等于限制系统规模，没意义）。
- **预算耗尽 ≠ 跳过**：被挤掉的进 deferred 队列，报告里显式列出，覆盖度矩阵能看到它们。
  「没查」必须与「查了没问题」区分开。

排序键必须**确定性**：先按 oracle 多样性保底，再按优先级填满。不得引入随机性或模型打分。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeTask:
    """一个判定作业：对某个单元用某种判定手段做一次判定。"""
    task_id: str
    script: str
    oracle: str
    layer: str
    argv: tuple[str, ...] = ()
    tokens: int = 0          # 0 = 确定性轨
    priority: float = 0.0
    unit_hint: str = ""


@dataclass
class Admission:
    admitted: list[ProbeTask] = field(default_factory=list)
    deferred: list[tuple[ProbeTask, str]] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        dist: dict[str, int] = {}
        for t in self.admitted:
            dist[t.oracle] = dist.get(t.oracle, 0) + 1
        return {
            "admitted": [asdict(t) for t in self.admitted],
            "admitted_count": len(self.admitted),
            "deferred": [{"task_id": t.task_id, "reason": r} for t, r in self.deferred],
            "deferred_count": len(self.deferred),
            "oracle_distribution": dict(sorted(dist.items())),
            "tokens_spent": sum(t.tokens for t in self.admitted),
        }


class BudgetLedger:
    """一轮执行的预算闸门。"""

    def __init__(self, max_agents: int, max_tokens_per_round: int,
                 diversity_source: str = "bound_oracle") -> None:
        self.max_agents = max_agents
        self.max_tokens_per_round = max_tokens_per_round
        self.diversity_source = diversity_source

    def admit(self, tasks: list[ProbeTask]) -> Admission:
        result = Admission()
        used_tokens = 0
        # 确定性排序：优先级降序，同优先级按 task_id 升序（完全确定性）
        ordered = sorted(tasks, key=lambda t: (-t.priority, t.task_id))
        chosen: set[str] = set()

        def take(t: ProbeTask) -> bool:
            if t.task_id in chosen:
                return False
            if len(result.admitted) >= self.max_agents:
                return False
            if used_tokens + t.tokens > self.max_tokens_per_round:
                return False
            chosen.add(t.task_id)
            result.admitted.append(t)
            return True

        # 第一轮：每个 oracle 先保一个最高优先任务（多样性保底）
        if self.diversity_source == "bound_oracle":
            seen_oracle: set[str] = set()
            for t in ordered:
                if t.oracle in seen_oracle:
                    continue
                seen_oracle.add(t.oracle)
                take(t)

        # 第二轮：按优先级把剩余名额填满
        for t in ordered:
            if t.task_id in chosen:
                continue
            take(t)

        # 没进来的 → deferred（带原因）
        for t in ordered:
            if t.task_id in chosen:
                continue
            if len(result.admitted) >= self.max_agents:
                reason = "max_agents reached"
            elif used_tokens + t.tokens > self.max_tokens_per_round:
                reason = "max_tokens_per_round reached"
            else:
                reason = "duplicate task_id"
            result.deferred.append((t, reason))
        return result


def load_tasks(path: Path) -> list[ProbeTask]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ProbeTask(
        task_id=t["task_id"], script=t["script"], oracle=t["oracle"], layer=t["layer"],
        argv=tuple(t.get("argv", [])), tokens=int(t.get("tokens", 0)),
        priority=float(t.get("priority", 0.0)), unit_hint=t.get("unit_hint", ""),
    ) for t in data["tasks"]]
