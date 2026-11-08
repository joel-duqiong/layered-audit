"""第二载体复核（通用层，零项目字面量）。

文件层载体（字节码整类实例化）与类层载体（反射导出）能各自独立看到同一批结构事实。
本模块把**文件载体的观测**挂到**类层的结论**上：

  - 挂上了 -> 同一条结论被两个独立载体各自复现（服务的正是「可复现」这条性质）
  - 没挂上 -> 文件载体看到了、却没有任何层开单 = **覆盖缺口**，必须报出来，
    不能让观测悄悄消失（这正是「有观测但没人开单」的盲区形态）

项目侧要做的只有一件事：在 class_scenarios.json 的判定参数里声明
「这条判定对应文件载体的哪个检查器」（file_carrier_detector）。
哪个检查器对哪条判定，是项目知识；怎么匹配、怎么记缺口，是通用逻辑。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 证据里最可能代表「这是哪个类」的键，按可信度排序
_CLASS_KEYS = ("class", "overrider", "entity", "target_class", "class_name")


def load(artifacts_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """读文件层的检查器产物。文件层没跑（增量跳过）时返回空 dict。"""
    path = Path(artifacts_dir) / "file_detectors.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


def _class_of(evidence: Any) -> str | None:
    if not isinstance(evidence, dict):
        return None
    for k in _CLASS_KEYS:
        v = evidence.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _match(observation: dict[str, Any], evidence: Any) -> bool:
    """一条文件载体的观测是否就是这条结论说的那件事。

    主判据：观测里的 class 与结论证据里的类名一致（优先取证据里的 class 键）。
    观测带 method 时还要 method 一致 —— 同一类上可能有多处泄漏，方法必须对上。
    """
    want_class = _class_of(evidence)
    got_class = observation.get("class")
    if not isinstance(got_class, str):
        return False
    if want_class is not None:
        if got_class != want_class:
            return False
    else:
        # 证据里没有类名：退化成「类名出现在证据的任何字符串值里」
        values = {v for v in evidence.values() if isinstance(v, str)} if isinstance(evidence, dict) else set()
        if got_class not in values and not any(got_class in v for v in values):
            return False
    if isinstance(observation.get("method"), str):
        values = {v for v in evidence.values() if isinstance(v, str)} if isinstance(evidence, dict) else set()
        if observation["method"] not in values:
            return False
    return True


def attach(finding: dict[str, Any], detector: str | None, ctx: dict) -> dict[str, Any]:
    """把文件载体对同一件事的独立观测挂到结论的证据上。"""
    if not detector:
        return finding
    detectors = ctx.get("file_detectors") or {}
    observations = detectors.get(detector) or []
    hits = [o for o in observations if _match(o, finding.get("evidence"))]
    if not hits:
        return finding
    evidence = finding.get("evidence")
    if isinstance(evidence, dict):
        evidence = dict(evidence)
        evidence["file_carrier"] = {
            "detector": detector,
            "observations": hits,
        }
        finding = dict(finding)
        finding["evidence"] = evidence
    consumed = ctx.setdefault("file_carrier_consumed", set())
    for o in hits:
        consumed.add((detector, o.get("class"), o.get("method")))
    return finding


def _excluded(observation: dict[str, Any], detector: str,
              exclusions: list[dict[str, Any]]) -> str | None:
    """这条观测是否被项目声明为「已知不适用」。

    排除规则是项目知识（哪些面在这个项目里本来就不该判），声明在 class_scenarios.json：
        {"detector": "...", "class_pattern": "...", "reason": "..."}
    class_pattern 是正则（对类名匹配）；detector 省略则对所有检查器生效。
    排除掉的理由会一起记进产物 —— 排除不是「让它消失」，是「让决策留下痕迹」。
    """
    for ex in exclusions:
        if ex.get("detector") and ex["detector"] != detector:
            continue
        pattern = ex.get("class_pattern")
        if pattern and not re.search(pattern, str(observation.get("class") or "")):
            continue
        return str(ex.get("reason") or "项目声明不适用")
    return None


def unowned(detectors: dict[str, list[dict[str, Any]]],
            consumed: set[tuple[Any, Any, Any]],
            exclusions: list[dict[str, Any]] | None = None
            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """文件载体看到、但没有任何结论认领的观测。

    返回 (缺口清单, 已声明不适用的清单)。
    缺口 = 有人看到了、没人认领、也没声明不适用 —— 那是真的盲区。
    """
    exclusions = exclusions or []
    gaps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for detector, observations in detectors.items():
        for o in observations:
            key = (detector, o.get("class"), o.get("method"))
            if key in consumed:
                continue
            reason = _excluded(o, detector, exclusions)
            if reason:
                skipped.append({"detector": detector, "observation": o, "reason": reason})
            else:
                gaps.append({"detector": detector, "observation": o})
    return gaps, skipped
