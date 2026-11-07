"""证据 schema —— 单一事实来源是 ../layers.yaml，本模块只做读取与校验。

layers.yaml 的 evidence.required 声明了 10 个必填字段。这里做两件事：

1. 从 layers.yaml 解析「层 -> 允许的 category 列表」。不硬编码，避免契约漂移；
   解析用最小正则子集（本项目的 YAML 只用到固定形状），不引入 pyyaml 依赖。
2. 校验归一化后的 finding 是否满足契约，输出可读的违规清单。

语义约定：
- ground_truth=verified 表示「结论已由实测或可复算证据确认」，正向结论（positive）
  同样可以是 verified —— 表示「该性质确实成立」。
- repro_cmd 允许为空字符串（契约原文：可复跑命令，无则空），但覆盖率会被单独统计。
- category 用「层.类别」限定形式（如 function.boundary），无合适类别时取 UNMAPPED，
  并在 fit 字段标注 exact / stretched / unmapped。
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# 项目绑定配置：换项目只改这个文件 + 换标注表 + 重写 agent 脚本，引擎零改动。
# 默认位置 engine/ 的同级目录 target.json；可用环境变量 LAYERED_AUDIT_TARGET 覆盖。
ENV_TARGET = "LAYERED_AUDIT_TARGET"
DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "target.json"

REQUIRED_FIELDS: tuple[str, ...] = (
    "id", "layer", "category", "location", "claim",
    "evidence_refs", "repro_cmd", "confidence", "ground_truth", "cwe",
)
OPTIONAL_FIELDS: tuple[str, ...] = (
    "triggering_condition", "affected_scope", "counter_evidence",
)
LAYERS: tuple[str, ...] = ("function", "file", "class", "service", "integration")
GROUND_TRUTH: tuple[str, ...] = ("verified", "unverified", "mutant_survivor")
CATEGORY_FIT: tuple[str, ...] = ("exact", "stretched", "unmapped")
UNMAPPED = "UNMAPPED"


@functools.lru_cache(maxsize=1)
def target_path() -> Path:
    return Path(os.environ.get(ENV_TARGET, str(DEFAULT_TARGET))).resolve()


@functools.lru_cache(maxsize=1)
def load_target() -> dict[str, Any]:
    """读项目绑定配置。缺段就报错，不猜默认值。"""
    path = target_path()
    if not path.exists():
        raise FileNotFoundError(f"找不到项目绑定配置: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("contract", "source_roots", "annotation_table", "evidence_sources")
               if k not in data]
    if missing:
        raise ValueError(f"{path.name} 缺少必填段: {missing}")
    return data


@functools.lru_cache(maxsize=1)
def contract_path() -> Path:
    """层契约路径（相对 target.json 所在目录）。"""
    base = target_path().parent
    return (base / load_target()["contract"]).resolve()


@functools.lru_cache(maxsize=1)
def load_annotation_table() -> ModuleType:
    """加载项目标注表（category / claim / owner / confidence / root_cause_key 的人工判定）。"""
    base = target_path().parent
    path = (base / load_target()["annotation_table"]).resolve()
    spec = importlib.util.spec_from_file_location("target_annotation_table", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载标注表: {path}")
    module = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules：dataclass 在装饰时会按 __module__ 反查模块，
    # 没登记会抛 AttributeError（Python 3.14 更严格）
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_layer_categories(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """从 layers.yaml 解析每层允许的 category 列表。解析不出来就报错，不猜。"""
    text = (path or contract_path()).read_text(encoding="utf-8")
    layers: dict[str, list[str]] = {}
    current: str | None = None
    in_categories = False
    for line in text.splitlines():
        m_layer = re.match(r"^  - id: (\w+)\s*$", line)
        if m_layer:
            current = m_layer.group(1)
            layers.setdefault(current, [])
            in_categories = False
            continue
        if re.match(r"^    categories:\s*$", line):
            in_categories = True
            continue
        if in_categories:
            m_cat = re.match(r"^      - \{id: (\w+),", line)
            if m_cat:
                if current is None:
                    raise ValueError("layers.yaml 结构异常：categories 出现在层定义之前")
                layers[current].append(m_cat.group(1))
                continue
            if line.strip():
                in_categories = False
    missing = [lay for lay in LAYERS if lay not in layers]
    if missing:
        raise ValueError(f"layers.yaml 缺少层定义: {missing}")
    empty = [lay for lay, cats in layers.items() if not cats]
    if empty:
        raise ValueError(f"layers.yaml 这些层没有 category: {empty}")
    return {lay: tuple(cats) for lay, cats in layers.items()}


def load_escalation_targets(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """解析每层的 escalation_to（允许上提的层）。用于校验归属是否合法。"""
    text = (path or contract_path()).read_text(encoding="utf-8")
    out: dict[str, tuple[str, ...]] = {}
    current: str | None = None
    in_esc = False
    for line in text.splitlines():
        m_layer = re.match(r"^  - id: (\w+)\s*$", line)
        if m_layer:
            current = m_layer.group(1)
            out.setdefault(current, ())
            in_esc = False
            continue
        m_inline = re.match(r"^    escalation_to:\s*\[([^\]]*)\]\s*$", line)
        if m_inline and current:
            out[current] = tuple(x.strip() for x in m_inline.group(1).split(",") if x.strip())
            in_esc = False
            continue
        if re.match(r"^    escalation_to:\s*$", line):
            in_esc = True
            continue
        if in_esc:
            m = re.match(r"^      \[([^\]]*)\]\s*$", line)
            if m and current:
                out[current] = tuple(x.strip() for x in m.group(1).split(",") if x.strip())
                in_esc = False
                continue
            if line.strip():
                in_esc = False
    return out


def load_out_of_scope(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """解析每层的 out_of_scope（明确不许这层开单的类别）。支持行内数组写法。"""
    text = (path or contract_path()).read_text(encoding="utf-8")
    out: dict[str, tuple[str, ...]] = {}
    current: str | None = None
    for line in text.splitlines():
        m_layer = re.match(r"^  - id: (\w+)\s*$", line)
        if m_layer:
            current = m_layer.group(1)
            out.setdefault(current, ())
            continue
        m = re.match(r"^    out_of_scope:\s*\[([^\]]*)\]\s*$", line)
        if m and current:
            out[current] = tuple(x.strip() for x in m.group(1).split(",") if x.strip())
    return out


def can_escalate(frm: str, to: str, targets: dict[str, tuple[str, ...]]) -> bool:
    """判断 from 层是否被契约允许把归属上提到 to 层（逐跳传递）。"""
    if frm == to:
        return True
    seen: set[str] = set()
    stack = list(targets.get(frm, ()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == to:
            return True
        stack.extend(targets.get(cur, ()))
    return False


def validate_finding(rec: dict[str, Any], categories: dict[str, tuple[str, ...]]) -> list[str]:
    """返回该条 finding 的契约违规清单；空列表表示通过。"""
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in rec:
            problems.append(f"缺少必填字段 {field}")
    if problems:
        return problems

    if rec["layer"] not in LAYERS:
        problems.append(f"layer 取值非法: {rec['layer']}")
    if rec["ground_truth"] not in GROUND_TRUTH:
        problems.append(f"ground_truth 取值非法: {rec['ground_truth']}")
    fit = rec.get("category_fit")
    if fit is not None and fit not in CATEGORY_FIT:
        problems.append(f"category_fit 取值非法: {fit}")

    category = rec["category"]
    if category == UNMAPPED:
        problems.append("category 为 UNMAPPED：契约的类别列表装不下这条发现")
    else:
        layer_part, _, cat_part = category.partition(".")
        if layer_part not in categories:
            problems.append(f"category 的层前缀非法: {layer_part}")
        elif cat_part not in categories[layer_part]:
            problems.append(f"category 不在 {layer_part} 的允许列表: {cat_part}")

    loc = rec["location"]
    if not isinstance(loc, dict):
        problems.append("location 必须是对象")
    else:
        for key in ("file", "line", "symbol", "endpoint"):
            if key not in loc:
                problems.append(f"location 缺少 {key}")

    if not isinstance(rec["evidence_refs"], list) or not rec["evidence_refs"]:
        problems.append("evidence_refs 必须是非空数组")
    if not isinstance(rec["cwe"], list):
        problems.append("cwe 必须是数组")
    if not isinstance(rec["confidence"], (int, float)):
        problems.append("confidence 必须是数值")
    elif not 0.0 <= float(rec["confidence"]) <= 1.0:
        problems.append(f"confidence 越界: {rec['confidence']}")
    if not isinstance(rec["claim"], str) or not rec["claim"].strip():
        problems.append("claim 不能为空")
    if not isinstance(rec["repro_cmd"], str):
        problems.append("repro_cmd 必须是字符串（可为空串）")
    return problems
