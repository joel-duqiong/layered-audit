"""Project legacy unit/edge snapshots into a conservative, read-only cell graph.

The input snapshots may be stale and may collapse overloaded methods.  This
projection preserves those limitations instead of inventing precise identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

PREFIX = {"method": "fn", "class": "cls", "file": "file", "service": "svc", "flow": "flow"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(repo: str, kind: str, key: str) -> str:
    raw = json.dumps([repo, kind, key], ensure_ascii=False, separators=(",", ":"))
    return "cell:" + digest(raw.encode("utf-8"))[:24]


def relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in ("", "."):
        return None
    return str(path)


def build_graph(units: dict[str, Any], edges: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    repo = units.get("repo_id")
    records = units.get("units")
    relations = edges.get("edges")
    if not isinstance(repo, str) or not repo or not isinstance(records, list) or not isinstance(relations, list):
        raise ValueError("units must contain repo_id and units; edges must contain edges")
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("kind") not in PREFIX or not isinstance(record.get("unit_key"), str) or not record["unit_key"]:
            raise ValueError(f"invalid unit at index {index}")
        groups.setdefault((record["kind"], record["unit_key"]), []).append((index, record))

    nodes: list[dict[str, Any]] = []
    structural: list[dict[str, Any]] = []
    lookup: dict[str, list[dict[str, Any]]] = {}
    files: dict[str, str] = {}
    missing_paths = 0
    for (kind, key), group in sorted(groups.items()):
        cid = identity(repo, kind, key)
        ambiguous = len(group) > 1
        cell = {"id": cid, "kind": kind, "key": key,
                "identity_status": "ambiguous" if ambiguous else "indexed",
                "assessment": "unknown", "record_indices": [i for i, _ in group],
                "source": "units", "note": "file kind is a legacy class-based file scope" if kind == "file" else ""}
        nodes.append(cell)
        lookup.setdefault(f"{PREFIX[kind]}:{key}", []).append(cell)
        paths = {relative_path(r.get("file")) for _, r in group}
        paths.discard(None)
        if kind in ("method", "file", "class") and not paths:
            missing_paths += 1
        for path in sorted(paths):
            fid = files.setdefault(path, identity(repo, "source_file", path))
            structural.append({"from": fid, "to": cid, "type": "indexed_in_file",
                               "resolution": "inferred", "source": "units",
                               "record_indices": [i for i, r in group if relative_path(r.get("file")) == path],
                               "identity_status": cell["identity_status"]})
    for path, fid in sorted(files.items()):
        nodes.append({"id": fid, "kind": "source_file", "key": path,
                      "identity_status": "indexed", "assessment": "unknown", "source": "units"})

    def endpoint(raw: Any) -> dict[str, Any]:
        name = str(raw or "")
        candidates = lookup.get(name, [])
        return {"reference": name, "candidates": [c["id"] for c in candidates],
                "status": ("ambiguous" if any(c["identity_status"] == "ambiguous" for c in candidates)
                           else "resolved" if len(candidates) == 1 else "external" if name.startswith("ext:") else "unknown")}

    behavior = []
    for index, edge in enumerate(relations):
        if not isinstance(edge, dict):
            raise ValueError(f"invalid edge at index {index}")
        behavior.append({"type": edge.get("kind", "unknown"), "from": endpoint(edge.get("from_agent_id")),
                         "to": endpoint(edge.get("to_agent_id")), "resolution": edge.get("resolution", "unknown"),
                         "source": "edges", "record_index": index, "source_edge_id": edge.get("edge_id")})
    return {"schema_version": 1, "repo_id": repo, "sources": sources,
            "limitations": ["snapshot freshness not verified", "no runtime trace or business capability mapping",
                            "file paths inferred by upstream index", "overloaded methods may share an identity"],
            "counts": {"cells": len(nodes), "ambiguous_groups": sum(c["identity_status"] == "ambiguous" for c in nodes),
                       "missing_source_paths": missing_paths, "structural_edges": len(structural),
                       "behavior_edges": len(behavior),
                       "unresolved_behavior_edges": sum(e["from"]["status"] in ("unknown", "ambiguous") or e["to"]["status"] in ("unknown", "ambiguous") or e["resolution"] == "unknown" for e in behavior),
                       "external_behavior_edges": sum(e["from"]["status"] == "external" or e["to"]["status"] == "external" for e in behavior)},
            "nodes": nodes, "structural_edges": structural, "behavior_edges": behavior}


def main() -> None:
    parser = argparse.ArgumentParser(description="Project unit and edge snapshots into a cell graph")
    parser.add_argument("--units", required=True, type=Path)
    parser.add_argument("--edges", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.resolve() in (args.units.resolve(), args.edges.resolve()):
        parser.error("output must differ from the inputs")
    blobs = {"units": args.units.read_bytes(), "edges": args.edges.read_bytes()}
    sources = {name: {"path": str(path), "sha256": digest(blobs[name])}
               for name, path in (("units", args.units), ("edges", args.edges))}
    graph = build_graph(json.loads(blobs["units"]), json.loads(blobs["edges"]), sources)
    args.out.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(graph["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
