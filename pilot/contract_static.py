"""静态层：产出每个 endpoint 的契约声明。
声明来源 = OpenAPI(注解生成) + 控制器源码扫描 + 全局配置事实。
纯静态，不发任何请求。
"""
import project
import json, os, re, sys, glob, datetime


ROOT = str(project.backend_root())
CTRL = str(project.controller_dir())
OUT = str(project.artifact_dir() / "declaration.json")

MAP_ANN = re.compile(r'@(Get|Post|Put|Delete|Patch)Mapping(?:\(\s*(?:value\s*=\s*)?"([^"]*)"[^)]*\))?')
CLASS_MAP = re.compile(r'@RequestMapping\(\s*"([^"]*)"')
METHOD_SIG = re.compile(r'public\s+[\w<>,\[\]\.\s]+\s+(\w+)\s*\(')
AUTH_ANN = re.compile(r'@(PreAuthorize|Secured|RolesAllowed)\b')

def scan_controllers():
    ops = {}
    # 控制器按注解发现，不靠固定目录 —— 真实项目的包名五花八门
    # （controller / web / api / rest / interfaces…），写死一个目录就只支持一种习惯。
    ctrl_files = []
    for f in glob.glob(os.path.join(str(project.java_source_root()), "**", "*.java"), recursive=True):
        head = open(f, encoding="utf-8", errors="replace").read(4000)
        if "@RestController" in head or "@Controller" in head:
            ctrl_files.append(f)
    for f in sorted(ctrl_files):
        src = open(f).read()
        lines = src.splitlines()
        cls = CLASS_MAP.search(src)
        base = cls.group(1) if cls else ""
        # 逐个方法级 mapping 注解
        for m in MAP_ANN.finditer(src):
            http = m.group(1).upper()
            sub = m.group(2) or ""
            tail = src[m.end():m.end() + 600]
            sig = METHOD_SIG.search(tail)
            # 注解与签名之间的行
            between = src[m.end():m.end() + (sig.start() if sig else 0)]
            line = src[:m.start()].count("\n") + 1
            ops[(http, base + sub)] = {
                "file": os.path.relpath(f, ROOT),
                "line": line,
                "handler": sig.group(1) if sig else None,
                "has_valid": "@Valid" in between,
                "has_request_body": "@RequestBody" in between,
                "has_path_variable": "@PathVariable" in between,
                "authz_in_file": bool(AUTH_ANN.search(src)),
            }
    return ops

def resolve_ref(spec, schemas):
    if "$ref" in spec:
        name = spec["$ref"].split("/")[-1]
        return schemas.get(name, {})
    return spec

def op_schema(op, schemas):
    for code, r in (op.get("responses") or {}).items():
        content = (r.get("content") or {}).get("application/json") or {}
        s = content.get("schema")
        if s:
            return code, resolve_ref(s, schemas), s.get("$ref", "").split("/")[-1] or None
    return None, None, None

def main():
    api_path = project.artifact_dir() / "openapi.json"
    if not api_path.exists():
        # 没有 OpenAPI 快照（目标没开 springdoc，或没抓过）→ 只做 controller 声明抽取，
        # 跳过 schema 比对部分。**不静默**：报告里会显式标注这一段没跑。
        print("  提示：没有 openapi.json，跳过响应 schema 比对部分")
        api = {"paths": {}}
    else:
        api = json.load(open(api_path))
    schemas = api.get("components", {}).get("schemas", {})
    ctrl = scan_controllers()

    src_all = ""
    for root, _, files in os.walk(os.path.join(ROOT, "src/main/java")):
        for fn in files:
            if fn.endswith(".java"):
                src_all += open(os.path.join(root, fn)).read()
    pom = open(os.path.join(ROOT, "pom.xml")).read()

    page_max = max(int(x) for x in re.findall(r'Math\.min\(Math\.max\(pageSize,\s*1\),\s*(\d+)\)', src_all)) \
        if re.findall(r'Math\.min\(Math\.max\(pageSize', src_all) else None
    err_enum = re.search(r'public enum ErrorCode \{(.*?)\}', src_all, re.S)
    error_codes = [x.strip() for x in err_enum.group(1).split(",") if x.strip()] if err_enum else []

    operations = []
    for path, item in api.get("paths", {}).items():
        for http, op in item.items():
            if http not in ("get", "post", "put", "delete", "patch"):
                continue
            code, sch, sch_name = op_schema(op, schemas)
            req_schema, req_name = None, None
            rb = (op.get("requestBody") or {}).get("content", {}).get("application/json")
            if rb:
                req_schema = resolve_ref(rb.get("schema", {}), schemas)
                req_name = rb.get("schema", {}).get("$ref", "").split("/")[-1] or None
            params = [{"name": p.get("name"), "in": p.get("in"), "required": p.get("required", False),
                       "schema": p.get("schema", {})} for p in op.get("parameters", [])]
            key = (http.upper(), path)
            c = ctrl.get(key, {})
            operations.append({
                "key": f"{http.upper()} {path}",
                "method": http.upper(), "path": path,
                "source": {"file": c.get("file"), "line": c.get("line"), "handler": c.get("handler")},
                "declared_status": sorted((op.get("responses") or {}).keys()),
                "request_schema": req_name,
                "request_required": (req_schema or {}).get("required", []),
                "response_schema": sch_name,
                "response_props": sorted(((sch or {}).get("properties") or {}).keys()),
                "params": params,
                "has_valid": c.get("has_valid", False),
                "authz": {"declared": bool(c.get("authz_in_file")), "mechanism": None},
            })

    if not operations and ctrl:
        # 没有 OpenAPI 快照（目标没开 springdoc）→ 用控制器扫描结果兜底，
        # 至少保住「有哪些接口、从哪个 handler 进、收不收 @Valid」这些事实。
        for (http, path), c in sorted(ctrl.items()):
            operations.append({
                "key": f"{http} {path}",
                "method": http, "path": path,
                "source": {"file": c.get("file"), "line": c.get("line"),
                           "handler": c.get("handler")},
                "declared_status": [],
                "request_schema": None, "request_required": [],
                "response_schema": None, "response_props": [],
                "params": [],
                "has_valid": c.get("has_valid", False),
                "authz": {"declared": bool(c.get("authz_in_file")), "mechanism": None},
                "openapi_missing": True,
            })
        print(f"  已用控制器扫描兜底，产出 {len(operations)} 个 operation（缺响应 schema）")

    decl = {
        "generated_at": datetime.datetime.now().isoformat(),
        "global": {
            "security_annotations": sorted(set(AUTH_ANN.findall(src_all))),
            "security_on_classpath": "spring-boot-starter-security" in pom,
            "page_size_max": page_max,
            "error_codes": error_codes,
            "envelope_fields": ["success", "code", "message", "data", "traceId", "timestamp"],
        },
        "operations": sorted(operations, key=lambda o: o["key"]),
    }
    json.dump(decl, open(OUT, "w"), ensure_ascii=False, indent=2)
    print(f"声明生成: {len(operations)} 个 operation -> {OUT}")
    print("  全局: 鉴权注解", decl["global"]["security_annotations"] or "无",
          "| spring-security 依赖:", decl["global"]["security_on_classpath"],
          "| pageSize 上限:", page_max)
    print("  仅声明 200 的 operation 数:",
          sum(1 for o in operations if o["declared_status"] == ["200"]))

if __name__ == "__main__":
    main()
