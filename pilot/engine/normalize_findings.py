"""把五层的 findings 归一化成一张符合 layers.yaml 证据 schema 的表。

输入（pilot/ 下各层脚本的产物）：
    fn_findings.json          函数层   {meta, findings[], passed_checks[], hypotheses_verdict[]}
    file_findings.json        文件层   裸数组
    findings.json             服务层   裸数组
    class_findings.json       类层     裸数组
    integration_findings.json 集成层   {generated_at, findings[], passed_checks[], summary}

输出：
    all_findings.json         归一化结果（含 source 溯源与 meta 标注来源）
    all_findings.md           人读摘要

不做的事：不改判任何缺陷的严重性，不改写原始 detail，不凭空造 repro_cmd ——
没有可跑命令就留空串，并在报告里计入「repro 缺口」。
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

import findings_schema

HERE = Path(__file__).resolve().parent
PROJECT_DIR = findings_schema.target_path().parent
UNKNOWN_LINE = 0  # line 为 int 且必填；0 = 该层不以行号为定位单位


# ------------------------------------------------------------------ 项目绑定

@functools.lru_cache(maxsize=1)
def _target() -> dict[str, Any]:
    return findings_schema.load_target()


@functools.lru_cache(maxsize=1)
def java_root() -> Path:
    return Path(_target()["source_roots"]["java"])


@functools.lru_cache(maxsize=1)
def pkg_root() -> str:
    return _target()["source_roots"]["java_package"]


@functools.lru_cache(maxsize=1)
def annotation_table():
    """项目标注表（人工判定）：category / claim / owner / confidence / root_cause_key。"""
    return findings_schema.load_annotation_table()


@functools.lru_cache(maxsize=1)
def class_entities() -> dict[str, str]:
    """表名 -> 实体源码文件（相对 java 包根）。"""
    return _target().get("class_entities", {})


def evidence_sources() -> tuple[tuple[str, str, str | None], ...]:
    """证据源清单：(层, 文件名, 取值键)。键为 None 表示文件顶层就是数组。"""
    return tuple((s["layer"], s["file"], s.get("key"))
                 for s in _target()["evidence_sources"])

# 证据源清单在 target.json 里（换项目改配置，不改代码）

LAYER_REPLAY = {
    "file": "python3 file_dynamic.py",
    "class": "python3 class_dynamic.py",
}


# ------------------------------------------------------------------ 源码定位

def _java_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in java_root().rglob("*.java"):
        index.setdefault(path.stem, path)
    return index


@functools.lru_cache(maxsize=1)
def java_index() -> dict[str, Path]:
    return _java_index()


def _pkg_rel(pkg_path: str) -> str:
    """'config/WebConfig.java' -> '<java_package>/config/WebConfig.java'"""
    return f"{pkg_root()}/{pkg_path}"


def _expand_file_ref(ref: str) -> list[str]:
    """'service/{OrderService,PackageService}.java' -> 两个具体文件路径"""
    out: list[str] = []
    for part in ref.split(" + "):
        part = part.strip()
        m = re.match(r"^(?P<dir>.*)\{(?P<names>[^}]+)\}(?P<suffix>.*)$", part)
        if m:
            for name in m.group("names").split(","):
                out.append(f"{m.group('dir')}{name.strip()}{m.group('suffix')}")
        elif part:
            out.append(part)
    return out


def _resolve_line(java_path: Path, method: str) -> int:
    try:
        text = java_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UNKNOWN_LINE
    pat = re.compile(rf"\b{re.escape(method)}\s*\(")
    for idx, line in enumerate(text.splitlines(), start=1):
        if pat.search(line) and not line.strip().startswith("//"):
            return idx
    return UNKNOWN_LINE


def _fn_location(rec: dict[str, Any]) -> dict[str, Any]:
    """函数层：symbol 来自 function 字段，file 来自 evidence 里的全限定类名。"""
    fn_field = rec.get("function") or ""
    symbols = [s.strip() for s in fn_field.split(" / ") if s.strip()]
    symbol = fn_field
    java_path: Path | None = None
    method = ""
    evidence = rec.get("evidence")
    if isinstance(evidence, dict):
        evidence = [evidence]
    for ev in evidence or []:
        fqn = ev.get("function") or ""
        if "#" in fqn:
            cls_fqn, _, meth = fqn.partition("#")
            cls = cls_fqn.rsplit(".", 1)[-1]
            if symbols and not symbols[0].startswith(cls):
                continue
            java_path = java_index().get(cls)
            method = meth
            break
    if java_path is None and symbols:
        cls = symbols[0].split(".")[0]
        java_path = java_index().get(cls)
        method = symbols[0].split(".")[-1]
    line = _resolve_line(java_path, method) if java_path and method else UNKNOWN_LINE
    return {
        "file": str(java_path) if java_path else "",
        "line": line,
        "symbol": symbol,
        "endpoint": "",
    }


def _file_location(rec: dict[str, Any]) -> dict[str, Any]:
    """文件层：root_cause_file 是包内相对路径，可能指多个文件。"""
    refs = _expand_file_ref(rec.get("root_cause_file") or "")
    files = [str(java_root() / _pkg_rel(r)) for r in refs]
    return {
        "file": files[0] if files else "",
        "line": UNKNOWN_LINE,
        "symbol": refs[0] if refs else "",
        "endpoint": "",
        "files": files,
    }


def _class_location(rec: dict[str, Any]) -> dict[str, Any]:
    ev = rec.get("evidence") or {}
    hint = annotation_table().CLASS_FILE_HINTS.get(rec.get("id", ""))
    table = ev.get("table") or ""
    entity = ev.get("entity") or ""
    entities = class_entities()
    if not hint and table in entities:
        hint = entities[table]
    if not hint and entity in entities:
        hint = entities[entity]
    if not hint:
        column = ev.get("column") or ""
        hinted = annotation_table().COLUMN_FILE_HINTS.get(column)
        if hinted:
            hint = hinted
    path = str(java_root() / _pkg_rel(hint)) if hint else ""
    return {"file": path, "line": UNKNOWN_LINE, "symbol": ev.get("column") or table or "", "endpoint": ""}


def _build_route_index() -> dict[str, tuple[Path, int]]:
    """扫 controller 源码，建 路由 -> (文件, 行号) 索引。纯文本解析，不启动应用。"""
    index: dict[str, tuple[Path, int]] = {}
    ctrl_dir = java_root() / pkg_root() / "controller"
    for path in sorted(ctrl_dir.glob("*Controller.java")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        base = ""
        pending: tuple[str, int] | None = None
        for idx, line in enumerate(lines, start=1):
            m_base = re.match(r"^@RequestMapping\(\s*\"([^\"]*)\"", line.strip())
            if m_base:
                base = m_base.group(1)
                continue
            m_map = re.match(r"^@(Get|Post|Put|Patch|Delete)Mapping(?:\(\s*\"([^\"]*)\"\s*\))?", line.strip())
            if m_map:
                method = m_map.group(1).upper()
                suffix = m_map.group(2) or ""
                route = base + (suffix if suffix.startswith("/") or suffix == "" else "/" + suffix)
                if route.endswith("/") and route != "/":
                    route = route[:-1]
                index[f"{method} {route}"] = (path, idx)
                pending = (method, idx)
                continue
    return index


@functools.lru_cache(maxsize=1)
def route_index() -> dict[str, tuple[Path, int]]:
    return _build_route_index()


def _endpoint_pairs(endpoint: str) -> list[tuple[str, str]]:
    """把 'GET/POST/PUT /api/orders'、'GET /a, POST /b' 拆成 (METHOD, PATH)。"""
    pairs: list[tuple[str, str]] = []
    for piece in endpoint.split(","):
        piece = piece.strip()
        if not piece:
            continue
        methods, _, path = piece.partition(" ")
        path = path.strip()
        if not path:
            continue
        for m in methods.split("/"):
            m = m.strip().upper()
            if m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                pairs.append((m, path))
    return pairs


def _endpoint_location(rec: dict[str, Any]) -> dict[str, Any]:
    endpoint = rec.get("endpoint") or ""
    file_path, line = "", UNKNOWN_LINE
    for method, path in _endpoint_pairs(endpoint):
        hit = route_index().get(f"{method} {path}")
        if hit:
            file_path, line = str(hit[0]), hit[1]
            break
    return {"file": file_path, "line": line, "symbol": endpoint, "endpoint": endpoint}


# ------------------------------------------------------------------ repro / evidence

def _fn_repro(rec: dict[str, Any]) -> tuple[str, list[str]]:
    evidence = rec.get("evidence")
    if isinstance(evidence, dict):
        evidence = [evidence]
    cases = [ev.get("case_id") for ev in (evidence or []) if ev.get("case_id")]
    if not cases:
        return "", []
    return f"python3 fn_dynamic.py --case {cases[0]}", [f"fn_dynamic.py#case:{c}" for c in cases]


COMMAND_BINARIES = ("grep", "awk", "sed", "cat", "jq", "docker", "psql", "find", "ls", "rg")


def _granularity(repro_cmd: str) -> str:
    """复现命令的粒度。

    case      单个单元用例（fn_dynamic.py --case）
    scenario  单个缺陷场景（integration_dynamic.py --scenario / integration_chain.py --chain）
    request   一个 HTTP 请求（curl …）
    command   其它可直接执行的 shell 命令（grep / docker / psql …）
    layer     整层脚本，无更细粒度
    reference 只是一句指引（如「见 observation.json 中 key=…」），不是命令 —— 不许伪装成可跑命令
    none      没有复现信息
    """
    if not repro_cmd:
        return "none"
    head = repro_cmd.strip()
    if head.startswith("python3 fn_dynamic.py --case"):
        return "case"
    if "--scenario" in head or "--chain" in head:
        return "scenario"
    if head.startswith(("curl", "http", "bash ")):
        return "request"
    if head.startswith("python3"):
        return "layer"
    if re.match(rf"({'|'.join(COMMAND_BINARIES)})\b", head):
        return "command"
    return "reference"


def _build(layer: str, rec: dict[str, Any], index: int, source_file: str) -> dict[str, Any]:
    fid = rec.get("id", "")
    meta = annotation_table().lookup(layer, fid)
    inferred: list[str] = []
    if meta is None:
        inferred.append("category/claim/owner/confidence/ground_truth/cwe/root_cause_key")
        meta = annotation_table().Meta(
            category="UNMAPPED", fit="unmapped",
            claim=f"（未标注：{fid}）",
            owner_layer=layer, confidence=0.4, kind="defect", cwe=(),
            root_cause_key=f"unannotated.{layer}.{fid}",
        )

    # 复现命令与证据引用：按层决定
    if layer == "function":
        repro_cmd, extra_refs = _fn_repro(rec)
        base_refs = [f"{source_file}#{fid}"]
    elif layer == "file":
        repro_cmd = LAYER_REPLAY["file"]
        base_refs = [f"{source_file}#{fid}"]
        extra_refs = []
    elif layer == "service":
        repro_cmd = rec.get("repro") or ""
        base_refs = [f"{source_file}#{fid}"]
        extra_refs = []
    elif layer == "class":
        repro_cmd = LAYER_REPLAY["class"]
        base_refs = [f"{source_file}#{fid}"]
        extra_refs = []
    else:
        repro_cmd = rec.get("repro") or ""
        base_refs = [f"{source_file}#{fid}"]
        extra_refs = []

    # 定位：发现自带 location 时采信，否则按层推导
    own_location = rec.get("location")
    if isinstance(own_location, dict):
        location = {"file": own_location.get("file", ""),
                    "line": own_location.get("line", UNKNOWN_LINE),
                    "symbol": own_location.get("symbol", ""),
                    "endpoint": own_location.get("endpoint", "")}
    elif layer == "function":
        location = _fn_location(rec)
    elif layer == "file":
        location = _file_location(rec)
    elif layer == "class":
        location = _class_location(rec)
    else:
        location = _endpoint_location(rec)
        if location.get("endpoint", "").startswith("CHAIN-"):
            extra_refs.append("integration_chains.json#chain_steps")

    cwe = sorted({m.group(0) for m in (re.search(r"CWE-\d+", c) for c in rec.get("cwe") or []) if m})
    cwe = sorted(set(cwe) | set(meta.cwe))

    record = {
        "id": fid,
        "uid": f"{layer}:{fid}#{index}",
        "layer": layer,
        "category": meta.category,
        "category_fit": meta.fit,
        "suggested_category": meta.suggested_category or "",
        "location": location,
        "claim": meta.claim,
        "evidence_refs": base_refs + extra_refs,
        "repro_cmd": repro_cmd,
        "repro_granularity": _granularity(repro_cmd),
        "confidence": meta.confidence,
        "ground_truth": meta.ground_truth,
        "cwe": cwe,
        "owner": {
            "layer": meta.owner_layer,
            "relation": meta.relation,
            "basis": "attribution.rule：能给出可复现证据的最低层"
                     if meta.relation == "own" else
                     f"根因归 {meta.owner_layer}；本层拥有链级后果",
        },
        "root_cause_key": meta.root_cause_key,
        "related_root_causes": list(meta.related_root_causes),
        "kind": meta.kind,
        "severity": rec.get("severity", ""),
        "title": rec.get("title", ""),
        "detail": rec.get("detail", ""),
        "related": rec.get("related_findings") or rec.get("related") or [],
        "note": meta.note,
        "source": {"file": source_file, "index": index},
        "meta": {
            "inferred_fields": inferred,
            "confidence_source": "manual" if not inferred else "inferred",
            "ground_truth_source": "manual",
            "duplicate_id_in_layer": False,
        },
    }
    return record


def load_layer(layer: str, path: str, key: str | None) -> list[dict[str, Any]]:
    """证据源文件在项目目录（target.json 的同级），不在 engine/ 里。"""
    data = json.loads((PROJECT_DIR / path).read_text(encoding="utf-8"))
    if key:
        data = data[key]
    if not isinstance(data, list):
        raise TypeError(f"{path} 不是数组")
    return data


def main() -> None:
    categories = findings_schema.load_layer_categories()
    sources = evidence_sources()
    records: list[dict[str, Any]] = []
    for layer, path, key in sources:
        for idx, rec in enumerate(load_layer(layer, path, key)):
            records.append(_build(layer, rec, idx, path))

    # id 在层内重复 = 契约违规（id 应能唯一标识一条发现），标记出来
    seen: dict[tuple[str, str], int] = {}
    for rec in records:
        key = (rec["layer"], rec["id"])
        seen[key] = seen.get(key, 0) + 1
    for rec in records:
        if seen[(rec["layer"], rec["id"])] > 1:
            rec["meta"]["duplicate_id_in_layer"] = True

    problems: list[str] = []
    for rec in records:
        for p in findings_schema.validate_finding(rec, categories):
            problems.append(f"{rec['uid']}: {p}")
    structural = [p for p in problems if "UNMAPPED" not in p]
    unmapped_problems = [p for p in problems if "UNMAPPED" in p]

    out = {
        "generated_from": "normalize_findings.py",
        "contract": str(findings_schema.contract_path()),
        "target": str(findings_schema.target_path()),
        "counts": {
            "total": len(records),
            "by_layer": {lay: sum(1 for r in records if r["layer"] == lay)
                         for lay, _p, _k in sources},
            "by_kind": {k: sum(1 for r in records if r["kind"] == k)
                        for k in ("defect", "positive", "observation")},
        },
        "findings": records,
    }
    (PROJECT_DIR / "all_findings.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    _report(records, problems, structural, unmapped_problems)
    if structural:
        raise SystemExit(1)
    if unmapped_problems:
        raise SystemExit(2)


def _report(records: list[dict[str, Any]], problems: list[str],
            structural: list[str], unmapped_problems: list[str]) -> None:
    total = len(records)
    print(f"归一化 -> {PROJECT_DIR / 'all_findings.json'}  共 {total} 条")

    print("\n[必填字段：结构齐备 / 有值]")
    for field in findings_schema.REQUIRED_FIELDS:
        present = sum(1 for r in records if field in r)
        filled = sum(1 for r in records if r.get(field) not in (None, "", [], {}))
        note = "  (空值合法：契约允许无对应 CWE)" if field == "cwe" else ""
        print(f"  {field:<16} 结构 {present}/{total}   有值 {filled}/{total}{note}")

    print("\n[repro_cmd 有可跑命令]")
    by_gran: dict[str, int] = {}
    for r in records:
        by_gran[r["repro_granularity"]] = by_gran.get(r["repro_granularity"], 0) + 1
    for g, n in sorted(by_gran.items()):
        print(f"  {g:<10} {n}")
    no_repro = [r["uid"] for r in records if not r["repro_cmd"]]
    if no_repro:
        print(f"  无可跑命令 {len(no_repro)} 条：{', '.join(no_repro[:8])}{' ...' if len(no_repro) > 8 else ''}")

    print("\n[location 定位质量]")
    with_file = sum(1 for r in records if r["location"]["file"])
    with_line = sum(1 for r in records if r["location"]["line"])
    with_sym = sum(1 for r in records if r["location"]["symbol"])
    print(f"  有文件定位 {with_file}/{total}   有行号 {with_line}/{total}   有符号 {with_sym}/{total}")
    missing_file = [r["uid"] for r in records if not r["location"]["file"]]
    if missing_file:
        print(f"  无文件定位 {len(missing_file)} 条：{', '.join(missing_file[:10])}{' ...' if len(missing_file) > 10 else ''}")

    print("\n[类别落地]")
    unmapped = [r for r in records if r["category"] == "UNMAPPED"]
    stretched = [r for r in records if r["category_fit"] == "stretched"]
    exact = [r for r in records if r["category_fit"] == "exact"]
    print(f"  exact {len(exact)}  stretched {len(stretched)}  unmapped {len(unmapped)}")
    for r in unmapped:
        sug = r["suggested_category"] or "-"
        print(f"    UNMAPPED {r['uid']:<52} 建议类别 {sug}")

    print("\n[契约违规]")
    if structural:
        print(f"  结构性违规 {len(structural)} 条：")
        for p in structural[:40]:
            print(f"    {p}")
    else:
        print("  结构性违规 0 条")
    if unmapped_problems:
        print(f"  契约缺口（类别装不下）{len(unmapped_problems)} 条 —— 需要人工决策是否修订 layers.yaml：")
        for p in unmapped_problems:
            print(f"    {p}")

    dup_ids = [r["uid"] for r in records if r["meta"]["duplicate_id_in_layer"]]
    print(f"\n[id 层内重复] {len(dup_ids)} 条（同 id 多行，聚合时按 root_cause_key 归并）")

    low_conf = [(r["uid"], r["confidence"]) for r in records if r["confidence"] < 0.6]
    print(f"[弱置信度 <0.6] {len(low_conf)} 条 {low_conf if low_conf else ''}")


if __name__ == "__main__":
    main()
