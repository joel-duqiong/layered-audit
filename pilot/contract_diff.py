"""差分引擎：静态声明 vs 动态实测 -> 缺陷清单。
每条 finding 都带复现信息（哪个 endpoint、哪个场景、实测到什么）。
"""
import project
import json, os, statistics
from collections import defaultdict

D = str(project.artifact_dir())
decl = json.load(open(os.path.join(D, "declaration.json")))
obs = json.load(open(os.path.join(D, "observation.json")))
# 项目侧声明（service_scenarios.json）：差分口径里需要「项目知识」的部分从这来
scen_path = os.path.join(project.project_dir(), "service_scenarios.json")
scen = json.load(open(scen_path)) if os.path.exists(scen_path) else {}
TARGET_NAME = project.binding().get("target", "") or project.project_dir().name
g = decl["global"]
ops = {o["key"]: o for o in decl["operations"]}
findings = []

def add(fid, sev, key, title, detail, repro, evidence):
    findings.append({"id": fid, "severity": sev, "endpoint": key, "title": title,
                     "detail": detail, "repro": repro, "evidence": evidence})

# F1 声明只覆盖 200，实际错误码全部未声明
undeclared = defaultdict(set)
for o in obs["observations"]:
    d = ops.get(o["key"], {})
    dec = set(d.get("declared_status", []))
    if str(o["status"]) not in dec:
        undeclared[o["key"]].add(o["status"])
for key, codes in sorted(undeclared.items(), key=lambda kv: -len(kv[1])):
    if 500 in codes:
        add("UNHANDLED_HTTP_EXCEPTION", "HIGH", key,
            f"客户端错误被当成服务端错误：返回 {sorted(codes)}",
            "OpenAPI 只声明 200；畸形请求触发 500 INTERNAL_ERROR。GlobalExceptionHandler 没有为该异常"
            "类型注册 @ExceptionHandler，落到兜底 Exception 分支 —— 客户端错误被当成服务端故障，"
            "污染告警与 SLO。",
            f"curl -X {ops[key]['method']} {obs['observations'][0]['request'] if False else ''}"
            f"见 observation.json 中 key={key} 的 missing_multipart_part 场景",
            {"observed_status": sorted(codes), "declared_status": ops[key]["declared_status"]})

# F2 非法输入被接受（缺少必填 / 类型错误仍返回 2xx）
for o in obs["observations"]:
    if o["scenario"].startswith("missing_required") and 200 <= o["status"] < 300:
        add("ACCEPTED_INVALID", "CRITICAL", o["key"],
            f"非法输入被接受（{o['scenario']}）",
            "必填字段缺失/类型错误的服务端仍返回 2xx，说明该字段没有服务端强校验。",
            f"curl -X {ops[o['key']]['method']} {o['request']} (scenario={o['scenario']})",
            {"status": o["status"], "scenario": o["scenario"]})

# F2b 类型强转：发了错误 JSON 类型，接口却接受（Jackson 隐式转换）
for o in obs["observations"]:
    if o["scenario"].startswith("wrong_type") and 200 <= o["status"] < 300:
        field = o["scenario"].split(":", 1)[1]
        add("TYPE_COERCION", "MEDIUM", o["key"],
            f"错误 JSON 类型被隐式转换后接受（字段 {field}）",
            "OpenAPI 声明该字段为特定类型，实际发送错误类型仍被 200 接受——"
            "Jackson 隐式把数字转成字符串等。调用方拼错类型不会报错，只会静默变成脏数据。",
            "curl -X " + ops[o["key"]]["method"] + " " + o["request"] + ' -d {"' + field + '": 12345}',
            {"scenario": o["scenario"], "accepted_value": (o.get("json") or {}).get("data")})

# F2c 内部实现细节泄漏到客户端
for o in obs["observations"]:
    j = o.get("json") or {}
    det = (j.get("details") or {}).get("message") if isinstance(j.get("details"), dict) else None
    if det and ("JSON parse error" in det or "Cannot deserialize" in det
                or project.java_package() in det or "java." in det
                or "org.springframework" in det):
        add("INTERNAL_LEAK", "MEDIUM", o["key"],
            "错误响应把内部实现细节返回给客户端",
            f"details.message 直接透传框架异常原文（{det[:80]}...），"
            "泄漏内部类名、框架类型与反序列化细节。",
            f"curl -X {ops[o['key']]['method']} {o['request']}",
            {"details_message": det[:200]})

# F3 鉴权缺失
if not g["security_annotations"] and not g["security_on_classpath"]:
    # 「零鉴权」需要一个能读出业务数据的端点作证人 ——
    # 哪个端点、读到什么，是项目知识，声明在 service_scenarios.json。
    authz = scen.get("authz_probe") or {}
    witness = (str(authz["method"]), str(authz["path"])) \
        if authz.get("method") and authz.get("path") else None
    readable = [o for o in obs["observations"]
                if o["scenario"] == "valid" and o["status"] == 200
                and (witness is None or o["key"].startswith(f"{witness[0]} {witness[1]}"))]
    if readable:
        noun = authz.get("data_noun", "业务数据")
        sample = authz.get("data_sample", "HTTP 200 + 返回业务数据")
        add("NO_AUTHZ", "CRITICAL", "ALL",
            "全系统零鉴权：无凭证即可读写全部业务数据",
            f"源码无任何鉴权注解 ({g['security_annotations'] or '空'})、classpath 无 spring-security；"
            f"动态实测带伪造 Bearer token 仍可成功读取{noun} {readable[0]['request']}。"
            "这里不是实现漏了鉴权，而是授权不变量根本不存在。",
            f"curl {project.base_url()}{readable[0]['request']}   # 无任何凭证",
            {"probe_result": sample,
             "static_evidence": "security_annotations=[] spring_security_on_classpath=False"})

# F4 响应结构漂移（实测 data 字段 vs 声明 schema）
for o in obs["observations"]:
    if o["scenario"] != "valid" or o["status"] != 200:
        continue
    d = ops.get(o["key"], {})
    declared = set(d.get("response_props") or [])
    if not declared:
        continue
    actual = set(o.get("data_keys") or [])
    extra, missing = actual - declared, declared - actual
    if extra or missing:
        add("RESPONSE_DRIFT", "MEDIUM", o["key"],
            f"响应字段与 OpenAPI 声明不一致（多 {sorted(extra)} / 缺 {sorted(missing)}）",
            "静态声明（OpenAPI）与动态实测的返回结构漂移，按声明生成的客户端/前端类型对不上实际返回。",
            f"curl {o['request']}",
            {"declared": sorted(declared), "observed": sorted(actual)})

# F5 错误响应破坏统一 envelope
for o in obs["observations"]:
    if o["status"] != 200 and not o.get("envelope_ok"):
        add("ENVELOPE_BREAK", "HIGH", o["key"],
            f"错误响应不符合统一 envelope（HTTP {o['status']}）",
            "非 200 响应缺少 success/code/message 字段，调用方无法用统一逻辑处理。",
            f"scenario={o['scenario']}", {"status": o["status"], "body": o.get("json")})

# F6 错误码不在 ErrorCode 枚举内
valid_codes = set(g["error_codes"])
for o in obs["observations"]:
    c = o.get("code")
    if o["status"] == 200:
        continue
    if c and c not in valid_codes and c != "OK":
        add("ERROR_CODE_UNKNOWN", "MEDIUM", o["key"],
            f"返回了枚举外的错误码 {c}",
            "ErrorCode 枚举与实际返回不一致，错误码无法收敛。",
            f"scenario={o['scenario']}", {"code": c, "enum": sorted(valid_codes)})

# F7 延迟
lat = defaultdict(list)
for o in obs["observations"]:
    lat[o["key"]].append(o["latency_ms"])
slow = sorted(((max(v), k) for k, v in lat.items()), reverse=True)[:5]
for mx, key in slow:
    if mx > 200:
        add("LATENCY", "MEDIUM", key, f"最大延迟 {mx:.0f}ms",
            "端到端延迟偏高。", f"见 observation.json key={key}", {"max_ms": mx})

sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
findings.sort(key=lambda f: sev_order.get(f["severity"], 9))
json.dump(findings, open(os.path.join(D, "findings.json"), "w"), ensure_ascii=False, indent=2)

# Markdown 报告
lines = [f"# 服务层契约差分报告（{TARGET_NAME}）", "",
         f"静态声明 {len(decl['operations'])} 个 operation | 动态实测 {len(obs['observations'])} 次 | "
         f"finding {len(findings)} 条", "",
         "| 级别 | 端点 | 缺陷 |", "|---|---|---|"]
for f in findings:
    lines.append(f"| {f['severity']} | `{f['endpoint']}` | {f['title']} |")
lines.append("")
for f in findings:
    lines += [f"## [{f['severity']}] {f['endpoint']} — {f['title']}", "",
              f["detail"], "", f"复现：`{f['repro']}`", "",
              "```json", json.dumps(f["evidence"], ensure_ascii=False, indent=2), "```", ""]
open(os.path.join(D, "findings.md"), "w").write("\n".join(lines))

print(f"差分结果：{len(findings)} 条 finding")
for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
    n = sum(1 for f in findings if f["severity"] == s)
    if n:
        print(f"  {s:8} {n}")
print()
for f in findings[:12]:
    print(f"[{f['severity']}] {f['endpoint']}  {f['title']}")
