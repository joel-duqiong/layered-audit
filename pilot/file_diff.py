"""文件层差分：字节码声明（file_declaration.json）↔ 整类实例化 + 真实容器实测（file_observation.json）。

差分的意义：两边对不上的地方就是缺陷；两边对得上的地方给出通过结论
（避免只报问题、不给「查过没问题」的证据）。

归属规则（全局）：owner = 能给出可复现证据的最低层。文件层比服务层低，
因此服务层的 INTERNAL_LEAK 这类「现象级」结论在这里收敛成「根因级」结论，
并在 related 里写明被收敛的是哪一条高层开单，不重复开单。

本层只对三件事负责：
  O1 跨方法一致性（同一文件里几个方法对同一条规则的表达是否一致）
  O2 顺序与原子性（方法体内的调用顺序，中途失败会不会留下残局）
  O3 资源边界同源性（文件持有的资源/根目录/日志/线程局部状态是否可信）
"""
from __future__ import annotations

import json
from pathlib import Path

D = Path(__file__).parent
decl = json.loads((D / "file_declaration.json").read_text())
obs = json.loads((D / "file_observation.json").read_text())

findings: list[dict] = []


def add(**kw) -> None:
    findings.append(kw)


def raw_of(sid: str) -> dict:
    for r in obs["results"]:
        if r["id"] == sid:
            return r.get("raw", {})
    return {}


def http_raw() -> dict:
    return raw_of("HTTP-PROBES")


def check_of(sid: str, name: str) -> dict:
    for r in obs["results"]:
        if r["id"] == sid:
            for c in r["checks"]:
                if c["check"] == name:
                    return c
    return {}


def file_of(simple: str) -> dict:
    for f in decl["files"]:
        if f["class"].split(".")[-1] == simple:
            return f
    return {}


# ---------------------------------------------------------------- 取证据

pkg_raw = raw_of("PKG-UPDATE-MISSING-PRODUCT")
pkg_items = raw_of("PKG-UPDATE-SUBSET")
qty_raw = raw_of("PKG-QUANTITY-ZERO")
null_type = raw_of("PKG-NULL-BANQUET-TYPE")
empty_items = raw_of("PKG-EMPTY-ITEMS")
dup_items = raw_of("PKG-DUPLICATE-ITEMS")
dup_db = raw_of("PKG-DUP-DB-ITEMS")
cors = raw_of("CORS-ORIGIN-MATRIX")
cors_cred = raw_of("CORS-CREDENTIALS")
loc = raw_of("UPLOADS-RESOURCE-LOCATION")
same_root = raw_of("UPLOADS-SAME-ROOT")
xff = raw_of("TRACE-XFF-SPOOF")
trace = raw_of("TRACE-ID-VERBATIM")
integrity = raw_of("ERR-DATA-INTEGRITY-LEAK")
badreq = raw_of("ERR-BADREQUEST-ECHO")
abort = raw_of("ERR-CLIENT-ABORT-ENVELOPE")
unknown = raw_of("ERR-UNKNOWN-NO-LEAK")
advice = raw_of("ADVICE-ENVELOPE")
http = http_raw()
rollback = http.get("HTTP-PKG-ROLLBACK", {})
cors_real = http.get("HTTP-CORS-REAL", {})
qzero = http.get("HTTP-PKG-QTY-ZERO", {})

pkg_static = file_of("PackageService")
apply_items = next((m for m in pkg_static["methods"] if m["name"] == "applyItems"), {})
pkg_update = next((m for m in pkg_static["methods"] if m["name"] == "update"), {})
ctx_static = file_of("TraceContext")

# ---------------------------------------------------------------- O2 顺序与原子性

add(id="FILE-PKG-MUTATE-BEFORE-VALIDATE",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-662 (Improper Synchronization)",
    title="PackageService.applyItems 先删后校验：明细被清空后才去查商品",
    detail=(
        "applyItems 的编排顺序是「先把没被提交的明细从实体上摘掉，再去 DB 查每个商品是否存在」。"
        "商品查不到时抛 404，但实体已经被改过一轮 —— 内存里的套餐此刻是一条空明细的套餐。"
        "实测：2 条明细的套餐，用一个不存在的商品提交更新，异常抛出后实体的 items 变成 0 条。\n"
        "字节码层面是硬证据：applyItems 的指令序列里 java.util.List.removeIf 出现在 "
        "ProductRepository.findByIdAndIsDeletedFalse 之前。\n"
        "为什么不是 HIGH：update 方法带 @Transactional，实测真实 HTTP 链路里 404 之后 "
        "DB 的 package_items 与 packages.name 都没变（事务完整回滚）。"
        "所以这是「纵深防御缺口」而不是现网数据损坏 —— 但编排顺序本身就是错的："
        "只要有人给 update 去掉 @Transactional、或未来改成内部自调用绕过代理，"
        "这个顺序立刻变成真实的明细丢失。校验应该在变更之前，不是之后。"
    ),
    evidence={
        "bytecode_order_applyItems": apply_items.get("calls_external_ordered", []),
        "static_proof": "java.util.List.removeIf 出现在 ProductRepository.findByIdAndIsDeletedFalse 之前",
        "dynamic_before_items": pkg_raw.get("before_items"),
        "dynamic_after_items": pkg_raw.get("after_items"),
        "dynamic_error": f"{pkg_raw.get('error_code')} / {pkg_raw.get('http_status')}",
        "boundary_protected": "HTTP 层实测回滚：items_unchanged=%s, name_unchanged=%s"
                              % (rollback.get("items_unchanged"), rollback.get("name_unchanged")),
        "transaction_on": pkg_update.get("transactional"),
    },
    root_cause_file="service/PackageService.java",
    related=["FN-APPLY-NULL-CONTRACT（函数层同构问题：入参未校验）"],
    note="服务文件里的编排顺序错了，被事务边界挡住了 —— 这就是分层审计要抓的「差一层才发现」的中间态。")

# ---------------------------------------------------------------- O1 跨方法 / 跨文件一致性

add(id="FILE-VALIDATION-STATUS-SPLIT",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-436 (Interpretation Conflict)",
    title="同一类「参数非法」返回两种状态码：DTO 校验给 400，服务自校验给 422",
    detail=(
        "参数校验被拆在两个文件里，两边各自选了不同的状态码：\n"
        "  · DTO 文件声明的约束（数量 @Min(1)、价格 @Min(0)）走 Bean Validation → "
        "MethodArgumentNotValidException → handleValidation → 400；\n"
        "  · 服务文件自己做的校验（明细为空、明细重复）抛 BusinessException → 422。\n"
        "实测同一个接口 PUT /api/packages/4：数量=0 → 400；明细为空 → 422。"
        "两者在语义上都是「请求参数不合法」，前端却要写两个分支才能区分，"
        "监控按状态码分类时也会把一类错误拆成两堆。\n"
        "根因不是某一处写错，而是校验责任分散在 DTO 与 service 两个文件、且没有统一的错误语义约定。"
    ),
    evidence={
        "dto_validation_status": qzero.get("status"),
        "dto_validation_body": qzero.get("body"),
        "service_validation_status": empty_items.get("http_status"),
        "service_validation_code": empty_items.get("error_code"),
        "split_check": check_of("HTTP-PROBES", "validation_status_split"),
    },
    root_cause_file="dto/PackageItemRequest.java + service/PackageService.java",
    related=["UNHANDLED_HTTP_EXCEPTION（服务层）"])

add(id="FILE-UPLOAD-LIMIT-SPLIT-ACROSS-THREE-FILES",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-1078 (Inappropriate Source Code Style or Formatting)",
    title="上传体积上限 5MB 这个数字散落三处，改一处另两处就开始说谎",
    detail=(
        "UploadService.java 这个文件里没有任何体积判断 —— 上限完全由配置文件 application.yml 的 "
        "spring.servlet.multipart.max-file-size 承担。而同一个数字 5MB 又以两种形式被复制到代码里：\n"
        "  · GlobalExceptionHandler.handleMaxUploadSize 的错误文案「图片不能超过 5MB」；\n"
        "  · UploadController @Operation 的接口文档「最大 5MB」。\n"
        "真实容器实测：1MB 通过、4MB 通过、6MB 被拒、20MB 被拒 —— 5MB 确实生效，"
        "函数层那条「无体积上限、20MB 被完整写盘」在 HTTP 边界被配置文件挡住了。"
        "但挡住它的是配置，不是代码：任何一次环境覆盖（SPRING_APPLICATION_JSON / 环境变量 / "
        "配置中心 / 默认值回落）都会静默放开上传，而错误文案与接口文档仍然写着 5MB —— "
        "限制放宽了，提示没变，用户按提示做的判断是错的。"
    ),
    evidence={
        "declared_in_yml": "spring.servlet.multipart.max-file-size: 5MB",
        "coded_in_handler": "GlobalExceptionHandler.handleMaxUploadSize -> \"图片不能超过 5MB\"",
        "coded_in_openapi_doc": "UploadController @Operation(description=\"... 最大 5MB。\")",
        "measured_1mb": "200 接受",
        "measured_4mb": "200 接受",
        "measured_6mb": "400 拒绝（图片不能超过 5MB）",
        "measured_20mb": "400 拒绝",
        "service_file_has_size_check": False,
        "related_fn_finding": "FN-UPLOAD-NO-SIZE-LIMIT（函数层在服务方法粒度成立，HTTP 边界被配置文件挡住）",
    },
    root_cause_file="service/UploadService.java + resources/application.yml + exception/GlobalExceptionHandler.java")

add(id="FILE-DUPLICATE-HELPERS-ACROSS-SERVICES",
    severity="LOW", kind="defect", layer="file",
    cwe="CWE-561 (Dead Code)",
    title="分页归一化等 4 组方法在 3 个 service 文件里逐字复制",
    detail=(
        "字节码 body_hash 完全相同的方法体：\n"
        "  · normalizePage ×3（OrderService / PackageService / ProductService）\n"
        "  · normalizePageSize ×3（同上）\n"
        "  · OrderService#normalizePackageItems ≈ PackageService#normalizeItems（改名复制）\n"
        "  · OrderService#findProduct = PackageService#findProduct（同名复制）\n"
        "服务层假设「四个 service 的分页上限行为不一致」在当前代码上被证伪：\n"
        "normalizePageSize 的三份字节码逐字相同，上限都是 100。"
        "所以现在没有行为差异，这是维护性风险而不是现网缺陷 —— 上限要改 3 处，漏一处就产生分页契约漂移。"
    ),
    evidence={
        "duplicate_groups": [
            {"body_hash": g["body_hash"], "copies": g["copies"],
             "distinct_files": g["distinct_files"], "kind": g["kind"], "members": g["members"]}
            for g in decl["duplicate_method_groups"]
        ],
        "service_layer_hypothesis": "上限行为不一致 -> FALSIFIED（三份字节码逐字相同）",
    },
    root_cause_file="service/{OrderService,PackageService,ProductService}.java")

# ---------------------------------------------------------------- O3 资源边界

add(id="FILE-XFF-CLIENT-IP-SPOOF",
    severity="HIGH", kind="defect", layer="file",
    cwe="CWE-290 (Authentication Bypass by Spoofing) / CWE-350 (Reliance on Reverse DNS)",
    title="审计日志里的 clientIp 完全由客户端伪造：X-Forwarded-For 首段直接采信",
    detail=(
        "TraceIdFilter.clientIp 的实现是「有 X-Forwarded-For 就取第一段，没有才用 remoteAddr」，"
        "没有任何可信代理判定（不看 RemoteAddr 是否落在已知代理网段、不看是否有多跳链）。"
        "真实容器实测：请求带 X-Forwarded-For: 9.9.9.9 时，容器日志里写的是 clientIp=9.9.9.9，"
        "而对端真实地址是 127.0.0.1。\n"
        "影响：clientIp 是这套日志里唯一的来源标识，两条日志（start / end）都写它。"
        "任何人都能把审计日志里的来源 IP 改成任意值 —— 日志不再是「谁干的」的证据，"
        "排查、限流、风控都失去了输入。这条在函数层、服务层都没被覆盖："
        "它既不是单个表达式的逻辑错，也不是 HTTP 契约问题，是这一个文件里「取哪个值」的编排决策。"
    ),
    evidence={
        "in_jvm": {"remote_addr": xff.get("remote_addr"),
                   "xff_header": xff.get("xff_header"),
                   "logged_client_ip": xff.get("logged_client_ip")},
        "real_container": {"spoofed_ip_in_logs": http.get("HTTP-XFF-SPOOF", {}).get("spoofed_ip_in_logs"),
                           "log_segment": http.get("HTTP-XFF-SPOOF", {}).get("log_segment", "")[-300:]},
        "implementation": "TraceIdFilter.clientIp: forwardedFor.split(\",\")[0].trim()",
    },
    root_cause_file="config/TraceIdFilter.java")

add(id="FILE-TRACEID-CLIENT-CONTROLLED",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-117 (Improper Output Neutralization for Logs)",
    title="X-Trace-Id 完全由客户端指定，无格式与长度白名单，原样进日志与响应头",
    detail=(
        "TraceIdFilter 对客户端传来的 X-Trace-Id 只做了一次 isBlank 判断，随后原样：\n"
        "  · 写入 MDC 与 ThreadLocal；\n"
        "  · 拼进两条日志的 traceId= 位置；\n"
        "  · 原样写回响应头 X-Trace-Id。\n"
        "实测边界行为：\n"
        "  · 含 CRLF 的 traceId（abc\\r\\nFORGED-LOG-LINE-9X）被 Tomcat 连接器拒绝，返回 400 —— "
        "这个最经典的日志伪造向量在边界上已经被挡住了（假设被证伪，是好消息）；\n"
        "  · 但 U+2028（Unicode 行分隔符）不被拦：请求 200，伪造内容原样出现在容器日志里 —— "
        "把日志行从中间切开。按行分隔解析日志的采集器（部分 filebeat/日志查看器）会把它当成新行，"
        "日志结构仍然可被污染；\n"
        "  · 没有长度上限，traceId 可以是任意长度 → 日志放大。\n"
        "与上一条同源：同一个文件里「客户端可控的值」被当成可信数据直接使用。"
    ),
    evidence={
        "crlf": {"status_line": http.get("HTTP-TRACEID-CRLF", {}).get("status_line"),
                 "forged_line_in_logs": http.get("HTTP-TRACEID-CRLF", {}).get("forged_line_in_logs")},
        "u2028": {"status_line": http.get("HTTP-TRACEID-U2028", {}).get("status_line"),
                  "u2028_in_logs": http.get("HTTP-TRACEID-U2028", {}).get("u2028_in_logs")},
        "echo": http.get("HTTP-TRACEID-ECHO", {}),
        "no_length_limit": True,
        "no_format_whitelist": True,
    },
    root_cause_file="config/TraceIdFilter.java")

add(id="FILE-CORS-INTRANET-WIDE",
    severity="HIGH", kind="defect", layer="file",
    cwe="CWE-942 (Permissive Cross-domain Policy)",
    title="CORS 白名单等于「整个内网 + 所有本地端口」，且全是明文 http",
    detail=(
        "WebConfig 的 allowedOriginPatterns 来自配置，默认为：\n"
        "  http://localhost:[*], http://127.0.0.1:[*], http://192.168.*.*:[*], "
        "http://10.*.*.*:[*], http://172.*.*.*:[*]\n"
        "其中 [*] 是任意端口。实测真实容器：请求 Origin: http://192.168.66.66:1234，"
        "响应头原样回显 Access-Control-Allow-Origin: http://192.168.66.66:1234 —— "
        "内网任意主机的任意端口都是允许源。http://localhost:65535 同样被回显。\n"
        "为什么是 HIGH：这个系统的鉴权是零（服务层 NO_AUTHZ）。allowCredentials(false) 在这里"
        "不构成任何补偿控制 —— 本来就不需要凭证，任意网页都能读全部业务数据。"
        "允许源里的 localhost:[*] 尤其危险：任何能在用户本机起一个 http://localhost:端口 的进程"
        "（恶意 npm postinstall、被劫持的开发服务器、用户自己装的软件）都能让浏览器把整套业务数据读走；"
        "内网三条规则则把「内网里任意一台被攻陷的设备」也变成攻击面。\n"
        "正向结论：白名单不是通配全放 —— evil.example.com、https://evil.example.com、"
        "localhost.evil.com、null、[::1] 全部被拒，正则是精确匹配。"
    ),
    evidence={
        "declared_patterns": cors.get("allowed_origin_patterns"),
        "real_container_acao": cors_real,
        "allow_credentials": cors_cred.get("allow_credentials"),
        "allowed_methods": cors_cred.get("allowed_methods"),
        "negative_probes_rejected": [p["origin"] for p in cors.get("probes", []) if not p["allowed"]],
        "amplified_by": "NO_AUTHZ（服务层）：无鉴权时 allowCredentials(false) 不构成保护",
    },
    root_cause_file="config/WebConfig.java",
    related=["NO_AUTHZ（服务层，CRITICAL）—— CORS 是它的放大器"])

add(id="FILE-STATIC-ROOT-NO-TRAILING-SLASH",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-706 (Use of Incorrectly-Resolved Name or Reference)",
    title="静态资源根目录的规范化依赖「目录在启动前就存在」这个环境偶然",
    detail=(
        "WebConfig.addResourceHandlers 用 Path.of(uploadDir).toAbsolutePath().normalize().toUri().toString() "
        "生成静态映射根。Path.toUri() 只有在目录**已存在**时才补尾部斜杠 —— 目录不存在时它给的是 "
        "file:///app/uploads（无斜杠）。Spring 拼相对路径时会直接字符串拼接，于是 "
        "/uploads/seed/x.jpg 变成 /app/uploadseed/x.jpg，全部 404。\n"
        "实测：存在的目录 toUri() 以 / 结尾、相对路径拼接正确；不存在的目录不以 / 结尾、"
        "相对路径拼到上一级目录去了（relative_without_slash_ok=false）。\n"
        "现状：Dockerfile 里有 mkdir -p /app/uploads，目录在镜像层就存在，所以容器部署现在是好的。"
        "但这是环境替代码兜了底 —— 换卷、换基础镜像、目录被清理、或本地不用容器启动，"
        "上传能成功（UploadService 自己 createDirectories），静态映射却指到别处，"
        "表现为「传得上去、看不到」，且没有任何报错。\n"
        "同时证伪了 manifest 的假设：WebConfig 的静态根与 UploadService 的上传根是同一个配置值，同源。"
    ),
    evidence={
        "existing_dir_to_uri": loc.get("existing_dir_to_uri"),
        "missing_dir_to_uri": loc.get("missing_dir_to_uri"),
        "relative_with_slash": loc.get("relative_with_slash"),
        "relative_without_slash": loc.get("relative_without_slash"),
        "same_root": same_root.get("same_root"),
        "webconfig_root": same_root.get("webconfig_root"),
        "uploadservice_root": same_root.get("uploadservice_root"),
        "masked_by": "Dockerfile: mkdir -p /app/uploads",
    },
    root_cause_file="config/WebConfig.java")

add(id="FILE-ERROR-BODY-SHAPE-INCONSISTENT",
    severity="LOW", kind="defect", layer="file",
    cwe="CWE-436 (Interpretation Conflict)",
    title="同一个异常处理文件里三种响应形状：信封 / 空体 / 未知",
    detail=(
        "GlobalExceptionHandler 一个文件里，错误响应有三种形状：\n"
        "  · handleBusiness / handleValidation / handleBadRequest / handleDataIntegrity / "
        "handleUnknown → 标准信封 {success,code,message,details,...}；\n"
        "  · handleClientAbort → 499 + **空 body**（实测 body_is_null=true，没有任何信封字段）；\n"
        "  · handleMaxUploadSize → 信封，但 details=null 且文案写死。\n"
        "前端按信封统一解析，遇到 499 会拿到 null / 解析异常。"
        "实测 499 的 body 确实是空的。\n"
        "另有一条正向结论：handleUnknown 只回「系统异常，请稍后重试」，不泄露任何内部细节。"
    ),
    evidence={
        "client_abort": abort,
        "unknown_handler": unknown,
        "max_upload_shape": "信封 + details=null + 硬编码文案",
    },
    root_cause_file="exception/GlobalExceptionHandler.java")

add(id="FILE-NULL-BODY-WRAPPED-AS-SUCCESS",
    severity="LOW", kind="defect", layer="file",
    cwe="CWE-754 (Improper Check for Unusual or Exceptional Conditions)",
    title="null 响应体也被包成 success=true 的信封（data 字段缺失）",
    detail=(
        "ApiResponseAdvice.beforeBodyWrite 对 body==null 也照样包一层，产出 "
        "{success:true, code:OK, message:成功, traceId, timestamp}，data 因为 NON_NULL 被省略。"
        "前端拿到 success=true 但没有 data 字段，只能靠「data 存不存在」判断有无结果，"
        "与其它接口「data 一定有值」的假设冲突。\n"
        "实际链路无害：499 那条走 ResponseEntity<Void>，协商不出 JSON content type，"
        "不会走到包装分支。所以这是 INFO 级的契约不一致，不是现网故障。\n"
        "顺带两条正向结论：supports() 对 ResponseEntity<ApiResponse<...>> 返回 false（声明类型是 "
        "ResponseEntity），但 beforeBodyWrite 的 instanceof ApiResponse 守卫仍然挡住了二次包装；"
        "已经包好的信封不会被改写（already_envelope_unchanged=true）。"
    ),
    evidence={
        "null_body_json": advice.get("null_body_json"),
        "supports": advice.get("supports"),
        "already_envelope_unchanged": advice.get("already_envelope_unchanged"),
        "mismatch": check_of("ADVICE-ENVELOPE", "null_body_result_is_null"),
    },
    root_cause_file="config/ApiResponseAdvice.java")

# ---------------------------------------------------------------- 信息泄露（归属收敛）

add(id="FILE-DB-CONSTRAINT-LEAK",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-209 (Generation of Error Message Containing Sensitive Information)",
    title="DB 约束冲突的原始错误文本被原样回给客户端",
    detail=(
        "handleDataIntegrity 把 ex.getMostSpecificCause().getMessage() 原样放进响应 details。"
        "实测：返回体里直接出现 PostgreSQL 的约束名 uk_orders_order_no、"
        "「could not execute statement [ERROR: duplicate key value violates unique constraint ...]」。\n"
        "归属说明：服务层把这一现象记成了 24 条 INTERNAL_LEAK（每个接口一条）。"
        "按 owner = 能给出可复现证据的最低层，根因就在这一个文件的一行代码上，"
        "所以开单收敛到这里；服务层的 24 条是同一根因在 24 个端点上的投影，"
        "不是 24 个独立缺陷。\n"
        "暴露面：约束名 → 表结构 → 唯一键语义 → 可枚举的订单号空间，"
        "对攻击者来说这就是一张可用的 schema 地图。"
    ),
    evidence={
        "http_status": integrity.get("http_status"),
        "body": integrity.get("body"),
        "body_contains_constraint_name": integrity.get("body_contains_constraint_name"),
        "body_contains_db_vendor_detail": integrity.get("body_contains_db_vendor_detail"),
        "implementation": "GlobalExceptionHandler.handleDataIntegrity -> ex.getMostSpecificCause().getMessage()",
    },
    root_cause_file="exception/GlobalExceptionHandler.java",
    related=["INTERNAL_LEAK（服务层，24 条）-> 收敛为本条根因"],
    supersedes="INTERNAL_LEAK")

add(id="FILE-BADREQUEST-ECHO",
    severity="MEDIUM", kind="defect", layer="file",
    cwe="CWE-209 (Generation of Error Message Containing Sensitive Information)",
    title="400 错误把原始请求体片段原样回显",
    detail=(
        "handleBadRequest 用 Map.of(\"message\", ex.getMessage()) 把 Jackson 的解析错误信息原样送出，"
        "而 Jackson 的解析错误里**带着请求体的原始片段**。\n"
        "实测：请求体 {\"password\":\"hunter2\"} 格式错误 → 400 响应体里原样出现 "
        "\"password\":\"hunter2\"。\n"
        "影响：客户端往任何接口发错一个 JSON，body 就被回显回来。响应体是最容易被中间层"
        "（网关、APM、前端埋点、错误上报 SDK）记录的地方 —— 密码、token、身份证号"
        "只要在出错的那次请求里，就会从「一个错误响应」变成「一条被长期留存的日志」。"
        "同一个 handleBadRequest 还把解析器内部细节（字符码、行列号）一起送出去了。"
    ),
    evidence={
        "http_status": badreq.get("http_status"),
        "body": badreq.get("body"),
        "body_contains_raw_fragment": badreq.get("body_contains_raw_fragment"),
        "implementation": "GlobalExceptionHandler.handleBadRequest -> Map.of(\"message\", ex.getMessage())",
    },
    root_cause_file="exception/GlobalExceptionHandler.java",
    related=["INTERNAL_LEAK（服务层）-> 收敛为本条根因"],
    supersedes="INTERNAL_LEAK")

# ---------------------------------------------------------------- 正向结论（差分也能给「没问题」）

def positive(fid, title, detail, evidence, severity="INFO"):
    add(id=fid, severity=severity, kind="positive", layer="file", title=title,
        detail=detail, evidence=evidence, cwe="", root_cause_file="", related=[], note="")


positive("FILE-TXN-BOUNDARY-COMPLETE",
         "全仓写方法的事务边界一个不缺",
         "静态侧扫了 63 个文件 / 466 个方法：所有「写实体字段或调仓库写方法」的服务方法都带 "
         "@Transactional，缺失数为 0。这是静态差分给出的通过结论 —— 不是没查，是查过确实没有。",
         {"files": decl["meta"]["files"], "methods": decl["meta"]["methods"],
          "write_methods_without_transaction": sum(
              len(f["write_methods_without_transaction"]) for f in decl["files"]),
          "services_with_writes": {f["class"].split(".")[-1]: f["write_methods"]
                                   for f in decl["files"] if f["write_methods"]}})

positive("FILE-ROLLBACK-PROVEN",
         "事务边界实测有效：中途失败不留残局",
         "服务文件里存在「先删后校验」的编排缺陷，但真实 HTTP 链路下 404 之后 DB 的 "
         "package_items（10 条明细，逐条比对）与 packages.name 都完全没变。"
         "声明（有 @Transactional）与实测（真回滚）一致 —— 这条是正向结论，"
         "它同时也是上一条缺陷被降级为 MEDIUM 的依据。",
         {"status": rollback.get("status"),
          "items_unchanged": rollback.get("items_unchanged"),
          "name_unchanged": rollback.get("name_unchanged"),
          "items_before": rollback.get("items_before"),
          "items_after": rollback.get("items_after")})

positive("FILE-NO-DEAD-PRIVATE-METHOD",
         "没有真正无人调用的私有方法",
         "私有方法可达性用「同类内部调用 + 方法引用（常量池里的 ::）」两条证据判定。"
         "全仓只有 2 个方法报不可达，都是枚举编译器生成的 $values（合成方法，非死代码）。",
         {"unreachable": {f["class"].split(".")[-1]: f["unreachable_private_methods"]
                          for f in decl["files"] if f["unreachable_private_methods"]},
          "note": "$values 是 javac 为枚举 values() 生成的合成方法"})

positive("FILE-CORS-NEGATIVE-PRECISION",
         "CORS 白名单是精确匹配，不是通配全放",
         "负向探针全部被拒：evil.example.com、https://evil.example.com、localhost.evil.com、"
         "null、http://[::1]:5173 都没有拿到 Access-Control-Allow-Origin。"
         "这条不修 CORS 的话没人会知道 —— 白名单本身没写错，错在范围太大。",
         {"rejected": [p["origin"] for p in cors.get("probes", []) if not p["allowed"]],
          "accepted_sample": [p["origin"] for p in cors.get("probes", []) if p["allowed"]][:6]})

positive("FILE-STATIC-SAME-ROOT",
         "静态映射根与上传根同源（manifest 假设被证伪）",
         "WebConfig 的静态资源根与 UploadService 的上传根读的是同一个配置项 app.file-upload-dir，"
         "实测都是 /app/uploads。manifest 里「未做同源校验」的怀疑不成立 —— 问题不在根不同源，"
         "而在 toUri() 的尾部斜杠规范化。",
         {"webconfig_root": same_root.get("webconfig_root"),
          "uploadservice_root": same_root.get("uploadservice_root"),
          "same_root": same_root.get("same_root")})

positive("FILE-STATIC-ORACLE-BLIND-SPOT",
         "静态 oracle 的一处已知盲区：编译期常量内联",
         "字段使用矩阵把 TraceContext.TRACE_ID_HEADER / MDC_TRACE_ID 标成 unused，"
         "但它们确实在被用。原因是 javac 把 static final String 常量在编译期内联进了调用点，"
         "字节码里根本没有 getstatic 指令 —— 单看一个类的字节码，看不出跨文件的常量引用。\n"
         "记录在案：这一类结论（字段是否真的没人用）只能靠动态侧补，"
         "不能拿静态矩阵当定论。这也是分层审计的意义 —— 静态给方向，动态给定论。",
         {"false_unused_fields": [k for k, v in ctx_static.get("fields", {}).items() if v.get("unused")],
          "why": "static final String 编译期常量内联，字节码无 getstatic"})

# ---------------------------------------------------------------- 输出

SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
findings.sort(key=lambda f: (SEV.get(f["severity"], 9), f["id"]))
(D / "file_findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2))

defects = [f for f in findings if f["kind"] == "defect"]
positives = [f for f in findings if f["kind"] == "positive"]

L = ["# 文件层差分报告（jiuyan）", "",
     f"文件 {decl['meta']['files']} 个 / 方法 {decl['meta']['methods']} 个 | "
     f"动态用例 {obs['meta']['scenarios']} + 探针 {obs['meta']['http_probes']} | "
     f"缺陷 {len(defects)} 条 / 通过结论 {len(positives)} 条", "",
     "| 级别 | 缺陷 | 根因文件 |", "|---|---|---|"]
for f in defects:
    L.append(f"| {f['severity']} | {f['title']} | `{f['root_cause_file']}` |")
L += ["", "## 通过结论（差分也能给「查过没问题」）", "",
      "| 级别 | 结论 |", "|---|---|"]
for f in positives:
    L.append(f"| {f['severity']} | {f['title']} |")
L.append("")
for f in findings:
    L += [f"## [{f['severity']}] {f['title']}", "",
          f"- id: `{f['id']}`", f"- kind: {f['kind']}",
          f"- 根因文件: `{f['root_cause_file']}`"]
    if f.get("cwe"):
        L.append(f"- CWE: {f['cwe']}")
    if f.get("related"):
        L.append(f"- 相关: {', '.join(f['related'])}")
    L += ["", f["detail"], "", "```json",
          json.dumps(f["evidence"], ensure_ascii=False, indent=2), "```", ""]
(D / "file_findings.md").write_text("\n".join(L))

print(f"文件层差分 -> {D / 'file_findings.json'}")
print(f"  缺陷 {len(defects)} 条 / 通过结论 {len(positives)} 条")
for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
    n = sum(1 for f in findings if f["severity"] == s)
    if n:
        print(f"  {s:8} {n}")
print()
for f in findings:
    print(f"[{f['severity']:8}] {f['kind']:9} {f['id']}")
