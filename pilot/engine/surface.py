"""跨仓库的面清单与边构建。

**问题**：两个仓库之间会互相调用。仓库 A 以为 B 提供某个接口，B 那边其实没有 ——
这种「契约漂移」在单仓库审计里永远看不见，因为任何一边的静态分析都自洽。

**面清单（surface manifest）**：每个仓库声明自己对外提供什么、依赖谁。
```json
{"repo_id": "...", "http_endpoints": [{"method": "GET", "path": "/fx"}],
 "tables_owned": ["fx_rate"], "events_published": [], "events_consumed": [],
 "agent_tools": [], "declared_outbound_calls": [{"target_repo_hint": "x", "method": "GET", "path": "/fx"}]}
```

**边只信双边**：调用方声明了、且提供方确实声明了这个面 -> 边成立（declared）；
只有一方声明 -> **漂移**（drift），是缺陷，不生成链。

**边可信性三级**：
  CONFIRMED  运行时观测到这次调用真的发生了
  DECLARED   双边声明一致，但没观测到
  DRIFT      只有一方声明 —— 契约漂移，产缺陷，不产链

**跨仓库的 owner 规则**：owner = **最早违反契约的那一跳**（因果序最靠前），
受害方只被引用，不重复开单。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import findings_schema

PROJECT_DIR = findings_schema.target_path().parent


def load_surfaces(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """读一个目录下的所有面清单（每个仓库一份 <repo_id>.json）。"""
    base = directory or (PROJECT_DIR / "surfaces")
    out: dict[str, dict[str, Any]] = {}
    if not base.exists():
        return out
    for path in sorted(base.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        repo_id = data.get("repo_id") or path.stem
        out[repo_id] = data
    return out


def _norm(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def build_graph(surfaces: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """构建跨仓库图，并标出契约漂移。"""
    provided: dict[str, set[str]] = {}
    for repo_id, surface in surfaces.items():
        provided[repo_id] = {_norm(e.get("method", "GET"), e.get("path", ""))
                            for e in surface.get("http_endpoints", [])}

    edges: list[dict[str, Any]] = []
    drifts: list[dict[str, Any]] = []
    for repo_id, surface in surfaces.items():
        for call in surface.get("declared_outbound_calls", []):
            method = call.get("method", "GET")
            path = call.get("path", "")
            hint = call.get("target_repo_hint")
            candidates = [r for r in provided
                          if r != repo_id and (not hint or hint == r)]
            matched = [r for r in candidates if _norm(method, path) in provided[r]]
            if matched:
                for target in matched:
                    edges.append({
                        "from": repo_id, "to": target,
                        "method": method.upper(), "path": path,
                        "kind": "http",
                        "resolution": "declared",
                    })
            else:
                drifts.append({
                    "from": repo_id, "to_hint": hint,
                    "method": method.upper(), "path": path,
                    "reason": "调用方声明了这个调用，但没有任何仓库声明提供这个面",
                })
    return {"edges": edges, "drifts": drifts, "provided": {k: sorted(v) for k, v in provided.items()}}


def mark_confirmed(graph: dict[str, Any], observed: set[tuple[str, str, str]]) -> None:
    """把运行时观测到的调用标成 CONFIRMED。

    observed 里是 (from_repo, method, path)。
    """
    for edge in graph["edges"]:
        key = (edge["from"], edge["method"], edge["path"])
        if key in observed:
            edge["resolution"] = "confirmed"


def owner_of_chain(chain: list[dict[str, Any]]) -> tuple[str, str]:
    """跨仓库链的归属：**最早违反契约的那一跳**。

    chain 是按因果顺序的边列表。第一条边就是最早的那一跳 —— 它是责任方。
    受害方（后面的）只被引用。

    返回 (owner_repo, reason)。
    """
    if not chain:
        return "", "空链"
    first = chain[0]
    return first["from"], (f"最早违反契约的一跳：{first['from']} -> {first['to']}"
                           f" {first['method']} {first['path']}")
