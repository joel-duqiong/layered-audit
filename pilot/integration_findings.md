# 集权层（集成层）缺陷清单

三方对账：前端 JS 声明 × 后端 Java 声明 × 录制流量画像 × 实测行为

- 缺陷 12 条：CRITICAL 2、HIGH 4、LOW 1、MEDIUM 5（无则写 0）
- 通过的对账项 31 条

## 缺陷

| 严重度 | ID | 端点 | 标题 |
|---|---|---|---|
| CRITICAL | `INT-ANON-WRITE-REWRITES-SNAPSHOT` | CHAIN-A | 匿名写入会连带重写订单快照：订单历史快照失去证据效力 |
| CRITICAL | `INT-UPLOAD-ANONYMOUS-READBACK` | CHAIN-B | 上传内容匿名可读回：落盘字节与上传字节逐字节一致 |
| HIGH | `INT-PII-MASKED-ONLY-IN-FRONTEND` | CHAIN-A | 手机号脱敏只做在渲染层：接口与库里都是明文，匿名可批量拉取 |
| HIGH | `LIST_TRUNCATED_AT_100` | GET /api/products, GET /api/packages, GET /api/orders | 前端列表固定只取前 100 条并丢弃 total，超出的数据永远不可见 |
| HIGH | `PACKAGE_ITEM_NOT_VALIDATED` | POST /api/orders | 可以把不属于该套餐的商品写进套餐明细，后端照单全收 |
| HIGH | `PACKAGE_QUANTITY_NOT_VALIDATED` | POST /api/orders | 明细数量可以改成 99，价格仍按套餐原价结算（加量不加价） |
| MEDIUM | `INT-PACKAGE-COUNT-INCONSISTENT` | packages.item_count | 同一个套餐，界面上有两个互不相同的「商品数」 |
| MEDIUM | `INT-PRODUCT-IMAGE-DOMAIN-FALLBACK` | products.image_url | 商品缺图时前端兜底成了套餐种子图 |
| MEDIUM | `RECORDING_COVERAGE_GAP` | PATCH /api/packages/{id}/status | 1/25 个接口既无真实流量样本、前端也不调用；17 个写接口的真实流量样本为 0 |
| MEDIUM | `SNAPSHOT_CONTRACT_UNDECLARED` | GET/POST/PUT /api/orders | 前端依赖的订单快照内部结构，在 API 契约里零声明 |
| MEDIUM | `UNKNOWN_FIELD_SILENT_DROP` | POST /api/orders | 字段名拼错会被静默吞掉，并被「套餐默认明细」悄悄顶替 |
| LOW | `DEAD_BACKEND_ENDPOINT` | GET /api/health, GET /api/orders/{id}, GET /api/packages/{id}, PATCH /api/packages/{id}/status | 后端 4 个接口前端从不调用 |


### CRITICAL · INT-ANON-WRITE-REWRITES-SNAPSHOT

**匿名写入会连带重写订单快照：订单历史快照失去证据效力**

- 端点：CHAIN-A
- 归属层：集成层（链级后果；越权本身归服务层 NO_AUTHZ）
- 说明：PUT 走 apply() → buildPackageSnapshot()，匿名会话改明细后，订单快照被整体重建，原来记下的套餐明细不再可信。
- 复现：python3 integration_chain.py --chain CHAIN-A
- 证据：`{"chain_steps": [{"n": 1, "method": "POST", "path": "/api/orders", "status": 200, "evidence": "订单主人创建订单 id=2619（请求里没有任何凭证头）"}, {"n": 2, "method": "GET", "path": "/api/orders/2619", "status": 200, "evidence": "全新连接（无 Cookie / Token）读到 4 个 PII 字段：['customerName', 'customerPhone', 'address', 'remark']"}, {"n": 3, "method": "前端", "path": "App.jsx 订单详情抽屉", "status": null, "evidence": "接口返回手机号明文 13800000999，前端渲染时才脱敏成 138****0999 —— 脱敏只发生在展示层，接口与库里都是明文"}, {"n": 4, "method": "PATCH", "path": "/api/orders/2619/status", "status": 200, "evidence": "匿名改状态 → DEPOSIT_PAID"}, {"n": 5, "method": "PUT", "path": "/api/orders/2619", "status": 200, "evidence": "匿名全量覆盖订单：改电话 + 把明细数量改成 99"}, {"n": 6, "method": "DB", "path": "SELECT ... WHERE id=2619", "status": 200, "evidence": "库里的快照已被重写：明细数量=99，电话=13900000000，状态=PENDING"}], "evidence": "库内快照明细数量=99，电话=13900000000"}`

### CRITICAL · INT-UPLOAD-ANONYMOUS-READBACK

**上传内容匿名可读回：落盘字节与上传字节逐字节一致**

- 端点：CHAIN-B
- 归属层：集成层（链级后果；内容不嗅探归函数层 FN-UPLOAD-NO-CONTENT-SNIFF）
- 说明：上传接口不校验内容（只信客户端声明），写盘路径又在 /uploads/** 公开映射下，任何人都能把任意字节放进站点并当作静态资源取回。
- 复现：python3 integration_chain.py --chain CHAIN-B
- 证据：`{"chain_steps": [{"n": 1, "method": "POST", "path": "/api/uploads/product-image", "status": 200, "evidence": "上传非图片字节 55 字节，自报 image/png、文件名 probe.png → imageUrl=/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png"}, {"n": 2, "method": "GET", "path": "/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png", "status": 200, "evidence": "匿名连接（无凭证）下载 → 200，字节完全一致=True，Content-Type=image/png"}, {"n": 3, "method": "FS", "path": "/app/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png", "status": 200, "evidence": "-rw-r--r-- 1 root root 55 Nov 14 16:55 /app/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png"}], "evidence": "下载字节与上传字节一致=True，Content-Type 由服务端按扩展名给出"}`

### HIGH · INT-PII-MASKED-ONLY-IN-FRONTEND

**手机号脱敏只做在渲染层：接口与库里都是明文，匿名可批量拉取**

- 端点：CHAIN-A
- 归属层：集成层（链级后果；零鉴权归服务层 NO_AUTHZ）
- 说明：订单详情抽屉把手机号显示成 138****9999，说明产品侧认定它是敏感字段；但 API 原样返回明文，库里也是明文。配合零鉴权，任何人可分页拉全量手机号。
- 复现：python3 integration_chain.py --chain CHAIN-A
- 证据：`{"chain_steps": [{"n": 1, "method": "POST", "path": "/api/orders", "status": 200, "evidence": "订单主人创建订单 id=2619（请求里没有任何凭证头）"}, {"n": 2, "method": "GET", "path": "/api/orders/2619", "status": 200, "evidence": "全新连接（无 Cookie / Token）读到 4 个 PII 字段：['customerName', 'customerPhone', 'address', 'remark']"}, {"n": 3, "method": "前端", "path": "App.jsx 订单详情抽屉", "status": null, "evidence": "接口返回手机号明文 13800000999，前端渲染时才脱敏成 138****0999 —— 脱敏只发生在展示层，接口与库里都是明文"}, {"n": 4, "method": "PATCH", "path": "/api/orders/2619/status", "status": 200, "evidence": "匿名改状态 → DEPOSIT_PAID"}, {"n": 5, "method": "PUT", "path": "/api/orders/2619", "status": 200, "evidence": "匿名全量覆盖订单：改电话 + 把明细数量改成 99"}, {"n": 6, "method": "DB", "path": "SELECT ... WHERE id=2619", "status": 200, "evidence": "库里的快照已被重写：明细数量=99，电话=13900000000，状态=PENDING"}], "evidence": "接口返回 customerPhone=13800000999，前端渲染脱敏为 138****0999"}`

### HIGH · LIST_TRUNCATED_AT_100

**前端列表固定只取前 100 条并丢弃 total，超出的数据永远不可见**

- 端点：GET /api/products, GET /api/packages, GET /api/orders
- 归属层：集权层
- 说明：后端返回 total=136，实际 items 只有 100 条。前端三个列表接口都写死 page=1&pageSize=100，且拿到响应后只做 data.items.map(...)，PageResponse.total / totalPages 被整个丢弃 —— 用户看不到「还有 35 条」，也没有任何翻页入口。商品/套餐/订单一旦超过 100 条，数据库里多出来的记录在 Pad 端凭空消失。
- 复现：python3 integration_dynamic.py --scenario SC05_LIST_TRUNCATION
- 证据：`{"total": 136, "items_count": 100, "frontend_code": "GET /api/products${toQuery({ page: 1, pageSize: 100, ...params })} -> data.items.map(mapProduct)", "root_cause": "前端硬编码 pageSize=100 + 未读取 total/totalPages"}`

### HIGH · PACKAGE_ITEM_NOT_VALIDATED

**可以把不属于该套餐的商品写进套餐明细，后端照单全收**

- 端点：POST /api/orders
- 归属层：集权层
- 说明：下单时 packageItems 里塞进套餐外的商品（['喜糖果盘']），后端只校验商品存在、不校验归属，200 通过并写进订单快照。订单快照里的「套餐明细」因此不代表真实套餐，后续对账无据可查。
- 复现：python3 integration_dynamic.py --scenario SC02_FOREIGN_ITEM_IN_PACKAGE
- 证据：`{"snapshot_items": [{"productId": 30, "productName": "喜糖果盘", "priceCent": 4200, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-30.jpg", "categoryName": "凉菜"}], "order_id": 2615, "root_cause": "OrderService.apply 未校验 packageItems 与 packageId 的归属关系"}`

### HIGH · PACKAGE_QUANTITY_NOT_VALIDATED

**明细数量可以改成 99，价格仍按套餐原价结算（加量不加价）**

- 端点：POST /api/orders
- 归属层：集权层
- 说明：下单时把套餐内商品数量改成 99 份，planTotalCent/finalPriceCent 仍是套餐原价 46800 分 —— 后端不重算、不拒绝。
- 复现：python3 integration_dynamic.py --scenario SC03_QUANTITY_OVERRIDE
- 证据：`{"snapshot_items": [{"productId": 1, "productName": "五香卤牛肉", "priceCent": 5800, "quantity": 99, "unit": "份", "imageUrl": "/uploads/seed/products/product-01.jpg", "categoryName": "凉菜"}], "final_price_cent": 46800, "root_cause": "OrderService.apply 不依据明细重算金额"}`

### MEDIUM · INT-PACKAGE-COUNT-INCONSISTENT

**同一个套餐，界面上有两个互不相同的「商品数」**

- 端点：packages.item_count
- 归属层：集成层（前端消费语义）
- 说明：套餐卡片用 mapPackage.dishes（= API 的 itemCount，含已下架商品），选套餐弹层用 App.jsx 的 visibleItemCount（只数在架商品）。只要套餐里有下架商品，两个地方显示的数字就不一样，而且都不等于实际可下单明细数。
- 复现：造一个套餐，明细含一个 is_active=false 的商品 → 对比卡片与弹层的数字
- 证据：`{"note": "套餐卡片显示「1 个商品」（= API itemCount，含下架商品），选套餐弹层显示「0 个菜品」（= 在架商品数），同一套餐两个数字不同", "usage": "App.jsx visibleItemCount vs mapPackage.dishes"}`

### MEDIUM · INT-PRODUCT-IMAGE-DOMAIN-FALLBACK

**商品缺图时前端兜底成了套餐种子图**

- 端点：products.image_url
- 归属层：集成层（前端消费语义）
- 说明：mapProduct 的兜底常量写的是套餐目录下的种子图（/uploads/seed/packages/...），商品位没有自己的兜底图。结果：任何没配图的商品，界面上都会显示一张宴会套餐照片。
- 复现：POST /api/products（imageUrl 留空）→ 前端 mapProduct → image 字段等于套餐种子图路径
- 证据：`{"note": "商品 imageUrl 为空，前端真实 mapper 兜底成 /uploads/seed/packages/package-01.jpg —— 商品位用了套餐种子图", "usage": "mapProduct.image = imageUrl || photo"}`

### MEDIUM · RECORDING_COVERAGE_GAP

**1/25 个接口既无真实流量样本、前端也不调用；17 个写接口的真实流量样本为 0**

- 端点：PATCH /api/packages/{id}/status
- 归属层：集权层
- 说明：录制流量只有 22 条、9 个路径，且 100% 是 GET 只读流量；下单、改价、改状态、删除、上传这些写路径一条真实样本都没有。上面几条集成缺陷之所以只能靠构造流量才发现，就是因为真实流量从来没覆盖过这些路径。
- 复现：见 /Users/june/tests/auto-api/recordings/jiuyan_readonly.jsonl
- 证据：`{"uncovered_endpoints": ["PATCH /api/packages/{id}/status"], "recorded_rows": 22, "write_endpoints_without_real_traffic": ["DELETE /api/categories/{id}", "DELETE /api/package-types/{id}", "DELETE /api/packages/{id}", "DELETE /api/products/{id}", "PATCH /api/orders/{id}/status", "PATCH /api/packages/{id}/status", "PATCH /api/products/{id}/status", "POST /api/categories", "POST /api/orders", "POST /api/package-types", "POST /api/packages", "POST /api/products", "POST /api/uploads/package-image", "POST /api/uploads/product-image", "PUT /api/orders/{id}", "PUT /api/packages/{id}", "PUT /api/products/{id}"], "recorded_paths": ["GET /api/categories", "GET /api/health", "GET /api/orders", "GET /api/orders/8", "GET /api/package-types", "GET /api/packages", "GET /api/packages/4", "GET /api/products", "GET /uploads/seed/packages/package-01.jpg"]}`

### MEDIUM · SNAPSHOT_CONTRACT_UNDECLARED

**前端依赖的订单快照内部结构，在 API 契约里零声明**

- 端点：GET/POST/PUT /api/orders
- 归属层：集权层
- 说明：OrderResponse 把 packageSnapshot / addedItemsSnapshot 声明成 JsonNode（自由 JSON），OpenAPI 与 DTO 都没有描述内部字段。前端 mapOrder 读取的 ['banquetType', 'items', 'packageId', 'packageName', 'priceCent', 'productId', 'productName', 'quantity'] 只靠「OrderService 里那段拼 JSON 的代码」维持约定 —— 任何人改动那段拼装逻辑，前端不会报错，只会显示 undefined。这是集权层专属的契约缺口：服务层自洽、类层自洽，只有两端拼在一起才看得见。
- 复现：grep -n 'packageSnapshot' backend/src/main/java/com/jiuyan/banquet/dto/OrderResponse.java
- 证据：`{"undeclared_paths": {"packageSnapshot": ["banquetType", "items", "packageId", "packageName"], "packageSnapshot.items": ["priceCent", "productId", "productName", "quantity"], "addedItemsSnapshot": ["priceCent", "productId", "productName", "quantity"]}, "actual_snapshot_keys": {"root": ["packageId", "packageName", "banquetType", "priceCent", "imageUrl", "items"], "item": ["productId", "productName", "priceCent", "quantity", "unit", "imageUrl", "categoryName"]}, "note": "实测这些字段当前确实存在（见 SNAPSHOT_SHAPE_MATCHES），但没有任何契约或测试守着它"}`

### MEDIUM · UNKNOWN_FIELD_SILENT_DROP

**字段名拼错会被静默吞掉，并被「套餐默认明细」悄悄顶替**

- 端点：POST /api/orders
- 归属层：集权层
- 说明：把 packageItems 写成 packageItem 后端不报错（Jackson 默认忽略未知字段），而 OrderService 在明细为空时会回退到套餐自身的默认 items —— 于是用户明明选了 3 道菜并改了数量，订单快照里躺的是套餐出厂明细。前端界面显示的是用户的选择，库里存的是别的东西，两边永远对不上，也没有任何报错。
- 复现：python3 integration_dynamic.py --scenario SC04_UNKNOWN_FIELD_SILENT_DROP
- 证据：`{"snapshot_items": [{"productId": 1, "productName": "五香卤牛肉", "priceCent": 5800, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-01.jpg", "categoryName": "凉菜"}, {"productId": 4, "productName": "拍黄瓜", "priceCent": 1500, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-04.jpg", "categoryName": "凉菜"}, {"productId": 7, "productName": "土鸡烧香菇", "priceCent": 9600, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-07.jpg", "categoryName": "热菜"}, {"productId": 10, "productName": "蒜蓉粉丝虾", "priceCent": 11800, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-10.jpg", "categoryName": "热菜"}, {"productId": 11, "productName": "清炒时蔬", "priceCent": 2800, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-11.jpg", "categoryName": "热菜"}, {"productId": 13, "productName": "毛血旺", "priceCent": 7600, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-13.jpg", "categoryName": "热菜"}, {"productId": 19, "productName": "八宝糯米饭", "priceCent": 3600, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-19.jpg", "categoryName": "主食"}, {"productId": 21, "productName": "扬州炒饭", "priceCent": 3800, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-21.jpg", "categoryName": "主食"}, {"productId": 25, "productName": "酸辣汤", "priceCent": 2400, "quantity": 1, "unit": "份", "imageUrl": "/uploads/seed/products/product-25.jpg", "categoryName": "汤品"}, {"productId": 26, "productName": "上门灶台服务", "priceCent": 30000, "quantity": 1, "unit": "场", "imageUrl": "/uploads/seed/products/product-26.jpg", "categoryName": "服务项"}], "root_cause": "spring.jackson 未开 fail-on-unknown-properties + buildPackageSnapshot 空明细回退"}`

### LOW · DEAD_BACKEND_ENDPOINT

**后端 4 个接口前端从不调用**

- 端点：GET /api/health, GET /api/orders/{id}, GET /api/packages/{id}, PATCH /api/packages/{id}/status
- 归属层：集权层
- 说明：这些路由在前端 banquetApi.js 里没有任何调用点。GET /api/orders/{id} 与 GET /api/packages/{id} 属于详情接口冗余；真正值得看的是 PATCH /api/packages/{id}/status —— 它是唯一的状态机式接口，Pad 端却从不调用，套餐上下架只能整单 PUT 全量覆盖（连同 items 一起重传），绕开了最小变更路径。
- 复现：grep -n 'api/' /Users/june/jiuyan/src/api/banquetApi.js
- 证据：`{"unused_endpoints": ["GET /api/health", "GET /api/orders/{id}", "GET /api/packages/{id}", "PATCH /api/packages/{id}/status"], "note": "GET /api/health 属运维探针，不算缺陷"}`

## 攻击链（跨层可执行链，每一步都有证据）

| 链 | 结论 | 步数 | 标题 |
|---|---|---|---|
| `CHAIN-A` | CONFIRMED | 6 | 匿名会话可读可写他人订单，且写入会连带重写订单快照 |
| `CHAIN-B` | CONFIRMED | 3 | 任意字节以图片名落盘，且落盘后匿名可原样下载 |
| `CHAIN-C` | CONFIRMED | 4 | 成交价由客户端说了算：DB、API、前端视图模型同时认下同一个假数字 |
| `CHAIN-D` | CONFIRMED | 3 | 明细被改成 99 件后，订单里同时存在三个互不相等的价格，而明细合计从不显示 |
| `CHAIN-E` | CONFIRMED | 3 | 订单可以被直接推进到「已完成」，定金 0 元、金额 0.01 元 |


### CHAIN-A · CONFIRMED

**匿名会话可读可写他人订单，且写入会连带重写订单快照**

- 假设：订单接口零鉴权，跨连接读写成功；写入路径会重建快照
- 归属：service（引用 NO_AUTHZ，链级后果归集成层）
- 引用下层缺陷：`NO_AUTHZ`、`FN-SNAPSHOT-SEMANTICS`
- 结论：匿名会话完成了读 / 改状态 / 全量覆盖，且覆盖动作重写了订单快照

| # | 动作 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | POST | `/api/orders` | 200 | 订单主人创建订单 id=2619（请求里没有任何凭证头） |
| 2 | GET | `/api/orders/2619` | 200 | 全新连接（无 Cookie / Token）读到 4 个 PII 字段：['customerName', 'customerPhone', 'address', 'remark'] |
| 3 | 前端 | `App.jsx 订单详情抽屉` | None | 接口返回手机号明文 13800000999，前端渲染时才脱敏成 138****0999 —— 脱敏只发生在展示层，接口与库里都是明文 |
| 4 | PATCH | `/api/orders/2619/status` | 200 | 匿名改状态 → DEPOSIT_PAID |
| 5 | PUT | `/api/orders/2619` | 200 | 匿名全量覆盖订单：改电话 + 把明细数量改成 99 |
| 6 | DB | `SELECT ... WHERE id=2619` | 200 | 库里的快照已被重写：明细数量=99，电话=13900000000，状态=PENDING |


### CHAIN-B · CONFIRMED

**任意字节以图片名落盘，且落盘后匿名可原样下载**

- 假设：上传只信客户端声明的内容类型，静态映射又对所有人开放
- 归属：service（引用 FN-UPLOAD-NO-CONTENT-SNIFF，链级后果归集成层）
- 引用下层缺陷：`FN-UPLOAD-NO-CONTENT-SNIFF`、`FILE-STATIC-ROOT-NO-TRAILING-SLASH`
- 结论：非图片字节以 .png 落盘，并可被匿名无凭证原样下载

| # | 动作 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | POST | `/api/uploads/product-image` | 200 | 上传非图片字节 55 字节，自报 image/png、文件名 probe.png → imageUrl=/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png |
| 2 | GET | `/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png` | 200 | 匿名连接（无凭证）下载 → 200，字节完全一致=True，Content-Type=image/png |
| 3 | FS | `/app/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png` | 200 | -rw-r--r-- 1 root root 55 Nov 14 16:55 /app/uploads/products/70206bca-2453-43fd-a82e-0f7065ebe3ca.png |


### CHAIN-C · CONFIRMED

**成交价由客户端说了算：DB、API、前端视图模型同时认下同一个假数字**

- 假设：finalPriceCent 无上界，与套餐价、明细合计没有任何算术关系
- 归属：function（引用 FN-PRICE-CLIENT-CONTROLLED）
- 引用下层缺陷：`FN-PRICE-CLIENT-CONTROLLED`、`CLASS-RULE-MISSING-IN-ALL-LAYERS`
- 结论：成交价 0.01 元被三方一致接受，而明细合计是 150.0 元，差 14999 分（150.0 元）；服务端没有任何一处核对

| # | 动作 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | POST | `/api/orders` | 200 | 下单时 finalPriceCent=1（1 分），明细 3 件 × 5000 分 = 15000 分 |
| 2 | DB | `SELECT final_price_cent FROM orders WHERE id=2620` | 200 | 库里的成交价 = 1 分 |
| 3 | GET | `/api/orders/2620` | 200 | API 回读成交价 = 1 分 |
| 4 | 前端 | `mapOrder（前端真实 mapper）` | 200 | 前端渲染订单金额 = 0.01 元；它自己看到的明细合计 = 150.0 元 |


### CHAIN-D · CONFIRMED

**明细被改成 99 件后，订单里同时存在三个互不相等的价格，而明细合计从不显示**

- 假设：明细数量与成交价、套餐价三者无约束，且订单详情不展示明细合计
- 归属：integration（链级后果；明细归属归函数层，快照语义归函数层）
- 引用下层缺陷：`FN-SNAPSHOT-SEMANTICS`、`CLASS-RULE-MISSING-IN-ALL-LAYERS`、`PACKAGE_QUANTITY_NOT_VALIDATED`
- 结论：订单金额 0.01 元、明细合计 4950.00 元、套餐标价 88.88 元三者并存，而界面不展示明细合计 —— 客户端控价的后果在业务侧不可见

| # | 动作 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | PUT | `/api/orders/2621` | 200 | 匿名把明细数量改成 99，成交价保持 1 分 |
| 2 | GET | `/api/orders/2621` | 200 | 订单金额 0.01 元 / 明细合计 4950.0 元 / 套餐标价 88.88 元 —— 三个数字互不相等 |
| 3 | 前端 | `App.jsx 订单详情抽屉` | None | 抽屉只渲染 已选套餐 / 加购商品 / 订单金额 / 定金，明细单价与明细合计一律不渲染，员工在界面上无法核对订单金额的构成 |


### CHAIN-E · CONFIRMED

**订单可以被直接推进到「已完成」，定金 0 元、金额 0.01 元**

- 假设：状态机没有任何约束，PENDING 可一步跳到 COMPLETED
- 归属：class（引用 NO_STATE_MACHINE）
- 引用下层缺陷：`NO_STATE_MACHINE`、`CLASS-RULE-MISSING-IN-ALL-LAYERS`
- 结论：0 元定金、0.01 元金额的订单被推成「已完成」，业务侧看到的是一张已交付订单

| # | 动作 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | POST | `/api/orders` | 200 | 建单 id=2622：PENDING、定金 0、成交价 1 分 |
| 2 | PATCH | `/api/orders/2622/status` | 200 | 匿名 PENDING → COMPLETED 一次成功；前端渲染状态 = 已完成，定金 = 0 元，订单金额 = 0.01 元 |
| 3 | DB | `SELECT status, deposit_cent FROM orders WHERE id=2622` | 200 | 库里的状态 = COMPLETED，定金 = 0 分 |


## 通过的对账项（本层不重复计缺陷）

- ✅ **C1_PATH_ALIGNED** — 前端 21 个调用路径在后端路由表中全部存在
- ✅ **C3_BODY_FIELDS_ALIGNED** — 前端发送的请求字段与后端 DTO record 组件完全一致（无多余、无缺失必填）
- ✅ **C4_RESPONSE_FIELDS_PRESENT** — 前端三个映射函数读取的字段，在后端响应 DTO 与快照结构中全部存在
- ✅ **SNAPSHOT_SHAPE_MATCHES** — 前端读取的快照内部字段，今天确实都存在（但契约里没有任何声明守着它们）
- ✅ **C5_ENUM_ALIGNED** — 订单状态枚举三方一致（前端 statusToApi/statusFromApi × 后端 OrderStatus × DB CHECK）；日期范围枚举前端 3 个取值后端 parseDateScope 全部接受
- ✅ **C6_NO_RECORDING_DRIFT** — 22 条录制流量全部回放，状态码与录制时完全一致（0 漂移）
- ✅ **SC06_ENUM_RANGE** — 订单状态/日期范围枚举的每个前端取值，后端都接受（HTTP 200）
- ✅ **FIELD_CHECK_mapProduct** — mapProduct 读取的 9 个字段在真实响应里全部存在
- ✅ **FIELD_CHECK_mapPackage** — mapPackage 读取的 9 个字段在真实响应里全部存在
- ✅ **FIELD_CHECK_mapOrder** — mapOrder 读取的 19 个字段在真实响应里全部存在
- ✅ **RECON_ALIGNED_products_price_cent** — products.price_cent 三方闭合：DB/API 都是 5000 分，前端真实 mapper 渲染 50 元
- ✅ **RECON_ALIGNED_products_is_active** — products.is_active 三方闭合：DB is_active=False → API False → 前端 False
- ✅ **RECON_ALIGNED_packages_price_cent** — packages.price_cent 三方闭合：DB/API 都是 8888 分，前端真实 mapper 渲染 88.88 元
- ✅ **RECON_INTERNAL_KEY_HIDDEN_package_items_id** — package_items.id 是内部主键，接口刻意不外露（前端也用不到）
- ✅ **RECON_INTERNAL_KEY_HIDDEN_package_items_package_id** — package_items.package_id 是内部主键，接口刻意不外露（前端也用不到）
- ✅ **RECON_ALIGNED_packages_item_count** — packages.item_count 三方闭合：API itemCount=1 vs DB 明细 1 行
- ✅ **RECON_ALIGNED_orders_final_price_cent** — orders.final_price_cent 三方闭合：DB/API 都是 13579 分，前端真实 mapper 渲染 135.79 元
- ✅ **RECON_ALIGNED_orders_reservation_time** — orders.reservation_time 三方闭合：写入 2027-03-05T18:30:00+08:00 → DB 2027-03-05T10:30:00+00:00 → API 2027-03-05T10:30:00Z → 前端渲染 2027-03-05 18:30
- ✅ **RECON_ALIGNED_orders_plan_total_cent** — orders.plan_total_cent 三方闭合：DB/API 都是 24680 分，前端真实 mapper 渲染 246.8 元
- ✅ **RECON_ALIGNED_orders_status** — orders.status 三方闭合：DB PENDING → API PENDING → 前端 待确认
- ✅ **RECON_ALIGNED_orders_package_snapshot** — orders.package_snapshot 三方闭合：DB 快照键 ['banquetType', 'imageUrl', 'items', 'packageId', 'packageName', 'priceCent'] / API 快照键 ['banquetType', 'imageUrl', 'items', 'packageId', 'packageName', 'priceCent'] / 前端要读 ['banquetType', 'items', 'packageId', 'packageName']
- ✅ **RECON_ALIGNED_orders_package_snapshot.items** — orders.package_snapshot.items 三方闭合：DB 明细键 ['categoryName', 'imageUrl', 'priceCent', 'productId', 'productName', 'quantity', 'unit'] / API 明细键 ['categoryName', 'imageUrl', 'priceCent', 'productId', 'productName', 'quantity', 'unit']
- ✅ **RECON_CORROBORATES_orders_final_price_cent** — orders.final_price_cent 语义不闭合（前端看到的明细合计 25000 分（250.0 元） vs 订单金额 13579 分（135.79 元），差额 11421 分；服务端对 finalPriceCent 不做任何核对）—— 根因归function层，但其业务预期未经确认，已进待确认疑点清单、不进缺陷台账，本层只做跨源佐证，不重复计缺陷
- ✅ **RECON_ALIGNED_orders_added_items_snapshot** — orders.added_items_snapshot 三方闭合：前端要读 ['priceCent', 'productId', 'productName', 'quantity'] / API 加购明细键 ['categoryName', 'imageUrl', 'priceCent', 'productId', 'productName', 'quantity', 'unit']
- ✅ **RECON_CORROBORATES_packages_banquet_type** — packages.banquet_type 语义不闭合（服务层有 PACKAGE_TYPE_NOT_FOUND 校验，DB 直写畅通无阻）—— 根因已在class层开单，本层只做跨源佐证，不重复计缺陷
- ✅ **RECON_CORROBORATES_orders_customer_phone** — orders.customer_phone 语义不闭合（服务层有 无 校验，DB 直写畅通无阻）—— 根因已在class层开单，本层只做跨源佐证，不重复计缺陷
- ✅ **RECON_DERIVED_FIELDS_CLOSED** — 38 个 API 字段在 DB 里没有对应列（join / 计算派生），前端消费口径与 API 一致
- ✅ **RECON_FRONTEND_UNUSED_FIELDS_BY_DESIGN** — 10 个 API 字段前端不消费：审计时间戳，或前端按 productId 回查商品表
- ✅ **RECON_TOTAL** — 三方对账共 111 项
- ✅ **CHAIN_CORROBORATES_CHAIN-C** — CHAIN-C 跨层观测：成交价 0.01 元被三方一致接受，而明细合计是 150.0 元，差 14999 分（150.0 元）；服务端没有任何一处核对（引用的 FN-PRICE-CLIENT-CONTROLLED 业务预期未经确认，进待确认疑点清单，不作为已开单缺陷）
- ✅ **CHAIN_CORROBORATES_CHAIN-E** — CHAIN-E 证实了低层缺陷：0 元定金、0.01 元金额的订单被推成「已完成」，业务侧看到的是一张已交付订单

---

口径说明：与服务层/类层已报缺陷重复的根因，本层只补集成症状并在 owner 字段标注；
所有结论均由 `integration_dynamic.py` 实测产生，每条都带可重放的复现路径。
