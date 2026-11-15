"""零配置审计入口 —— 把四个支柱引擎编排成一条「给仓库路径就能跑」的链。

为什么需要它：四个支柱引擎都已建成且经独立证伪，但各自是独立脚本，接入一个项目
仍要手写项目绑定配置（target.json + 层契约 + 人工标注表）才能跑上游图。本入口把
「能零配置做的」做完，把「做不到的」显式报缺口，绝不假绿：

  · 业务判据支柱（oracle_harvest + oracle_bridge）：输入全部从仓库自动发现，零配置。
  · 图支柱（capability_map / arch_rules / frame_check）：需要 units.json / edges.json /
    relation_net.json 上游产物。这些产物由项目绑定流水线（字节码声明抽取）生成，
    本入口不伪造；仓库自带或显式供给则跑，否则该支柱 INCOMPLETE 并写明缺什么。

三态铁律（项目决议 D8）：每个支柱 GREEN / RED / INCOMPLETE。
  RED        = 引擎跑完且查出确定违反。
  GREEN      = 引擎跑完、覆盖完整、零违反零 unknown。
  INCOMPLETE = 缺输入 / 引擎失败 / 有 unknown 或未覆盖部分 ——「没查完」不是「没问题」。
退出码：0 全绿 / 1 有红 / 2 没查完（INCOMPLETE 绝不许当 0）。

只读被测仓库：所有产物写到 --out（默认临时目录），不向仓库写一个字节。
零项目字面量：所有路径由调用方给出或从仓库结构推导，引擎名相对本文件定位。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import oracle_bridge

ENGINE_DIR = Path(__file__).resolve().parent
DEFAULT_LAYERS = ENGINE_DIR.parent.parent / "layers.yaml"

GREEN, RED, INCOMPLETE = "GREEN", "RED", "INCOMPLETE"
SKIP_DIRS = {"node_modules", ".git", "target", "dist", "build", ".idea", "__pycache__", "backups", ".venv"}
ROLE_NAMES = ("controller", "service", "repository", "mapper", "entity", "dto")
ARTIFACT_NAMES = ("units.json", "edges.json", "relation_net.json")


# ---------------------------------------------------------------- 自动发现

def discover(repo: Path) -> dict[str, Any]:
    """从仓库结构推导输入。推导不出的留空，由调用方报缺口，不猜。"""
    java_roots: list[Path] = []
    test_roots: list[Path] = []
    migrations: list[Path] = []
    docs_dirs: list[Path] = []
    openapi: Path | None = None
    for cur, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        cur_p = Path(cur)
        if cur_p.name == "java" and cur_p.parent.name == "main" and cur_p.parent.parent.name == "src":
            java_roots.append(cur_p)
        if cur_p.name == "java" and cur_p.parent.name == "test" and cur_p.parent.parent.name == "src":
            test_roots.append(cur_p)
        if cur_p.name in ("migration", "migrations"):
            migrations.extend(sorted(cur_p.rglob("*.sql")))
        if cur_p.name == "docs":
            docs_dirs.append(cur_p)
        for f in sorted(files):
            low = f.lower()
            if openapi is None and low.startswith(("openapi", "swagger")) and low.endswith((".json", ".yaml", ".yml")):
                openapi = cur_p / f
    docs = sorted(p for p in repo.glob("*.md"))
    for d in docs_dirs:
        docs.extend(sorted(d.rglob("*.md")))
    packages = {name for name in ROLE_NAMES for jr in java_roots
                if any(p.is_dir() for p in jr.rglob(name))}
    return {"java_roots": java_roots, "test_roots": test_roots, "migrations": migrations,
            "docs": sorted(set(docs)), "openapi": openapi, "packages": packages}


def find_artifact(repo: Path, name: str) -> Path | None:
    for cand in (repo / name, repo / ".audit" / name):
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------- 默认架构规则

def default_arch_config(packages: set[str], has_members: bool) -> dict[str, Any] | None:
    """从实测分层包布局归纳通用惯例规则（source.kind=convention，未经项目确认）。

    不是猜业务规则：角色来自仓库真实包结构，规则是分层结构惯例，全部显式标未确认。
    布局识别不出则返回 None，由调用方报缺口。visibility 规则仅在供给成员声明时加入
    （arch_rules 契约：缺成员声明拒绝出 visibility 数），否则由调用方显式登记缺口。
    """
    roles = [{"id": r, "class_patterns": [f"*.{r}.*"]} for r in ROLE_NAMES if r in packages]
    if not roles:
        return None
    rules: list[dict[str, Any]] = []

    def convention(rid: str, rtype: str, claim: str, **kw: Any) -> None:
        rules.append({"id": rid, "type": rtype, "severity": "high", "claim": claim,
                      "source": {"kind": "convention", "reference": "通用分层惯例默认规则，未经项目书面确认",
                                 "confirmed_by": ""},
                      "unmapped_reason": "默认层契约无架构分层/依赖方向类别，标 UNMAPPED，需走契约提交修订", **kw})

    if {"controller", "service", "repository"} <= packages:
        convention("ARCH-D1", "forbidden_call", "controller 直调 repository，绕过 service 层事务与业务校验边界",
                   **{"from": "controller", "to": "repository"})
        convention("ARCH-D2", "layer_skip", "调用跳过 chain 中间层，被跳过层的不变式与事务边界在该路径不生效",
                   chain=["controller", "service", "repository"])
    if {"controller", "mapper"} <= packages:
        convention("ARCH-D3", "forbidden_call", "controller 直调 mapper 装配响应，绕过 service 层",
                   **{"from": "controller", "to": "mapper"})
    if {"entity", "service"} <= packages:
        convention("ARCH-D4", "dependency_direction", "实体反向依赖服务层，领域模型被编排逻辑绑定",
                   **{"from": "entity", "to": "service"})
    if {"repository", "service"} <= packages:
        convention("ARCH-D5", "dependency_direction", "持久层反向依赖服务层，事务边界无法独立推理",
                   **{"from": "repository", "to": "service"})
    if has_members:
        rules.append({"id": "ARCH-D6", "type": "visibility", "severity": "high",
                      "levels": ["private", "package_private"], "exempt_same_top_level_class": True,
                      "layer": "class", "category": "encapsulation_leak",
                      "claim": "private/包私有成员被其他顶层类调用，封装边界已被穿透",
                      "source": {"kind": "language_spec", "reference": "Java 语言规范：private/包私有成员不可被其他类访问",
                                 "confirmed_by": ""}})
    return {"schema": "arch-rules/1", "roles": roles, "rules": rules,
            "member_source": {"records": "members", "class": "class", "method": "method", "modifiers": "modifiers"},
            "role_fact_source": {"records": "facts", "class": "class", "mechanism": "mechanism"},
            "unknown_markers": [{"field": "access", "values": ["unknown"]}]}


# ---------------------------------------------------------------- 引擎调用

def run_engine(script: str, argv: list[str], out_path: Path | None) -> tuple[int | None, str, Any]:
    cmd = [sys.executable, str(ENGINE_DIR / script), *argv]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ENGINE_DIR))
    except OSError as exc:
        return None, f"无法启动引擎 {script}: {exc}", None
    data = None
    if out_path is not None and out_path.is_file():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    return proc.returncode, proc.stderr.strip()[-1500:], data


def pillar(state: str, reason: str, evidence: list[str], numerator: int | None = None,
           denominator: int | None = None, counts: dict | None = None, **extra: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason, "numerator": numerator, "denominator": denominator,
            "counts": counts or {}, "evidence": evidence, **extra}


def failed(script: str, rc: int | None, err: str, evidence: list[str]) -> dict[str, Any]:
    return pillar(INCOMPLETE, f"引擎 {script} 未成功退出(rc={rc})，不得读成合规: {err or '无 stderr'}", evidence)


# ---------------------------------------------------------------- 四支柱

def _unreadable_inputs(d: dict) -> list[str]:
    """供给但读不到的输入 → 显式缺口（红线1：unknown 不得变 pass）。只看路径真值会洗绿。"""
    gaps = []
    for p in d["docs"]:
        if not Path(p).exists():
            gaps.append(f"文档供给但不可读: {p}")
    for p in d["migrations"]:
        if not Path(p).exists():
            gaps.append(f"迁移供给但不可读: {p}")
    for p in d["java_roots"]:
        if not Path(p).is_dir():
            gaps.append(f"源码根供给但不可读: {p}")
    if d["openapi"] and not Path(d["openapi"]).is_file():
        gaps.append(f"OpenAPI 供给但不可读: {d['openapi']}")
    return gaps


def pillar_oracle(d: dict, out: Path, target: str) -> dict[str, Any]:
    if not (d["docs"] or d["migrations"] or d["java_roots"] or d["openapi"]):
        return pillar(INCOMPLETE, "无可采集输入：文档/迁移/源码/OpenAPI 均未发现", [])
    unreadable = _unreadable_inputs(d)
    harvested = out / "oracles_harvested.json"
    argv = ["--out", str(harvested)]
    if d["docs"]:
        argv += ["--docs", *map(str, d["docs"])]
    if d["migrations"]:
        argv += ["--migrations", *map(str, d["migrations"])]
    if d["java_roots"]:
        argv += ["--java-root", *map(str, d["java_roots"])]
    if d["openapi"]:
        argv += ["--openapi", str(d["openapi"])]
    rc, err, hdata = run_engine("oracle_harvest.py", argv, harvested)
    if rc != 0:
        return failed("oracle_harvest.py", rc, err, [str(harvested)])
    report = out / "oracle_bridge.json"
    rc, err, data = run_engine("oracle_bridge.py",
                               ["--harvested", str(harvested), "--target", target, "--out-report", str(report)], report)
    if rc != 0 or not isinstance(data, dict):
        return failed("oracle_bridge.py", rc, err, [str(harvested), str(report)])
    c = data["counts"]
    by_source = hdata.get("counts", {}).get("by_source", {}) if isinstance(hdata, dict) else {}
    gaps = list(unreadable)
    if c["unmappable"]:
        gaps.append(f"{c['unmappable']} 条判据不可映射到封闭原语")
    if data.get("untranscribed_count"):
        gaps.append(f"{data['untranscribed_count']} 条散文规则未转录")
    if c["excluded"]:
        gaps.append(f"{c['excluded']} 条被排除(需确认/明确未定义)")
    if not d["docs"]:
        gaps.append("未发现文档，业务规则(L0)未采集")
    elif not by_source.get("document"):
        gaps.append("文档供给但未产出 L0 判据（不可读或无可转录条款）")
    if not d["openapi"]:
        gaps.append("未发现 OpenAPI，接口契约(L2)未采集")
    elif not by_source.get("openapi"):
        gaps.append("OpenAPI 供给但未产出 L2 判据（不可读或无契约）")
    if d["migrations"] and not by_source.get("ddl"):
        gaps.append("迁移供给但未产出 L1 DDL 判据")
    state = INCOMPLETE if gaps else GREEN
    reason = "；".join(gaps) if gaps else "采集判据全部可执行化，零未转录零排除"
    return pillar(state, reason, [str(harvested), str(report)], c["numerator"], c["denominator"], c,
                  harvested_path=str(harvested), by_source=by_source)


def pillar_capability(d: dict, out: Path, units: Path | None, net: Path | None,
                      harvested: Path | None) -> dict[str, Any]:
    missing = [n for n, p in (("units.json", units), ("relation_net.json", net)) if p is None]
    if not d["java_roots"]:
        missing.append("源码根(src/main/java)")
    if missing:
        return pillar(INCOMPLETE, "缺上游输入: " + "、".join(missing) +
                      "（units/relation_net 由项目绑定流水线生成，本入口不伪造）", [])
    if harvested is None or not harvested.is_file():
        return pillar(INCOMPLETE, "缺判据输入：oracle 支柱未产出 harvested", [])
    report = out / "capability_map.json"
    argv = ["--units", str(units), "--oracles", str(harvested), "--relation-net", str(net),
            "--source-root", *map(str, d["java_roots"]), "--out", str(report)]
    if d["test_roots"]:
        argv += ["--test-root", *map(str, d["test_roots"])]
    rc, err, data = run_engine("capability_map.py", argv, report)
    if rc != 0 or not isinstance(data, dict):
        return failed("capability_map.py", rc, err, [str(report)])
    c = data["counts"]
    if c["unverified_edges"]:
        return pillar(RED, f"复核不过 {c['unverified_edges']} 条边（映射产出不可复核）",
                      [str(report)], c["oracles_implemented"], c["oracles_total"], c)
    gaps = []
    if c["oracles_unlinked"]:
        gaps.append(f"{c['oracles_unlinked']} 条判据无实现/消费证据")
    if c["field_write_edges_enforcement_unknown"]:
        gaps.append(f"{c['field_write_edges_enforcement_unknown']} 条写边执行证据 unknown")
    if c.get("unknown"):
        gaps.append(f"{c['unknown']} 个细胞使命 unknown")
    if not d["test_roots"]:
        gaps.append("未发现测试根，confirmed_test 证据缺失")
    state = INCOMPLETE if gaps else GREEN
    return pillar(state, "；".join(gaps) if gaps else "判据全部连边且执行证据完整",
                  [str(report)], c["oracles_implemented"], c["oracles_total"], c)


def pillar_arch(d: dict, out: Path, units: Path | None, edges: Path | None,
                rules: Path | None, layers: Path | None, members: Path | None,
                role_facts: Path | None) -> dict[str, Any]:
    missing = [n for n, p in (("units.json", units), ("edges.json", edges)) if p is None]
    if missing:
        return pillar(INCOMPLETE, "缺上游输入: " + "、".join(missing) + "（由项目绑定流水线生成）", [])
    rules_source = "provided"
    if rules is None:
        cfg = default_arch_config(d["packages"], members is not None)
        if cfg is None:
            return pillar(INCOMPLETE, "缺架构规则配置(--rules)，且未从仓库识别出分层包布局", [])
        out.mkdir(parents=True, exist_ok=True)
        rules = out / "arch_rules.default.json"
        rules.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        rules_source = "embedded_generic_convention"
    visibility_unchecked = rules_source != "provided" and members is None
    if layers is None:
        layers = DEFAULT_LAYERS if DEFAULT_LAYERS.is_file() else None
    if layers is None:
        return pillar(INCOMPLETE, "缺层契约(--layers)，默认 layers.yaml 不存在", [])
    report = out / "arch_rules.json"
    argv = ["--units", str(units), "--edges", str(edges), "--rules", str(rules),
            "--layers", str(layers), "--out", str(report)]
    if d["java_roots"]:
        argv += ["--source-root", str(d["java_roots"][0])]
    if members:
        argv += ["--members", str(members)]
    if role_facts:
        argv += ["--role-facts", str(role_facts)]
    rc, err, data = run_engine("arch_rules.py", argv, report)
    if rc != 0 or not isinstance(data, dict):
        return failed("arch_rules.py", rc, err, [str(report)])
    c = data["counts"]
    # 红线2：分子只算 compliant，not_applicable 单列不得进分子（坑3 入口层形态）。
    rule_counts = [r.get("counts", {}) for r in data.get("rules", [])]
    compliant = sum(rc.get("compliant", 0) for rc in rule_counts)
    not_applicable = sum(rc.get("not_applicable", 0) for rc in rule_counts)
    violation = sum(rc.get("violation", 0) for rc in rule_counts)
    unknown = sum(rc.get("unknown", 0) for rc in rule_counts)
    applicable = compliant + violation + unknown
    extra = {"rules_source": rules_source, "rules_confirmed": rules_source == "provided",
             "visibility_unchecked": visibility_unchecked, "input_warnings": data.get("input_warnings", []),
             "compliant": compliant, "not_applicable": not_applicable, "applicable_denominator": applicable,
             "total_evaluations": c["evaluations"], "idle_rules": c["rules_unmatched"],
             "applicable_over_total": round(applicable / c["evaluations"], 4) if c["evaluations"] else 0.0}
    if c["findings"] or c["declared_type_findings"]:
        reason = f"确定违反 resolved={c['findings']} declared={c['declared_type_findings']}"
        if rules_source != "provided":
            reason += "（默认惯例规则未经项目确认，开单前需人工确认）"
        return pillar(RED, reason, [str(report)], compliant, applicable, c, **extra)
    gaps = []
    if c["unknown_evaluations"] or c["declared_type_unknown_evaluations"]:
        gaps.append(f"unknown 评估 resolved={c['unknown_evaluations']} declared={c['declared_type_unknown_evaluations']}")
    if c["rules_unmatched"]:
        gaps.append(f"{c['rules_unmatched']} 条规则角色未匹配(空转/不可证伪)")
    if data.get("input_warnings"):
        gaps.append(f"{len(data['input_warnings'])} 条输入告警")
    if visibility_unchecked:
        gaps.append("visibility 规则未检查：缺成员声明(--members)")
    state = INCOMPLETE if gaps else GREEN
    reason = "；".join(gaps) if gaps else "适用域内零违反零 unknown"
    reason += f"（适用域 {applicable}/{c['evaluations']}，not_applicable {not_applicable} 单列）"
    return pillar(state, reason, [str(report)], compliant, applicable, c, **extra)


def pillar_emergence(d: dict, out: Path, units: Path | None, net: Path | None,
                     harvested: Path | None) -> dict[str, Any]:
    missing = [n for n, p in (("units.json", units), ("relation_net.json", net)) if p is None]
    if missing:
        return pillar(INCOMPLETE, "缺上游输入: " + "、".join(missing) + "（由项目绑定流水线生成）", [])
    report = out / "frame_check.json"
    argv = ["--net", str(net), "--units", str(units), "--out", str(report),
            "--business-levels", "L0_business_rule"]
    if harvested is not None and harvested.is_file():
        argv += ["--oracles", str(harvested)]
    try:
        net_doc = json.loads(net.read_text(encoding="utf-8"))
        nodes = net_doc.get("nodes") or []
        items = nodes.values() if isinstance(nodes, dict) else nodes
        if any(isinstance(n, dict) and n.get("type") == "REPO_OP" for n in items):
            argv += ["--plumbing-types", "REPO_OP"]
    except (OSError, ValueError):
        pass
    rc, err, data = run_engine("frame_check.py", argv, report)
    if rc != 0 or not isinstance(data, dict):
        return failed("frame_check.py", rc, err, [str(report)])
    c = data["counts"]
    candidates = data.get("candidates", [])
    # 三态纪律对称面（硬约束3 / 坑66 同族）：足迹重叠(frame_violation)证明的是 frame rule
    # 旁条件失效 mod(A)∩fp(B)≠∅ 这一**结构事实**——局部判定不可孤立组合，是待查线索，
    # 不是缺陷。已证实不一致的唯一机器通道是环一致性障碍(约束集不相交)；候选 δ⁰ 只有
    # consistent/unknown 两态，给不出 inconsistent。故 RED 只认证据充分的环障碍，
    # 「0 已证实 + N unknown」一律 INCOMPLETE，绝不把 unknown 洗成「确定违反」。
    proven = c.get("cycle_obstructions", 0)
    delta0 = c.get("candidates_delta0", {})
    unknown_n = delta0.get("unknown", 0)
    consistent_n = delta0.get("consistent", 0)
    overlap = [x for x in candidates
               if x.get("frame_violation") and x.get("evidence_strength") == "definite"]
    # 三口径分报：域内已测 / 域内 unknown / 域外 not_applicable。
    # 域外细胞本图根本不建模，既不是「已测无问题」也不是「域内待查」—— 必须单列且仍算没查完。
    # 否则 unknown_pairs_over_total 归零会被读成绿灯，那就是把 unknown 洗成 pass。
    in_dom = c.get("cells_in_domain", 0)
    in_meas = c.get("cells_in_domain_measured", 0)
    in_unk = c.get("cells_in_domain_unknown", 0)
    out_dom = c.get("cells_out_of_domain", 0)
    na_pairs = c.get("pairs_not_applicable", 0)
    headline = (f"已证实不一致 {proven} / 一致性未知 {unknown_n}；"
                f"域内已测 {in_meas}/{in_dom}、域内 unknown {in_unk}、域外 not_applicable {out_dom}")
    extra = {"proven_inconsistent": proven, "delta0_consistent": consistent_n,
             "delta0_unknown": unknown_n, "side_condition_leads": len(overlap),
             "cells_in_domain": in_dom, "cells_in_domain_measured": in_meas,
             "cells_in_domain_unknown": in_unk, "cells_out_of_domain": out_dom,
             "pairs_not_applicable": na_pairs,
             "domain_declared": c.get("domain_declared", False)}
    if proven:
        reason = headline
        if overlap:
            reason += f"；另有 {len(overlap)} 个候选足迹重叠(旁条件失效)待查"
        return pillar(RED, reason, [str(report)],
                      c["pairs_decidable"], c["pairs_total"], c, **extra)
    gaps = []
    if unknown_n:
        gaps.append(f"{unknown_n} 个候选一致性未知(待查线索,非确定缺陷)")
    if c["pairs_unknown"]:
        gaps.append(f"{c['pairs_unknown']} 对足迹 unknown 不可判")
    if c["cells_footprint_unknown"]:
        gaps.append(f"{c['cells_footprint_unknown']} 个细胞足迹 unknown")
    if out_dom:
        gaps.append(f"{out_dom} 个细胞不在关系网建模域内(not_applicable {na_pairs} 对；本图未建模，不是已测无问题)")
    # 「未声明建模域」不单独记缺口：frame_check 已就此发 input_warning（下面统一计），
    # 且保守回退把域外全算 unknown，已由 cells_footprint_unknown 覆盖。
    # 在这里再记一条会把「全已测的干净图」误判红（坑 78 反向假阳性，实测两条既有守卫红）。
    may = [x for x in candidates if x.get("evidence_strength") == "may_write_unknown"]
    if may:
        gaps.append(f"{len(may)} 个候选仅 may-write，违反不确定")
    if data.get("variety_deficit", {}).get("business_oracle_channel") == "INCOMPLETE":
        gaps.append("business-oracle 通道 INCOMPLETE（业务判据未测量，不是无）")
    if data.get("input_warnings"):
        gaps.append(f"{len(data['input_warnings'])} 条引擎输入告警")
    state = INCOMPLETE if gaps else GREEN
    reason = headline + ("；" + "；".join(gaps) if gaps else "；零候选零 unknown")
    return pillar(state, reason, [str(report)],
                  c["pairs_decidable"], c["pairs_total"], c, **extra)


# ---------------------------------------------------------------- 上游生产者

PILOT_DIR = ENGINE_DIR.parent
# 成员声明的输入形状（是**形状**不是业务规则）：上游容器叫 functions，规范容器叫 members。
MEMBER_SHAPE = {"member_source": [{"records": "functions", "class": "class", "method": "method",
                                   "modifiers": "modifiers"}]}


def run_producer(script: Path, argv: list[str], out_path: Path | None,
                 env: dict[str, str] | None = None) -> tuple[int | None, str, Any]:
    """跑上游生产者（项目侧脚本，不在 engine/）。与 run_engine 分开，不动既有桩契约。"""
    cmd = [sys.executable, str(script), *argv]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(script.parent), env=env)
    except OSError as exc:
        return None, f"无法启动 {script.name}: {exc}", None
    data = None
    if out_path is not None and out_path.is_file():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    return proc.returncode, proc.stderr.strip()[-1500:], data


def find_bytecode(repo: Path) -> Path | None:
    """找已编译的 target/classes（只读复用）。找不到不编译 —— 禁 maven/javac。"""
    for cur, dirs, _files in os.walk(repo):
        dirs[:] = sorted(x for x in dirs if x not in ("node_modules", ".git", "__pycache__"))
        p = Path(cur)
        if p.name == "classes" and p.parent.name == "target" and any(p.rglob("*.class")):
            return p
    return None


def find_entity_dir(java_roots: list[Path]) -> Path | None:
    for jr in java_roots:
        for p in sorted(jr.rglob("entity")):
            if p.is_dir():
                return p
    return None


def derive_java_package(java_roots: list[Path]) -> str:
    """从源码布局推包前缀：所有 .java 父目录的最长公共前缀。推不出返回空串。"""
    rels = [p.parent.relative_to(jr).as_posix() for jr in java_roots for p in jr.rglob("*.java")]
    rels = [r for r in rels if r and r != "."]
    return os.path.commonpath(rels).replace("/", ".") if rels else ""


# ---------------------------------------------------------------- 入口契约 / 锚表推导
# 这两项原先被声明为「不可约、无生产者」。实测形状可推导，但**推导 ≠ 人工确认**：
# 产物与缺口都显式标降级，人工供给永远优先。

MAPPING_VERBS = {"GetMapping": "get", "PostMapping": "post", "PutMapping": "put",
                 "DeleteMapping": "delete", "PatchMapping": "patch"}
_ANN_VALUE = re.compile(r'(?:value|path)\s*=\s*(\[[^\]]*\]|"[^"]*")')
_ANN_STR = re.compile(r'"([^"]*)"')
_TAG_NAME = re.compile(r'name\s*=\s*"([^"]*)"')


def annotation_paths(raw: str) -> list[str]:
    """注解 raw 里的 path 列表。无 value/path → 空列表（= 不追加子路径）。"""
    m = _ANN_VALUE.search(raw or "")
    return _ANN_STR.findall(m.group(1) if m else (raw or ""))


def join_entry_path(base: str, sub: str) -> str:
    """类级 + 方法级 path 拼接：空 path 不产生双斜杠，尾斜杠保留。"""
    segs = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    tail = sub if sub else base
    return "/" + "/".join(segs) + ("/" if tail.endswith("/") else "")


def synthesize_entry_contract(fn_decl: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """mapping 注解 → 入口契约（OpenAPI 形状，relation_net.bind_entries 原样消费）。

    只供入口绑定，**不进判据采集**：DTO 约束已由 annotation 判据采过，再采一遍是重复计数。
    fn_declaration 不区分类级/方法级注解（javap 把类级注解挂在该类某个方法记录上），
    这里按「无 method= 的 RequestMapping 即类级基路径」转录；分不清的记 ambiguous，不猜。
    """
    by_class: dict[str, list[dict]] = defaultdict(list)
    for f in fn_decl.get("functions") or []:
        if f.get("class"):
            by_class[f["class"]].append(f)
    paths: dict[str, Any] = {}
    info: dict[str, Any] = {"controllers": 0, "ambiguous_classes": [], "path_collisions": [],
                            "controllers_without_mapping": [],
                            "tag_basis": "声明的 @Tag name + 声明类简名（两者都是字节码事实）"}
    seq = 0
    for cls, fs in sorted(by_class.items()):
        simple = cls.rsplit(".", 1)[-1]
        if not simple.endswith("Controller"):
            continue
        info["controllers"] += 1
        mappings = [(f, a) for f in fs for a in (f.get("annotations") or [])
                    if a.get("type", "").rsplit(".", 1)[-1] in MAPPING_VERBS]
        if not mappings:
            info["controllers_without_mapping"].append(cls)
            continue
        bases_raw = sorted({(a.get("raw") or "") for f in fs for a in (f.get("annotations") or [])
                            if a.get("type", "").rsplit(".", 1)[-1] == "RequestMapping"})
        if len(bases_raw) > 1 or any("method" in r for r in bases_raw):
            info["ambiguous_classes"].append({"class": cls, "request_mapping_raw": bases_raw})
            continue
        bases = (annotation_paths(bases_raw[0]) if bases_raw else [""]) or [""]
        tag = ""
        for f in fs:
            for a in f.get("annotations") or []:
                if a.get("type", "").rsplit(".", 1)[-1] == "Tag":
                    m = _TAG_NAME.search(a.get("raw") or "")
                    if m:
                        tag = m.group(1)
        for f, a in mappings:
            verb = MAPPING_VERBS[a["type"].rsplit(".", 1)[-1]]
            for sub in annotation_paths(a.get("raw")) or [""]:
                for base in bases:
                    path = join_entry_path(base, sub)
                    seq += 1
                    op = {"operationId": f"{f['method']}_{seq}",
                          "tags": [t for t in (tag, simple) if t]}
                    slot = paths.setdefault(path, {})
                    if verb in slot:
                        info["path_collisions"].append(
                            {"path": path, "verb": verb, "kept": slot[verb]["operationId"],
                             "dropped": op["operationId"]})
                        continue
                    slot[verb] = op
    info["operations"] = sum(len(v) for v in paths.values())
    info["path_items"] = len(paths)
    doc = {"openapi": "3.0.1",
           "info": {"title": "derived-entry-contract", "version": "0",
                    "description": "由 mapping 注解静态推导，非应用运行时导出的 /v3/api-docs；"
                                   "只供入口绑定，不含 schema，不用于判据采集"},
           "paths": paths}
    return doc, info


def derive_invariant_anchors(bridge_report: Path, harvested: Path,
                             class_decl: dict[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    """已确认判据 → 不变式锚表（**推导，非人工业务声明**）。

    转录复用 oracle_bridge.to_invariants（不另立转录表，坑 11）。另加两道自己的闸：
      · 逐条回核采集器 confirmation —— 桥的过滤若回归，这里拒绝而不是洗白；
      · entity 只在能唯一落到真实 JPA 实体时才写：写 DTO 名会让 relation_net 的
        snake_case 兜底造出**不存在的表**，那是伪造绑定，比缺绑定更坏。
    """
    result = json.loads(bridge_report.read_text(encoding="utf-8"))
    oracles = json.loads(harvested.read_text(encoding="utf-8")).get("oracles") or []
    conf = {o.get("id"): o.get("confirmation") for o in oracles}
    targets = {o.get("id"): (o.get("target") or {}) for o in oracles}
    ent_map = {k.rsplit(".", 1)[-1]: v["table"] for k, v in (class_decl.get("jpa") or {}).items()
               if isinstance(v, dict) and v.get("table")}
    # 只认 DDL 里真存在的表：jpa 可能声明了没有迁移的表（如基类），写进去就是伪造绑定。
    ddl_tables = set((class_decl.get("ddl") or {}).get("tables") or {})
    ent_map = {e: t for e, t in ent_map.items() if t in ddl_tables}
    by_table: dict[str, list[str]] = defaultdict(list)
    for e, t in ent_map.items():
        by_table[t].append(e)

    mapped, refused = [], []
    for m in result.get("mapped") or []:
        if conf.get(m.get("oracle_id")) != oracle_bridge.CONFIRMED:
            refused.append(m.get("id"))
        else:
            mapped.append(m)
    oid_of = {m["id"]: m.get("oracle_id") for m in mapped}
    out, bound, unbound, dto = [], [], [], []
    for inv in oracle_bridge.to_invariants({**result, "mapped": mapped}):
        t = targets.get(oid_of.get(inv["id"]), {})
        ent = str(t.get("entity") or "")
        if ent and ent not in ent_map:
            dto.append(inv["id"])
            ent = ""
        if not ent:
            cands = by_table.get(str(t.get("table") or ""), [])
            ent = cands[0] if len(cands) == 1 else ""
        if ent:
            bound.append(inv["id"])
            out.append({**inv, "entity": ent})
        else:
            unbound.append(inv["id"])
            out.append(inv)
    # 跨字段口径按 assert 是否含 right 键判（field_*_field 的结构特征），不另立原语名清单（坑 11）。
    cross = sum(1 for i in out if "right" in i["assert"])
    info = {"transcriber": "oracle_bridge.to_invariants", "derived": True, "human_declared": False,
            "invariants": len(out), "entity_bound": len(bound), "no_container": len(unbound),
            "dto_container_not_entity": len(dto), "refused_unconfirmed": refused,
            "jpa_entities_available": len(ent_map),
            "cross_field_assertions": cross, "single_field_assertions": len(out) - cross,
            "note": "锚表由已确认的**单字段技术约束**推导，非人工业务声明。"
                    "跨字段业务口径不在类推导覆盖范围内，仍需人工 --invariants 供给。"
                    "no_container 条锚不到表（不是「已验证」）；"
                    "dto_container_not_entity 的容器是 DTO 不是 JPA 实体，故不写 entity，"
                    "以免消费者兜底造出不存在的表；entity 只写 DDL 里真存在的表"}
    return out, info


def minimal_binding(repo: Path, d: dict, out: Path, layers: Path | None) -> Path:
    """自动派生最小项目绑定（**结构派生，非项目确认**）。只写 out，供上游脚本读路径。

    carriers / obligation_templates 留空 → 图里 fidelity=unknown、义务表述为空，
    这是诚实降级而不是伪造；派生事实记进 info 由调用方报缺口。
    """
    pkg = derive_java_package(d["java_roots"])
    contract = layers if layers and layers.is_file() else DEFAULT_LAYERS
    # backend 必须是**含 src/main/java 的模块根**，不是仓库根：file_static 用
    # BACKEND/src/main/java 反查源文件并算相对路径，给错了会让 63 个 file 字段全成 null，
    # 进而使同类自调用边从 61 塌到 2（实测过）。多模块仓库取第一个源码根的模块。
    jr = d["java_roots"][0] if d["java_roots"] else None
    backend = jr.parents[2] if jr and jr.as_posix().endswith("src/main/java") else repo
    path = out / "upstream" / "target.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "target": repo.name,
        "contract": str(contract),
        "annotation_table": "",
        "evidence_sources": {},
        "source_roots": {"java": str(jr) if jr else "",
                         "java_package": pkg.replace(".", "/")},
        "carriers": {}, "layer_carriers": {}, "obligation_templates": {},
        "paths": {"backend": str(backend), "java_package": pkg.replace(".", "/"),
                  "work_dir": str(out / "upstream" / "work")},
        "note": "audit_entry 自动派生的最小绑定：仅结构事实，未经项目确认；"
                "carriers/obligation_templates 为空是诚实降级",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def produce_upstream(repo: Path, d: dict, out: Path, overrides: dict[str, Any],
                     bridge_report: Path | None = None, harvested: Path | None = None
                     ) -> tuple[Path | None, Path | None, Path | None, Path | None, list[str], dict]:
    """缺图产物时驱动上游链：声明抽取 → units/edges → relation_net → members。

    全部写 out/upstream，绝不写被测仓库；**不编译**（复用已有字节码）。
    openapi 与不变式锚表缺人工供给时**尝试静态推导**（mapping 注解 / 已确认判据），
    推导成功也显式记降级缺口 —— 推导不是人工确认。推不出则记缺口，不伪造产物。
    """
    gaps: list[str] = []
    info: dict[str, Any] = {"derived": True}
    up = out / "upstream"
    up.mkdir(parents=True, exist_ok=True)

    classes = overrides.get("classes_dir") or find_bytecode(repo)
    if classes is None or not Path(classes).is_dir():
        gaps.append("缺字节码目录(--classes-dir)：本入口不编译（禁 maven/javac），"
                    "函数层/文件层声明无法抽取")
        return None, None, None, None, gaps, info
    info["classes_dir"] = str(classes)

    layers = overrides.get("layers")
    binding = overrides.get("target") or minimal_binding(repo, d, out, layers)
    info["binding"] = str(binding)
    info["binding_source"] = "provided" if overrides.get("target") else "auto_derived"
    env = {**os.environ, "LAYERED_AUDIT_TARGET": str(binding)}

    fn_data: Any = None
    for script, name in (("fn_static.py", "fn_declaration.json"),
                         ("file_static.py", "file_declaration.json")):
        rc, err, data = run_producer(PILOT_DIR / script,
                                     ["--classes-dir", str(classes), "--out", str(up / name)],
                                     up / name, env)
        if rc != 0:
            gaps.append(f"{script} 未成功退出(rc={rc})：{err or '无 stderr'}")
            return None, None, None, None, gaps, info
        if name == "fn_declaration.json":
            fn_data = data

    mig_dir = Path(d["migrations"][0]).parent if d["migrations"] else None
    ent_dir = overrides.get("entity_dir") or find_entity_dir(d["java_roots"])
    class_data: Any = None
    if mig_dir and ent_dir:
        rc, err, class_data = run_producer(PILOT_DIR / "class_static.py",
                                           ["--migration-dir", str(mig_dir), "--entity-dir", str(ent_dir),
                                            "--out", str(up / "class_declaration.json")],
                                           up / "class_declaration.json", env)
        if rc != 0:
            gaps.append(f"class_static.py 未成功退出(rc={rc})：{err or '无 stderr'}")
            class_data = None
    else:
        missing = [n for n, v in (("迁移目录(--migrations)", mig_dir),
                                  ("实体目录(--entity-dir)", ent_dir)) if not v]
        gaps.append("类层声明未抽取，缺 " + "、".join(missing) + "（关系网将没有表节点与不变量锚）")

    if d["openapi"]:
        (up / "openapi.json").write_text(Path(d["openapi"]).read_text(encoding="utf-8"),
                                         encoding="utf-8")
        info["entry_contract"] = {"source": "provided", "path": str(d["openapi"])}
    else:
        doc, syn = synthesize_entry_contract(fn_data if isinstance(fn_data, dict) else {})
        if not isinstance(fn_data, dict):
            syn["unavailable_reason"] = "函数声明产物不可解析"
        if syn["operations"]:
            (up / "openapi.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
            info["entry_contract"] = {"source": "derived_from_mapping_annotations", **syn}
            gaps.append(f"openapi 由 mapping 注解推导（{syn['operations']} 个操作 / "
                        f"{syn['path_items']} 个 path，非运行时 /v3/api-docs），只供入口绑定、不含 schema；"
                        "L2 接口契约判据仍缺，需 --openapi 供给真实契约")
        else:
            info["entry_contract"] = {"source": "unavailable", **syn}
            gaps.append("openapi 无生产者（仓库内也未发现，mapping 注解也推不出入口）：需 --openapi 显式供给，"
                        "否则关系网没有 HTTP 入口节点")
    if overrides.get("invariants"):
        (up / "invariants.json").write_text(
            Path(overrides["invariants"]).read_text(encoding="utf-8"), encoding="utf-8")
        info["invariant_anchors"] = {"source": "provided", "path": str(overrides["invariants"]),
                                     "human_declared": True}
    elif bridge_report is not None and harvested is not None:
        invs, di = derive_invariant_anchors(bridge_report, harvested,
                                            class_data if isinstance(class_data, dict) else {})
        if invs:
            (up / "invariants.json").write_text(
                json.dumps({"invariants": invs, "derivation": di}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            info["invariant_anchors"] = {"source": "derived_from_confirmed_oracles", **di}
            gaps.append(f"业务不变式锚表由已确认的单字段技术约束推导（{di['invariants']} 条，"
                        f"{di['entity_bound']} 条落到真实表、{di['no_container']} 条无容器，"
                        f"跨字段断言 {di['cross_field_assertions']} 条），非人工业务声明；"
                        "跨字段业务口径仍需人工 --invariants 供给（类推导不覆盖，"
                        "人工业务不变式仍是不可约输入）")
        else:
            info["invariant_anchors"] = {"source": "unavailable", **di}
            gaps.append("业务不变式是人工声明，无生产者（已确认判据也推不出锚表）：需 --invariants 供给，"
                        "否则关系网零不变量锚（不是「无不变量」）")
    else:
        info["invariant_anchors"] = {"source": "unavailable", "reason": "判据桥未产出报告"}
        gaps.append("业务不变式是人工声明，无生产者：需 --invariants 供给，"
                    "否则关系网零不变量锚（不是「无不变量」）")

    rc, err, _ = run_producer(ENGINE_DIR / "build_agent_graph.py",
                              ["--artifacts-dir", str(up), "--out-dir", str(up)],
                              up / "units.json", env)
    if rc != 0:
        gaps.append(f"build_agent_graph.py 未成功退出(rc={rc})：{err or '无 stderr'}")
        return None, None, None, None, gaps, info
    rc, err, net_data = run_producer(ENGINE_DIR / "relation_net.py",
                                     ["--artifacts-dir", str(up), "--project-dir", str(up),
                                      "--out", str(up / "relation_net.json")],
                                     up / "relation_net.json")
    if rc != 0:
        gaps.append(f"relation_net.py 未成功退出(rc={rc})：{err or '无 stderr'}")
        return None, None, None, None, gaps, info
    if isinstance(net_data, dict):
        nc = net_data.get("counts") or {}
        # 推导增益与四支柱分子并排可见：这三个数涨不代表任何支柱分子涨（支柱分子另行报告）。
        info["graph_counts"] = {k: nc.get(k) for k in ("bindings", "unbound", "invariants")}

    members = overrides.get("members")
    if not members:
        rules = up / "member_shape.json"
        rules.write_text(json.dumps(MEMBER_SHAPE, ensure_ascii=False), encoding="utf-8")
        rc, err, _ = run_producer(ENGINE_DIR / "member_declaration.py",
                                  ["--input", f"fn={up / 'fn_declaration.json'}",
                                   "--rules", str(rules), "--out", str(up / "members.json")],
                                  up / "members.json")
        if rc == 0:
            members = up / "members.json"
        else:
            gaps.append(f"成员声明未产出(rc={rc})：visibility 规则不可检查 —— {err or '无 stderr'}")
    info["upstream_dir"] = str(up)
    return up / "units.json", up / "edges.json", up / "relation_net.json", members, gaps, info


# ---------------------------------------------------------------- 编排

def aggregate_exit(states: list[str]) -> int:
    """0 全绿 / 1 有红 / 2 没查完。INCOMPLETE 绝不许当 0（D8）。"""
    if RED in states:
        return 1
    return 2 if INCOMPLETE in states else 0


def _with_upstream_gaps(p: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    """上游缺口必须让图支柱保持 INCOMPLETE —— 降级图上算出的数不许当完整结论。"""
    if not gaps:
        return p
    if p["state"] != RED:
        p["state"] = INCOMPLETE
    text = "上游缺口: " + "；".join(gaps)
    p["reason"] = f"{p['reason']}；{text}" if p.get("reason") else text
    p["upstream_gaps"] = list(gaps)
    return p


def audit(repo: Path, out: Path, overrides: dict[str, Any]) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    d = discover(repo)
    for key in ("docs", "migrations", "java_roots", "test_roots"):
        if overrides.get(key):
            d[key] = overrides[key]
    if overrides.get("openapi"):
        d["openapi"] = overrides["openapi"]
    units = overrides.get("units") or find_artifact(repo, "units.json")
    edges = overrides.get("edges") or find_artifact(repo, "edges.json")
    net = overrides.get("relation_net") or find_artifact(repo, "relation_net.json")

    # 判据支柱先跑：锚表推导要吃它的桥报告（原先顺序相反，推导拿不到输入）。
    oracle = pillar_oracle(d, out, repo.name)
    harvested = Path(oracle["harvested_path"]) if oracle.get("harvested_path") else None
    bridge_report = out / "oracle_bridge.json"

    upstream_gaps: list[str] = []
    upstream_info: dict[str, Any] = {"attempted": False}
    members = overrides.get("members")
    if units is None or edges is None or net is None:
        produced = produce_upstream(repo, d, out, overrides,
                                    bridge_report=bridge_report if bridge_report.is_file() else None,
                                    harvested=harvested)
        p_units, p_edges, p_net, p_members, upstream_gaps, upstream_info = produced
        upstream_info["attempted"] = True
        units, edges, net = units or p_units, edges or p_edges, net or p_net
        members = members or p_members

    discovery = {
        "java_roots": {"paths": list(map(str, d["java_roots"])), "rule": "src/main/java 目录"},
        "test_roots": {"paths": list(map(str, d["test_roots"])), "rule": "src/test/java 目录"},
        "migrations": {"paths": list(map(str, d["migrations"])), "rule": "migration/migrations 下 *.sql"},
        "docs": {"paths": list(map(str, d["docs"])), "rule": "仓库根 *.md + docs/ 下 *.md"},
        "openapi": {"paths": [str(d["openapi"])] if d["openapi"] else [], "rule": "openapi*/swagger*.{json,yaml,yml}"},
        "packages": {"paths": sorted(d["packages"]), "rule": "源码根下分层包名"},
        "artifacts": {"units": str(units) if units else None, "edges": str(edges) if edges else None,
                      "relation_net": str(net) if net else None,
                      "rule": "显式供给或仓库根/.audit 下同名产物，缺则图支柱 INCOMPLETE"},
    }
    capability = _with_upstream_gaps(pillar_capability(d, out, units, net, harvested), upstream_gaps)
    arch = _with_upstream_gaps(
        pillar_arch(d, out, units, edges, overrides.get("rules"), overrides.get("layers"),
                    members, overrides.get("role_facts")), upstream_gaps)
    emergence = _with_upstream_gaps(pillar_emergence(d, out, units, net, harvested), upstream_gaps)
    pillars = {"oracle": oracle, "capability": capability, "architecture": arch, "emergence": emergence}
    states = [p["state"] for p in pillars.values()]
    exit_code = aggregate_exit(states)
    return {"schema": "audit-entry/1", "repo": str(repo), "work_dir": str(out),
            "discovery": discovery, "pillars": pillars, "exit_code": exit_code,
            "upstream": upstream_info, "upstream_gaps": upstream_gaps,
            "limitations": [
                "图支柱上游(units/edges/relation_net)缺省时由本入口驱动生产者生成到 --out/upstream，"
                "复用已有字节码、不编译；缺不可约输入则显式报缺口并保持 INCOMPLETE",
                "openapi 与不变式锚表缺人工供给时按**静态推导降级**，两者都仍是需人工供给的输入："
                "① 入口契约由 mapping 注解合成，只供入口绑定、不含 schema，非运行时 /v3/api-docs，"
                "故 L2 接口契约判据仍如实报缺；② 锚表由已确认的**单字段技术约束**推导，"
                "**跨字段业务口径仍需人工 --invariants 供给（类推导不覆盖）**，无容器的条锚不到表、"
                "不是「已验证」。人工供给永远优先于推导，且**不与推导合并**（两类判据人口不相交，"
                "合并会混淆口径）；推导版仍记缺口、图支柱保持 INCOMPLETE。"
                "推导只改变关系网的绑定/锚计数，**不改变四支柱任何分子分母**",
                "自动派生的最小绑定只含结构事实(包前缀/源码根)，carriers 与义务表述为空是诚实降级，未经项目确认",
                "默认架构规则是通用分层惯例(convention，未经项目确认)，findings 开单前需人工确认",
                "业务 PRD 若在仓库外，需 --docs 显式供给，否则 L0 业务规则采集不全",
                "本入口只做静态编排，不产出动态观测；判据执行判定需另接求值器与观测",
            ]}


def main() -> int:
    ap = argparse.ArgumentParser(description="零配置四支柱审计入口（三态报告，只读被测仓库）")
    ap.add_argument("--repo", required=True, type=Path, help="被测仓库路径")
    ap.add_argument("--out", type=Path, help="产物目录（默认临时目录，绝不写被测仓库）")
    ap.add_argument("--docs", type=Path, nargs="*", help="覆盖文档发现（仓库外 PRD 在此供给）")
    ap.add_argument("--migrations", type=Path, nargs="*", help="覆盖迁移 SQL 发现")
    ap.add_argument("--java-root", type=Path, nargs="*", help="覆盖源码根发现")
    ap.add_argument("--test-root", type=Path, nargs="*", help="覆盖测试根发现")
    ap.add_argument("--openapi", type=Path, help="覆盖 OpenAPI 发现")
    ap.add_argument("--units", type=Path, help="上游 units.json")
    ap.add_argument("--edges", type=Path, help="上游 edges.json")
    ap.add_argument("--relation-net", type=Path, help="上游 relation_net.json")
    ap.add_argument("--rules", type=Path, help="架构规则配置（缺省用通用惯例默认）")
    ap.add_argument("--layers", type=Path, help="层契约（缺省用随附 layers.yaml）")
    ap.add_argument("--members", type=Path, help="成员声明（visibility 规则需要）")
    ap.add_argument("--role-facts", type=Path, help="角色机制事实")
    ap.add_argument("--classes-dir", type=Path,
                    help="已有字节码目录（只读复用，绝不编译）；缺省自动找 target/classes")
    ap.add_argument("--invariants", type=Path, help="业务不变式声明（人工供给，优先于判据推导）")
    ap.add_argument("--target", type=Path, help="项目绑定 target.json（缺省自动派生最小绑定）")
    ap.add_argument("--entity-dir", type=Path, help="JPA 实体目录（缺省从源码根找 entity 包）")
    args = ap.parse_args()
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"仓库路径不存在或不是目录: {repo}", file=sys.stderr)
        return 2
    out = args.out.resolve() if args.out else Path(tempfile.mkdtemp(prefix="audit-entry-"))
    if out == repo or repo in out.parents:
        print(f"拒绝：--out 落在被测仓库内({out})，审计绝不写被测仓库；请用仓库外目录或默认临时目录",
              file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    def rl(ps):
        return [p.resolve() for p in ps] if ps else ps

    def r1(p):
        return p.resolve() if p else p

    overrides = {"docs": rl(args.docs), "migrations": rl(args.migrations), "java_roots": rl(args.java_root),
                 "test_roots": rl(args.test_root), "openapi": r1(args.openapi), "units": r1(args.units),
                 "edges": r1(args.edges), "relation_net": r1(args.relation_net), "rules": r1(args.rules),
                 "layers": r1(args.layers), "members": r1(args.members), "role_facts": r1(args.role_facts),
                 "classes_dir": r1(args.classes_dir), "invariants": r1(args.invariants),
                 "target": r1(args.target), "entity_dir": r1(args.entity_dir)}
    report = audit(repo, out, overrides)
    report_path = out / "audit_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"仓库 {repo}  产物 {out}")
    for name, p in report["pillars"].items():
        frac = f"{p['numerator']}/{p['denominator']}" if p["denominator"] is not None else "—"
        print(f"  [{p['state']:10s}] {name:13s} {frac:>10s}  {p['reason']}")
    print(f"退出码 {report['exit_code']}（0 全绿 / 1 有红 / 2 没查完）  报告 {report_path}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())