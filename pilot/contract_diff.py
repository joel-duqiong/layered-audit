"""差分引擎：静态声明 vs 动态实测 -> 缺陷清单。
每条 finding 都带复现信息（哪个 endpoint、哪个场景、实测到什么）。
"""
import project
import json, os, statistics, sys
from collections import defaultdict

sys.path.insert(0, project.engine_path())
import oracle_gate  # noqa: E402

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
held = []

# ---------------------------------------------------------------------------
# 判据来源声明（warrant）—— 区分「技术可自证」与「业务预期」，逐条可复核。
#
# 分类判据（三问，按序）：
#   Q1 域替换：把被测系统换成任意 CRUD 应用，这条断言是否仍是缺陷？
#   Q2 权威出处：这条「应该」能否指向通用权威（CWE / 语言规范 / 平台语义 / HTTP 规范），
#              而不必引用本项目的业务量？
#   Q3 混合裁决：若两者都像，问「撤销该业务规则后断言是否仍成立」——
#              仍成立归 technical，不成立归 business。
#
# technical → 直接开单（**绝不压**，压掉技术缺陷是漏报，比假阳性更危险）
# business  → 必须有 confirmed oracle，否则进待确认疑点 / 已证伪留痕
# 未声明    → 保守压住并标「未裁定」，不静默开单
# ---------------------------------------------------------------------------
WARRANTS = {
    "UNHANDLED_HTTP_EXCEPTION": {
        "kind": "technical", "authority": "CWE-755 + HTTP 语义（5xx 表示服务端故障）",
        "domain_swap": "holds",
        "basis": "畸形客户端请求触发 500 而非 4xx，在任何应用里都是错误分类缺陷；"
                 "AGENTS.md:157 也明确 500=服务内部错误。判据不依赖本项目业务规则。",
        "evidence": "exception/GlobalExceptionHandler.java:92 handleUnknown 兜底分支"},
    "ACCEPTED_INVALID": {
        "kind": "technical", "authority": "CWE-20（不当输入校验）",
        "domain_swap": "holds",
        "basis": "声明为必填的字段缺失仍返回 2xx，是服务端校验缺失的客观事实；"
                 "「必填」来自 OpenAPI/DTO 声明本身，不是业务方偏好。",
        "evidence": "declaration.json 的 declared_status 与 observation.json 实测 2xx 对账"},
    "TYPE_COERCION": {
        "kind": "technical", "authority": "CWE-704（不正确的类型转换）",
        "domain_swap": "holds",
        "basis": "OpenAPI 声明的类型与实测接受的类型不一致，是契约与实现的客观漂移，"
                 "不涉及业务语义。",
        "evidence": "declaration.json response/request schema vs observation.json wrong_type 场景"},
    "INTERNAL_LEAK": {
        "kind": "technical", "authority": "CWE-209（错误消息含敏感信息）",
        "domain_swap": "holds",
        "basis": "框架异常原文（内部类名/反序列化细节）透传给客户端，在任何应用里都是信息泄漏。",
        "evidence": "exception/GlobalExceptionHandler.java:60 handleBadRequest / :85 handleDataIntegrity"},
    "NO_AUTHZ": {
        "kind": "technical", "authority": "CWE-306 / CWE-862（关键功能缺失认证/授权）",
        "domain_swap": "holds",
        "basis": "无凭证即可读写含 PII（客户姓名/电话/地址）的全部业务数据，"
                 "是通用安全不变式，不依赖本项目业务规则。",
        "evidence": "源码 security_annotations=[] 且 classpath 无 spring-security；实测伪造 Bearer 仍 200"},
    "RESPONSE_DRIFT": {
        "kind": "technical", "authority": "CWE-436（解释冲突）",
        "domain_swap": "holds",
        "basis": "实测返回字段集合与 OpenAPI 声明集合的差集是客观事实，"
                 "按声明生成的客户端必然对不上，不涉及业务语义。",
        "evidence": "declaration.json response_props vs observation.json data_keys"},
    "ENVELOPE_BREAK": {
        "kind": "business", "domain_swap": "fails",
        "basis": "「所有接口必须返回统一 JSON 信封」是本项目明文 API 契约，不是通用规范；"
                 "换个不要求信封的应用就不成立。该契约有文档依据且已被权威对照表确认。",
        "evidence": "exception/GlobalExceptionHandler.java:78 handleClientAbort 返回 ResponseEntity<Void>",
        "oracle": {"source": "project_doc", "confirmation": "confirmed",
                   "reference": "/Users/june/jiuyan/AGENTS.md:100",
                   "text": "所有接口必须返回统一 JSON。",
                   "note": "业务校验对照.md:12 在「已确认一致的规则」表里记 envelope 一致，"
                           "预期来源 AGENTS.md；非 200 响应缺 success/code/message 即违背该明文契约。"}},
    "ERROR_CODE_UNKNOWN": {
        "kind": "technical", "authority": "CWE-755（异常条件处理不当）",
        "domain_swap": "holds",
        "basis": "返回的错误码不在代码自己声明的 ErrorCode 枚举内，是代码内部自相矛盾，"
                 "客观可判，不需业务方确认。",
        "evidence": "declaration.json global.error_codes vs observation.json code"},
    "LATENCY": {
        "kind": "business", "domain_swap": "fails",
        "basis": "200ms 这个阈值写死在 contract_diff.py:140，全语料无任何文档声明延迟预算；"
                 "「多慢算慢」是业务/运维预算，不是通用不变式。",
        "evidence": "contract_diff.py:140 `if mx > 200`",
        "oracle": {"source": "inferred", "confirmation": "unconfirmed",
                   "reference": "无文档依据（全语料未声明延迟预算）",
                   "note": "阈值 200ms 无来源。延迟随数据量增长是客观观测，但「超过 200ms 即缺陷」"
                           "需要业务/运维确认预算后才能开单。"}},
}


def add(fid, sev, key, title, detail, repro, evidence):
    filed = {"id": fid, "severity": sev, "endpoint": key, "title": title,
             "detail": detail, "repro": repro, "evidence": evidence}
    kept, gated = oracle_gate.warrant_split(WARRANTS.get(fid), [filed], rule_id=fid)
    findings.extend(kept)
    held.extend(gated)

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

# 判据来源不足的业务预期：不进缺陷台账，但必须留痕（硬约束 5）。无条件写。
_hb = oracle_gate.buckets(held)
json.dump({"unconfirmed": _hb["unconfirmed"], "unconfirmed_count": len(_hb["unconfirmed"]),
           "refuted": _hb["refuted"], "refuted_count": len(_hb["refuted"]),
           "note": "服务层契约差分的判据来源分流：technical（通用安全/正确性/契约自洽）直接开单，"
                   "business 必须有 confirmed oracle。未经确认的进待确认疑点（规范层风险），"
                   "被文档明确否定的只保留观测；未声明 warrant 的按「未裁定」保守留痕。"},
          open(os.path.join(D, "service_rule_suspects.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

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
