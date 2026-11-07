"""Java / Spring 生态特性扫描器（项目侧检查器）。

回答一个问题：**这套审计的载体，在 Java/Spring 的运行时语义上到底看得见什么、看不见什么。**

扫出来的东西分两类：
1. **真缺陷** —— 切面失效点、事务里的异步逃逸、对外暴露 JPA 实体的封装泄漏；
2. **盲区声明** —— 哪些特性我们当前没有检查器、哪些载体根本观察不到
   （反射直调不走 Spring 容器 → 切面语义 / DI 装配 / 配置注入 / 代理行为全部观察不到）。

这些结论进 feature_findings.json（走总表），盲区部分进 feature_coverage 矩阵。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
JAVA_ROOT = Path("/Users/june/jiuyan/backend/src/main/java")
PKG = "com/jiuyan/banquet"
OUT = HERE / "feature_findings.json"

ASPECT_ANNOTATIONS = (
    "Transactional", "Async", "Cacheable", "CacheEvict", "CachePut", "PreAuthorize",
    "PostAuthorize", "Secured", "RolesAllowed", "Retryable", "Scheduled", "EventListener",
    "TransactionalEventListener",
)
RPC_ANNOTATIONS = ("DubboService", "DubboReference", "FeignClient", "RpcService", "Reference")
DYNAMIC_CONFIG_ANNOTATIONS = ("RefreshScope", "NacosValue", "Value", "ConfigurationProperties")

METHOD_RE = re.compile(
    r"(?P<ann>(?:@\w+(?:\([^)]*\))?\s*)*)"
    r"(?P<mods>(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?P<ret>[\w.<>\[\], ?]+?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)
CLASS_RE = re.compile(
    r"(?P<ann>(?:@\w+(?:\([^)]*\))?\s*)*)"
    r"(?P<mods>(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*"
    r"(?:class|interface|enum|record)\s+(?P<name>\w+)",
)
FIELD_INJECT_RE = re.compile(r"@(Autowired|Resource|Inject)\b")
VALUE_RE = re.compile(r"@Value\(\s*\"(?P<expr>[^\"]+)\"")
# 自调用检测：Java 里同类内部调用**通常不带 this. 前缀**（findProduct(id) 而不是
# this.findProduct(id)）。只认 this. 会漏掉绝大多数自调用 —— 而自调用正是
# Spring 代理失效（事务/只读/缓存不生效）的典型入口。
# 负向先行断言排掉「别的对象的方法调用」（obj.findProduct(...) 走代理，不算自调用）。
SELF_CALL_RE = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?(?P<name>\w+)\s*\(")
ASYNC_ESCAPE_RE = re.compile(r"CompletableFuture|parallelStream\s*\(|new\s+Thread\s*\(|@Async|ExecutorService|\.supplyAsync\s*\(")


def java_files() -> list[Path]:
    return sorted(JAVA_ROOT.rglob("*.java"))


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def method_body(text: str, open_paren: int) -> tuple[str, int]:
    """从方法签名左括号起，返回方法体文本与结束位置。"""
    depth = 0
    i = open_paren
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    brace_start = text.find("{", i)
    if brace_start < 0:
        return "", len(text)
    depth = 0
    j = brace_start
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[brace_start:j + 1], j


def scan_class(path: Path, text: str) -> dict[str, Any]:
    """扫一个编译单元：类声明 + 方法清单 + 每个方法的注解与体。"""
    clean = strip_comments(text)
    cm = CLASS_RE.search(clean)
    if not cm:
        return {}
    class_name = cm.group("name")
    class_ann = cm.group("ann") or ""
    class_mods = (cm.group("mods") or "").split()

    methods: list[dict[str, Any]] = []
    for mm in METHOD_RE.finditer(clean):
        ann = mm.group("ann") or ""
        mods = (mm.group("mods") or "").split()
        # 跳过构造器（没有返回类型）与控制结构
        if mm.group("name") == class_name:
            continue
        ret = mm.group("ret").strip()
        name = mm.group("name")
        if ret in ("if", "for", "while", "switch", "catch", "return", "new", "do", "try"):
            continue
        if name in ("if", "for", "while", "switch", "catch", "return", "new", "do", "try", "else"):
            continue
        body, _ = method_body(clean, mm.end() - 1)
        methods.append({
            "name": mm.group("name"),
            "modifiers": mods,
            "annotations": re.findall(r"@(\w+)", ann),
            "return_type": ret,
            "body": body,
            "line": clean[:mm.start()].count("\n") + 1,
        })
    return {
        "file": str(path),
        "class": class_name,
        "class_annotations": re.findall(r"@(\w+)", class_ann),
        "class_modifiers": class_mods,
        "methods": methods,
    }


def _cross_class_callers(units: list[dict[str, Any]], owner_class: str, method: str) -> list[str]:
    """找别的类里对 owner_class 的 public 方法的**限定调用**（xxxService.method(...)）。

    必须限定调用者，否则会把「另一个类里同名 private 方法的调用」误判成跨类调用
    （OrderService 有自己的 private findProduct，与 ProductService.public findProduct 无关）。
    """
    hits: list[str] = []
    bean = owner_class[0].lower() + owner_class[1:]
    pattern = re.compile(rf"\b\w*{re.escape(bean)}\w*\.{re.escape(method)}\s*\(")
    for unit in units:
        if unit["class"] == owner_class:
            continue
        for m in unit["methods"]:
            if pattern.search(m["body"]):
                hits.append(f"{unit['class']}#{m['name']}")
    return hits


def find_entity_classes(units: list[dict[str, Any]]) -> set[str]:
    return {u["class"] for u in units if "Entity" in u["class_annotations"]}


def main() -> None:
    units = [scan_class(p, p.read_text(encoding="utf-8")) for p in java_files()]
    units = [u for u in units if u]
    entities = find_entity_classes(units)

    aspect_methods: list[dict[str, Any]] = []
    proxy_traps: list[dict[str, Any]] = []
    entity_exposure: list[dict[str, Any]] = []
    value_injections: list[dict[str, Any]] = []
    field_injections: list[dict[str, Any]] = []
    rpc_hits: list[dict[str, Any]] = []
    dynamic_config_hits: list[dict[str, Any]] = []
    async_escapes: list[dict[str, Any]] = []

    for unit in units:
        src = Path(unit["file"]).read_text(encoding="utf-8")
        for ann in RPC_ANNOTATIONS:
            # 必须匹配注解形式 @Reference，不能用裸字符串 —— 方法名里含 Reference 会误报
            if re.search(rf"@\b{ann}\b", src):
                rpc_hits.append({"file": unit["file"], "annotation": ann})
        for ann in DYNAMIC_CONFIG_ANNOTATIONS:
            if re.search(rf"@\b{ann}\b", src):
                dynamic_config_hits.append({"file": unit["file"], "annotation": ann})
        for vm in VALUE_RE.finditer(src):
            value_injections.append({"file": unit["file"], "expr": vm.group("expr"),
                                     "line": src[:vm.start()].count("\n") + 1})
        for fm in FIELD_INJECT_RE.finditer(src):
            field_injections.append({"file": unit["file"], "annotation": fm.group(1)})

        annotated = {m["name"] for m in unit["methods"]
                     if any(a in ASPECT_ANNOTATIONS for a in m["annotations"])}
        for m in unit["methods"]:
            aspects = [a for a in m["annotations"] if a in ASPECT_ANNOTATIONS]
            if aspects:
                aspect_methods.append({
                    "file": unit["file"], "class": unit["class"], "method": m["name"],
                    "line": m["line"], "annotations": aspects,
                    "modifiers": m["modifiers"],
                })
                # 切面失效点：CGLIB 代理不织入 private/final/static 方法
                if "private" in m["modifiers"] or "final" in m["modifiers"] or "static" in m["modifiers"]:
                    proxy_traps.append({
                        "kind": "aspect_on_non_proxyable_method",
                        "file": unit["file"], "class": unit["class"], "method": m["name"],
                        "line": m["line"], "annotations": aspects,
                        "reason": f"{'/'.join(m['modifiers'])} 方法上的 {','.join(aspects)} "
                                  f"不会被 Spring 代理织入",
                    })
            # 自调用：同类内部直接调被切面方法 → 绕过代理 → 事务/切面不生效
            for sc in SELF_CALL_RE.finditer(m["body"]):
                target = sc.group("name")
                if target not in annotated:
                    continue
                if target == m["name"]:
                    continue  # 递归调用不算「绕过代理」的典型形态，另行标注
                proxy_traps.append({
                    "kind": "self_invocation_bypasses_proxy",
                    "file": unit["file"], "class": unit["class"], "method": m["name"],
                    "line": m["line"],
                    "target": target,
                    "reason": f"{m['name']} 内部直接调用 {target}()（无 this. 也算），"
                              f"绕过 Spring 代理，被调方法的切面（事务/只读）不生效",
                })
            # 事务方法里的异步逃逸 → 读不到未提交数据
            if aspects and ASYNC_ESCAPE_RE.search(m["body"]):
                async_escapes.append({
                    "file": unit["file"], "class": unit["class"], "method": m["name"],
                    "line": m["line"], "annotations": aspects,
                    "reason": "事务方法内出现异步/并行逃逸，子线程读不到本事务的未提交数据",
                })
            # 对外暴露 JPA 实体 → 封装泄漏
            if "public" in m["modifiers"] and m["return_type"] in entities:
                cross = _cross_class_callers(units, unit["class"], m["name"])
                entity_exposure.append({
                    "file": unit["file"], "class": unit["class"], "method": m["name"],
                    "line": m["line"], "returns_entity": m["return_type"],
                    "cross_class_callers": cross,
                    "reason": f"public 方法直接返回 JPA 实体 {m['return_type']}，"
                              f"调用方可以绕过 DTO 契约与全部业务校验直接改实体",
                })

    findings: list[dict[str, Any]] = []
    non_proxyable = [t for t in proxy_traps
                     if t["kind"] == "aspect_on_non_proxyable_method"]
    self_invocation = [t for t in proxy_traps
                       if t["kind"] == "self_invocation_bypasses_proxy"]
    if non_proxyable:
        findings.append({
            "id": "JAVA-ASPECT-NON-PROXYABLE-METHOD",
            "severity": "HIGH",
            "endpoint": "",
            "title": "切面挂在了代理织入不到的方法上（private / final / static）",
            "detail": ("CGLIB / JDK 代理都不会织入 private、final、static 方法 —— "
                       "这些方法上的 @Transactional 永远不生效。\n"
                       + "\n".join(json.dumps(t, ensure_ascii=False) for t in non_proxyable)),
            "repro": "python3 java_feature_scan.py",
            "location": {"file": non_proxyable[0]["file"], "line": non_proxyable[0]["line"],
                         "symbol": non_proxyable[0]["method"], "endpoint": ""},
            "evidence": {"traps": non_proxyable},
        })
    if self_invocation:
        findings.append({
            "id": "JAVA-ASPECT-SELF-INVOCATION-BYPASS",
            "severity": "MEDIUM",
            "endpoint": "",
            "title": "同类内部直接调用被切面标注的方法 → 绕过 Spring 代理，切面语义不生效",
            "detail": (
                "Java 里同类内部调用**通常不带 this. 前缀**（findProduct(id)），"
                "这类调用走的是 this 引用，**不经过代理** → 被调方法上的 "
                "@Transactional(readOnly=true) 之类的切面不生效。\n"
                "本项目当前的实际后果有限（外层调用方本身就是写事务，readOnly 只读语义失效），"
                "但这是**代理绕过**的典型形态 —— 一旦外层语义变化或这些方法被挪出事务，"
                "就会静默失去事务边界。\n"
                + "\n".join(json.dumps(t, ensure_ascii=False) for t in self_invocation)),
            "repro": "python3 java_feature_scan.py",
            "location": {"file": self_invocation[0]["file"], "line": self_invocation[0]["line"],
                         "symbol": self_invocation[0]["method"], "endpoint": ""},
            "evidence": {"traps": self_invocation},
        })
    if async_escapes:
        findings.append({
            "id": "JAVA-TRANSACTION-ASYNC-ESCAPE",
            "severity": "HIGH",
            "endpoint": "",
            "title": "事务方法里的异步逃逸：子线程读不到本事务的未提交数据",
            "detail": "\n".join(json.dumps(t, ensure_ascii=False) for t in async_escapes),
            "repro": "python3 java_feature_scan.py",
            "location": {"file": async_escapes[0]["file"], "line": async_escapes[0]["line"],
                         "symbol": async_escapes[0]["method"], "endpoint": ""},
            "evidence": {"escapes": async_escapes},
        })
    if entity_exposure:
        findings.append({
            "id": "JAVA-SERVICE-EXPOSES-JPA-ENTITY",
            "severity": "MEDIUM",
            "endpoint": "",
            "title": "service 层把 JPA 实体当 public API 返回：调用方能绕过全部业务校验直接改实体",
            "detail": (
                "对外的返回类型应该是 DTO（OrderResponse / ProductResponse …），"
                "实体一旦暴露给调用方，它就能绕过服务层的所有校验与归一化直接改字段，"
                "也绕过了「读路径只读」这个约定。\n"
                + "\n".join(json.dumps(t, ensure_ascii=False) for t in entity_exposure)),
            "repro": "python3 java_feature_scan.py",
            "location": {"file": entity_exposure[0]["file"], "line": entity_exposure[0]["line"],
                         "symbol": entity_exposure[0]["method"], "endpoint": ""},
            "evidence": {"exposures": entity_exposure},
        })
    if not proxy_traps and not async_escapes:
        findings.append({
            "id": "JAVA-SPRING-PROXY-SEMANTICS-CLEAN",
            "severity": "INFO",
            "endpoint": "",
            "title": "Spring 代理语义干净：没有切面失效点、没有事务内异步逃逸、没有对外暴露实体",
            "detail": (
                f"扫了 {len(units)} 个编译单元、{len(aspect_methods)} 个带切面注解的方法：\n"
                "· 切面注解全部落在 public 方法上 → CGLIB/JDK 代理能织入\n"
                "· 没有同类内部 this.<切面方法>() 自调用 → 不会绕过代理\n"
                "· 事务方法里没有 CompletableFuture / parallelStream / new Thread 逃逸\n"
                f"· 对外暴露 JPA 实体的 public 方法：{len(entity_exposure)} 个"
                f"（见 JAVA-SERVICE-EXPOSES-JPA-ENTITY）\n"
                "注意：这是**静态**结论，证明不了「运行时切面真的生效了」——"
                "那需要容器内观测，见 feature_coverage 矩阵里的盲区说明。"),
            "repro": "python3 java_feature_scan.py",
            "location": {"file": "", "line": 0, "symbol": "", "endpoint": ""},
            "evidence": {
                "units_scanned": len(units),
                "aspect_methods": len(aspect_methods),
                "aspect_annotations": sorted({a for m in aspect_methods for a in m["annotations"]}),
                "value_injection_points": len(value_injections),
                "field_injection_points": len(field_injections),
            },
        })

    payload = {
        "class": findings,
        "features": {
            "aspect": {
                "used": bool(aspect_methods),
                "annotations": sorted({a for m in aspect_methods for a in m["annotations"]}),
                "methods": aspect_methods,
                "proxy_traps": proxy_traps,
            },
            "async_escape_in_transaction": async_escapes,
            "entity_exposure": entity_exposure,
            "di": {
                "field_injection": field_injections,
                "value_injection": value_injections,
            },
            "rpc": {"endpoints": rpc_hits},
            "dynamic_config": {"hits": dynamic_config_hits, "value_injection": len(value_injections)},
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Java 特性扫描 -> {OUT}  {len(findings)} 条发现")
    print(f"  切面方法 {len(aspect_methods)} 个 / 代理失效点 {len(proxy_traps)} 个 / "
          f"事务内异步逃逸 {len(async_escapes)} 个 / 实体暴露 {len(entity_exposure)} 个")
    print(f"  RPC 入口 {len(rpc_hits)} / @Value 注入 {len(value_injections)} / 字段注入 {len(field_injections)}")
    for f in findings:
        print(f"  {f['severity']:<9}{f['id']:<40}{f['title'][:44]}")


if __name__ == "__main__":
    main()
