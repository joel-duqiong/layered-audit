"""变更影响分析 —— 「改一处，只动受影响的 Agent」。

输入：被测仓库的 git diff（哪些文件变了）+ 已有边表（谁调用谁）
输出：受影响的 Agent 实例集合、必须重跑的场景、以及**上界**（保守失效的结果）

为什么要区分「解析出的」和「上界」：
  resolution=unknown 的边（例如 Spring DI 多候选实现、@Primary/@Qualifier 组合）
  结构上解不出来。这类边一律触发**保守失效** —— 宁可多标不可漏标，
  否则「只动受影响的部分」会变成「漏更新」，门禁假绿。
  所以报告同时给 N_resolved 与 N_upper_bound，两者差多少就是这次的不确定性。

失效强度由边的 stability 决定：
  signature 变 → 上游必须重跑（硬失效）
  behavior  变 → 上游需重验（软失效）
  semantic  变 → 上游仅复核
  unknown  边 → 上游保守标 stale
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema

PROJECT_DIR = findings_schema.target_path().parent


def _sut() -> dict[str, Any]:
    return findings_schema.load_target().get("sut", {})


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[-200:]}")
    return proc.stdout


def diff_range(base: str) -> str:
    """base 的两种语义：
    纯 hash（dcc9b06）→ 该提交自身的改动（base^..base）
    相对锚点（HEAD~1）→ 从锚点到 HEAD 的累计改动（base..HEAD）
    """
    if "~" in base or base == "HEAD":
        return f"{base}..HEAD"
    return f"{base}^..{base}"


def changed_files(repo: str, base: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", diff_range(base))
    return [line.strip() for line in out.splitlines() if line.strip()]


def build() -> dict[str, Any]:
    sut = _sut()
    repo = sut.get("repo", "")
    base = sut.get("git_base", "HEAD~1")
    depth = int(sut.get("impact_depth", 3))

    agents = json.loads((PROJECT_DIR / "agents.json").read_text(encoding="utf-8"))
    edges = json.loads((PROJECT_DIR / "edges.json").read_text(encoding="utf-8"))["edges"]

    files = changed_files(repo, base)

    # 文件 -> 受影响单元（函数层单元没有 file 字段，用类名反查文件单元）
    file_units = {u["unit"]["unit_key"]: u for u in agents["agents"] if u["layer"] == "file"}
    class_to_file = {k: v["unit"].get("file", "") for k, v in file_units.items()}
    unit_to_file: dict[str, str] = {}
    for u in agents["agents"]:
        if u["layer"] != "function":
            continue
        cls = u["unit"]["unit_key"].split("#")[0]
        unit_to_file[u["agent_id"]] = class_to_file.get(cls, "")

    # 改动文件 -> 类名（简单名）。路径口径不一致（仓库相对 vs 包相对），
    # 用类名匹配最稳：一次改名会同时改两边，但类名通常不变。
    changed_types = {Path(cf).stem for cf in files if cf.endswith(".java")}
    changed_units: set[str] = set()
    for u in agents["agents"]:
        cls = u["unit"]["unit_key"].split("#")[0]
        simple = cls.rsplit(".", 1)[-1]
        if simple in changed_types:
            changed_units.add(u["agent_id"])

    # 反向边：被调方 -> 调用方
    in_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        in_edges[e["to_agent_id"]].append(e)

    resolved: set[str] = set(changed_units)
    upper: set[str] = set(changed_units)
    reasons: dict[str, list[str]] = defaultdict(list)
    frontier = list(changed_units)
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for e in in_edges.get(node, []):
                caller = e["from_agent_id"]
                if caller in upper:
                    continue
                upper.add(caller)
                reasons[caller].append(f"{e['kind']}/{e['resolution']}/{e['stability']} @{node}")
                if e["resolution"] != "unknown":
                    resolved.add(caller)
                    nxt.append(caller)
        frontier = nxt
        if not frontier:
            break

    # 受影响的根因（按 unit 归属）
    rc = json.loads((PROJECT_DIR / "root_causes.json").read_text(encoding="utf-8"))
    agent_root_causes: dict[str, set[str]] = defaultdict(set)
    for a in agents["agents"]:
        for key in a["conclusions"].get("root_cause_keys") or []:
            if key:
                agent_root_causes[key].add(a["agent_id"])
    affected_rc = sorted(k for k, agents_of in agent_root_causes.items()
                         if agents_of & (upper))

    by_layer: dict[str, int] = defaultdict(int)
    for a in agents["agents"]:
        if a["agent_id"] in upper:
            by_layer[a["layer"]] += 1

    total_agents = len({a["agent_id"] for a in agents["agents"]})
    return {
        "base": base,
        "diff_range": diff_range(base),
        "repo": repo,
        "changed_files": files,
        "changed_units": sorted(changed_units),
        "affected_resolved": sorted(resolved),
        "affected_upper_bound": sorted(upper),
        "counts": {
            "changed_files": len(files),
            "seed_units": len(changed_units),
            "resolved": len(resolved),
            "upper_bound": len(upper),
            "total_units": total_agents,
            "resolved_ratio": round(len(resolved) / total_agents, 4) if total_agents else 0,
            "upper_bound_ratio": round(len(upper) / total_agents, 4) if total_agents else 0,
            "affected_by_layer": dict(by_layer),
            "affected_root_causes": len(affected_rc),
        },
        "affected_root_causes": affected_rc,
        "reasons": {k: v[:5] for k, v in list(reasons.items())[:50]},
    }


def main() -> None:
    payload = build()
    (PROJECT_DIR / "change_impact.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    c = payload["counts"]
    print(f"变更影响 -> {PROJECT_DIR / 'change_impact.json'}")
    print(f"  改动 {c['changed_files']} 个文件 → 种子单元 {c['seed_units']} 个")
    print(f"  解析出的受影响：{c['resolved']}（{c['resolved_ratio']:.2%}）")
    print(f"  保守上界：      {c['upper_bound']}（{c['upper_bound_ratio']:.2%}）")
    print(f"  按层：{c['affected_by_layer']}；波及根因 {c['affected_root_causes']} 条")


if __name__ == "__main__":
    main()
