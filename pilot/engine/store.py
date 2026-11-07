"""分片存储 —— 「改一处只动受影响的那几个」的地基。

**为什么分片**：产物写成一个大 JSON，就只能整份重算。分成「一个靶区一个分片」之后，
改动只让受影响的分片失效，其余复用。

**三条铁律**：
1. **并集不缩集**：上一次全量跑产出的每个分片，这次必须仍在。
   少一个 → **退出码 2（没查完）**，绝不是 0（绿）。
2. **指纹两层**：code（代码/用例变了没）+ env（环境变了没：DB schema、镜像、种子数据）。
   env 取不到 → 该分片不可复用，宁可重跑。
3. **复用必须可验证**：指纹一致才复用；不一致就重跑。

分片结构：
    <产物目录>/store/<artifact>/_index.json      索引：分片 -> 指纹/时间/来源
    <产物目录>/store/<artifact>/<shard_id>.json  分片内容
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import findings_schema

PROJECT_DIR = findings_schema.target_path().parent
STORE_ROOT_NAME = ".store"


def artifact_dir() -> Path:
    d = PROJECT_DIR / findings_schema.load_target().get("paths", {}).get(
        "artifacts_dir", str(PROJECT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def shard_dir(artifact: str) -> Path:
    d = artifact_dir() / STORE_ROOT_NAME / artifact
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_path(artifact: str) -> Path:
    return shard_dir(artifact) / "_index.json"


def sha(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(json.dumps(part, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return "sha256:" + h.hexdigest()[:16]


def env_fingerprint(parts: list[Any]) -> str | None:
    """环境指纹：由调用方把环境事实传进来（引擎不查库、不认识具体数据库）。

    调用方（项目侧）负责采集：DB schema 摘要、容器镜像 digest、种子数据摘要。
    采不到就传 None —— 调用方必须把它当成「不可复用」，宁可重跑。
    """
    if not parts:
        return None
    return sha(parts)


def read_index(artifact: str) -> dict[str, Any]:
    p = index_path(artifact)
    if not p.exists():
        return {"artifact": artifact, "shards": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def write_shard(artifact: str, shard_id: str, payload: Any, fingerprint: str,
                tool_version: str = "", source: str = "") -> None:
    path = shard_dir(artifact) / f"{shard_id}.json"
    path.write_text(json.dumps({
        "artifact": artifact, "shard_id": shard_id, "payload": payload,
        "input_fingerprint": fingerprint, "tool_version": tool_version,
        "source": source,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(artifact: str, fresh: dict[str, dict[str, Any]],
          fingerprints: dict[str, str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """并集合并：新鲜的用新鲜的，旧的指纹一致才复用，其余算缺失。

    返回 (合并后的分片, 缺失的分片 id)。**缺失不为空 → 调用方必须退出码 2。**
    """
    index = read_index(artifact)
    known = index.get("shards", {})
    merged: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    for shard_id, meta in known.items():
        if shard_id in fresh:
            merged[shard_id] = fresh[shard_id]
            continue
        path = shard_dir(artifact) / f"{shard_id}.json"
        if not path.exists():
            missing.append(shard_id)
            continue
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("input_fingerprint") != fingerprints.get(shard_id):
            missing.append(shard_id)      # 指纹变了 → 必须重跑，不能复用
            continue
        merged[shard_id] = stored.get("payload")

    for shard_id, payload in fresh.items():
        merged.setdefault(shard_id, payload)
    return merged, missing


def write_index(artifact: str, shards: dict[str, dict[str, Any]],
                tool_version: str = "") -> None:
    index = {
        "artifact": artifact,
        "tool_version": tool_version,
        "shards": {sid: {"file": f"{sid}.json",
                         "input_fingerprint": meta.get("input_fingerprint"),
                         "generated_at": meta.get("generated_at"),
                         "source": meta.get("source")}
                 for sid, meta in shards.items()},
    }
    index_path(artifact).write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
