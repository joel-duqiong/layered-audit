"""从已有的各层声明产物，构造 Agent 群（agents.json）与全局边表（edges.json）。

**不新增任何检测器** —— 全部输入来自现有脚本已经产出的 JSON。
这一版的目的是证明：「群」是现有能力的重新组织，而不是重写。

产出：
  agents.json  每个 (role, unit) 一个 Agent 实例，带静态命/动态命/义务/关系/结论/状态
  edges.json   一张全局边表，跨 repo 共用，edge_id 全局唯一

边的解析状态（resolution）决定失效强度：
  static    静态可确定（javap/AST 直接抽到）
  inferred  推断得到（如 DI 唯一候选实现）
  observed  运行时观测到（打桩代理采集 / 真实请求）
  unknown   解不出来 —— 一律按保守失效处理，宁可多标不可漏标
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import findings_schema
from agent_schema import agent_id, validate_edge

PROJECT_DIR = findings_schema.target_path().parent


def _load(name: str) -> Any:
    path = PROJECT_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def generation_stamp(fn_decl: Any, class_decl: Any) -> dict[str, str]:
    """与 relation_net.generation_stamp 同一口径：两个构建器共同输入的内容哈希。

    两处各写一份是文件所有权分离所致；跨载体口径相等由 test_path_overrides 锁住（坑 11）。
    """
    return {"fn_declaration": _sha(fn_decl), "class_declaration": _sha(class_decl)}


def _roles(categories: dict[str, tuple[str, ...]]) -> dict[str, dict[str, Any]]:
    """角色 = (层, 类别)。来自层契约，角色数与代码规模无关。"""
    carriers = findings_schema.load_target().get("carriers", {})
    layer_carriers = findings_schema.load_target().get("layer_carriers", {})
    roles: dict[str, dict[str, Any]] = {}
    for layer, cats in categories.items():
        for cat in cats:
            role_id = f"{layer}.{cat}"
            carrier = layer_carriers.get(layer, "")
            roles[role_id] = {
                "role_id": role_id,
                "layer": layer,
                "category": cat,
                "carrier": carrier,
                "fidelity": carriers.get(carrier, {}).get("fidelity", "unknown"),
            }
    return roles


def _units() -> dict[str, list[dict[str, Any]]]:
    """单元索引：函数 / 文件 / 类 / 服务 / 端到端。"""
    units: dict[str, list[dict[str, Any]]] = defaultdict(list)

    fn = _load("fn_declaration.json") or {}
    for fn_rec in fn.get("functions") or []:
        cls = fn_rec.get("class") or ""
        method = fn_rec.get("method") or ""
        units["function"].append({
            "unit_kind": "method",
            "unit_key": f"{cls}#{method}",
            "class": cls,
            "symbol": f"{cls.split('.')[-1]}.{method}",
            "file": fn_rec.get("file", ""),
            "facts": fn_rec,
        })

    fd = _load("file_declaration.json") or {}
    for rec in fd.get("files") or []:
        cls = rec.get("class") or ""
        units["file"].append({
            "unit_kind": "file",
            "unit_key": cls,
            "class": cls,
            "symbol": cls.split(".")[-1],
            "file": rec.get("file", ""),
            "facts": rec,
        })

    cd = _load("class_declaration.json") or {}
    for cls, rec in (cd.get("jpa") or {}).items():
        units["class"].append({
            "unit_kind": "class",
            "unit_key": cls,
            "class": cls,
            "symbol": cls,
            "facts": rec,
        })

    # 函数单元的源码文件：fn_declaration 不带 file 字段，
    # 从文件单元（file_declaration 有 file）按类名反查补上 ——
    # 增量调度要按文件路径定位受影响单元，这条链路不能是死的。
    class_to_file: dict[str, str] = {}
    for u in units["file"]:
        if u.get("file"):
            class_to_file.setdefault(u["class"], u["file"])
    for u in units["function"]:
        if not u.get("file"):
            u["file"] = class_to_file.get(u["class"], "")

    # 服务与端到端各是一个整体单元
    units["service"].append({
        "unit_kind": "service",
        "unit_key": "service:api",
        "class": "",
        "symbol": "api",
        "facts": {"note": "对外 HTTP 契约边界"},
    })
    units["integration"].append({
        "unit_kind": "flow",
        "unit_key": "flow:end-to-end",
        "class": "",
        "symbol": "end-to-end",
        "facts": {"note": "API/DB/前端三方对账 + 攻击链"},
    })
    return units


def _callee_key(callee: str) -> str:
    """调用方名归一：'a.b.C.m' / 'a.b.C#m' -> 'a.b.C#m'。"""
    if "#" in callee:
        return callee
    cls, _, method = callee.rpartition(".")
    return f"{cls}#{method}" if cls else callee


# ---- 同类自调用边的源码证据通道 ----
# 同类调用在 javap 里省略 owner 前缀，上游抽取曾整行丢弃（fn_static 的
# CALL_RE 已修：无 owner 归回本类）。本通道从源码方法体取代理态与
# 文件:行 证据：调用点必须真的写在源码里，不按名字猜。
# 代理三态：bypassed = 裸调用 / this.（不经代理拦截）；
# effective = 经带注入证据的自身类型字段调用；unknown = 是自身类型字段
# 但拿不到注入证据。接收者无法证实是本类的调用一律不产边。
# 声明里已有的调用对：证据钉到原边上，不产重复边；声明缺的才补新边。

_JAVA_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_JAVA_TEXT_BLOCK_RE = re.compile(r'""".*?"""', re.S)
_JAVA_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_JAVA_METHOD_RE = re.compile(
    r"(?P<prefix>[\w$<>\[\],.@?\s]+?\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"\((?P<params>[^()]*)\)\s*(?:throws\s+[\w$.,\s]+)?\{")
_JAVA_CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(")
_JAVA_METHOD_REF_RE = re.compile(r"(?<![\w$:])([A-Za-z_$][\w$]*)::([A-Za-z_$][\w$]*)")
_JAVA_KEYWORDS = frozenset((
    "if", "for", "while", "switch", "catch", "return", "new", "throw", "else",
    "do", "try", "synchronized", "assert", "case", "break", "continue",
    "super", "this", "instanceof", "yield", "record", "sealed", "permits",
))
_INJECTION_MARKERS = ("Autowired", "Resource", "Lazy", "Inject")


def _java_clean(text: str) -> str:
    """去注释与字符串字面量，保留换行 —— 行号仍可回源码核对。"""
    def _blank(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")
    text = _JAVA_COMMENT_RE.sub(_blank, text)
    text = _JAVA_TEXT_BLOCK_RE.sub(_blank, text)
    return _JAVA_LITERAL_RE.sub(_blank, text)


def _java_top_class(clean: str) -> tuple[str, str, int]:
    """顶层类名、类体、类体在全文中的起始偏移。"""
    m = re.search(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)", clean)
    if not m:
        return "", "", 0
    start = clean.find("{", m.end())
    if start < 0:
        return m.group(1), "", 0
    depth = 0
    for i in range(start, len(clean)):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                return m.group(1), clean[start + 1:i], start + 1
    return m.group(1), clean[start + 1:], start + 1


def _java_methods(class_body: str) -> list[dict[str, Any]]:
    """类体一层（depth=0）的方法：名字、方法体、体起始偏移。

    嵌套类/匿名类里的方法 depth>0，不取 —— 它们的调用不属于外层类。
    """
    out: list[dict[str, Any]] = []
    pos = 0
    scan = 0
    depth = 0
    while True:
        m = _JAVA_METHOD_RE.search(class_body, pos)
        if not m:
            break
        for ch in class_body[scan:m.start()]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        scan = m.start()
        prefix = (m.group("prefix") or "").strip()
        last = prefix.split()[-1] if prefix else ""
        if (depth != 0 or not prefix or last in _JAVA_KEYWORDS
                or re.search(r"[=;{}]$", prefix)
                or not re.fullmatch(r"[\w$<>\[\],.@?\s]+", prefix)):
            pos = m.end()
            continue
        start = m.end()
        d = 1
        i = start
        while i < len(class_body) and d:
            if class_body[i] == "{":
                d += 1
            elif class_body[i] == "}":
                d -= 1
            i += 1
        if d:
            break
        out.append({"name": m.group("name"),
                    "body": class_body[start:i - 1], "offset": start})
        pos = i
    return out


def _java_self_fields(class_body: str, class_name: str) -> dict[str, bool]:
    """自身类型的字段 -> 是否有注入证据。只认类型证据，不按名字猜。"""
    fields: dict[str, bool] = {}
    for m in re.finditer(
            rf"\b{re.escape(class_name)}(?:<[^;={{}}]*>)?\s+([A-Za-z_$][\w$]*)\s*[;=]",
            class_body):
        head = class_body[max(0, m.start() - 200):m.start()]
        head = re.split(r"[;{}]", head)[-1]
        injected = any(f"@{mark}" in head for mark in _INJECTION_MARKERS)
        fields[m.group(1)] = injected
    return fields


def _resolve_source(source_root: Path, rel: str) -> Path | None:
    """声明里的相对路径逐级剥前缀去源码根下找 —— 不假设布局字面量。"""
    parts = [p for p in rel.split("/") if p]
    for i in range(len(parts)):
        cand = source_root.joinpath(*parts[i:])
        if cand.is_file():
            return cand
    return None


def _self_call_facts(source_root: Path | None,
                     units: dict[str, list[dict[str, Any]]],
                     fn: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
    """从源码恢复同类调用事实（含 :: 方法引用）。每条带 文件:行 证据与代理三态。"""
    if not source_root or not source_root.is_dir():
        return []
    fd = _load("file_declaration.json") or {}
    if fn is None:
        fn = _load("fn_declaration.json") or {}
    class_file = {rec.get("class"): rec.get("file") for rec in fd.get("files") or []
                  if rec.get("class") and rec.get("file")}
    method_names: dict[str, set[str]] = defaultdict(set)
    for u in units.get("function", []):
        method_names[u["class"]].add(u["unit_key"].split("#", 1)[-1])

    facts: list[dict[str, Any]] = []
    for cls, names in sorted(method_names.items()):
        rel = class_file.get(cls)
        if not rel:
            continue
        path = _resolve_source(source_root, rel)
        if path is None:
            continue
        clean = _java_clean(path.read_text(encoding="utf-8"))
        class_name, class_body, body_offset = _java_top_class(clean)
        if not class_body or class_name != cls.rsplit(".", 1)[-1]:
            continue
        self_fields = _java_self_fields(class_body, class_name)
        line_starts = [0]
        for i, ch in enumerate(clean):
            if ch == "\n":
                line_starts.append(i + 1)
        for meth in _java_methods(class_body):
            body = meth["body"]
            for cm in _JAVA_CALL_RE.finditer(body):
                callee = cm.group(1)
                if callee not in names or callee in _JAVA_KEYWORDS:
                    continue
                head = body[:cm.start()].rstrip()
                if head.endswith("."):
                    recv = re.search(r"([A-Za-z_$][\w$]*)\s*$", head[:-1])
                    if not recv:
                        continue
                    if recv.group(1) == "this":
                        mode = "bypassed"
                    elif recv.group(1) in self_fields:
                        mode = "effective" if self_fields[recv.group(1)] else "unknown"
                    else:
                        continue
                elif head.endswith("::"):
                    continue
                else:
                    mode = "bypassed"
                line = bisect.bisect_right(
                    line_starts, body_offset + meth["offset"] + cm.start())
                facts.append({
                    "class": cls, "caller": meth["name"], "callee": callee,
                    "proxy_mode": mode, "via": "call",
                    "evidence": {"file": str(path), "line": line},
                })
            for rm in _JAVA_METHOD_REF_RE.finditer(body):
                recv, callee = rm.group(1), rm.group(2)
                if callee not in names or callee in _JAVA_KEYWORDS:
                    continue
                if recv == "this" or recv == class_name:
                    mode = "bypassed"
                elif recv in self_fields:
                    mode = "effective" if self_fields[recv] else "unknown"
                else:
                    continue
                line = bisect.bisect_right(
                    line_starts, body_offset + meth["offset"] + rm.start())
                facts.append({
                    "class": cls, "caller": meth["name"], "callee": callee,
                    "proxy_mode": mode, "via": "method_reference",
                    "evidence": {"file": str(path), "line": line},
                })
    return facts


def _method_ref_facts(source_root: Path | None,
                      units: dict[str, list[dict[str, Any]]],
                      fn: dict[str, Any]) -> list[dict[str, Any]]:
    """字节码方法引用事实：常量池 MethodHandle 是权威证据。

    接收者证据：this/static -> bypassed（static 方法本就不经代理）；
    自身类型字段 -> 有注入证据 effective，否则 unknown；拿不到 -> unknown。
    """
    method_names: dict[str, set[str]] = defaultdict(set)
    for u in units.get("function", []):
        method_names[u["class"]].add(u["unit_key"].split("#", 1)[-1])
    fd = _load("file_declaration.json") or {}
    class_file = {rec.get("class"): rec.get("file") for rec in fd.get("files") or []
                  if rec.get("class") and rec.get("file")}
    self_fields_cache: dict[str, dict[str, bool]] = {}

    def _self_fields(cls: str) -> dict[str, bool]:
        if cls not in self_fields_cache:
            fields: dict[str, bool] = {}
            rel = class_file.get(cls)
            path = _resolve_source(source_root, rel) if (rel and source_root) else None
            if path is not None:
                clean = _java_clean(path.read_text(encoding="utf-8"))
                name, body, _ = _java_top_class(clean)
                if body and name == cls.rsplit(".", 1)[-1]:
                    fields = _java_self_fields(body, name)
            self_fields_cache[cls] = fields
        return self_fields_cache[cls]

    facts: list[dict[str, Any]] = []
    for rec in fn.get("functions") or []:
        cls = rec.get("class") or ""
        names = method_names.get(cls)
        if not names:
            continue
        for ref in rec.get("method_reference_calls") or []:
            target = ref.get("target") or ""
            owner, _, callee = target.rpartition(".")
            # lambda 是编译器合成方法：其方法体就在调用方源码里，
            # 源码通道已按调用方计足迹；再产边等于重复计算（坑 9 同族）。
            if owner != cls or callee not in names or callee.startswith("lambda$"):
                continue
            recv = ref.get("receiver") or ""
            if recv in ("this", "static"):
                mode = "bypassed"
            elif recv.startswith("field:"):
                fields = _self_fields(cls)
                fname = recv[len("field:"):]
                mode = ("effective" if fields[fname] else "unknown") \
                    if fname in fields else "unknown"
            else:
                mode = "unknown"
            facts.append({
                "class": cls, "caller": rec.get("method") or "", "callee": callee,
                "proxy_mode": mode, "via": "method_reference",
                "evidence": {"kind": "bytecode_method_handle", "class": cls,
                             "cp": ref.get("cp", ""), "ref": ref.get("kind", "")},
            })
    return facts


def _self_call_edges(units: dict[str, list[dict[str, Any]]],
                     edges: list[dict[str, Any]],
                     facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """声明边 + 源码证据 -> 完整边表。

    声明里已有的同类调用对：把代理态与 文件:行 证据钉到原边（不产重复边）；
    同一对多个调用点代理态不一致 -> mixed，不猜单一态。
    声明缺的对：补新边；目标单元解不出 -> resolution=unknown，绝不冒充 static。
    """
    by_key: dict[str, str] = {}
    for layer in ("function", "class", "file"):
        for u in units.get(layer, []):
            by_key[u["unit_key"]] = agent_id(u["unit_kind"], u["unit_key"])
    out = [dict(e) for e in edges]
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for e in out:
        if e["kind"] == "call":
            by_pair.setdefault((e["from_agent_id"], e["to_agent_id"]), e)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for f in facts:
        src = by_key.get(f"{f['class']}#{f['caller']}")
        if not src:
            continue
        dst = by_key.get(f"{f['class']}#{f['callee']}")
        grouped[(src, dst or f"fn:UNRESOLVED#{f['class']}#{f['callee']}")].append(f)

    new: list[dict[str, Any]] = []
    for (src, dst), fs in grouped.items():
        modes = {f["proxy_mode"] for f in fs}
        mode = fs[0]["proxy_mode"] if len(modes) == 1 else "mixed"
        evidence = [f["evidence"] for f in fs]
        via = sorted({f.get("via", "call") for f in fs})
        # 同一调用点的源码证据与字节码证据是互相佐证，不是两次调用：
        # 有源码调用点就按调用点数计，字节码事实只补证据不补数。
        sites = [f for f in fs if "file" in f["evidence"]]
        count = len(sites) if sites else len(fs)
        hit = by_pair.get((src, dst))
        if hit is not None:
            hit["self_call"] = True
            hit["proxy_mode"] = mode
            hit["evidence"] = evidence
            hit["via"] = via
            hit["call_count"] = max(int(hit.get("call_count") or 1), count)
            continue
        new.append({
            "edge_id": f"E-{len(out) + len(new) + 1:06d}",
            "kind": "call",
            "from_agent_id": src,
            "to_agent_id": dst,
            "resolution": "static" if not dst.startswith("fn:UNRESOLVED#") else "unknown",
            "stability": "behavior",
            "invalidation": "reverify_caller",
            "call_count": count,
            "self_call": True,
            "proxy_mode": mode,
            "via": via,
            "evidence": evidence,
        })
    return out + new


def _edges(units: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """全局边表：函数级 call、类级 type_use。"""
    edges: list[dict[str, Any]] = []
    counter = 0

    by_key: dict[str, str] = {}
    for layer in ("function", "class", "file"):
        for u in units.get(layer, []):
            by_key[u["unit_key"]] = agent_id(u["unit_kind"], u["unit_key"])

    fn = _load("fn_declaration.json") or {}
    for fn_rec in fn.get("functions") or []:
        src = by_key.get(f"{fn_rec.get('class')}#{fn_rec.get('method')}")
        if not src:
            continue
        for callee in fn_rec.get("internal_calls") or []:
            dst = by_key.get(_callee_key(callee))
            counter += 1
            edges.append({
                "edge_id": f"E-{counter:06d}",
                "kind": "call",
                "from_agent_id": src,
                "to_agent_id": dst or f"fn:UNRESOLVED#{callee}",
                "resolution": "static" if dst else "unknown",
                "stability": "behavior",
                "invalidation": "reverify_caller",
                "call_count": 1,
            })
        for callee in fn_rec.get("external_calls") or []:
            counter += 1
            edges.append({
                "edge_id": f"E-{counter:06d}",
                "kind": "call",
                "from_agent_id": src,
                "to_agent_id": f"ext:{callee}",
                "resolution": "static",
                "stability": "semantic",
                "invalidation": "recheck_boundary",
                "call_count": 1,
            })

    fd = _load("file_declaration.json") or {}
    for rec in fd.get("cross_file_edges") or []:
        src = by_key.get(rec.get("from", ""))
        dst = by_key.get(rec.get("to", ""))
        if not src or not dst:
            continue
        counter += 1
        edges.append({
            "edge_id": f"E-{counter:06d}",
            "kind": "type_use",
            "from_agent_id": src,
            "to_agent_id": dst,
            "resolution": "static",
            "stability": "semantic",
            "invalidation": "recheck_consumer",
            "call_count": rec.get("calls", 1),
        })
    return edges


def _evidence_index(units: dict[str, list[dict[str, Any]]]
                    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """动态用例按目标函数索引；发现按 unit 索引。

    发现里的 location.symbol 常是「简单类名.方法名」（OrderService.apply），
    而 unit_key 是全限定名（com.example.svc.OrderService#apply）。
    不做简单名→全名的解析，文件层单元就会永远关联不上自己的 finding，
    在盲区地图里被误标成「完全没查」。
    """
    simple_to_fqn: dict[str, str] = {}
    for layer_units in units.values():
        for u in layer_units:
            key = u["unit_key"]
            simple = key.split("#")[0].rsplit(".", 1)[-1]
            simple_to_fqn.setdefault(simple, key.split("#")[0])

    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fo = _load("fn_observation.json") or {}
    for case in fo.get("cases") or []:
        target = case.get("target") or ""
        if "#" in target:
            cases[target].append(case)

    # 同层单元的别名索引：
    #   文件层单元 = 一个源文件（key 是它的主类全名，file 是相对路径）
    #   类层单元   = 一个类（key 是简单类名）
    #   服务层单元 = 服务边界（key 是 service:<name>）
    # 结论只能记到**同层**的单元头上 —— 函数层的缺陷不决定某个文件的
    # 「内聚性」义务成立与否，反之亦然。
    file_by_rel: dict[str, str] = {}
    class_units: set[str] = set()
    service_units: set[str] = set()
    for layer, layer_units in units.items():
        for u in layer_units:
            key = u["unit_key"]
            rel = u.get("file") or ""
            # 只收**文件层**的单元：函数单元也带 file 字段，而且排在前面 ——
            # 混进来的话，同一个路径会映射到函数单元的 key 上。
            if rel and layer == "file":
                file_by_rel.setdefault(rel, key)
            if layer == "class":
                class_units.add(key)
            elif layer == "service":
                service_units.add(key)

    findings_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    af = _load("all_findings.json") or {}
    for rec in af.get("findings") or []:
        loc = rec.get("location") or {}
        sym = loc.get("symbol") or ""
        simple = sym.split(".")[0] if "." in sym else ""
        method = sym.split(".")[-1] if sym else ""
        fqn = simple_to_fqn.get(simple, "")
        keys = {k for k in (f"{fqn}#{method}" if fqn else "", f"{simple}#{method}" if simple else "",
                            sym, loc.get("file", "")) if k}
        # 层以类别的层为准（类别带层前缀），没有类别就看结论自己的层
        cat_layer = (rec.get("category") or "").partition(".")[0]
        layer = cat_layer if cat_layer in ("function", "file", "class", "service", "integration") \
            else (rec.get("layer") or "")
        if layer == "class":
            # 类层单元的 key 是简单类名。类层结论的 location.symbol 常是空的，
            # 定位在 location.file（实体的源文件）—— 用文件名反查类名。
            if simple in class_units:
                keys.add(simple)
            if fqn in class_units:
                keys.add(fqn)
            abs_path = loc.get("file") or ""
            base = abs_path.rsplit("/", 1)[-1] if abs_path else ""
            if base.endswith(".java"):
                base = base[:-len(".java")]
            if base and base in class_units:
                keys.add(base)
        elif layer == "service":
            keys |= service_units
        elif layer == "file":
            # 文件层单元按路径后缀匹配（结论里的路径是绝对路径，单元里是相对路径）
            abs_path = loc.get("file") or ""
            for rel, key in file_by_rel.items():
                if abs_path and abs_path.endswith(rel):
                    keys.add(key)
        for k in keys:
            findings_by_unit[k].append(rec)
    return cases, findings_by_unit


def _build_units(roles: dict[str, dict[str, Any]],
                 units: dict[str, list[dict[str, Any]]],
                 edges: list[dict[str, Any]],
                 templates: dict[str, str]) -> list[dict[str, Any]]:
    """单元索引 —— 每个可寻址的代码实体一条记录。

    **刻意不物化「Agent 实例」**：一个实例 = (单元 × 角色)，533×19 = 2071 条，
    但绝大部分既没有观测也没有结论，字段全是占位符 —— 那是纯成本。
    真正有意义的聚合是「这个单元该被哪些判定手段检验、现在验过没有、有没有结论」，
    这三样都能实时算，不需要落盘。

    「Agent 实例」这个概念保留在概念层：判定某单元时，(单元, 判定手段) 就是一次
    Agent 作业；它有观测或结论时才有内容。
    """
    cases, findings_by_unit = _evidence_index(units)
    centrality: dict[str, int] = defaultdict(int)
    for e in edges:
        centrality[e["to_agent_id"]] += int(e.get("call_count") or 1)

    # 逻辑密度：只看「这个单元有多少值得测的分支/副作用/守卫」
    logic: dict[str, dict[str, Any]] = {}
    fn = _load("fn_declaration.json") or {}
    for rec in fn.get("functions") or []:
        key = f"{rec.get('class')}#{rec.get('method')}"
        branches = int(rec.get("branch_count") or 0)
        effects = len(rec.get("side_effects") or [])
        guards = int(rec.get("null_guard_count") or 0)
        logic[key] = {
            "branches": branches, "side_effects": effects, "null_guards": guards,
            "is_pure": bool(rec.get("is_pure")),
            "score": branches + effects * 3 + guards + (0 if rec.get("is_pure") else 5),
        }
    member_logic: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"branches": 0, "side_effects": 0, "null_guards": 0,
                 "is_pure": True, "score": 0})
    for key, d in logic.items():
        agg = member_logic[key.split("#")[0]]
        for f in ("branches", "side_effects", "null_guards", "score"):
            agg[f] += d[f]
        agg["is_pure"] = agg["is_pure"] and d["is_pure"]

    out: list[dict[str, Any]] = []
    for layer, layer_units in units.items():
        layer_roles = sorted(r["role_id"] for r in roles.values() if r["layer"] == layer)
        for unit in layer_units:
            key = unit["unit_key"]
            unit_cases = cases.get(key, [])
            unit_findings = findings_by_unit.get(key, []) or findings_by_unit.get(unit["symbol"], [])
            violated = [f for f in unit_findings if f.get("kind") != "positive"]
            positive = [f for f in unit_findings if f.get("kind") == "positive"]
            # 单元状态要看**用例的判定结果**，不能只看「有没有用例」——
            # 否则一组全 PASS 的用例会被记成「有观测但义务未定」，
            # 覆盖率被系统性低估（实测 13 个单元受影响）。
            verdicts = {c.get("verdict") for c in unit_cases}
            failing = [c for c in unit_cases if c.get("verdict") == "FAIL"]
            passing = [c for c in unit_cases if c.get("verdict") == "PASS"]
            # 该单元「被判定为满足」与「被判定为违反」的义务类别。
            # 类别来自用例文件声明的分组映射（用例带 category）。
            # 义务是 (层, 类别) 对：函数层单元的义务只能是 function.* 类别。
            # 别的层的类别（比如类层的 inheritance_design）不能记到函数层单元头上 ——
            # 那是别的层的义务，这一层只是提供了一个观测。
            def _own(cat: str) -> bool:
                return bool(cat) and cat.partition(".")[0] == layer

            satisfied_categories = sorted(
                {c["category"] for c in passing
                 if c.get("category") and _own(c["category"])}
                # 同层的**正面结论**也算「这一类义务被验过、且成立」
                | {f.get("category") for f in positive
                   if f.get("category") and _own(f.get("category"))})
            violated_categories = sorted({c["category"] for c in failing
                                          if c.get("category") and _own(c["category"])}
                                         | {f.get("category") for f in violated
                                            if f.get("category")
                                            and f.get("category") != "UNMAPPED"
                                            and _own(f.get("category"))})
            if violated or failing:
                status = "FAIL"
            elif passing:
                status = "PASS"
            elif unit_cases:
                status = "OBSERVED"
            elif positive:
                status = "PASS"
            else:
                status = "UNKNOWN"
            lg = logic.get(key) or member_logic.get(key) or {
                "branches": 0, "side_effects": 0, "null_guards": 0,
                "is_pure": True, "score": 0}
            out.append({
                "unit_key": key,
                "kind": unit["unit_kind"],
                "layer": layer,
                "class": unit.get("class", ""),
                "symbol": unit["symbol"],
                "file": unit.get("file", ""),
                "role_count": len(layer_roles),
                "centrality": centrality.get(agent_id(unit["unit_kind"], key), 0),
                "logic": lg,
                "priority": centrality.get(agent_id(unit["unit_kind"], key), 0) * (1 + lg["score"]),
                "status": status,
                "observations": len(unit_cases),
                "satisfied_categories": satisfied_categories,
                "violated_categories": violated_categories,
                "case_ids": [c["id"] for c in unit_cases][:20],
                "finding_ids": [f["uid"] for f in unit_findings][:20],
                "root_cause_keys": sorted({f.get("root_cause_key", "") for f in unit_findings
                                           if f.get("root_cause_key")}),
                "obligation_template": templates.get(layer_roles[0], "") if layer_roles else "",
                "obligation_source_layer": layer,
            })
    return out


def main() -> None:
    global PROJECT_DIR
    ap = argparse.ArgumentParser(description="Agent 群骨架（units.json）与全局边表（edges.json）构建器")
    ap.add_argument("--artifacts-dir", type=Path,
                    help="声明产物目录（fn/class/file_declaration、fn_observation、all_findings）；默认项目绑定目录")
    ap.add_argument("--out-dir", type=Path, help="units/edges 输出目录（默认同产物目录）")
    args = ap.parse_args()
    # _load 有 10 处调用点且都读模块级 PROJECT_DIR，改这一个绑定点比改签名小一个量级。
    if args.artifacts_dir:
        PROJECT_DIR = args.artifacts_dir
    out_dir = args.out_dir if args.out_dir else PROJECT_DIR

    target = findings_schema.load_target()
    # _load 读 PROJECT_DIR，而 relation_net 读 paths.artifacts_dir。两者分叉时这里会静默读空，
    # 产出「2 单元 / 0 边 / 契约违规 0 条 / exit 0」的假绿 —— 缺输入必须判红，不是零违规。
    declared = (target.get("paths") or {}).get("artifacts_dir")
    if not args.artifacts_dir and declared and Path(declared).resolve() != PROJECT_DIR.resolve():
        raise SystemExit(
            f"输入目录分叉：绑定 paths.artifacts_dir={declared}，本构建器却读 {PROJECT_DIR}。"
            f"请显式传 --artifacts-dir（静默读空会产出 0 边假绿）")
    categories = findings_schema.load_layer_categories()
    templates = target.get("obligation_templates", {})
    roles = _roles(categories)
    units = _units()
    if not any(units.get(k) for k in ("function", "file", "class")):
        raise SystemExit(
            f"缺声明输入：{PROJECT_DIR} 下 fn/file/class_declaration 均无记录，"
            f"拒绝出空图（缺输入不是零违规）")
    edges = _edges(units)
    java_root = target.get("source_roots", {}).get("java", "")
    source_root = Path(java_root) if java_root else None
    fn = _load("fn_declaration.json") or {}
    facts = _self_call_facts(source_root, units, fn) + _method_ref_facts(source_root, units, fn)
    edges = _self_call_edges(units, edges, facts)
    self_edges = [e for e in edges if e.get("self_call")]
    unit_records = _build_units(roles, units, edges, templates)

    problems: list[str] = []
    for u in unit_records:
        if not u["unit_key"] or not u["role_count"]:
            problems.append(f"{u.get('unit_key')}: 单元缺少 unit_key 或 roles")
    for e in edges:
        for p in validate_edge(e):
            problems.append(f"{e['edge_id']}: {p}")

    payload = {
        "schema_version": 2,
        "repo_id": target.get("target", ""),
        "generation": generation_stamp(_load("fn_declaration.json") or {},
                                       _load("class_declaration.json") or {}),
        "roles": list(roles.values()),
        "layer_roles": {lay: sorted(r["role_id"] for r in roles.values() if r["layer"] == lay)
                        for lay in sorted({r["layer"] for r in roles.values()})},
        "counts": {
            "units": len(unit_records),
            "roles": len(roles),
            "edges": len(edges),
            "edges_unresolved": sum(1 for e in edges if e["resolution"] == "unknown"),
            "edges_self_call": len(self_edges),
            "self_call_proxy": {
                m: sum(1 for e in self_edges if e["proxy_mode"] == m)
                for m in ("bypassed", "effective", "unknown")},
            "units_with_observations": sum(1 for u in unit_records if u["observations"]),
            "units_with_findings": sum(1 for u in unit_records if u["finding_ids"]),
        },
        "units": unit_records,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "units.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "edges.json").write_text(
        json.dumps({"schema_version": 1, "generation": payload["generation"], "edges": edges},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    c = payload["counts"]
    print(f"单元索引 -> {out_dir / 'units.json'}")
    print(f"  单元 {c['units']} / 角色 {c['roles']} / 边 {c['edges']}"
          f"（未解析边 {c['edges_unresolved']}）")
    print(f"  同类自调用边 {c['edges_self_call']}（代理绕过 {c['self_call_proxy']['bypassed']}"
          f" / 代理生效 {c['self_call_proxy']['effective']}"
          f" / unknown {c['self_call_proxy']['unknown']}）")
    print(f"  有动态观测的单元 {c['units_with_observations']} / 有结论的 {c['units_with_findings']}")
    if problems:
        print(f"  契约违规 {len(problems)} 条：")
        for p in problems[:10]:
            print(f"    {p}")
    else:
        print("  契约违规 0 条")


if __name__ == "__main__":
    main()
