"""能力图 —— 把「细胞是干啥的」和「哪些细胞实现哪条业务规则」连起来。

三件事：
  1. 使命挖掘 —— 从代码自身找证据，分级 + 带溯源，找不到就留 unknown，不猜
  2. 向上聚合 —— 函数 → 类 → 服务，沿 class 归属与包路径合成，结果是 derived
  3. implemented_by —— 判据的 target（实体/字段/表）↔ 细胞，只认字段级数据流证据

证据分级（权威度从高到低）：
  confirmed_test        测试真的请求了该细胞声明的 HTTP 方法+路径（不是符号子串匹配）
  confirmed_annotation  绑定在该声明上的框架注解声明了它的角色
  declared_comment      绑定在该声明上的作者原文（@Operation/@Schema/@Tag/Javadoc/行注释）
  naming_hint           方法名里的动词 —— 最弱，只是方向，不算使命
  unknown               没有证据 —— 保持 unknown，不许编一个使命

两条硬规则：
  · 注解/注释只认它真正修饰的那个声明。扫描器按声明边界（; { }）清空待归属缓冲，
    不做固定行数回看 —— 回看会跨声明串味。
  · 只读不算实现。写/产出该字段的细胞进 implemented_by，只读的进 consumes_field。

零项目字面量：所有输入路径由调用方显式给出。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

TIERS = ("confirmed_test", "confirmed_annotation", "declared_comment", "naming_hint", "unknown")
STATED_TIERS = ("confirmed_test", "confirmed_annotation", "declared_comment")

_ROLE_ANNO: dict[str, str] = {
    "PostMapping": "http_write_entry", "GetMapping": "http_read_entry",
    "PutMapping": "http_write_entry", "DeleteMapping": "http_write_entry",
    "PatchMapping": "http_write_entry", "RequestMapping": "http_entry",
    "Transactional": "transaction_boundary", "Scheduled": "scheduled_job",
    "EventListener": "event_handler", "KafkaListener": "event_handler",
    "Valid": "input_validation", "Validated": "input_validation",
    "PreAuthorize": "authorization_check",
}
_TYPE_ROLE_ANNO: dict[str, str] = {
    "RestController": "http_entry", "Controller": "http_entry",
    "RequestMapping": "http_entry", "Service": "service_component",
    "Repository": "data_access", "Entity": "persistent_entity",
    "Table": "persistent_entity", "Configuration": "configuration",
    "Component": "component", "ControllerAdvice": "exception_handling",
    "RestControllerAdvice": "exception_handling", "SpringBootApplication": "application_entry",
}
_MISSION_ANNO: dict[str, tuple[str, ...]] = {
    "Operation": ("summary", "description"),
    "Schema": ("description", "title"),
    "Tag": ("name", "description"),
    "ApiOperation": ("value", "notes"),
}
_HTTP_VERB = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
              "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": ""}
_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract",
              "synchronized", "native", "default", "strictfp", "transient", "volatile"}
_NOT_DECL = {"if", "for", "while", "switch", "catch", "return", "new", "else", "do",
             "try", "synchronized", "throw", "case", "assert", "yield", "super", "this"}
_RE_TYPE = re.compile(r"\b(class|interface|enum|record)\s+(\w+)")
_RE_ANNO = re.compile(r"@([\w.]+)")
_VERBS = ("resolve", "build", "validate", "create", "update", "delete", "find", "get",
          "list", "save", "load", "map", "to", "from", "apply", "check", "compute",
          "normalize", "record", "occupy", "book", "within", "export", "import")
_RE_SNAKE = re.compile(r"_([a-z])")
_TEST_SUFFIXES = (".py", ".java", ".kt", ".js", ".ts", ".go", ".rb", ".cs")
_RE_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def camel(field: str) -> str:
    return _RE_SNAKE.sub(lambda m: m.group(1).upper(), field)


# ---------------------------------------------------------------- 源码扫描

def _mask_comments(text: str) -> tuple[str, list[dict[str, Any]]]:
    """把注释替换成等长空格（保留行号），同时记下注释原文与行号。字符串里的 // 不算注释。"""
    out = list(text)
    comments: list[dict[str, Any]] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in "\"'":
            quote = c
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                if i < n and text[i] == "\n":
                    line += 1
                i += 1
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            comments.append({"line": line, "end_line": line,
                             "kind": "line", "text": text[i + 2:end].strip()})
            for k in range(i, end):
                out[k] = " "
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            block = text[i:end]
            comments.append({"line": line, "end_line": line + block.count("\n"),
                             "kind": "javadoc" if block.startswith("/**") else "block",
                             "text": block})
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            line += block.count("\n")
            i = end
        else:
            i += 1
    return "".join(out), comments


def _iter_segments(code: str) -> Iterator[tuple[str, int, str]]:
    """按声明边界切段：只在括号深度 0 处的 ; { } 切。产出 (段文本, 起始偏移, 终止符)。"""
    start, paren, i, n = 0, 0, 0, len(code)
    while i < n:
        c = code[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < n and code[i] != quote:
                if code[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c in "{;}" and paren == 0:
            head = code[start:i]
            lead = len(head) - len(head.lstrip())
            yield head.strip(), start + lead, c
            start = i + 1
        i += 1
    if code[start:].strip():
        head = code[start:]
        yield head.strip(), start + len(head) - len(head.lstrip()), ""


def _split_annotations(header: str, base: int, line_of: Any) -> tuple[list[dict[str, Any]], str, int]:
    """剥出声明前缀上的注解（含跨行参数），返回注解列表、剩余声明文本、剩余偏移。"""
    annos: list[dict[str, Any]] = []
    i = 0
    while i < len(header) and header[i] == "@":
        m = _RE_ANNO.match(header, i)
        if not m:
            break
        j = m.end()
        while j < len(header) and header[j].isspace():
            j += 1
        args = ""
        if j < len(header) and header[j] == "(":
            depth, k = 0, j
            while k < len(header):
                if header[k] == "(":
                    depth += 1
                elif header[k] == ")":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
            args = header[j + 1:k - 1]
            j = k
        annos.append({"name": m.group(1).split(".")[-1], "args": args,
                      "line": line_of(base + i), "on": "declaration"})
        i = j
        while i < len(header) and header[i].isspace():
            i += 1
    return annos, header[i:], i


def _paren_content(rest: str) -> tuple[str, int]:
    """第一个平衡括号里的内容，及其在 rest 内的偏移。"""
    open_at = rest.find("(")
    if open_at < 0:
        return "", -1
    depth = 0
    for k in range(open_at, len(rest)):
        if rest[k] == "(":
            depth += 1
        elif rest[k] == ")":
            depth -= 1
            if depth == 0:
                return rest[open_at + 1:k], open_at + 1
    return "", -1


def _split_top_level(text: str) -> list[tuple[str, int]]:
    """按顶层逗号切（泛型/括号/注解参数里的逗号不算），保留偏移。"""
    parts: list[tuple[str, int]] = []
    depth, cur, start = 0, "", 0
    for i, ch in enumerate(text):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append((cur, start))
            cur, start = "", i + 1
        else:
            cur += ch
    if cur.strip():
        parts.append((cur, start))
    return parts


def _parse_params(inner: str, base: int, line_of: Any) -> list[dict[str, Any]]:
    """方法参数 / record 组件 / 字段：名字、类型、注解、行号。级联校验靠它。"""
    out: list[dict[str, Any]] = []
    for part, off in _split_top_level(inner):
        if not part.strip():
            continue
        abs_off = base + off + len(part) - len(part.lstrip())
        annos, rest, ro = _split_annotations(part.strip(), abs_off, line_of)
        toks = rest.split()
        if len(toks) < 2 or not re.fullmatch(r"\w+", toks[-1]):
            continue
        typ = " ".join(t for t in toks[:-1] if t not in _MODIFIERS)
        words = re.findall(r"\w+", typ)
        out.append({"name": toks[-1], "type": typ, "nested": words[-1] if words else "",
                    "annotations": annos, "line": line_of(abs_off + ro)})
    return out


def _decl_name(rest: str) -> tuple[str, str] | None:
    """从声明文本里取 (名字, 种类)。取不到就不是方法/构造器。"""
    paren = rest.find("(")
    if paren < 0:
        return None
    before = rest[:paren].strip()
    if "=" in before or not before:
        return None
    toks = before.split()
    name = toks[-1]
    if not re.fullmatch(r"\w+", name) or name in _NOT_DECL:
        return None
    if any(t in _NOT_DECL for t in toks[:-1]):
        return None
    if len(toks) == 1 or all(t in _MODIFIERS for t in toks[:-1]):
        return name, "constructor"
    return name, "method"


def _anno_strings(args: str) -> list[str]:
    return re.findall(r'"([^"]*)"', args)


def _anno_value(args: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        m = re.search(rf"\b{key}\s*=\s*\"([^\"]*)\"", args)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _javadoc_text(block: str) -> str:
    body = block.strip().lstrip("/*").rstrip("*/")
    parts = [ln.strip().lstrip("*").strip() for ln in body.splitlines()]
    return " ".join(p for p in parts if p and not p.startswith("@")).strip()


def scan_java(path: Path, rel: str) -> dict[str, Any]:
    """扫一个 Java 文件：类型声明 + 成员声明，注解/注释按声明边界归属。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    code, comments = _mask_comments(text)
    newlines = [m.start() for m in re.finditer("\n", code)]

    def line_of(off: int) -> int:
        return bisect.bisect_left(newlines, off) + 1

    pkg = ""
    pm = re.search(r"^\s*package\s+([\w.]+)\s*;", code, re.M)
    if pm:
        pkg = pm.group(1)

    types: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    boundary_line = 0

    def comment_for(line: int) -> dict[str, Any] | None:
        near = [c for c in comments if boundary_line <= c["line"] and c["end_line"] < line]
        if not near:
            return None
        c = near[-1]
        if c["kind"] == "javadoc":
            body = _javadoc_text(c["text"])
            return {"text": body, "line": c["line"], "source": "javadoc"} if body else None
        if c["kind"] == "line":
            return {"text": c["text"], "line": c["line"], "source": "line_comment"}
        return None

    def owner() -> str:
        names = [t["simple"] for t in stack if t["kind"] == "type"]
        return ".".join(([pkg] if pkg else []) + names)

    for seg, off, term in _iter_segments(code):
        annos, rest, rest_off = _split_annotations(seg, off, line_of)
        sig_line = line_of(off + rest_off)
        if term == "{":
            tm = _RE_TYPE.search(rest)
            first_paren = rest.find("(")
            if tm and (first_paren < 0 or tm.start() < first_paren):
                kind, simple = tm.group(1), tm.group(2)
                fq = ".".join(([pkg] if pkg else []) +
                              [t["simple"] for t in stack if t["kind"] == "type"] + [simple])
                decl = {"kind": kind, "simple": simple, "fq": fq, "line": sig_line,
                        "reference": f"{rel}:{sig_line}", "annotations": pending + annos,
                        "comment": comment_for(sig_line), "file": rel}
                if kind == "record":
                    inner, inner_off = _paren_content(rest)
                    decl["components"] = (_parse_params(inner, off + rest_off + inner_off, line_of)
                                          if inner_off >= 0 else [])
                types.append(decl)
                stack.append({"kind": "type", "simple": simple})
            else:
                named = _decl_name(rest)
                scope = stack[-1]["kind"] if stack else "root"
                if named and scope in ("type", "root"):
                    name, mkind = named
                    if mkind == "constructor" and stack and stack[-1]["kind"] == "type":
                        if name != stack[-1]["simple"]:
                            mkind = "method"
                    inner, inner_off = _paren_content(rest)
                    params = (_parse_params(inner, off + rest_off + inner_off, line_of)
                              if inner_off >= 0 else [])
                    decl = {"kind": mkind, "name": name, "owner": owner(),
                            "line": sig_line, "reference": f"{rel}:{sig_line}", "file": rel,
                            "annotations": pending + annos + [
                                {**a, "on": "parameter"} for p in params for a in p["annotations"]],
                            "parameters": params, "comment": comment_for(sig_line)}
                    members.append(decl)
                    stack.append({"kind": "method", "simple": name, "decl": decl,
                                  "body_start": code.index("{", off + len(seg)) + 1})
                else:
                    stack.append({"kind": "other", "simple": ""})
            pending = []
        elif term == "}":
            if stack:
                top = stack.pop()
                if top["kind"] == "method":
                    end = code.index("}", off + len(seg))
                    top["decl"]["guards"] = _mine_guards(
                        top["decl"], code[top["body_start"]:end], top["body_start"], line_of)
            pending = []
        else:
            scope = stack[-1]["kind"] if stack else "root"
            if scope == "type":
                named = _decl_name(rest)
                if named:
                    name, mkind = named
                    members.append({"kind": mkind, "name": name, "owner": owner(),
                                    "line": sig_line, "reference": f"{rel}:{sig_line}", "file": rel,
                                    "annotations": pending + annos,
                                    "comment": comment_for(sig_line)})
                elif rest and not rest.startswith(("package ", "import ")):
                    head, depth = rest, 0
                    for i, ch in enumerate(rest):
                        if ch in "<([":
                            depth += 1
                        elif ch in ">)]":
                            depth -= 1
                        elif ch == "=" and depth == 0:
                            head = rest[:i]
                            break
                    parsed = _parse_params(head, off + rest_off, line_of)
                    if parsed:
                        f = parsed[0]
                        members.append({"kind": "field", "name": f["name"], "owner": owner(),
                                        "line": sig_line, "reference": f"{rel}:{sig_line}", "file": rel,
                                        "type": f["type"], "nested": f["nested"],
                                        "annotations": pending + annos + f["annotations"],
                                        "comment": comment_for(sig_line)})
            pending = []
        boundary_line = line_of(off + len(seg))
    return {"package": pkg, "types": types, "members": members, "file": rel}


def _endpoint(decl: dict[str, Any], type_decl: dict[str, Any] | None) -> list[tuple[str, str]]:
    """该声明对外暴露的 (HTTP 方法, 路径模板)。类级 @RequestMapping 是基路径。"""
    bases = [""]
    if type_decl:
        for a in type_decl["annotations"]:
            if a["name"] == "RequestMapping":
                paths = [p for p in _anno_strings(a["args"]) if p.startswith("/")]
                bases = paths or [""]
    out: list[tuple[str, str]] = []
    for a in decl["annotations"]:
        if a["on"] != "declaration" or a["name"] not in _HTTP_VERB:
            continue
        verb = _HTTP_VERB[a["name"]]
        if not verb:
            vm = re.search(r"\bmethod\s*=\s*(?:RequestMethod\.)?(\w+)", a["args"])
            verb = vm.group(1).upper() if vm else ""
        paths = [p for p in _anno_strings(a["args"]) if p.startswith("/")] or [""]
        for base in bases:
            for p in paths:
                joined = "/" + "/".join(x for x in (base.strip("/"), p.strip("/")) if x)
                out.append((verb, joined))
    return out


def _normalize_path(path: str) -> str:
    p = path.split("?", 1)[0]
    for _ in range(4):
        p = _RE_PLACEHOLDER.sub("*", p)
    p = re.sub(r"\$\{[^{}]*\}|:\w+", "*", p)
    return "/" + "/".join(seg for seg in p.split("/") if seg)


_HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
_RE_DEF = re.compile(r"^([ \t]*)def\s+(\w+)\s*\(([^)]*)\)", re.M)
_PATH_PARAM_NAMES = ("path", "url", "endpoint", "route")


def _path_matches(template: str, concrete: str) -> bool:
    """模板的 * 段匹配任意具体值。`/{id}` 必须能匹配 `/7`，否则具体值调用会被误报成没测试。"""
    ts = [s for s in template.split("/") if s]
    cs = [s for s in concrete.split("/") if s]
    return len(ts) == len(cs) and all(t == "*" or t == c for t, c in zip(ts, cs))


def _helper_aliases(text: str) -> dict[str, str]:
    """测试套件里自建的 HTTP helper 别名 → 真实动词。

    别名是从测试自己的代码里推出来的：某个函数的 path 参数被转发给一个字面量动词
    （`self._send("POST", path, ...)`）或一个标准动词方法（`self.post(path)`）。
    不写死任何 helper 名字 —— 漏抽别名会把「解析器没抽到」误报成「真的没测试」。
    """
    lines = text.splitlines()
    aliases: dict[str, str] = {}
    verb_alt = "|".join(_HTTP_VERBS)
    for m in _RE_DEF.finditer(text):
        indent, name = len(m.group(1)), m.group(2)
        params = [p.strip().split(":")[0].split("=")[0].strip()
                  for p in m.group(3).split(",")]
        params = [p for p in params if p and p not in ("self", "cls")]
        path_param = next((p for p in params if p in _PATH_PARAM_NAMES),
                          params[0] if params else None)
        if not path_param:
            continue
        start = text.count("\n", 0, m.start())
        body: list[str] = []
        for ln in lines[start + 1:]:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
                break
            body.append(ln)
        blob = "\n".join(body)
        esc = re.escape(path_param)
        vm = re.search(rf"\.\s*\w+\s*\(\s*[\"']({verb_alt})[\"']\s*,\s*{esc}\b", blob)
        if vm:
            aliases[name] = vm.group(1)
            continue
        vm = re.search(rf"\.\s*({'|'.join(v.lower() for v in _HTTP_VERBS)})\s*\(\s*{esc}\b", blob)
        if vm:
            aliases[name] = vm.group(1).upper()
    return aliases


def _test_files(test_roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in test_roots:
        out.extend(sorted(p for p in root.rglob("*")
                          if p.is_file() and p.suffix in _TEST_SUFFIXES)
                   if root.is_dir() else [root])
    return out


def _call_sites(text: str, ref_name: str, aliases: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (动词已绑定的调用点, 动词未绑定的调用点)。后者是解析器缺口，不是覆盖缺口。"""
    known = {v.lower() for v in _HTTP_VERBS} | set(aliases)
    alt = "|".join(sorted(re.escape(k) for k in known))
    bound: list[dict[str, Any]] = []
    for m in re.finditer(rf"\.\s*({alt})\s*\(\s*[frbFRB]?(?P<q>[\"'])(?P<path>/.*?)(?P=q)", text):
        verb = aliases.get(m.group(1).lower(), m.group(1).upper())
        line = text.count("\n", 0, m.start()) + 1
        path_line = text.count("\n", 0, m.start("path")) + 1
        ref = f"{ref_name}:{line}" if path_line == line else f"{ref_name}:{line}-{path_line}"
        bound.append({"verb": verb, "path": _normalize_path(m.group("path")), "reference": ref})
    unresolved: list[dict[str, Any]] = []
    for m in re.finditer(r"\.\s*(\w+)\s*\(\s*[frbFRB]?(?P<q>[\"'])(?P<path>/[^\"'\s]*)(?P=q)", text):
        if m.group(1).lower() in known:
            continue
        line = text.count("\n", 0, m.start()) + 1
        unresolved.append({"callee": m.group(1), "path": _normalize_path(m.group("path")),
                           "reference": f"{ref_name}:{line}"})
    return bound, unresolved


def http_call_sites(test_roots: list[Path]) -> list[dict[str, Any]]:
    """从测试里抽 HTTP 调用点：动词 + 路径字面量。跨语言符号子串不算证据。"""
    files = _test_files(test_roots)
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    aliases: dict[str, str] = {}
    for text in texts.values():
        aliases.update(_helper_aliases(text))
    sites: list[dict[str, Any]] = []
    for path, text in texts.items():
        bound, _ = _call_sites(text, path.name, aliases)
        sites.extend(bound)
    return sites


def unresolved_call_sites(test_roots: list[Path]) -> list[dict[str, Any]]:
    """路径对得上、但动词绑不定的调用点 → 解析器缺口，必须与「真的没测试」分开报。"""
    files = _test_files(test_roots)
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    aliases: dict[str, str] = {}
    for text in texts.values():
        aliases.update(_helper_aliases(text))
    out: list[dict[str, Any]] = []
    for path, text in texts.items():
        _, unresolved = _call_sites(text, path.name, aliases)
        out.extend(unresolved)
    return out


_CONSTRAINT_ANNOS = {"NotNull", "NotBlank", "NotEmpty", "Null", "Min", "Max", "Size",
                     "Positive", "PositiveOrZero", "Negative", "NegativeOrZero",
                     "DecimalMin", "DecimalMax", "Digits", "Pattern", "Email",
                     "Future", "Past", "AssertTrue", "AssertFalse"}


def validation_index(scans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """@Valid 入口 → 级联可达的约束声明，带完整链路。

    框架校验是被审系统自身调节器的一部分（Conant–Ashby 良好调节器定理）：只建模
    业务方法的数据流，等于把整个校验子系统漏在模型外 —— 那是模型的多样性赤字，
    不是「校验侧连不上」。级联只在组件自己声明了 @Valid 时发生；没声明就不连。
    """
    decls: dict[str, dict[str, Any]] = {}
    for scan in scans:
        for t in scan["types"]:
            if t["kind"] == "record":
                decls[t["simple"]] = {"file": t["file"], "components": list(t.get("components") or [])}
        for m in scan["members"]:
            if m["kind"] == "field":
                owner = m["owner"].split(".")[-1]
                d = decls.setdefault(owner, {"file": m["file"], "components": []})
                d["components"].append({"name": m["name"], "nested": m.get("nested", ""),
                                        "annotations": m["annotations"], "line": m["line"]})
    for d in decls.values():
        for c in d["components"]:
            c["reference"] = f"{d['file']}:{c['line']}"
            c["anno_names"] = [a["name"] for a in c["annotations"]]

    def reach(root: str, seen: frozenset[str]) -> list[dict[str, Any]]:
        if root in seen or root not in decls:
            return []
        seen = seen | {root}
        out: list[dict[str, Any]] = []
        for c in decls[root]["components"]:
            out.append({"type": root, "field": c["name"], "annotations": c["anno_names"],
                        "reference": c["reference"], "chain": [c["reference"]]})
            if c["nested"] and "Valid" in c["anno_names"]:
                for r in reach(c["nested"], seen):
                    out.append({**r, "chain": [c["reference"]] + r["chain"]})
        return out

    entries: list[dict[str, Any]] = []
    for scan in scans:
        for m in scan["members"]:
            if m["kind"] not in ("method", "constructor"):
                continue
            for p in m.get("parameters") or []:
                if "Valid" not in [a["name"] for a in p["annotations"]] or not p["nested"]:
                    continue
                reachable = reach(p["nested"], frozenset())
                if reachable:
                    entries.append({"cell": f"{m['owner']}#{m['name']}", "param": p["name"],
                                    "root": p["nested"], "reference": m["reference"],
                                    "reachable": reachable})
    return entries


def _mask_strings(text: str) -> str:
    """把字符串/字符字面量的内容换成空格，保留引号与行号。guard 挖掘不许被字面量骗到。"""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text[i] in "\"'":
            quote = text[i]
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    if i + 1 < n and out[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 1
                if i < n and text[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1
        else:
            i += 1
    return "".join(out)


def _balanced(text: str, k: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    """text[k] 是开括号；返回 (内部文本, 闭括号之后的下标)。不闭合则返回 ("", -1)。"""
    depth = 0
    for i in range(k, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[k + 1:i], i + 1
    return "", -1


_NEGATE = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}
_LOWER = {">", ">="}
_UPPER = {"<", "<="}


def _mine_guards(decl: dict[str, Any], body: str, base: int, line_of: Any) -> list[dict[str, Any]]:
    """方法体内的字段级 guard：参数级绑定 + 精确 body 跨度 + throw 确认。

    只在「guard 里的标识符是本方法声明的参数」且「该参数经 setter 流入该字段」时成立。
    不做行数回看 —— 那会把别的方法的 guard 算进来（实测过：resolveFinalPrice 的
    guard 会被误算进 apply）。分离逻辑 frame rule 的旁条件在这里就是足迹必须相交：
    guard 的足迹 ∩ 字段写入的足迹 ≠ ∅，才谈得上蕴含。
    """
    params = {p["name"] for p in decl.get("parameters") or []}
    if not params:
        return []
    text = _mask_strings(body)
    flows: dict[str, set[str]] = defaultdict(set)
    for m in re.finditer(r"\.\s*set([A-Z]\w*)\s*\(\s*(\w+)\s*(?:\.\s*trim\s*\(\s*\)\s*)?\)", text):
        if m.group(2) in params:
            flows[m.group(2)].add(m.group(1)[0].lower() + m.group(1)[1:])
    if not flows:
        return []

    found: list[dict[str, Any]] = []

    def record(name: str, kind: str, op: str, value: Any, at: int) -> None:
        for fld in sorted(flows[name]):
            found.append({"field": fld, "param": name, "kind": kind, "op": op,
                          "value": value, "reference": f"{decl['file']}:{line_of(base + at)}"})

    for m in re.finditer(r"\bif\s*\(", text):
        inner, after = _balanced(text, m.end() - 1, "(", ")")
        if after < 0:
            continue
        k = after
        while k < len(text) and text[k].isspace():
            k += 1
        if k < len(text) and text[k] == "{":
            block, _ = _balanced(text, k, "{", "}")
        else:
            semi = text.find(";", k)
            block = text[k:semi] if semi >= 0 else ""
        if "throw" not in block:
            continue
        inner_at = m.end()
        for cm in re.finditer(r"\b(\w+)\s*(==|!=|<=|>=|<|>)\s*(null|-?\d+)\b", inner):
            name, op, val = cm.group(1), cm.group(2), cm.group(3)
            if name not in flows:
                continue
            if val == "null":
                if op == "==":
                    record(name, "not_null", "!=", None, inner_at + cm.start())
            else:
                record(name, "numeric", _NEGATE[op], int(val), inner_at + cm.start())
        for cm in re.finditer(r"!\s*[\w.]*hasText\s*\(\s*(\w+)\s*\)", inner):
            if cm.group(1) in flows:
                record(cm.group(1), "not_blank", "!=", None, inner_at + cm.start())
        for cm in re.finditer(r"\b(\w+)\s*\.\s*(?:isEmpty|isBlank)\s*\(\s*\)", inner):
            if cm.group(1) in flows:
                record(cm.group(1), "not_blank", "!=", None, inner_at + cm.start())
    uniq = {(g["field"], g["kind"], g["op"], g["value"]): g for g in found}
    return list(uniq.values())


def _implies(gop: str, gval: Any, oop: str, oval: Any) -> bool:
    """guard 强制的界是否蕴含判据要求的界。同向且更严才算，绝不放宽。"""
    if gval is None or oval is None:
        return False
    if gop in _LOWER and oop in _LOWER:
        return gval > oval or (gval == oval and (gop == ">" or oop == ">="))
    if gop in _UPPER and oop in _UPPER:
        return gval < oval or (gval == oval and (gop == "<" or oop == "<="))
    return False


def _assert_bounds(assertion: dict[str, Any]) -> tuple[str | None, list[tuple[str, Any]]]:
    """判据 → (种类, 界列表)。认不出的断言类型返回 None —— 不猜，保持 unknown。"""
    t = assertion.get("type")
    if t in ("not_null", "not_blank", "required"):
        return "not_null", []
    if t == "field_gte_const":
        return "numeric", [(">=", assertion.get("value"))]
    if t == "field_lte_const":
        return "numeric", [("<=", assertion.get("value"))]
    if t == "field_cmp":
        return "numeric", [(assertion.get("op"), assertion.get("value"))]
    if t == "range":
        bounds = []
        lo = assertion.get("minimum", assertion.get("min"))
        hi = assertion.get("maximum", assertion.get("max"))
        if lo is not None:
            bounds.append((">=", lo))
        if hi is not None:
            bounds.append(("<=", hi))
        return ("numeric", bounds) if bounds else (None, [])
    return None, []


def _enforces(g: dict[str, Any], assertion: dict[str, Any]) -> bool:
    kind, bounds = _assert_bounds(assertion)
    if kind == "not_null":
        return g["kind"] in ("not_null", "not_blank")
    if kind != "numeric" or g["kind"] != "numeric":
        return False
    return bool(bounds) and all(_implies(g["op"], g["value"], op, v) for op, v in bounds)


def _mission_text(decl: dict[str, Any]) -> tuple[str, str, int] | None:
    for a in decl["annotations"]:
        if a["name"] in _MISSION_ANNO:
            text = _anno_value(a["args"], _MISSION_ANNO[a["name"]])
            if text:
                return text, f"@{a['name']}", a["line"]
    c = decl.get("comment")
    if c and c["text"]:
        return c["text"], c["source"], c["line"]
    return None


def _evidence(decl: dict[str, Any], roles_map: dict[str, str],
              sites: list[dict[str, Any]], endpoint: list[tuple[str, str]]) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []
    for verb, path in endpoint:
        want = _normalize_path(path)
        for s in sites:
            if s["verb"] == verb and _path_matches(want, s["path"]):
                ev.append({"tier": "confirmed_test", "reference": s["reference"],
                           "text": f"测试请求 {verb} {want}", "http": [verb, want]})
    annos = [a for a in decl["annotations"] if a["name"] in roles_map]
    roles = sorted({roles_map[a["name"]] for a in annos})
    if roles:
        ev.append({"tier": "confirmed_annotation",
                   "reference": f"{decl['file']}:{annos[0]['line']}",
                   "text": ", ".join(f"@{a['name']}" for a in annos), "roles": roles})
    mission = _mission_text(decl)
    if mission:
        text, source, line = mission
        ev.append({"tier": "declared_comment",
                   "reference": f"{decl['file']}:{line}", "text": text, "source": source})
    name = decl.get("name") or decl.get("simple") or ""
    verb = next((v for v in _VERBS if name.lower().startswith(v)), None)
    if verb:
        ev.append({"tier": "naming_hint", "reference": decl["reference"],
                   "text": f"{name} → 动词 {verb}"})
    return ev


def _mission_entry(evidence: list[dict[str, Any]], references: list[str]) -> dict[str, Any]:
    if not evidence:
        return {"tier": "unknown", "statement": None, "statement_tier": "unknown",
                "roles": [], "evidence": [], "references": references,
                "note": "代码里找不到意图证据 —— 保持 unknown，不编造使命"}
    best = min(evidence, key=lambda e: TIERS.index(e["tier"]))
    stated = next((e for e in evidence if e["tier"] == "declared_comment"), None)
    picked = stated or best
    return {"tier": best["tier"], "statement": picked["text"],
            "statement_tier": picked["tier"],
            "roles": sorted({r for e in evidence for r in e.get("roles", [])}),
            "evidence": evidence, "references": references}


def mine_missions(source_roots: list[Path], units: list[dict[str, Any]],
                  test_roots: list[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """按细胞挖使命。细胞按全限定名定位，不按裸方法名 —— 裸名会跨类串味。"""
    scans: list[dict[str, Any]] = []
    for root in source_roots:
        files = sorted(root.rglob("*.java")) if root.is_dir() else [root]
        for path in files:
            rel = str(path.relative_to(root)) if root.is_dir() else path.name
            scans.append(scan_java(path, rel))

    sites = http_call_sites(test_roots)
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    types_by_fq: dict[str, dict[str, Any]] = {}
    types_by_simple: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for scan in scans:
        for t in scan["types"]:
            types_by_fq[t["fq"]] = t
            types_by_simple[t["simple"]].append(t)
        for m in scan["members"]:
            if m["kind"] in ("method", "constructor"):
                members[f"{m['owner']}#{m['name']}"].append(m)

    type_of = {t["fq"]: t for t in types_by_fq.values()}
    member_missions: dict[str, dict[str, Any]] = {}
    for key, decls in members.items():
        owner = key.split("#", 1)[0]
        ev: list[dict[str, Any]] = []
        endpoints: set[tuple[str, str]] = set()
        for d in decls:
            eps = _endpoint(d, type_of.get(owner))
            endpoints.update(eps)
            ev += _evidence(d, _ROLE_ANNO, sites, eps)
        entry = _mission_entry(ev, [d["reference"] for d in decls])
        if endpoints:
            entry["endpoints"] = [[v, _normalize_path(p)] for v, p in sorted(endpoints)]
        member_missions[key] = entry

    type_missions: dict[str, dict[str, Any]] = {}
    for fq, t in types_by_fq.items():
        ev = _evidence(t, _TYPE_ROLE_ANNO, [], [])
        type_missions[fq] = _mission_entry(ev, [t["reference"]])

    missions: dict[str, dict[str, Any]] = {}
    for u in units:
        key = u["unit_key"]
        if key in missions:
            continue
        layer = u.get("layer")
        if layer == "function":
            entry = member_missions.get(key)
        elif layer == "file":
            entry = type_missions.get(key)
        elif layer == "class":
            cands = types_by_simple.get(u.get("symbol") or key, [])
            entry = type_missions.get(cands[0]["fq"]) if len(cands) == 1 else None
        else:
            entry = None
        missions[key] = entry or _mission_entry([], [])
    index = {"types": type_missions, "types_by_fq": types_by_fq, "test_sites": sites,
             "unresolved_calls": unresolved_call_sites(test_roots),
             "validation": validation_index(scans),
             "guards": {f"{m['owner']}#{m['name']}": m["guards"]
                        for s in scans for m in s["members"]
                        if m["kind"] in ("method", "constructor") and m.get("guards")}}
    return missions, index


# ---------------------------------------------------------------- 向上聚合

def compose(missions: dict[str, dict[str, Any]], units: list[dict[str, Any]],
            index: dict[str, Any]) -> dict[str, Any]:
    """函数 → 类 → 服务。聚合是 derived；服务按包路径分组，不等于业务能力。"""
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in units:
        if u.get("layer") == "function" and u.get("class"):
            by_class[u["class"]].append(u)

    types_by_fq = index["types_by_fq"]
    classes: dict[str, Any] = {}
    for cls, members in sorted(by_class.items()):
        tiers = [missions.get(m["unit_key"], {}).get("tier", "unknown") for m in members]
        known = [t for t in tiers if t in STATED_TIERS]
        roles = sorted({r for m in members
                        for r in missions.get(m["unit_key"], {}).get("roles", [])})
        t = types_by_fq.get(cls)
        declared = None
        if t:
            tm = index["types"].get(cls) or {}
            if tm.get("statement_tier") == "declared_comment":
                declared = {"text": tm["statement"], "reference": t["reference"]}
        classes[cls] = {
            "member_count": len(members),
            "mission_coverage": round(len(known) / len(members), 4) if members else 0.0,
            "roles": roles, "tier": "derived", "declared_mission": declared,
            "members": [m["unit_key"] for m in members],
        }

    services: dict[str, Any] = defaultdict(
        lambda: {"classes": [], "roles": set(), "cells": 0, "declared_names": []})
    for cls, info in classes.items():
        svc = services[cls.rsplit(".", 1)[0]]
        svc["classes"].append(cls)
        svc["roles"].update(info["roles"])
        svc["cells"] += info["member_count"]
        if info["declared_mission"]:
            svc["declared_names"].append(
                {"class": cls, **info["declared_mission"]})
    return {"classes": classes,
            "services": {k: {**v, "roles": sorted(v["roles"]), "tier": "derived",
                             "grouping_basis": "java_package", "mission": None,
                             "limitation": "按包路径分组，不是业务能力的划分；包名不表达这群细胞完成什么业务"}
                         for k, v in sorted(services.items())}}


# ---------------------------------------------------------------- 判据连边

def _access_index(relation_net: dict[str, Any]) -> dict[str, Any]:
    """从关系网建作用域索引：(实体, 字段) → 写细胞/读细胞，表 → 实体，表 → 读写细胞。"""
    nodes = relation_net.get("nodes") or {}
    edges = relation_net.get("edges") or []
    writes: dict[tuple[str, str], set[str]] = defaultdict(set)
    reads: dict[tuple[str, str], set[str]] = defaultdict(set)
    write_why: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    read_why: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    table_entities: dict[str, set[str]] = defaultdict(set)

    for nid, node in nodes.items():
        if node.get("type") == "ENTITY_FIELD":
            ent, fld = node.get("entity"), node.get("field")
            tbl = node.get("table")
            if ent and fld:
                if tbl:
                    table_entities[tbl].add(ent)
        taint = node.get("taint") or {}
        for fq in taint.get("sinks") or []:
            parts = str(fq).split(".")
            if len(parts) < 2:
                continue
            ent, meth = parts[-2], parts[-1]
            fld = meth[3].lower() + meth[4:] if meth.startswith("set") and len(meth) > 3 else meth
            writes[(ent, camel(fld))].add(nid)
            write_why[(ent, camel(fld), nid)].append(f"taint sink {fq}")
        for fq in taint.get("source_fields") or []:
            parts = str(fq).split(".")
            if len(parts) < 2:
                continue
            ent, fld = parts[-2], parts[-1]
            if fld.startswith("get") and len(fld) > 3:
                fld = fld[3].lower() + fld[4:]
            reads[(ent, camel(fld))].add(nid)
            read_why[(ent, camel(fld), nid)].append(f"taint source {fq}")

    callers: dict[str, set[str]] = defaultdict(set)
    table_writes: dict[str, set[str]] = defaultdict(set)
    table_reads: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        src, dst, etype = str(e.get("from") or ""), str(e.get("to") or ""), e.get("type")
        if etype == "CALLS":
            callers[dst].add(src)
        elif etype == "DERIVES" and dst.startswith("field:"):
            ent, _, fld = dst[len("field:"):].partition(".")
            if ent and fld:
                writes[(ent, camel(fld))].add(src)
                write_why[(ent, camel(fld), src)].append(f"DERIVES {dst}")
        elif dst.startswith("table:") and etype in ("WRITES", "READS"):
            tbl = dst[len("table:"):]
            (table_writes if etype == "WRITES" else table_reads)[tbl].add(src)

    return {"writes": writes, "reads": reads, "write_why": write_why, "read_why": read_why,
            "table_entities": table_entities,
            "callers": callers, "table_writes": table_writes, "table_reads": table_reads}


def _oracle_fields(o: dict[str, Any]) -> list[str]:
    """判据里的字段操作数。schema 有多种写法，漏读会让字段级判据退化成表级扇出。"""
    target, assertion = o.get("target") or {}, o.get("assert") or {}
    seen: list[str] = []
    for f in [target.get("field"), assertion.get("field"), assertion.get("left"),
              assertion.get("target_field"), *(assertion.get("fields") or [])]:
        if f and f not in seen:
            seen.append(str(f))
    return seen


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _resolve_scope(o: dict[str, Any], idx: dict[str, Any]) -> tuple[list[tuple[str, str]], str]:
    """判据 → (实体, 字段) 作用域。保留实体作用域，绝不把字段简名当全局钥匙。"""
    target = o.get("target") or {}
    fields = [camel(f) for f in _oracle_fields(o)]
    entity = target.get("entity") or target.get("schema")
    table = target.get("table")
    if entity and fields:
        return [(entity, f) for f in fields], ""
    if table and fields:
        ents = sorted(idx["table_entities"].get(table, ()))
        if ents:
            return [(e, f) for e in ents for f in fields], ""
        return [], f"表 {table} 在关系网里没有对应的实体字段节点，无法定位字段作用域"
    if fields:
        owners = sorted({e for (e, f) in set(idx["writes"]) | set(idx["reads"]) if f in fields})
        if len(owners) == 1:
            return [(owners[0], f) for f in fields], ""
        if not owners:
            if any(_has_cjk(f) for f in fields):
                return [], (f"字段操作数 {fields} 是自然语言名词（含 CJK），关系网里没有同名实体字段"
                            " —— 上游判据不可连，非连边能力不足")
            return [], "关系网里没有触碰该字段的细胞"
        return [], f"字段 {fields} 无实体作用域，关系网里跨 {len(owners)} 个实体（{', '.join(owners)}）—— 不做名字匹配"
    return [], ""


def link_oracles(oracles: list[dict[str, Any]], units: list[dict[str, Any]],
                 relation_net: dict[str, Any],
                 validation: list[dict[str, Any]] | None = None,
                 guards: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """判据 → 细胞。写/产出或可证明的校验 = implemented_by；只读 = consumes_field；连不上 = unlinked。"""
    idx = _access_index(relation_net)
    validation = validation or []
    guards = guards or {}
    unit_keys = {u["unit_key"] for u in units}
    implemented: list[dict[str, Any]] = []
    consumes: list[dict[str, Any]] = []
    unlinked: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def emit(bucket: list[dict[str, Any]], o: dict[str, Any], nid: str,
             relation: str, resolution: str, why: list[str],
             extra: dict[str, Any] | None = None) -> None:
        key = (o["id"], nid, relation)
        if nid not in unit_keys or key in seen:
            return
        seen.add(key)
        edge = {"oracle_id": o["id"], "oracle_level": o["level"],
                "confirmation": o["confirmation"], "source_kind": o["source"]["kind"],
                "unit_key": nid, "relation": relation,
                "resolution": resolution, "evidence": list(dict.fromkeys(why))}
        if extra:
            edge.update(extra)
        bucket.append(edge)

    for o in oracles:
        target = o.get("target") or {}
        table = target.get("table")
        scopes, blocker = _resolve_scope(o, idx)
        hit = False
        assertion = o.get("assert") or {}
        for ent, fld in scopes:
            for nid in sorted(idx["writes"].get((ent, fld), ())):
                proof = [g for g in guards.get(nid, []) if g["field"] == fld and _enforces(g, assertion)]
                emit(implemented, o, nid, "implemented_by", "field_write",
                     idx["write_why"].get((ent, fld, nid), []) + [f"作用域 {ent}.{fld}"],
                     extra={"enforcement": (
                         {"kind": "guard", "field": fld, "param": proof[0]["param"],
                          "op": proof[0]["op"], "value": proof[0]["value"],
                          "reference": proof[0]["reference"]} if proof else {"kind": "unknown"})})
                hit = True
            for nid in sorted(idx["reads"].get((ent, fld), ())):
                emit(consumes, o, nid, "consumes_field", "field_read",
                     idx["read_why"].get((ent, fld, nid), []) + [f"作用域 {ent}.{fld}"])
                hit = True
        entity = target.get("entity") or target.get("schema")
        if entity:
            kind = o["source"]["kind"]
            want = set(re.findall(r"@(\w+)", o["source"].get("text") or ""))
            for fld in [camel(f) for f in _oracle_fields(o)]:
                for entry in validation:
                    for r in entry["reachable"]:
                        if r["type"] != entity or camel(r["field"]) != fld:
                            continue
                        if kind == "annotation":
                            if not (want & set(r["annotations"])):
                                continue
                        elif not (set(r["annotations"]) & _CONSTRAINT_ANNOS):
                            continue
                        emit(implemented, o, entry["cell"], "implemented_by",
                             "bean_validation_cascade",
                             [f"@Valid 入口 {entry['param']}: {entry['root']}（{entry['reference']}）"]
                             + [f"级联 {ref}" for ref in r["chain"]]
                             + [f"约束 @{','.join(r['annotations'])} 声明于 {r['reference']}"],
                             extra={"chain": [entry["reference"]] + r["chain"],
                                    "constraint_reference": r["reference"],
                                    "constraint_annotations": r["annotations"]})
                        hit = True
        if table and not _oracle_fields(o):
            for nid in sorted(idx["table_writes"].get(table, ())):
                for cell in sorted({nid} | idx["callers"].get(nid, ())):
                    emit(implemented, o, cell, "implemented_by", "table_write",
                         [f"WRITES table:{table}（{nid}）", f"作用域 表 {table}"])
                    hit = True
            for nid in sorted(idx["table_reads"].get(table, ())):
                for cell in sorted({nid} | idx["callers"].get(nid, ())):
                    emit(consumes, o, cell, "consumes_field", "table_read",
                         [f"READS table:{table}（{nid}）", f"作用域 表 {table}"])
                    hit = True
        if not hit:
            unlinked.append({"oracle_id": o["id"], "level": o["level"],
                             "reason": blocker or "关系网里没有触碰该作用域的细胞",
                             "fields": _oracle_fields(o), "table": table,
                             "entity": target.get("entity") or target.get("schema")})
    return {"implemented_by": implemented, "consumes_field": consumes,
            "unlinked_oracles": unlinked, "access_index": idx, "validation": validation}


def verify_links(links: list[dict[str, Any]], oracles: list[dict[str, Any]],
                 idx: dict[str, Any],
                 validation: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """独立复核：每条边的细胞是否真的写了该字段、或真的级联校验了它。跨实体边必须为 0。"""
    by_id = {o["id"]: o for o in oracles}
    validation = validation or []
    cross, unverified = [], []
    for e in links:
        o = by_id.get(e["oracle_id"])
        if not o:
            unverified.append(e["oracle_id"])
            continue
        scopes, _ = _resolve_scope(o, idx)
        if e["resolution"] == "bean_validation_cascade":
            target = o.get("target") or {}
            entity = target.get("entity") or target.get("schema")
            fields = {camel(f) for f in _oracle_fields(o)}
            ok = any(en["cell"] == e["unit_key"]
                     and any(r["type"] == entity and camel(r["field"]) in fields
                             and (set(r["annotations"]) & _CONSTRAINT_ANNOS
                                  or set(re.findall(r"@(\w+)", o["source"].get("text") or ""))
                                  & set(r["annotations"]))
                             for r in en["reachable"])
                     for en in validation)
            if not ok:
                unverified.append(f"{e['oracle_id']}→{e['unit_key']}")
            continue
        if e["resolution"] == "table_write":
            table = (o.get("target") or {}).get("table")
            ents = idx["table_entities"].get(table, set())
            ok = any(nid == e["unit_key"] or e["unit_key"] in idx["callers"].get(nid, ())
                     for nid in idx["table_writes"].get(table, ()))
            if not ok:
                unverified.append(f"{e['oracle_id']}→{e['unit_key']}")
            elif ents and not any(ent in ents for (ent, _f), cells in idx["writes"].items()
                                  if e["unit_key"] in cells):
                cross.append(f"{e['oracle_id']}→{e['unit_key']}")
            continue
        allowed = set(scopes)
        allowed_ents = {ent for ent, _f in scopes}
        claimed = [x[len("作用域 "):] for x in e["evidence"] if x.startswith("作用域 ")]
        ok = False
        for c in claimed:
            ent, _, fld = c.partition(".")
            if (ent, fld) in allowed and e["unit_key"] in idx["writes"].get((ent, fld), ()):
                ok = True
            elif ent not in allowed_ents:
                cross.append(f"{e['oracle_id']}→{e['unit_key']}（声称 {c}）")
        if not ok:
            unverified.append(f"{e['oracle_id']}→{e['unit_key']}")
    return {"cross_entity_edges": len(cross), "cross_entity_detail": cross[:20],
            "unverified_edges": len(unverified), "unverified_detail": unverified[:20]}


# ---------------------------------------------------------------- 报告

def build_report(units: list[dict[str, Any]], oracles: list[dict[str, Any]],
                 missions: dict[str, dict[str, Any]], composed: dict[str, Any],
                 linked: dict[str, Any], index: dict[str, Any] | None = None) -> dict[str, Any]:
    idx = linked["access_index"]
    index = index or {}
    links, consumes = linked["implemented_by"], linked["consumes_field"]
    tier_counts = {t: sum(1 for m in missions.values() if m["tier"] == t) for t in TIERS}
    statement_counts = {t: sum(1 for m in missions.values() if m["statement_tier"] == t) for t in TIERS}
    cells = len(missions)
    stated = sum(1 for m in missions.values() if m["statement_tier"] == "declared_comment")
    evidenced = sum(tier_counts[t] for t in STATED_TIERS)
    http_cells = {k: m for k, m in missions.items() if m.get("endpoints")}
    unresolved = index.get("unresolved_calls") or {}
    parser_miss: dict[str, Any] = {}
    no_evidence: dict[str, Any] = {}
    tested_n = 0
    for k, m in http_cells.items():
        tested = {tuple(e["http"]) for e in m["evidence"] if e["tier"] == "confirmed_test"}
        for ep in m["endpoints"]:
            if tuple(ep) in tested:
                tested_n += 1
                continue
            hits = [u for u in unresolved if _path_matches(ep[1], u["path"])]
            if hits:
                parser_miss.setdefault(k, []).append({"endpoint": ep, "unresolved_calls": hits})
            else:
                no_evidence.setdefault(k, []).append(ep)
    endpoint_total = sum(len(m["endpoints"]) for m in http_cells.values())
    lambda_cells = [k for k in missions if "lambda$" in k]
    unrecognized: dict[str, int] = defaultdict(int)
    for o in oracles:
        a = o.get("assert") or {}
        if _oracle_fields(o) and _assert_bounds(a)[0] is None:
            unrecognized[str(a.get("type"))] += 1
    prose = [o["id"] for o in oracles if any(_has_cjk(f) for f in _oracle_fields(o))]
    prose_linked = sorted(set(prose) & ({e["oracle_id"] for e in links} | {e["oracle_id"] for e in consumes}))
    by_layer: dict[str, dict[str, int]] = defaultdict(lambda: {"cells": 0, "evidenced": 0, "stated": 0})
    seen_keys: set[str] = set()
    for u in units:
        if u["unit_key"] in seen_keys:
            continue
        seen_keys.add(u["unit_key"])
        m = missions.get(u["unit_key"], {})
        b = by_layer[u.get("layer") or "?"]
        b["cells"] += 1
        b["evidenced"] += int(m.get("tier") in STATED_TIERS)
        b["stated"] += int(m.get("statement_tier") == "declared_comment")

    fanout: dict[str, int] = defaultdict(int)
    cell_fanout: dict[str, int] = defaultdict(int)
    for e in links:
        fanout[e["oracle_id"]] += 1
        cell_fanout[e["unit_key"]] += 1
    top = sorted(cell_fanout.items(), key=lambda kv: -kv[1])[:5]
    linked_ids = {e["oracle_id"] for e in links}
    consumed_ids = {e["oracle_id"] for e in consumes}
    dupes = len(units) - cells
    check = verify_links(links, oracles, idx, linked.get("validation"))

    return {
        "schema_version": 2, "engine": "capability_map",
        "counts": {
            "cells": len(units), "distinct_cells": cells, "duplicate_unit_keys": dupes,
            "missions_by_tier": tier_counts,
            "statements_by_tier": statement_counts,
            "mission_coverage": round(evidenced / cells, 4) if cells else 0.0,
            "stated_mission_coverage": round(stated / cells, 4) if cells else 0.0,
            "stated_missions": stated, "naming_hint_only": tier_counts["naming_hint"],
            "unknown": tier_counts["unknown"],
            "http_endpoint_cells": len(http_cells),
            "http_endpoints_total": endpoint_total,
            "http_endpoints_tested": tested_n,
            "http_endpoints_parser_miss": sum(len(v) for v in parser_miss.values()),
            "http_endpoints_no_test_evidence": sum(len(v) for v in no_evidence.values()),
            "lambda_cells": len(lambda_cells),
            "lambda_cells_without_declaration": sum(
                1 for k in lambda_cells if missions[k]["tier"] == "unknown"),
            "oracles_with_unrecognized_assertion_type": sum(unrecognized.values()),
            "unrecognized_assertion_types": dict(sorted(unrecognized.items())),
            "oracles_with_prose_field": len(prose),
            "prose_field_oracles_linked": len(prose_linked),
            "prose_field_oracle_ids": prose,
            "mission_coverage_by_layer": {k: dict(v) for k, v in sorted(by_layer.items())},
            "classes": len(composed["classes"]), "services": len(composed["services"]),
            "implemented_by_edges": len(links), "consumes_field_edges": len(consumes),
            "edges_by_resolution": dict(Counter(e["resolution"] for e in links)),
            "field_write_edges_with_guard_proof": sum(
                1 for e in links if e.get("enforcement", {}).get("kind") == "guard"),
            "field_write_edges_enforcement_unknown": sum(
                1 for e in links if e.get("enforcement", {}).get("kind") == "unknown"),
            "oracles_with_enforcement_proof": len({
                e["oracle_id"] for e in links
                if e.get("enforcement", {}).get("kind") == "guard"
                or e["resolution"] == "bean_validation_cascade"}),
            "validation_entry_cells": len({v["cell"] for v in linked.get("validation") or []}),
            "oracles_enforced_by_validation": len({e["oracle_id"] for e in links
                                                   if e["resolution"] == "bean_validation_cascade"}),
            "oracles_implemented": len(linked_ids),
            "oracles_consumed_only": len(consumed_ids - linked_ids),
            "oracles_unlinked": len(oracles) - len(linked_ids | consumed_ids),
            "unlinked_oracles": len(linked["unlinked_oracles"]),
            "oracles_total": len(oracles),
            "max_fanout": max(fanout.values()) if fanout else 0,
            "cells_with_edges": len(cell_fanout),
            "top_cells_by_edges": [{"unit_key": k, "edges": v} for k, v in top],
            "top5_edge_share": round(sum(v for _, v in top) / len(links), 4) if links else 0.0,
            "cross_entity_edges": check["cross_entity_edges"],
            "unverified_edges": check["unverified_edges"],
        },
        "semantics": {
            "implemented_by": "两种证据之一：(a) 细胞写/产出该字段（taint.sinks 的实体 setter、"
                              "DERIVES 边、表 WRITES 边及其直接调用方）—— 这是约束的**作用点/可违反点**，"
                              "不证明约束被执行；(b) bean_validation_cascade —— 从该细胞的 @Valid 参数"
                              "沿组件自己声明的 @Valid 级联走到约束声明，这是**可证明的执行点**，"
                              "每一跳都带 file:line。两种resolution 分开计数，不混为一谈。",
            "enforcement": "field_write 边上的 enforcement.kind：guard = 在该细胞体内找到了"
                           "参数级绑定 + throw 确认 + 界蕴含的字段级 guard，是可证明的执行；"
                           "unknown = 没找到，**不等于未实现**（可能由 DB 约束、父类、"
                           "跨方法数据流或框架执行）。unknown 不得读成 pass，也不得读成 violated。",
            "bean_validation_cascade": "框架校验是被审系统调节器的一部分（Conant–Ashby）。"
                                       "级联只在组件声明了 @Valid 时成立；没声明就不连 —— "
                                       "那是此路径未级联，不是「约束不存在」。",
            "consumes_field": "细胞只读该字段：taint.source_fields 的 DTO getter、表 READS 边。"
                              "只读不算实现 —— 单列，不计入 implemented_by。",
            "unlinked_oracles": "关系网里没有该作用域的细胞，或字段无实体作用域且同名跨多个实体。"
                                "那是覆盖缺口，不用名字匹配填平。",
            "unrecognized_assertion_types": "有字段操作数、但 guard miner 认不出的断言类型及条数。"
                                            "这是**上游 schema 漂移的可见信号**：这些判据仍可连边，"
                                            "但 enforcement 保持 unknown，绝不声称已执行。"
                                            "数字上涨说明上游改了词汇表，需要对齐而不是猜。",
            "prose_field_oracles": "字段操作数是自然语言名词（含 CJK）而非代码字段名的判据。"
                                   "这是**上游判据质量信号**，单列出来不混进通用缺口桶。"
                                   "prose_field_oracles_linked 必须为 0：连边不得挂到不可解析的字段上。",
            "mission_tier": "tier = 最强证据档；statement_tier = 使命原文来自哪一档。"
                            "有角色注解不等于有业务使命原文。",
            "mission_coverage": "有 confirmed_test/confirmed_annotation/declared_comment 证据的细胞占比。",
            "stated_mission_coverage": "有作者声明的原文（@Operation/@Schema/@Tag/Javadoc/注释）的细胞占比。",
        },
        "limitations": [
            "使命按证据分级，unknown 保持 unknown —— 不从命名编造业务意图",
            "naming_hint 只是方向，不计入使命覆盖",
            "confirmed_test 只认测试真的请求了该细胞声明的 HTTP 方法+路径；跨语言符号子串不是证据",
            "测试证据分三态：tested（动词+路径都绑定）/ parser_miss（路径对得上但动词绑不定，"
            "是解析器缺口）/ no_test_evidence（测试里没有该路径的请求）。两者不得混报",
            "helper 别名从测试自身代码推断（path 参数被转发给字面量动词）；推不出的进 parser_miss",
            "lambda 合成细胞：足迹归因来自关系网且已回源码核实，但扫描器不解析 lambda 体，"
            "使命保持 unknown —— 计入 lambda_cells_without_declaration，不编造",
            "只有声明 HTTP 端点的细胞能拿 confirmed_test；下游服务不因调用链继承该档",
            "注解/注释按声明边界归属，不做固定行数回看",
            "向上聚合是 derived；服务按包路径分组，不表达业务能力",
            "只读不算实现，consumes_field 单列",
            "implemented_by 的 field_write 档只证明数据流到达（写/产出该字段），不证明约束被执行；"
            "只有 bean_validation_cascade 档是可证明的执行点",
            "DTO 约束经 @Valid 参数 + 组件级 @Valid 级联定位执行入口；"
            "没有 @Valid 入口或组件未声明级联的，保持 unknown，不判定为「未实现」",
            "方法体 guard 只认参数级绑定（guard 标识符是本方法参数，且该参数经 setter 流入该字段）"
            "+ throw 确认 + 界蕴含；跨方法/经局部变量的数据流未追踪，那类保持 unknown",
            "表级判据经 WRITES 边 + 一跳 CALLS 传播到调用方，不做多跳推断",
            "父类继承字段（如审计时间戳）没有写证据，相关判据进 unlinked —— 未做父类字段解析",
            "重载方法共用一个 unit_key，证据按 key 合并",
        ],
        "verification": check,
        "endpoint_parser_miss": parser_miss,
        "endpoint_no_test_evidence": no_evidence,
        "missions": missions, "composition": composed,
        "implemented_by": links, "consumes_field": consumes,
        "unlinked_oracles": linked["unlinked_oracles"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="细胞使命挖掘 + 判据连边（只认证据，不猜）")
    ap.add_argument("--units", type=Path, required=True)
    ap.add_argument("--oracles", type=Path, required=True)
    ap.add_argument("--relation-net", type=Path, required=True,
                    help="字段级数据流（taint + WRITES/READS/DERIVES），连边只认它")
    ap.add_argument("--source-root", type=Path, nargs="+", required=True)
    ap.add_argument("--test-root", type=Path, nargs="*", default=[])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    units = json.loads(args.units.read_text(encoding="utf-8"))["units"]
    oracles = json.loads(args.oracles.read_text(encoding="utf-8"))["oracles"]
    relation_net = json.loads(args.relation_net.read_text(encoding="utf-8"))

    missions, index = mine_missions(args.source_root, units, args.test_root)
    composed = compose(missions, units, index)
    linked = link_oracles(oracles, units, relation_net, index["validation"], index["guards"])
    report = build_report(units, oracles, missions, composed, linked, index)
    report["reproduce"] = {
        "command": " ".join(sys.argv),
        "inputs": {name: {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                   for name, p in (("units", args.units), ("oracles", args.oracles),
                                   ("relation_net", args.relation_net))},
        "source_roots": [str(p) for p in args.source_root],
        "test_roots": [str(p) for p in args.test_root],
        "note": "输入哈希钉住快照：数字对不上时先比哈希，再比逻辑。上游改 schema 会无声退化，"
                "见 counts.unrecognized_assertion_types。",
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    c = report["counts"]
    print(f"细胞 {c['cells']}（去重 {c['distinct_cells']}，重复 key {c['duplicate_unit_keys']}）| "
          f"有证据 {c['mission_coverage']:.1%} | 有使命原文 {c['stated_mission_coverage']:.1%}")
    print(f"  分级: {c['missions_by_tier']}（和 {sum(c['missions_by_tier'].values())}）")
    print(f"  类 {c['classes']} | 服务 {c['services']}（按包分组，derived）")
    print(f"implemented_by {c['implemented_by_edges']} 条 {c['edges_by_resolution']} | "
          f"只读 consumes_field {c['consumes_field_edges']} 条 | "
          f"跨实体边 {c['cross_entity_edges']} | 复核不过 {c['unverified_edges']}")
    print(f"判据 实现 {c['oracles_implemented']}/{c['oracles_total']}"
          f"（级联校验可证明 {c['oracles_enforced_by_validation']}）| "
          f"仅被消费 {c['oracles_consumed_only']} | 覆盖缺口 {c['oracles_unlinked']}")
    print(f"执行证据: 级联 {c['edges_by_resolution'].get('bean_validation_cascade', 0)} 边 + "
          f"方法体 guard {c['field_write_edges_with_guard_proof']} 边 | "
          f"guard 未知 {c['field_write_edges_enforcement_unknown']} 边（不等于未实现）| "
          f"有执行证据的判据 {c['oracles_with_enforcement_proof']}")
    print(f"边落在 {c['cells_with_edges']} 个细胞上 | 单判据最大扇出 {c['max_fanout']} | "
          f"前 5 个细胞占 {c['top5_edge_share']:.1%}")
    print(f"端点 {c['http_endpoints_total']}：已测 {c['http_endpoints_tested']} | "
          f"解析器漏抽 {c['http_endpoints_parser_miss']} | 真无测试证据 {c['http_endpoints_no_test_evidence']}")
    print(f"lambda 合成细胞 {c['lambda_cells']}（使命 unknown {c['lambda_cells_without_declaration']}，"
          f"扫描器不解析 lambda 体）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())