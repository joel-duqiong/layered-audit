# 文件层差分报告（jiuyan）

文件 63 个 / 方法 466 个 | 动态用例 21 + 探针 12 | 缺陷 12 条 / 通过结论 6 条

| 级别 | 缺陷 | 根因文件 |
|---|---|---|
| HIGH | CORS 白名单等于「整个内网 + 所有本地端口」，且全是明文 http | `config/WebConfig.java` |
| HIGH | 审计日志里的 clientIp 完全由客户端伪造：X-Forwarded-For 首段直接采信 | `config/TraceIdFilter.java` |
| MEDIUM | 400 错误把原始请求体片段原样回显 | `exception/GlobalExceptionHandler.java` |
| MEDIUM | DB 约束冲突的原始错误文本被原样回给客户端 | `exception/GlobalExceptionHandler.java` |
| MEDIUM | PackageService.applyItems 先删后校验：明细被清空后才去查商品 | `service/PackageService.java` |
| MEDIUM | 静态资源根目录的规范化依赖「目录在启动前就存在」这个环境偶然 | `config/WebConfig.java` |
| MEDIUM | X-Trace-Id 完全由客户端指定，无格式与长度白名单，原样进日志与响应头 | `config/TraceIdFilter.java` |
| MEDIUM | 上传体积上限 5MB 这个数字散落三处，改一处另两处就开始说谎 | `service/UploadService.java + resources/application.yml + exception/GlobalExceptionHandler.java` |
| MEDIUM | 同一类「参数非法」返回两种状态码：DTO 校验给 400，服务自校验给 422 | `dto/PackageItemRequest.java + service/PackageService.java` |
| LOW | 分页归一化等 4 组方法在 3 个 service 文件里逐字复制 | `service/{OrderService,PackageService,ProductService}.java` |
| LOW | 同一个异常处理文件里三种响应形状：信封 / 空体 / 未知 | `exception/GlobalExceptionHandler.java` |
| LOW | null 响应体也被包成 success=true 的信封（data 字段缺失） | `config/ApiResponseAdvice.java` |

## 通过结论（差分也能给「查过没问题」）

| 级别 | 结论 |
|---|---|
| INFO | CORS 白名单是精确匹配，不是通配全放 |
| INFO | 没有真正无人调用的私有方法 |
| INFO | 事务边界实测有效：中途失败不留残局 |
| INFO | 静态 oracle 的一处已知盲区：编译期常量内联 |
| INFO | 静态映射根与上传根同源（manifest 假设被证伪） |
| INFO | 全仓写方法的事务边界一个不缺 |

## [HIGH] CORS 白名单等于「整个内网 + 所有本地端口」，且全是明文 http

- id: `FILE-CORS-INTRANET-WIDE`
- kind: defect
- 根因文件: `config/WebConfig.java`
- CWE: CWE-942 (Permissive Cross-domain Policy)
- 相关: NO_AUTHZ（服务层，CRITICAL）—— CORS 是它的放大器

WebConfig 的 allowedOriginPatterns 来自配置，默认为：
  http://localhost:[*], http://127.0.0.1:[*], http://192.168.*.*:[*], http://10.*.*.*:[*], http://172.*.*.*:[*]
其中 [*] 是任意端口。实测真实容器：请求 Origin: http://192.168.66.66:1234，响应头原样回显 Access-Control-Allow-Origin: http://192.168.66.66:1234 —— 内网任意主机的任意端口都是允许源。http://localhost:65535 同样被回显。
为什么是 HIGH：这个系统的鉴权是零（服务层 NO_AUTHZ）。allowCredentials(false) 在这里不构成任何补偿控制 —— 本来就不需要凭证，任意网页都能读全部业务数据。允许源里的 localhost:[*] 尤其危险：任何能在用户本机起一个 http://localhost:端口 的进程（恶意 npm postinstall、被劫持的开发服务器、用户自己装的软件）都能让浏览器把整套业务数据读走；内网三条规则则把「内网里任意一台被攻陷的设备」也变成攻击面。
正向结论：白名单不是通配全放 —— evil.example.com、https://evil.example.com、localhost.evil.com、null、[::1] 全部被拒，正则是精确匹配。

```json
{
  "declared_patterns": [
    "http://localhost:[*]",
    "http://127.0.0.1:[*]",
    "http://192.168.*.*:[*]",
    "http://10.*.*.*:[*]",
    "http://172.*.*.*:[*]"
  ],
  "real_container_acao": {
    "intranet_acao": "http://192.168.66.66:1234",
    "localhost_any_port_acao": "http://localhost:65535",
    "evil_acao": null
  },
  "allow_credentials": false,
  "allowed_methods": [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS"
  ],
  "negative_probes_rejected": [
    "http://evil.example.com",
    "https://evil.example.com",
    "http://localhost.evil.com",
    "null",
    "http://[::1]:5173"
  ],
  "amplified_by": "NO_AUTHZ（服务层）：无鉴权时 allowCredentials(false) 不构成保护"
}
```

## [HIGH] 审计日志里的 clientIp 完全由客户端伪造：X-Forwarded-For 首段直接采信

- id: `FILE-XFF-CLIENT-IP-SPOOF`
- kind: defect
- 根因文件: `config/TraceIdFilter.java`
- CWE: CWE-290 (Authentication Bypass by Spoofing) / CWE-350 (Reliance on Reverse DNS)

TraceIdFilter.clientIp 的实现是「有 X-Forwarded-For 就取第一段，没有才用 remoteAddr」，没有任何可信代理判定（不看 RemoteAddr 是否落在已知代理网段、不看是否有多跳链）。真实容器实测：请求带 X-Forwarded-For: 9.9.9.9 时，容器日志里写的是 clientIp=9.9.9.9，而对端真实地址是 127.0.0.1。
影响：clientIp 是这套日志里唯一的来源标识，两条日志（start / end）都写它。任何人都能把审计日志里的来源 IP 改成任意值 —— 日志不再是「谁干的」的证据，排查、限流、风控都失去了输入。这条在函数层、服务层都没被覆盖：它既不是单个表达式的逻辑错，也不是 HTTP 契约问题，是这一个文件里「取哪个值」的编排决策。

```json
{
  "in_jvm": {
    "remote_addr": "127.0.0.1",
    "xff_header": "9.9.9.9, 10.0.0.1",
    "logged_client_ip": "9.9.9.9"
  },
  "real_container": {
    "spoofed_ip_in_logs": true,
    "log_segment": "2026-11-14T11:06:49.659Z  INFO 1 --- [banquet-backend] [io-8080-exec-20] c.jiuyan.banquet.config.TraceIdFilter    : request end traceId=b50cb925-6b7f-448e-8ee5-a96dd475e574 method=GET path=/api/health status=200 durationMs=0 clientIp=9.9.9.9 userAgent=Python-urllib/3.14 errorCode=null\n"
  },
  "implementation": "TraceIdFilter.clientIp: forwardedFor.split(\",\")[0].trim()"
}
```

## [MEDIUM] 400 错误把原始请求体片段原样回显

- id: `FILE-BADREQUEST-ECHO`
- kind: defect
- 根因文件: `exception/GlobalExceptionHandler.java`
- CWE: CWE-209 (Generation of Error Message Containing Sensitive Information)
- 相关: INTERNAL_LEAK（服务层）-> 收敛为本条根因

handleBadRequest 用 Map.of("message", ex.getMessage()) 把 Jackson 的解析错误信息原样送出，而 Jackson 的解析错误里**带着请求体的原始片段**。
实测：请求体 {"password":"hunter2"} 格式错误 → 400 响应体里原样出现 "password":"hunter2"。
影响：客户端往任何接口发错一个 JSON，body 就被回显回来。响应体是最容易被中间层（网关、APM、前端埋点、错误上报 SDK）记录的地方 —— 密码、token、身份证号只要在出错的那次请求里，就会从「一个错误响应」变成「一条被长期留存的日志」。同一个 handleBadRequest 还把解析器内部细节（字符码、行列号）一起送出去了。

```json
{
  "http_status": 400,
  "body": "{\"success\":false,\"code\":\"VALIDATION_ERROR\",\"message\":\"参数错误\",\"details\":{\"message\":\"JSON parse error: Unexpected character ('s' (code 115)) in JSON string: {\\\"password\\\":\\\"hunter2\\\"} at [Source: (String)\\\"; line: 1, column: 20]\"},\"timestamp\":1794654403.753920000}",
  "body_contains_raw_fragment": true,
  "implementation": "GlobalExceptionHandler.handleBadRequest -> Map.of(\"message\", ex.getMessage())"
}
```

## [MEDIUM] DB 约束冲突的原始错误文本被原样回给客户端

- id: `FILE-DB-CONSTRAINT-LEAK`
- kind: defect
- 根因文件: `exception/GlobalExceptionHandler.java`
- CWE: CWE-209 (Generation of Error Message Containing Sensitive Information)
- 相关: INTERNAL_LEAK（服务层，24 条）-> 收敛为本条根因

handleDataIntegrity 把 ex.getMostSpecificCause().getMessage() 原样放进响应 details。实测：返回体里直接出现 PostgreSQL 的约束名 uk_orders_order_no、「could not execute statement [ERROR: duplicate key value violates unique constraint ...]」。
归属说明：服务层把这一现象记成了 24 条 INTERNAL_LEAK（每个接口一条）。按 owner = 能给出可复现证据的最低层，根因就在这一个文件的一行代码上，所以开单收敛到这里；服务层的 24 条是同一根因在 24 个端点上的投影，不是 24 个独立缺陷。
暴露面：约束名 → 表结构 → 唯一键语义 → 可枚举的订单号空间，对攻击者来说这就是一张可用的 schema 地图。

```json
{
  "http_status": 409,
  "body": "{\"success\":false,\"code\":\"VALIDATION_ERROR\",\"message\":\"数据约束冲突\",\"details\":{\"message\":\"could not execute statement [ERROR: duplicate key value violates unique constraint \\\"uk_orders_order_no\\\"]\"},\"timestamp\":1794654403.746444000}",
  "body_contains_constraint_name": true,
  "body_contains_db_vendor_detail": true,
  "implementation": "GlobalExceptionHandler.handleDataIntegrity -> ex.getMostSpecificCause().getMessage()"
}
```

## [MEDIUM] PackageService.applyItems 先删后校验：明细被清空后才去查商品

- id: `FILE-PKG-MUTATE-BEFORE-VALIDATE`
- kind: defect
- 根因文件: `service/PackageService.java`
- CWE: CWE-662 (Improper Synchronization)
- 相关: FN-APPLY-NULL-CONTRACT（函数层同构问题：入参未校验）

applyItems 的编排顺序是「先把没被提交的明细从实体上摘掉，再去 DB 查每个商品是否存在」。商品查不到时抛 404，但实体已经被改过一轮 —— 内存里的套餐此刻是一条空明细的套餐。实测：2 条明细的套餐，用一个不存在的商品提交更新，异常抛出后实体的 items 变成 0 条。
字节码层面是硬证据：applyItems 的指令序列里 java.util.List.removeIf 出现在 ProductRepository.findByIdAndIsDeletedFalse 之前。
为什么不是 HIGH：update 方法带 @Transactional，实测真实 HTTP 链路里 404 之后 DB 的 package_items 与 packages.name 都没变（事务完整回滚）。所以这是「纵深防御缺口」而不是现网数据损坏 —— 但编排顺序本身就是错的：只要有人给 update 去掉 @Transactional、或未来改成内部自调用绕过代理，这个顺序立刻变成真实的明细丢失。校验应该在变更之前，不是之后。

```json
{
  "bytecode_order_applyItems": [
    "java.util.List.isEmpty",
    "com.jiuyan.banquet.entity.BanquetPackage.getItems",
    "java.util.List.stream",
    "java.util.function.Function.identity",
    "java.util.stream.Collectors.toMap",
    "java.util.stream.Stream.collect",
    "com.jiuyan.banquet.entity.BanquetPackage.getItems",
    "java.util.List.removeIf",
    "java.util.Map.entrySet",
    "java.util.Set.iterator",
    "java.util.Iterator.hasNext",
    "java.util.Iterator.next",
    "java.util.Map$Entry.getKey",
    "java.util.Map.get",
    "com.jiuyan.banquet.entity.PackageItem.setBanquetPackage",
    "java.util.Map$Entry.getKey",
    "com.jiuyan.banquet.entity.PackageItem.setProduct",
    "com.jiuyan.banquet.entity.BanquetPackage.getItems",
    "java.util.List.add",
    "java.util.Map$Entry.getValue",
    "com.jiuyan.banquet.entity.PackageItem.setQuantity"
  ],
  "static_proof": "java.util.List.removeIf 出现在 ProductRepository.findByIdAndIsDeletedFalse 之前",
  "dynamic_before_items": [
    {
      "itemId": 1100,
      "productId": 11,
      "quantity": 2
    },
    {
      "itemId": 1300,
      "productId": 13,
      "quantity": 2
    }
  ],
  "dynamic_after_items": [],
  "dynamic_error": "PRODUCT_NOT_FOUND / 404",
  "boundary_protected": "HTTP 层实测回滚：items_unchanged=True, name_unchanged=True",
  "transaction_on": true
}
```

## [MEDIUM] 静态资源根目录的规范化依赖「目录在启动前就存在」这个环境偶然

- id: `FILE-STATIC-ROOT-NO-TRAILING-SLASH`
- kind: defect
- 根因文件: `config/WebConfig.java`
- CWE: CWE-706 (Use of Incorrectly-Resolved Name or Reference)

WebConfig.addResourceHandlers 用 Path.of(uploadDir).toAbsolutePath().normalize().toUri().toString() 生成静态映射根。Path.toUri() 只有在目录**已存在**时才补尾部斜杠 —— 目录不存在时它给的是 file:///app/uploads（无斜杠）。Spring 拼相对路径时会直接字符串拼接，于是 /uploads/seed/x.jpg 变成 /app/uploadseed/x.jpg，全部 404。
实测：存在的目录 toUri() 以 / 结尾、相对路径拼接正确；不存在的目录不以 / 结尾、相对路径拼到上一级目录去了（relative_without_slash_ok=false）。
现状：Dockerfile 里有 mkdir -p /app/uploads，目录在镜像层就存在，所以容器部署现在是好的。但这是环境替代码兜了底 —— 换卷、换基础镜像、目录被清理、或本地不用容器启动，上传能成功（UploadService 自己 createDirectories），静态映射却指到别处，表现为「传得上去、看不到」，且没有任何报错。
同时证伪了 manifest 的假设：WebConfig 的静态根与 UploadService 的上传根是同一个配置值，同源。

```json
{
  "existing_dir_to_uri": "file:///tmp/jiuyan_file/loc-test/exists/",
  "missing_dir_to_uri": "file:///tmp/jiuyan_file/loc-test/missing",
  "relative_with_slash": "file:/tmp/jiuyan_file/loc-test/exists/seed/banquet-table.png",
  "relative_without_slash": "file:/tmp/jiuyan_file/loc-test/seed/banquet-table.png",
  "same_root": true,
  "webconfig_root": "/app/uploads",
  "uploadservice_root": "/app/uploads",
  "masked_by": "Dockerfile: mkdir -p /app/uploads"
}
```

## [MEDIUM] X-Trace-Id 完全由客户端指定，无格式与长度白名单，原样进日志与响应头

- id: `FILE-TRACEID-CLIENT-CONTROLLED`
- kind: defect
- 根因文件: `config/TraceIdFilter.java`
- CWE: CWE-117 (Improper Output Neutralization for Logs)

TraceIdFilter 对客户端传来的 X-Trace-Id 只做了一次 isBlank 判断，随后原样：
  · 写入 MDC 与 ThreadLocal；
  · 拼进两条日志的 traceId= 位置；
  · 原样写回响应头 X-Trace-Id。
实测边界行为：
  · 含 CRLF 的 traceId（abc\r\nFORGED-LOG-LINE-9X）被 Tomcat 连接器拒绝，返回 400 —— 这个最经典的日志伪造向量在边界上已经被挡住了（假设被证伪，是好消息）；
  · 但 U+2028（Unicode 行分隔符）不被拦：请求 200，伪造内容原样出现在容器日志里 —— 把日志行从中间切开。按行分隔解析日志的采集器（部分 filebeat/日志查看器）会把它当成新行，日志结构仍然可被污染；
  · 没有长度上限，traceId 可以是任意长度 → 日志放大。
与上一条同源：同一个文件里「客户端可控的值」被当成可信数据直接使用。

```json
{
  "crlf": {
    "status_line": "HTTP/1.1 400 ",
    "forged_line_in_logs": false
  },
  "u2028": {
    "status_line": "HTTP/1.1 200 ",
    "u2028_in_logs": true
  },
  "echo": {
    "status": 200,
    "response_trace_id": "FILELAYER-TRACE-77",
    "verbatim": true
  },
  "no_length_limit": true,
  "no_format_whitelist": true
}
```

## [MEDIUM] 上传体积上限 5MB 这个数字散落三处，改一处另两处就开始说谎

- id: `FILE-UPLOAD-LIMIT-SPLIT-ACROSS-THREE-FILES`
- kind: defect
- 根因文件: `service/UploadService.java + resources/application.yml + exception/GlobalExceptionHandler.java`
- CWE: CWE-1078 (Inappropriate Source Code Style or Formatting)

UploadService.java 这个文件里没有任何体积判断 —— 上限完全由配置文件 application.yml 的 spring.servlet.multipart.max-file-size 承担。而同一个数字 5MB 又以两种形式被复制到代码里：
  · GlobalExceptionHandler.handleMaxUploadSize 的错误文案「图片不能超过 5MB」；
  · UploadController @Operation 的接口文档「最大 5MB」。
真实容器实测：1MB 通过、4MB 通过、6MB 被拒、20MB 被拒 —— 5MB 确实生效，函数层那条「无体积上限、20MB 被完整写盘」在 HTTP 边界被配置文件挡住了。但挡住它的是配置，不是代码：任何一次环境覆盖（SPRING_APPLICATION_JSON / 环境变量 / 配置中心 / 默认值回落）都会静默放开上传，而错误文案与接口文档仍然写着 5MB —— 限制放宽了，提示没变，用户按提示做的判断是错的。

```json
{
  "declared_in_yml": "spring.servlet.multipart.max-file-size: 5MB",
  "coded_in_handler": "GlobalExceptionHandler.handleMaxUploadSize -> \"图片不能超过 5MB\"",
  "coded_in_openapi_doc": "UploadController @Operation(description=\"... 最大 5MB。\")",
  "measured_1mb": "200 接受",
  "measured_4mb": "200 接受",
  "measured_6mb": "400 拒绝（图片不能超过 5MB）",
  "measured_20mb": "400 拒绝",
  "service_file_has_size_check": false,
  "related_fn_finding": "FN-UPLOAD-NO-SIZE-LIMIT（函数层在服务方法粒度成立，HTTP 边界被配置文件挡住）"
}
```

## [MEDIUM] 同一类「参数非法」返回两种状态码：DTO 校验给 400，服务自校验给 422

- id: `FILE-VALIDATION-STATUS-SPLIT`
- kind: defect
- 根因文件: `dto/PackageItemRequest.java + service/PackageService.java`
- CWE: CWE-436 (Interpretation Conflict)
- 相关: UNHANDLED_HTTP_EXCEPTION（服务层）

参数校验被拆在两个文件里，两边各自选了不同的状态码：
  · DTO 文件声明的约束（数量 @Min(1)、价格 @Min(0)）走 Bean Validation → MethodArgumentNotValidException → handleValidation → 400；
  · 服务文件自己做的校验（明细为空、明细重复）抛 BusinessException → 422。
实测同一个接口 PUT /api/packages/4：数量=0 → 400；明细为空 → 422。两者在语义上都是「请求参数不合法」，前端却要写两个分支才能区分，监控按状态码分类时也会把一类错误拆成两堆。
根因不是某一处写错，而是校验责任分散在 DTO 与 service 两个文件、且没有统一的错误语义约定。

```json
{
  "dto_validation_status": 400,
  "dto_validation_body": "{\"success\":false,\"code\":\"VALIDATION_ERROR\",\"message\":\"参数错误\",\"details\":{\"items[0].quantity\":\"must be greater than or equal to 1\"},\"traceId\":\"4e1946f6-edf6-45bd-8fbf-a654a6f37a09\",\"timestamp\":\"2026-11-14T19:06:50.968581594+08:00\"}",
  "service_validation_status": 422,
  "service_validation_code": "VALIDATION_ERROR",
  "split_check": {
    "check": "validation_status_split",
    "expected": true,
    "actual": true,
    "pass": true,
    "note": "同属「参数非法」：DTO 层校验返 400，服务文件自校验返 422 —— 状态码按校验责任分裂"
  }
}
```

## [LOW] 分页归一化等 4 组方法在 3 个 service 文件里逐字复制

- id: `FILE-DUPLICATE-HELPERS-ACROSS-SERVICES`
- kind: defect
- 根因文件: `service/{OrderService,PackageService,ProductService}.java`
- CWE: CWE-561 (Dead Code)

字节码 body_hash 完全相同的方法体：
  · normalizePage ×3（OrderService / PackageService / ProductService）
  · normalizePageSize ×3（同上）
  · OrderService#normalizePackageItems ≈ PackageService#normalizeItems（改名复制）
  · OrderService#findProduct = PackageService#findProduct（同名复制）
服务层假设「四个 service 的分页上限行为不一致」在当前代码上被证伪：
normalizePageSize 的三份字节码逐字相同，上限都是 100。所以现在没有行为差异，这是维护性风险而不是现网缺陷 —— 上限要改 3 处，漏一处就产生分页契约漂移。

```json
{
  "duplicate_groups": [
    {
      "body_hash": "b556d8e9c0ae",
      "copies": 3,
      "distinct_files": 3,
      "kind": "同名复制",
      "members": [
        "com.jiuyan.banquet.service.OrderService#normalizePage",
        "com.jiuyan.banquet.service.PackageService#normalizePage",
        "com.jiuyan.banquet.service.ProductService#normalizePage"
      ]
    },
    {
      "body_hash": "66e19c8804a4",
      "copies": 3,
      "distinct_files": 3,
      "kind": "同名复制",
      "members": [
        "com.jiuyan.banquet.service.OrderService#normalizePageSize",
        "com.jiuyan.banquet.service.PackageService#normalizePageSize",
        "com.jiuyan.banquet.service.ProductService#normalizePageSize"
      ]
    },
    {
      "body_hash": "ee677f99941d",
      "copies": 2,
      "distinct_files": 2,
      "kind": "改名复制",
      "members": [
        "com.jiuyan.banquet.service.OrderService#normalizePackageItems",
        "com.jiuyan.banquet.service.PackageService#normalizeItems"
      ]
    },
    {
      "body_hash": "ddaccbfe4ded",
      "copies": 2,
      "distinct_files": 2,
      "kind": "同名复制",
      "members": [
        "com.jiuyan.banquet.service.OrderService#findProduct",
        "com.jiuyan.banquet.service.PackageService#findProduct"
      ]
    }
  ],
  "service_layer_hypothesis": "上限行为不一致 -> FALSIFIED（三份字节码逐字相同）"
}
```

## [LOW] 同一个异常处理文件里三种响应形状：信封 / 空体 / 未知

- id: `FILE-ERROR-BODY-SHAPE-INCONSISTENT`
- kind: defect
- 根因文件: `exception/GlobalExceptionHandler.java`
- CWE: CWE-436 (Interpretation Conflict)

GlobalExceptionHandler 一个文件里，错误响应有三种形状：
  · handleBusiness / handleValidation / handleBadRequest / handleDataIntegrity / handleUnknown → 标准信封 {success,code,message,details,...}；
  · handleClientAbort → 499 + **空 body**（实测 body_is_null=true，没有任何信封字段）；
  · handleMaxUploadSize → 信封，但 details=null 且文案写死。
前端按信封统一解析，遇到 499 会拿到 null / 解析异常。实测 499 的 body 确实是空的。
另有一条正向结论：handleUnknown 只回「系统异常，请稍后重试」，不泄露任何内部细节。

```json
{
  "client_abort": {
    "id": "ERR-CLIENT-ABORT-ENVELOPE",
    "http_status": 499,
    "body_is_null": true,
    "has_envelope_fields": false,
    "repo_calls": []
  },
  "unknown_handler": {
    "id": "ERR-UNKNOWN-NO-LEAK",
    "http_status": 500,
    "body": "{\"success\":false,\"code\":\"INTERNAL_ERROR\",\"message\":\"系统异常，请稍后重试\",\"timestamp\":1794654403.754837000}",
    "body_contains_internal_detail": false,
    "repo_calls": []
  },
  "max_upload_shape": "信封 + details=null + 硬编码文案"
}
```

## [LOW] null 响应体也被包成 success=true 的信封（data 字段缺失）

- id: `FILE-NULL-BODY-WRAPPED-AS-SUCCESS`
- kind: defect
- 根因文件: `config/ApiResponseAdvice.java`
- CWE: CWE-754 (Improper Check for Unusual or Exceptional Conditions)

ApiResponseAdvice.beforeBodyWrite 对 body==null 也照样包一层，产出 {success:true, code:OK, message:成功, traceId, timestamp}，data 因为 NON_NULL 被省略。前端拿到 success=true 但没有 data 字段，只能靠「data 存不存在」判断有无结果，与其它接口「data 一定有值」的假设冲突。
实际链路无害：499 那条走 ResponseEntity<Void>，协商不出 JSON content type，不会走到包装分支。所以这是 INFO 级的契约不一致，不是现网故障。
顺带两条正向结论：supports() 对 ResponseEntity<ApiResponse<...>> 返回 false（声明类型是 ResponseEntity），但 beforeBodyWrite 的 instanceof ApiResponse 守卫仍然挡住了二次包装；已经包好的信封不会被改写（already_envelope_unchanged=true）。

```json
{
  "null_body_json": "{\"success\":true,\"code\":\"OK\",\"message\":\"成功\",\"traceId\":\"FILELAYER-CONTEXT-TRACE\",\"timestamp\":1794654403.763174000}",
  "supports": {
    "plain": true,
    "enveloped": false,
    "entity": true
  },
  "already_envelope_unchanged": true,
  "mismatch": {
    "check": "null_body_result_is_null",
    "expected": true,
    "actual": false,
    "pass": false,
    "note": "空 body 不应被包成 data:null 的成功信封（内容类型为空时不会发生）"
  }
}
```

## [INFO] CORS 白名单是精确匹配，不是通配全放

- id: `FILE-CORS-NEGATIVE-PRECISION`
- kind: positive
- 根因文件: ``

负向探针全部被拒：evil.example.com、https://evil.example.com、localhost.evil.com、null、http://[::1]:5173 都没有拿到 Access-Control-Allow-Origin。这条不修 CORS 的话没人会知道 —— 白名单本身没写错，错在范围太大。

```json
{
  "rejected": [
    "http://evil.example.com",
    "https://evil.example.com",
    "http://localhost.evil.com",
    "null",
    "http://[::1]:5173"
  ],
  "accepted_sample": [
    "http://localhost:5173",
    "http://localhost:9999",
    "http://127.0.0.1:5173",
    "http://192.168.1.5:8080",
    "http://192.168.1.5:9999",
    "http://10.0.0.7:3000"
  ]
}
```

## [INFO] 没有真正无人调用的私有方法

- id: `FILE-NO-DEAD-PRIVATE-METHOD`
- kind: positive
- 根因文件: ``

私有方法可达性用「同类内部调用 + 方法引用（常量池里的 ::）」两条证据判定。全仓只有 2 个方法报不可达，都是枚举编译器生成的 $values（合成方法，非死代码）。

```json
{
  "unreachable": {
    "OrderStatus": [
      "$values"
    ],
    "ErrorCode": [
      "$values"
    ]
  },
  "note": "$values 是 javac 为枚举 values() 生成的合成方法"
}
```

## [INFO] 事务边界实测有效：中途失败不留残局

- id: `FILE-ROLLBACK-PROVEN`
- kind: positive
- 根因文件: ``

服务文件里存在「先删后校验」的编排缺陷，但真实 HTTP 链路下 404 之后 DB 的 package_items（10 条明细，逐条比对）与 packages.name 都完全没变。声明（有 @Transactional）与实测（真回滚）一致 —— 这条是正向结论，它同时也是上一条缺陷被降级为 MEDIUM 的依据。

```json
{
  "status": 404,
  "items_unchanged": true,
  "name_unchanged": true,
  "items_before": "31|1|1\n32|4|1\n33|7|1\n34|10|1\n35|11|1\n36|13|1\n37|19|1\n38|21|1\n39|25|1\n40|26|1",
  "items_after": "31|1|1\n32|4|1\n33|7|1\n34|10|1\n35|11|1\n36|13|1\n37|19|1\n38|21|1\n39|25|1\n40|26|1"
}
```

## [INFO] 静态 oracle 的一处已知盲区：编译期常量内联

- id: `FILE-STATIC-ORACLE-BLIND-SPOT`
- kind: positive
- 根因文件: ``

字段使用矩阵把 TraceContext.TRACE_ID_HEADER / MDC_TRACE_ID 标成 unused，但它们确实在被用。原因是 javac 把 static final String 常量在编译期内联进了调用点，字节码里根本没有 getstatic 指令 —— 单看一个类的字节码，看不出跨文件的常量引用。
记录在案：这一类结论（字段是否真的没人用）只能靠动态侧补，不能拿静态矩阵当定论。这也是分层审计的意义 —— 静态给方向，动态给定论。

```json
{
  "false_unused_fields": [
    "TRACE_ID_HEADER",
    "MDC_TRACE_ID"
  ],
  "why": "static final String 编译期常量内联，字节码无 getstatic"
}
```

## [INFO] 静态映射根与上传根同源（manifest 假设被证伪）

- id: `FILE-STATIC-SAME-ROOT`
- kind: positive
- 根因文件: ``

WebConfig 的静态资源根与 UploadService 的上传根读的是同一个配置项 app.file-upload-dir，实测都是 /app/uploads。manifest 里「未做同源校验」的怀疑不成立 —— 问题不在根不同源，而在 toUri() 的尾部斜杠规范化。

```json
{
  "webconfig_root": "/app/uploads",
  "uploadservice_root": "/app/uploads",
  "same_root": true
}
```

## [INFO] 全仓写方法的事务边界一个不缺

- id: `FILE-TXN-BOUNDARY-COMPLETE`
- kind: positive
- 根因文件: ``

静态侧扫了 63 个文件 / 466 个方法：所有「写实体字段或调仓库写方法」的服务方法都带 @Transactional，缺失数为 0。这是静态差分给出的通过结论 —— 不是没查，是查过确实没有。

```json
{
  "files": 63,
  "methods": 466,
  "write_methods_without_transaction": 0,
  "services_with_writes": {
    "CategoryService": [
      "create",
      "delete"
    ],
    "OrderService": [
      "create",
      "update",
      "updateStatus"
    ],
    "PackageService": [
      "create",
      "update",
      "updateStatus",
      "delete"
    ],
    "PackageTypeService": [
      "create",
      "delete"
    ],
    "ProductService": [
      "create",
      "update",
      "updateStatus",
      "delete"
    ]
  }
}
```
