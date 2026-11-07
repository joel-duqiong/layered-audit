# 函数层缺陷报告（JVM 直调 · 字节码声明 vs 实测行为）

函数总数 461，用例 92 条，失败 28 条 / 通过 64 条，归并为 8 条缺陷。

## [CRITICAL] FN-PRICE-CLIENT-CONTROLLED — 成交价由客户端说了算：resolveFinalPrice 只做下界检查，完全不与套餐价比对

- 根因函数：`OrderService.resolveFinalPrice`
- 根因：resolveFinalPrice(boolean, Long, Long) 只校验成交价 >= 0，既不要求成交价 <= 套餐总价，也不要求有折扣依据，于是客户端可以把任意数字直接变成订单最终价。
- HTTP 可达：是（边界情况：CreateOrderRequest.finalPriceCent 只有 @Min(0)，上界没有任何约束）
- 证据（4 条实测）：
  - `RFP-05` com.jiuyan.banquet.service.OrderService#resolveFinalPrice — 不变量「成交价不得低于套餐总价（客户端不能自降定价权）」→ 期望 >= 46800，实际 1
  - `RFP-06` com.jiuyan.banquet.service.OrderService#resolveFinalPrice — 不变量「成交价不得高于套餐总价」→ 期望 <= 46800，实际 999999
  - `RFP-07` com.jiuyan.banquet.service.OrderService#resolveFinalPrice — 不变量「成交价不得为 0（0 元成交无任何业务依据）」→ 期望 >= 1，实际 0
  - `APL-15` com.jiuyan.banquet.service.OrderService#apply — 不变量「落库的成交价不得高于套餐总价」→ 期望 finalPriceCent <= 46800，实际 999999
- 跨层关联：
  - integration 层 `PRICE_CLIENT_CONTROLLED_XL`：集成症状：本层是根因（高层已开单）
  - service 层 `test_create_order_use_final_price`：既有测试把「保留客户端成交价」写成了断言——缺陷被固化成契约

## [CRITICAL] FN-UPLOAD-NO-CONTENT-SNIFF — 上传只信客户端自报的扩展名与 contentType：任意字节都以 .png 落盘

- 根因函数：`UploadService.uploadImage / extension`
- 根因：白名单校验的对象是客户端自己提供的 originalFilename 与 Content-Type，既不做内容嗅探，也不校验文件头；实测一个 PE 可执行文件（MZ 头）被原样写成 products/<uuid>.png。
- HTTP 可达：是（边界情况：HTTP 契约层看不出内容问题——必须真的写一次文件才暴露）
- CWE: CWE-434
- 证据（2 条实测）：
  - `UPL-04` com.jiuyan.banquet.service.UploadService#uploadProductImage — 不变量「文件内容不是图片时不得落盘（内容必须嗅探，不能只看客户端自报的 contentType）」→ 落盘文件: [{'name': 'products/96144727-175f-4fe3-850f-a1cb2ac5251c.png', 'size': 27, 'first_bytes_hex': '4d5a900003000000', 'magic_ascii': 'MZ......'}] | 落盘 products/96144727-175f-4fe3-850f-a1cb2ac5251c.png size=27 head=MZ......
  - `UPL-07` com.jiuyan.banquet.service.UploadService#uploadProductImage — 不变量「任意字节不得以图片扩展名落盘」→ 落盘文件: [{'name': 'products/187a8be9-6973-4e2f-ab8a-09998933f284.png', 'size': 40, 'first_bytes_hex': '6a75737420706c61', 'magic_ascii': 'just pla'}] | 落盘 products/187a8be9-6973-4e2f-ab8a-09998933f284.png size=40 head=just pla

## [HIGH] FN-ORDER-NO-COLLISION — 订单号每秒只有 1000 个槽位，并发下单必然撞号（唯一键冲突 → 500）

- 根因函数：`OrderService.generateOrderNo`
- 根因：订单号格式 DD + yyyyMMddHHmmss + 3 位随机，随机空间只有 1000/秒；existsByOrderNo 检查与 insert 非原子（TOCTOU），并发事务互相看不见，重复号直接撞 uk_orders_order_no 唯一约束。
- HTTP 可达：是（边界情况：无任何边界能挡住并发写；客户端请求速率就是下单速率）
- CWE: CWE-362
- 证据（1 条实测）：
  - `GON-02` # — 不变量「同一秒内生成的订单号必须互不相同（唯一槽位只有 1000 个/秒）」→ 并发生成 3000 个订单号，重复 2052 个 | generated=3000 distinct=948 elapsed=17ms

## [HIGH] FN-SNAPSHOT-SEMANTICS — 订单快照：明细归属不校验，空明细静默回退成套餐默认明细

- 根因函数：`OrderService.buildPackageSnapshot / normalizePackageItems`
- 根因：buildPackageSnapshot 用「requestedItems 是否为空」来决定用请求明细还是回退套餐默认明细，「用户明确不要明细」与「用户没传明细」在这里不可区分；同时它只校验商品存在，完全不校验商品是否属于该套餐。
- HTTP 可达：是（边界情况：DTO 只约束数量 >= 1 与 productId 非空，不约束归属，也不区分空 list 与未传）
- 证据（4 条实测）：
  - `BPS-03` com.jiuyan.banquet.service.OrderService#buildPackageSnapshot — 不变量「显式传空明细 = 用户不要明细，不得回退套餐默认明细」→ 期望 0 条明细，实际 {'packageId': 4, 'packageName': '双人精选宴', 'banquetType': 'WEDDING', 'priceCent': 46800, 'imageUrl': '/uploads/seed/packages/package-04.jpg', 'items': [{'productId': 11, 'productName': '花胶鸡汤', 'priceCent': 8800, 'quantity': 2, 'unit': '份', 'imageUrl': '/uploads/seed/products/product-11.jpg', 'categoryName': '汤品'}]}
  - `BPS-05` com.jiuyan.banquet.service.OrderService#buildPackageSnapshot — 不变量「套餐外商品（29 不在套餐 4 内）必须被拒绝」→ 期望抛异常，实际返回 {'packageId': 4, 'packageName': '双人精选宴', 'banquetType': 'WEDDING', 'priceCent': 46800, 'imageUrl': '/uploads/seed/packages/package-04.jpg', 'items': [{'productId': 29, 'productName': '套餐外商品', 'priceCent': 100, 'quantity': 1, 'unit': '份', 'imageUrl': None, 'categoryName': '汤品'}]}
  - `BPS-06` com.jiuyan.banquet.service.OrderService#buildPackageSnapshot — 不变量「快照里的数量必须 >= 1」→ 期望 quantity=1，实际 0
  - `APL-17` com.jiuyan.banquet.service.OrderService#apply — 不变量「显式传空明细时，订单快照必须为空而不是套餐默认明细」→ 期望快照 0 条明细，实际 {'packageId': 4, 'packageName': '双人精选宴', 'banquetType': 'WEDDING', 'priceCent': 46800, 'imageUrl': '/uploads/seed/packages/package-04.jpg', 'items': [{'productId': 11, 'productName': '花胶鸡汤', 'priceCent': 8800, 'quantity': 2, 'unit': '份', 'imageUrl': '/uploads/seed/products/product-11.jpg', 'categoryName': '汤品'}]}
- 跨层关联：
  - integration 层 `PACKAGE_ITEM_NOT_VALIDATED`：集成症状：本层是根因（高层已开单）
  - integration 层 `PACKAGE_QUANTITY_NOT_VALIDATED`：集成症状：本层是根因（高层已开单）
  - integration 层 `UNKNOWN_FIELD_SILENT_DROP`：集成症状：字段拼错 → 明细为空 → 落到本函数的回退分支（高层已开单）

## [MEDIUM] FN-APPLY-AMOUNT-INVARIANTS — apply 的金额/状态不变量只查了 >= 0：定金可以大于套餐总价，状态与预订时间可为空

- 根因函数：`OrderService.apply`
- 根因：apply 只校验 planTotalCent >= 0 与 depositCent >= 0，不校验 depositCent <= planTotalCent、tableCount >= 1、reservationTime/status 非空——DTO 上声明的约束没有在函数里落地。
- HTTP 可达：是（边界情况：tableCount 与 status、reservationTime 由 DTO 注解挡住；但「定金大于总价」没有任何约束，HTTP 可达）
- 证据（3 条实测）：
  - `APL-12` com.jiuyan.banquet.service.OrderService#apply — 不变量「定金不得大于套餐总价」→ 期望抛异常，实际返回 None
  - `APL-13` com.jiuyan.banquet.service.OrderService#apply — 不变量「桌数必须 >= 1（DTO 声明 @Min(1)）」→ 期望抛异常，实际返回 None
  - `APL-14` com.jiuyan.banquet.service.OrderService#apply — 不变量「预订时间缺失必须 422（DTO 声明 @NotNull）」→ 期望抛异常，实际返回 None

## [MEDIUM] FN-APPLY-NULL-CONTRACT — apply 直接对入参 .trim()：空值触发 NPE（500）而不是业务异常（422）

- 根因函数：`OrderService.apply / CategoryService.create`
- 根因：apply 对 customerName/customerPhone/banquetName/banquetType 直接调用 .trim()，空值抛 NullPointerException；CategoryService.create 对 name 同样。函数自身不做空值防御，把全部校验押在 DTO 注解上。
- HTTP 可达：否（边界情况：HTTP 路径由 @NotBlank 挡住；但任何内部调用或新增入口都会直接 500，属于防御纵深缺失而非可利用缺陷）
- 证据（5 条实测）：
  - `APL-02` com.jiuyan.banquet.service.OrderService#apply — 不变量「customerName 为空必须 422（DTO 声明 @NotBlank），不能是 500」→ 期望 HTTP 422，实际 None / None（抛出的是 java.lang.NullPointerException: Cannot invoke "String.trim()" because "<parameter2>" is null）
  - `APL-03` com.jiuyan.banquet.service.OrderService#apply — 不变量「customerPhone 为空必须 422，不能是 500」→ 期望 HTTP 422，实际 None / None（抛出的是 java.lang.NullPointerException: Cannot invoke "String.trim()" because "<parameter3>" is null）
  - `APL-04` com.jiuyan.banquet.service.OrderService#apply — 不变量「banquetType 为空必须 422，不能是 500」→ 期望 HTTP 422，实际 None / None（抛出的是 java.lang.NullPointerException: Cannot invoke "String.trim()" because "<parameter5>" is null）
  - `APL-09` com.jiuyan.banquet.service.OrderService#apply — 不变量「status 为空必须 422（DTO 声明 @NotNull）」→ 期望抛异常，实际返回 None
  - `CTG-02` com.jiuyan.banquet.service.CategoryService#create — 不变量「名称为 null 必须 422（DTO 声明 @NotBlank），不能是 500」→ 期望 HTTP 422，实际 None / None（抛出的是 java.lang.NullPointerException: Cannot invoke "String.trim()" because the return value of "com.jiuyan.banquet.dto.CreateCategoryRequest.name()" is null）

## [MEDIUM] FN-ITEM-QUANTITY-NOVALIDATION — 明细归一化只查重复，不查 productId/quantity：数量可为空、为 0、为负

- 根因函数：`OrderService.normalizePackageItems / normalizeAddedItems`
- 根因：两个归一化函数只做「同一商品重复」检查，productId 为 null 会变成 map 的 null 键、quantity 为 null/0/负数原样入表；quantity 也没有上限。
- HTTP 可达：是（边界情况：PackageItemRequest/AddedItemRequest 的 @NotNull @Min(1) 在 HTTP 边界已挡住 null/0/负数，但数量上限无任何约束——集成层实测 quantity=99 被接受）
- 证据（8 条实测）：
  - `NPI-04` com.jiuyan.banquet.service.OrderService#normalizePackageItems — 不变量「productId 为空必须拒绝（DTO 声明 @NotNull）」→ 实际 {'null': 2}
  - `NPI-05` com.jiuyan.banquet.service.OrderService#normalizePackageItems — 不变量「quantity 为空必须拒绝（DTO 声明 @NotNull）」→ 实际 {'11': None}
  - `NPI-06` com.jiuyan.banquet.service.OrderService#normalizePackageItems — 不变量「quantity 必须 >= 1（DTO 声明 @Min(1)）」→ 实际 {'11': 0}
  - `NPI-07` com.jiuyan.banquet.service.OrderService#normalizePackageItems — 不变量「数量为负必须拒绝」→ 实际 {'11': -3}
  - `NAI-04` com.jiuyan.banquet.service.OrderService#normalizeAddedItems — 不变量「productId 为空必须拒绝（DTO 声明 @NotNull）」→ 实际 {'null': 2}
  - `NAI-05` com.jiuyan.banquet.service.OrderService#normalizeAddedItems — 不变量「quantity 为空必须拒绝（DTO 声明 @NotNull）」→ 实际 {'11': None}
  - `NAI-06` com.jiuyan.banquet.service.OrderService#normalizeAddedItems — 不变量「quantity 必须 >= 1（DTO 声明 @Min(1)）」→ 实际 {'11': 0}
  - `NAI-07` com.jiuyan.banquet.service.OrderService#normalizeAddedItems — 不变量「数量为负必须拒绝」→ 实际 {'11': -3}
- 跨层关联：
  - integration 层 `PACKAGE_QUANTITY_NOT_VALIDATED`：集成症状：本层是根因（高层已开单）

## [MEDIUM] FN-UPLOAD-NO-SIZE-LIMIT — 上传无体积上限：单请求 20MB 被完整写盘（上传放大 DoS）

- 根因函数：`UploadService.uploadImage`
- 根因：uploadImage 从不检查 file.getSize()，也没有服务端配额，文件按客户端给的字节数完整落盘。
- HTTP 可达：是（边界情况：需要看容器/反向代理是否另有兜底限制——应用层没有）
- CWE: CWE-770
- 证据（1 条实测）：
  - `UPL-06` com.jiuyan.banquet.service.UploadService#uploadProductImage — 不变量「上传体积必须有上限（防止上传放大 DoS）」→ 落盘文件: [{'name': 'products/67edf8a4-a49b-46ba-b59f-1bf39bc8208d.png', 'size': 20971520, 'first_bytes_hex': '4141414141414141', 'magic_ascii': 'AAAAAAAA'}] | 落盘 products/67edf8a4-a49b-46ba-b59f-1bf39bc8208d.png size=20971520 head=AAAAAAAA

## 假设裁决

- **FALSIFIED**（jiuyan-layer-manifest.yaml / fn-order）：parseStatus 抛 IllegalArgumentException 而非 BusinessException，非法状态返回 500 → 字节码异常表里有 IllegalArgumentException 捕获；实测非法状态抛 ORDER_STATUS_INVALID / HTTP 422
- **CONFIRMED**（jiuyan-layer-manifest.yaml / fn-order）：generateOrderNo 每秒仅 1000 槽位，existsByOrderNo 与 insert 非原子 -> 并发重复单号 → 并发生成 3000 个订单号（16ms）实测重复 2052 个；DB 上存在 uk_orders_order_no 唯一约束
- **FALSIFIED**（jiuyan-layer-manifest.yaml / svc-catalog）：四个 service 各自复制 normalizePage/normalizePageSize，上限行为不一致 → 只有 ProductService 与 PackageService 有分页；两个实现逐用例行为完全一致（16 条用例全通过）
- **CONFIRMED**（函数层实测新发现）：（新增）空明细与套餐外明细是集成层现象，函数层应无问题 → 根因落在 buildPackageSnapshot / normalizePackageItems，函数层单独可见
