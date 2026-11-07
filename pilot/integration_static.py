"""集权层静态声明：三方来源的「声明」互相对账。

  1) 前端 JS   —— 业务语义最完整的一份声明（Pad 端真正想干什么）
  2) 后端 Java —— 契约声明（controller 路由 + DTO record 组件 + 服务层拼装的 JSON 形状）
  3) 录制流量 —— 历史真实行为画像

集权层不重复服务层/类层的活儿，它只回答一个问题：
**这三份声明彼此之间对不上的地方在哪里** —— 那才是只有集成才会暴露的缺陷。
"""
from __future__ import annotations
import project

import collections
import datetime
import glob
import json
import os
import re

D = str(project.artifact_dir())

FE = str(project.frontend_root())
BE = str(project.backend_package_dir())
REC = str(project.recordings_dir())
CLASS_DECL = os.path.join(D, "class_declaration.json")
OUT = os.path.join(D, "integration_declaration.json")


# --------------------------------------------------------------------------
# 通用括号/字符串扫描
# --------------------------------------------------------------------------
def match_bracket(src: str, start: int, opener: str = "{", closer: str = "}") -> tuple[str, int]:
    """src[start] 必须是 opener。返回 (括号内内容, 闭合括号后的下标)。跳过字符串字面量。"""
    depth = 0
    in_str = False
    quote = ""
    i = start
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
        elif c in "\"'`":
            in_str, quote = True, c
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return src[start + 1:i], i + 1
        i += 1
    return src[start + 1:], len(src)


def split_top(inner: str) -> list[str]:
    """按顶层逗号切分（括号/字符串内的逗号不算）。"""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    in_str = False
    quote = ""
    for ch in inner:
        if in_str:
            cur.append(ch)
            if ch == quote:
                in_str = False
            continue
        if ch in "\"'`":
            in_str, quote = True, ch
            cur.append(ch)
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def object_keys(inner: str) -> list[str]:
    """取对象字面量的顶层 key，支持 `k: v`、简写 `{k}`、忽略 `...spread`。"""
    keys: list[str] = []
    for part in split_top(inner):
        part = part.strip()
        if not part or part.startswith("..."):
            continue
        m = re.match(r"([A-Za-z_$][\w$]*)\s*:", part)
        if m:
            keys.append(m.group(1))
            continue
        if not part.startswith(("{", "[")):
            m2 = re.match(r"([A-Za-z_$][\w$]*)\s*$", part)
            if m2:
                keys.append(m2.group(1))
    return keys


def find_block(src: str, start_at: int, opener: str = "{", closer: str = "}") -> tuple[str, int] | None:
    """从 start_at 起找到第一个 opener 并取其平衡内容。"""
    i = src.find(opener, start_at)
    if i < 0:
        return None
    return match_bracket(src, i, opener, closer)


# --------------------------------------------------------------------------
# 前端：路径模板 / 请求体 / 映射函数 / 枚举
# --------------------------------------------------------------------------
def parse_template(raw: str) -> tuple[str, list[str]]:
    """模板字符串 -> (归一化路径, 查询参数 key 列表)。

    `${toQuery({...})}` 视为查询串（路径到此为止）；其余 `${...}` 视为路径变量 -> {id}。
    """
    out: list[str] = []
    query_keys: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "$" and raw[i + 1:i + 2] == "{":
            inner, end = match_bracket(raw, i + 1, "{", "}")
            expr = inner.strip()
            if expr.startswith("toQuery("):
                blk = find_block(expr, 0)
                if blk:
                    query_keys = object_keys(blk[0])
                break
            out.append("{id}")
            i = end
            continue
        out.append(raw[i])
        i += 1
    path = "".join(out)
    path = path.split("?")[0].rstrip("/")
    return path or "/", query_keys


def extract_body(body_src: str, options_inner: str) -> tuple[list[str], str]:
    """从 request 的 options 里提取请求体字段。返回 (字段列表, 类型)。"""
    m = re.search(r"body:\s*jsonBody\(\s*\{", options_inner)
    if m:
        inner, _ = match_bracket(options_inner, m.end() - 1, "{", "}")
        return object_keys(inner), "json"
    m = re.search(r"body:\s*jsonBody\(\s*([A-Za-z_$][\w$]*)", options_inner)
    if m:
        ident = m.group(1)
        vm = re.search(rf"(?:const|let|var)\s+{re.escape(ident)}\s*=\s*\{{", body_src)
        if vm:
            inner, _ = match_bracket(body_src, vm.end() - 1, "{", "}")
            return object_keys(inner), "json"
        return [], "json"
    m = re.search(r"body:\s*([A-Za-z_$][\w$]*)", options_inner)
    if m:
        ident = m.group(1)
        parts = re.findall(rf"{re.escape(ident)}\.set\(\s*[\"']([^\"']+)[\"']", body_src)
        return parts, "multipart"
    return [], "none"


def extract_frontend() -> dict:
    files = sorted(glob.glob(os.path.join(FE, "**", "*.js"), recursive=True)
                   + glob.glob(os.path.join(FE, "**", "*.jsx"), recursive=True))
    endpoints: list[dict] = []
    mappers: dict[str, dict] = {}
    enums: dict[str, list[str]] = {}

    for f in files:
        src = open(f, encoding="utf-8").read()
        rel = os.path.relpath(f, FE)

        for m in re.finditer(r"const\s+(\w+)\s*=\s*\{", src):
            name = m.group(1)
            if name.endswith("ToApi") or name.endswith("FromApi"):
                inner, _ = match_bracket(src, m.end() - 1, "{", "}")
                keys = re.findall(r'([\u4e00-\u9fffA-Za-z_][\w\u4e00-\u9fff]*)\s*:\s*"([^"]+)"', inner)
                enums[name] = {"keys": [k for k, _ in keys], "values": [v for _, v in keys]}

        for m in re.finditer(r"export function (map\w+)\((\w+)\)", src):
            fn, arg = m.group(1), m.group(2)
            end = src.find("\nexport ", m.end())
            body = src[m.end():end if end > 0 else len(src)]
            js_builtin = {"map", "filter", "forEach", "length", "then", "catch", "push", "find", "some"}

            # 局部别名：const alias = item.x  /  const alias = otherAlias.y
            decls = [(am.group(1), am.group(2).strip())
                     for am in re.finditer(rf"(?:const|let)\s+(\w+)\s*=\s*([^\n;]+)", body)]
            raw_alias: dict[str, str] = {}
            for _ in range(len(decls) + 1):
                for name, rhs in decls:
                    if name in raw_alias:
                        continue
                    for cand in re.findall(rf"(?:{arg}\.[\w.]+|[A-Za-z_$][\w$]*\.[\w.]+)", rhs):
                        head = cand.partition(".")[0]
                        if head == arg or head in raw_alias:
                            raw_alias[name] = cand
                            break

            def resolve(expr: str, depth: int = 0) -> list[str] | None:
                """把别名表达式解析成相对 item 的路径段，如 ['packageSnapshot','items']。"""
                if depth > 4:
                    return None
                expr = expr.strip()
                if expr.startswith(f"{arg}."):
                    return expr[len(arg) + 1:].split(".")
                head, _, rest = expr.partition(".")
                if head in raw_alias:
                    base = resolve(raw_alias[head], depth + 1)
                    if base is None:
                        return None
                    return base + ([rest] if rest else [])
                return None

            direct = sorted(set(re.findall(rf"\b{arg}\.(\w+)", body)))
            nested: dict[str, list[str]] = {}
            element: dict[str, list[str]] = {}
            for alias, expr in raw_alias.items():
                path = resolve(expr)
                if not path:
                    continue
                key = ".".join(path)
                found = sorted(set(re.findall(rf"\b{alias}\.(\w+)", body)) - js_builtin)
                if found:
                    nested.setdefault(key, [])
                    nested[key] += found

            # 数组元素字段：对每个 .map((x) => ...) 回调，回溯被映射的表达式
            for mm in re.finditer(r"\.map\(\s*\(?(\w+)\)?\s*=>\s*", body):
                param = mm.group(1)
                prefix = body[:mm.start()]
                idm = re.search(r"([A-Za-z_$][\w$]*)$", prefix)
                path = None
                if idm and idm.group(1) in raw_alias:
                    path = resolve(raw_alias[idm.group(1)])
                if path is None:
                    window = prefix[-140:]
                    for cm in reversed(list(re.finditer(
                            rf"(?:{arg}\.[\w.]+|[A-Za-z_$][\w$]*\.[\w.]+)", window))):
                        path = resolve(cm.group(0))
                        if path:
                            break
                if path is None:
                    continue
                brace = body.find("{", mm.end())
                if brace < 0:
                    continue
                blk, _ = match_bracket(body, brace, "{", "}")
                el = sorted(set(re.findall(rf"\b{param}\.(\w+)", blk)))
                if el:
                    key = ".".join(path)
                    element.setdefault(key, [])
                    element[key] += el

            mappers[fn] = {
                "file": rel,
                "direct_fields": direct,
                "nested_fields": {k: sorted(set(v)) for k, v in nested.items()},
                "element_fields": {k: sorted(set(v)) for k, v in element.items()},
            }

        for m in re.finditer(r"export async function (\w+)\(", src):
            fn = m.group(1)
            end = src.find("\nexport ", m.end())
            body = src[m.end():end if end > 0 else len(src)]
            for rm in re.finditer(r"request\(\s*(`|\"|')", body):
                q = rm.group(1)
                close = body.find(q, rm.end())
                if close < 0:
                    continue
                raw = body[rm.end():close]
                if not raw.startswith("/api") and not raw.startswith("/uploads"):
                    continue
                path, query_keys = parse_template(raw)
                rest = body[close + 1:]
                options_inner = ""
                mo = re.match(r"\s*(?:,\s*)?\{", rest)
                if mo:
                    options_inner, _ = match_bracket(rest, mo.end() - 1, "{", "}")
                meth = "GET"
                mm = re.search(r'method:\s*"(\w+)"', options_inner)
                if mm:
                    meth = mm.group(1)
                body_keys, body_kind = extract_body(body, options_inner)
                applied = sorted(set(re.findall(r"\b(map\w+)\s*\(", body)))
                endpoints.append({
                    "fn": fn,
                    "file": rel,
                    "method": meth,
                    "path": path,
                    "raw_path": raw,
                    "query_params": query_keys,
                    "body_kind": body_kind,
                    "body_keys": body_keys,
                    "mappers": applied,
                })
    return {"endpoints": endpoints, "mappers": mappers, "enums": enums}


# --------------------------------------------------------------------------
# 后端：控制器路由 / DTO record / 枚举 / 服务层拼装的 JSON 形状
# --------------------------------------------------------------------------
def parse_record(path: str) -> dict | None:
    src = open(path, encoding="utf-8").read()
    m = re.search(r"public record (\w+)\s*\(", src)
    if not m:
        return None
    inner, _ = match_bracket(src, m.end() - 1, "(", ")")
    comps = []
    for part in split_top(inner):
        part = part.strip()
        if not part:
            continue
        ann = re.findall(r"@(\w+)", part)
        clean = re.sub(r"@\w+(?:\([^)]*\))?", "", part).strip()
        mm = re.match(r"([\w<>, \[\]\.]*?)\s+(\w+)$", clean)
        if not mm:
            continue
        comps.append({
            "name": mm.group(2),
            "type": mm.group(1).strip(),
            "required": any(a in ("NotNull", "NotBlank", "NotEmpty") for a in ann),
            "annotations": ann,
        })
    return {"name": m.group(1), "components": comps}


def parse_controllers() -> list[dict]:
    ops: list[dict] = []
    for f in sorted(glob.glob(os.path.join(BE, "controller", "*.java"))):
        src = open(f, encoding="utf-8").read()
        rel = os.path.relpath(f, BE)
        base_m = re.search(r'@RequestMapping\(\s*"([^"]*)"\s*\)', src)
        base = base_m.group(1) if base_m else ""
        for m in re.finditer(r"@(Get|Post|Put|Patch|Delete)Mapping\b", src):
            method = m.group(1).upper()
            ann_text = src[m.end():m.end() + 400]
            sub = ""
            if ann_text.startswith("("):
                ann_inner, _ = match_bracket(src, m.end(), "(", ")")
                paths = re.findall(r'"(/[^"]*)"', ann_inner)
                sub = paths[0] if paths else ""
            p = src.find("public ", m.end())
            if p < 0:
                continue
            paren = src.find("(", p)
            if paren < 0:
                continue
            ret = src[p + 7:paren].strip().split()
            handler = ret[-1] if ret else ""
            ret = " ".join(ret[:-1]) if len(ret) > 1 else ""
            params_inner, _ = match_bracket(src, paren, "(", ")")
            req_dto = None
            dm = re.search(r"RequestBody\s+(\w+)", params_inner)
            if dm:
                req_dto = dm.group(1)
            parts = re.findall(r'RequestPart\(\s*"([^"]+)"', params_inner)
            query_params, path_vars = [], []
            for part in split_top(params_inner):
                if "RequestParam" in part:
                    ids = re.findall(r"[A-Za-z_$][\w$]*", re.sub(r"@RequestParam\([^)]*\)", "", part))
                    if ids:
                        query_params.append(ids[-1])
                if "PathVariable" in part:
                    ids = re.findall(r"[A-Za-z_$][\w$]*", re.sub(r"@PathVariable\([^)]*\)", "", part))
                    if ids:
                        path_vars.append(ids[-1])
            ret_dto = None
            if ret.startswith("Map<") or ret.startswith("Map "):
                ret_dto = "Map"
            elif "<" in ret:
                inner_t = ret[ret.index("<") + 1:ret.rindex(">")]
                ret_dto = inner_t.strip()
            elif ret not in ("void", "ResponseEntity", ""):
                ret_dto = ret
            ops.append({
                "method": method,
                "path": (base + sub) or "/",
                "file": rel,
                "request_dto": req_dto,
                "multipart_parts": parts,
                "query_params": query_params,
                "path_vars": path_vars,
                "response_dto": ret_dto,
                "handler": handler,
            })
    return ops


def parse_dtos() -> dict:
    dtos: dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.join(BE, "dto", "*.java"))):
        rec = parse_record(f)
        if rec:
            dtos[rec["name"]] = rec
    return dtos


def parse_backend_enums() -> dict:
    out = {}
    for f in sorted(glob.glob(os.path.join(BE, "entity", "*.java"))):
        src = open(f, encoding="utf-8").read()
        m = re.search(r"public enum (\w+)\s*\{", src)
        if not m:
            continue
        inner, _ = match_bracket(src, m.end() - 1, "{", "}")
        vals = []
        for part in split_top(inner):
            part = part.strip()
            if not part or part.startswith("//"):
                continue
            vm = re.match(r"^([A-Z][A-Z0-9_]*)\b", part)
            if vm:
                vals.append(vm.group(1))
        out[m.group(1)] = vals
    return out


def parse_snapshot_shape() -> dict:
    """OrderService 拼出来的 packageSnapshot / addedItemsSnapshot 的键集合。"""
    f = os.path.join(BE, "service", "OrderService.java")
    if not os.path.exists(f):
        return {}
    src = open(f, encoding="utf-8").read()
    shape: dict[str, list[str]] = {}
    for var, key in re.findall(r'(\w+)\.(?:put|set)\(\s*"(\w+)"', src):
        shape.setdefault(var, [])
        if key not in shape[var]:
            shape[var].append(key)
    return shape


def parse_db_checks() -> dict:
    if not os.path.exists(CLASS_DECL):
        return {}
    d = json.load(open(CLASS_DECL, encoding="utf-8"))
    out = {}
    for table, tdef in (d.get("ddl", {}).get("tables") or {}).items():
        for cname, c in (tdef.get("constraints") or {}).items():
            out[f"{table}.{cname}"] = c.get("expr", "")
    return out


# --------------------------------------------------------------------------
# 录制流量
# --------------------------------------------------------------------------
def extract_recordings() -> dict:
    rows = []
    for f in sorted(glob.glob(os.path.join(REC, "*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                r = json.loads(line)
                r["file"] = os.path.basename(f)
                rows.append(r)
    agg: dict[str, dict] = collections.defaultdict(lambda: {"calls": 0, "statuses": [], "latencies": [], "queries": []})
    for r in rows:
        key = f"{r['method']} {r['path']}"
        a = agg[key]
        a["calls"] += 1
        a["statuses"].append(r["status"])
        a["latencies"].append(r.get("latency_ms") or 0)
        if r.get("query"):
            a["queries"].append(r["query"])
    for a in agg.values():
        a["status_set"] = sorted(set(a["statuses"]))
        a["max_latency_ms"] = max(a["latencies"])
        a["min_latency_ms"] = min(a["latencies"])
    return {
        "rows": len(rows),
        "endpoints": dict(agg),
        "range": [min(r["ts"] for r in rows), max(r["ts"] for r in rows)] if rows else None,
        "raw": rows,
    }


def main() -> None:
    fe = extract_frontend()
    be = {
        "operations": parse_controllers(),
        "dtos": parse_dtos(),
        "enums": parse_backend_enums(),
        "snapshot_shape": parse_snapshot_shape(),
        "db_checks": parse_db_checks(),
    }
    rec = extract_recordings()
    decl = {
        "generated_at": datetime.datetime.now().isoformat(),
        "frontend": fe,
        "backend": be,
        "recordings": rec,
    }
    json.dump(decl, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"集权层声明 -> {OUT}")
    print(f"  前端 endpoint {len(fe['endpoints'])} 个 / 映射函数 {len(fe['mappers'])} 个 / 枚举表 {len(fe['enums'])} 个")
    for e in sorted(fe["endpoints"], key=lambda x: (x["path"], x["method"])):
        extra = ""
        if e["query_params"]:
            extra += f" query={e['query_params']}"
        if e["body_keys"]:
            extra += f" body={e['body_keys']}"
        if e["mappers"]:
            extra += f" -> {e['mappers']}"
        print(f"    {e['method']:6} {e['path']:32}{extra}")
    print(f"  后端 operation {len(be['operations'])} 个 / DTO {len(be['dtos'])} 个 / 枚举 {list(be['enums'])}")
    print(f"  快照形状 {be['snapshot_shape']}")
    print(f"  录制流量: {rec['rows']} 条, {len(rec['endpoints'])} 个 endpoint, 区间 {rec['range']}")


if __name__ == "__main__":
    main()
