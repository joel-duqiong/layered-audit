# 服务层契约差分报告（jiuyan）

静态声明 25 个 operation | 动态实测 61 次 | finding 30 条

| 级别 | 端点 | 缺陷 |
|---|---|---|
| CRITICAL | `ALL` | 全系统零鉴权：无凭证即可读写全部业务数据 |
| HIGH | `POST /api/uploads/package-image` | 客户端错误被当成服务端错误：返回 [500] |
| HIGH | `POST /api/uploads/product-image` | 客户端错误被当成服务端错误：返回 [500] |
| MEDIUM | `POST /api/categories` | 错误 JSON 类型被隐式转换后接受（字段 name） |
| MEDIUM | `POST /api/package-types` | 错误 JSON 类型被隐式转换后接受（字段 name） |
| MEDIUM | `DELETE /api/categories/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `DELETE /api/package-types/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `DELETE /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `DELETE /api/products/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `GET /api/orders/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `GET /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/orders/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/orders/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/orders/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/packages/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/packages/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/packages/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/products/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/products/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/products/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `POST /api/products` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/orders/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/orders/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/orders/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/products/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/products/{id}` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/products/{id}` | 错误响应把内部实现细节返回给客户端 |

## [CRITICAL] ALL — 全系统零鉴权：无凭证即可读写全部业务数据

源码无任何鉴权注解 (空)、classpath 无 spring-security；动态实测带伪造 Bearer token 仍可成功读取订单 /api/orders。这里不是实现漏了鉴权，而是授权不变量根本不存在。

复现：`curl http://127.0.0.1:8081/api/orders   # 无任何凭证`

```json
{
  "probe_result": "HTTP 200 + 返回客户姓名/电话",
  "static_evidence": "security_annotations=[] spring_security_on_classpath=False"
}
```

## [HIGH] POST /api/uploads/package-image — 客户端错误被当成服务端错误：返回 [500]

OpenAPI 只声明 200；畸形请求触发 500 INTERNAL_ERROR。GlobalExceptionHandler 没有为该异常类型注册 @ExceptionHandler，落到兜底 Exception 分支 —— 客户端错误被当成服务端故障，污染告警与 SLO。

复现：`curl -X POST 见 observation.json 中 key=POST /api/uploads/package-image 的 missing_multipart_part 场景`

```json
{
  "observed_status": [
    500
  ],
  "declared_status": [
    "200"
  ]
}
```

## [HIGH] POST /api/uploads/product-image — 客户端错误被当成服务端错误：返回 [500]

OpenAPI 只声明 200；畸形请求触发 500 INTERNAL_ERROR。GlobalExceptionHandler 没有为该异常类型注册 @ExceptionHandler，落到兜底 Exception 分支 —— 客户端错误被当成服务端故障，污染告警与 SLO。

复现：`curl -X POST 见 observation.json 中 key=POST /api/uploads/product-image 的 missing_multipart_part 场景`

```json
{
  "observed_status": [
    500
  ],
  "declared_status": [
    "200"
  ]
}
```

## [MEDIUM] POST /api/categories — 错误 JSON 类型被隐式转换后接受（字段 name）

OpenAPI 声明该字段为特定类型，实际发送错误类型仍被 200 接受——Jackson 隐式把数字转成字符串等。调用方拼错类型不会报错，只会静默变成脏数据。

复现：`curl -X POST /api/categories -d {"name": 12345}`

```json
{
  "scenario": "wrong_type:name",
  "accepted_value": {
    "id": 63,
    "name": "12345",
    "sortOrder": 140
  }
}
```

## [MEDIUM] POST /api/package-types — 错误 JSON 类型被隐式转换后接受（字段 name）

OpenAPI 声明该字段为特定类型，实际发送错误类型仍被 200 接受——Jackson 隐式把数字转成字符串等。调用方拼错类型不会报错，只会静默变成脏数据。

复现：`curl -X POST /api/package-types -d {"name": 12345}`

```json
{
  "scenario": "wrong_type:name",
  "accepted_value": {
    "id": 33,
    "name": "12345",
    "sortOrder": 120
  }
}
```

## [MEDIUM] DELETE /api/categories/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X DELETE /api/categories/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] DELETE /api/package-types/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X DELETE /api/package-types/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] DELETE /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X DELETE /api/packages/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] DELETE /api/products/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X DELETE /api/products/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] GET /api/orders/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X GET /api/orders/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] GET /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X GET /api/packages/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/orders/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/orders/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/orders/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/orders/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/orders/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/orders/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/packages/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/packages/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/packages/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/packages/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/packages/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/packages/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/products/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/products/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/products/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/products/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PATCH /api/products/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/products/None/status`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] POST /api/products — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Long` from String ...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X POST /api/products`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Long` from String \"not-a-number\": not a valid `java.lang.Long` value"
}
```

## [MEDIUM] PUT /api/orders/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/orders/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/orders/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/orders/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/orders/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/orders/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/packages/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/packages/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/packages/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/products/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/products/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/products/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/products/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```

## [MEDIUM] PUT /api/products/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（Method parameter 'id': Failed to convert value of type 'java.lang.String' to req...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/products/None`

```json
{
  "details_message": "Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: \"None\""
}
```
