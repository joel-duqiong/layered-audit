# 服务层契约差分报告（jiuyan）

静态声明 25 个 operation | 动态实测 59 次 | finding 10 条

| 级别 | 端点 | 缺陷 |
|---|---|---|
| CRITICAL | `ALL` | 全系统零鉴权：无凭证即可读写全部业务数据 |
| HIGH | `POST /api/uploads/package-image` | 客户端错误被当成服务端错误：返回 [500] |
| HIGH | `POST /api/uploads/product-image` | 客户端错误被当成服务端错误：返回 [500] |
| MEDIUM | `PATCH /api/orders/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/packages/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PATCH /api/products/{id}/status` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `POST /api/packages` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `POST /api/products` | 错误响应把内部实现细节返回给客户端 |
| MEDIUM | `PUT /api/packages/{id}` | 错误响应把内部实现细节返回给客户端 |
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

## [MEDIUM] PATCH /api/orders/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `com.jiuyan.banquet.entity.Or...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/orders/2192/status`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `com.jiuyan.banquet.entity.OrderStatus` from number 12345: index value outside legal index range [0..2]"
}
```

## [MEDIUM] PATCH /api/packages/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Boolean` from Stri...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/packages/428/status`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Boolean` from String \"not-a-boolean\": only \"true\" or \"false\" recognized"
}
```

## [MEDIUM] PATCH /api/products/{id}/status — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Boolean` from Stri...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PATCH /api/products/4956576/status`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Boolean` from String \"not-a-boolean\": only \"true\" or \"false\" recognized"
}
```

## [MEDIUM] POST /api/packages — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Long` from String ...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X POST /api/packages`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Long` from String \"not-a-number\": not a valid `java.lang.Long` value"
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

## [MEDIUM] PUT /api/packages/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Long` from String ...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/packages/428`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Long` from String \"not-a-number\": not a valid `java.lang.Long` value"
}
```

## [MEDIUM] PUT /api/products/{id} — 错误响应把内部实现细节返回给客户端

details.message 直接透传框架异常原文（JSON parse error: Cannot deserialize value of type `java.lang.Long` from String ...），泄漏内部类名、框架类型与反序列化细节。

复现：`curl -X PUT /api/products/4956576`

```json
{
  "details_message": "JSON parse error: Cannot deserialize value of type `java.lang.Long` from String \"not-a-number\": not a valid `java.lang.Long` value"
}
```
