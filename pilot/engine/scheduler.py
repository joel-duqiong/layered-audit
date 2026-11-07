"""调度器 —— 按预算闸门分轮执行探针作业。

**这是 `layers.yaml` 里 `agents.constraint` 的第一个真实消费点。**
在那之前，`max_agents: 12` / `max_tokens_per_round` 写在那里没人读 —— 契约说谎。

一轮怎么跑：
  1. 从 `probe_tasks.json` 读作业目录（项目配置）
  2. 用 BudgetLedger admit：≤ max_agents 个，且**先按 oracle 多样性保底**再按优先级填满
  3. 执行被 admit 的作业
  4. 被挤掉的进 deferred → 下一轮继续（直到没有 deferred）
  5. 产物**并集合并**（永不缩集 —— 少一个就是「没查完」，不是「没问题」）

每轮产出一份 schedule_report.json：admitted / deferred / oracle 分布。
「这一轮查了什么、没查什么」必须可见。
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema
from budget import BudgetLedger, ProbeTask, load_tasks

PROJECT_DIR = findings_schema.target_path().parent
TASKS_FILE = PROJECT_DIR / "probe_tasks.json"
REPORT = PROJECT_DIR / "schedule_report.json"
MERGE_SOURCES = ("config_findings", "mutation_findings")


def _merge_outputs() -> dict[str, int]:
    """把分片产物按 id 并集合并回主文件（并集，不缩集）。"""
    merged: dict[str, int] = {}
    for stem in MERGE_SOURCES:
        main = PROJECT_DIR / f"{stem}.json"
        shards = sorted(PROJECT_DIR.glob(f"{stem}.*.json"))
        if not shards:
            continue
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for src in shards + ([main] if main.exists() else []):
            data = json.loads(src.read_text(encoding="utf-8"))
            for section, rows in data.items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    # 只并「发现」，不并伴生记录（如变异测试的 mutants 列表）
                    if not isinstance(row, dict) or "id" not in row:
                        continue
                    if "severity" not in row:
                        continue
                    key = (section, row["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    buckets[section].append(row)
        payload = {k: v for k, v in buckets.items()}
        # 主文件里原有的伴生段（如 mutants / probes）保留，不丢
        if main.exists():
            original = json.loads(main.read_text(encoding="utf-8"))
            for k, v in original.items():
                if k not in payload:
                    payload[k] = v
        main.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        merged[stem] = len(seen)
        for shard in shards:
            shard.unlink()
    return merged


def run_round(tasks: list[ProbeTask], ledger: BudgetLedger, round_no: int) -> dict[str, Any]:
    admission = ledger.admit(tasks)
    executed: list[dict[str, Any]] = []
    for task in admission.admitted:
        proc = subprocess.run(
            ["python3", task.script, *task.argv],
            capture_output=True, text=True, timeout=1800, cwd=str(PROJECT_DIR))
        executed.append({
            "task_id": task.task_id, "script": task.script, "oracle": task.oracle,
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr.strip()[-200:] if proc.returncode else "",
        })
    return {
        "round": round_no,
        "admission": admission.report(),
        "executed": executed,
        "deferred": [{"task_id": t.task_id, "reason": r} for t, r in admission.deferred],
    }


def main() -> None:
    contract = PROJECT_DIR.parent / "layers.yaml"
    limits = _contract_limits(contract)
    ledger = BudgetLedger(max_agents=limits["max_agents"],
                          max_tokens_per_round=limits["max_tokens_per_round"])
    pending = load_tasks(TASKS_FILE)
    rounds: list[dict[str, Any]] = []
    round_no = 0
    while pending and round_no < 20:
        round_no += 1
        result = run_round(pending, ledger, round_no)
        rounds.append(result)
        done = {e["task_id"] for e in result["executed"] if e["returncode"] == 0}
        pending = [t for t in pending if t.task_id not in done]
        if not pending:
            break
    merged = _merge_outputs()
    report = {
        "limits": limits,
        "tasks_total": len(load_tasks(TASKS_FILE)),
        "rounds": rounds,
        "merged_outputs": merged,
        "all_done": not pending,
        "pending_task_ids": [t.task_id for t in pending],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"调度器 -> {REPORT}")
    for r in rounds:
        a = r["admission"]
        print(f"  第 {r['round']} 轮：admit {a['admitted_count']} / defer {a['deferred_count']}"
              f"  oracle 分布 {a['oracle_distribution']}")
    if pending:
        print(f"  未完成 {len(pending)} 个作业（预算不够，需下一轮或放宽 max_agents）："
              f" {[t.task_id for t in pending]}")


def _contract_limits(contract: Path) -> dict[str, int]:
    """从层契约里读预算上限 —— 契约是单一事实来源，代码不写死数字。"""
    import re
    text = contract.read_text(encoding="utf-8")
    def grab(key: str, default: int) -> int:
        m = re.search(rf"{key}:\s*(\d+)", text)
        return int(m.group(1)) if m else default
    return {
        "max_agents": grab("max_agents", 12),
        "max_tokens_per_round": grab("max_tokens_per_round", 2_000_000),
    }


if __name__ == "__main__":
    main()
