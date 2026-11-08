# 类层差分报告（jiuyan）

JPA 实体 7 个 | DDL 表 6 张 | 索引 22 个 | finding 23 条

| 级别 | 层 | 缺陷 |
|---|---|---|
| HIGH | class | 状态机缺失：9/9 种迁移全部被接受，含回退 |
| HIGH | class→service | DDL 声明的 idx_products_name 对服务层查询模式无效（7.558ms vs 0.012ms，差 630 倍） |
| HIGH | class | com.jiuyan.banquet.entity.BanquetPackage.getItems 把内部实体集合直接交出去 |
| HIGH | class | 4 条业务不变量在 DB / DTO / 服务层三层全都不存在 |
| MEDIUM | class | 软删除的行会被同名重建「复活」，id 与 sort_order 沿用旧值 |
| MEDIUM | class | orders 表没有软删/归档，永久增长（当前 184 kB） |
| MEDIUM | class | BanquetOrder 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | BanquetPackage 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | Category 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | PackageItem 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | PackageType 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | Product 没有值相等：放进集合或当键，语义会变 |
| MEDIUM | class | orders.address 声明 NOT NULL，实际用空串绕过 —— 非空约束只挡了 NULL |
| MEDIUM | class | orders.finalPriceCent 必填性校验落在服务层，DTO 少一条 @NotNull |
| MEDIUM | class | PUT 是全量覆盖：不传的 imageUrl 会被抹成 NULL |
| LOW | class | products.is_active 的默认值被声明了三遍 |
| LOW | class | packages.is_active 的默认值被声明了三遍 |
| LOW | class | finalPriceCent=null 的报错文案说的是「小于 0」，真实原因是「不能为空」 |
| LOW | class | 订单状态枚举大小写敏感，小写 pending 直接 400 |
| INFO | class | OrderStatus 枚举与 DB CHECK 白名单完全一致（3/3） |
| INFO | class | 可空性三方一致 27/31 列（同向设防 21 · DTO 更严 0 · 都可空 6） |
| INFO | class | 8/8 条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合 |
| INFO | class | PUT 缺省必填外键被 DTO 的 @NotNull 挡住（400，DB 无脏数据） |

## [HIGH] 状态机缺失：9/9 种迁移全部被接受，含回退

DDL 只有取值白名单 CHECK (status IN (...))，没有任何迁移约束；实测 9 种迁移全部 200 成功。业务后果：已付款（DEPOSIT_PAID）/已完成（COMPLETED）的订单可以被改回 PENDING，押金与结算状态和现实不符，而且没有留下任何迁移记录。

```json
{
  "allowed": [
    "PENDING->PENDING",
    "PENDING->DEPOSIT_PAID",
    "PENDING->COMPLETED",
    "DEPOSIT_PAID->PENDING",
    "DEPOSIT_PAID->DEPOSIT_PAID",
    "DEPOSIT_PAID->COMPLETED",
    "COMPLETED->PENDING",
    "COMPLETED->DEPOSIT_PAID",
    "COMPLETED->COMPLETED"
  ],
  "backward_transitions": [
    "PENDING->DEPOSIT_PAID",
    "PENDING->COMPLETED",
    "DEPOSIT_PAID->COMPLETED"
  ],
  "ddl_check": "status IN ('PENDING','DEPOSIT_PAID','COMPLETED')"
}
```

## [HIGH] DDL 声明的 idx_products_name 对服务层查询模式无效（7.558ms vs 0.012ms，差 630 倍）

迁移文件建了 products(name) 索引，但服务层关键词搜索写的是 lower(name) LIKE '%kw%'：lower() 让索引无法用于定位，前导 % 让 btree 本来也用不上。EXPLAIN 显示走 Filter 全量过滤而非 Index Cond。类层把索引交给服务层，但服务层不知道该怎么用它 —— 声明没有传递下去。

```json
{
  "like_prefix_lower_ms": 7.558,
  "exact_equal_ms": 0.012,
  "index_cond_present": true,
  "like_has_index_cond": false,
  "scale_rows": 50000
}
```

## [HIGH] com.jiuyan.banquet.entity.BanquetPackage.getItems 把内部实体集合直接交出去

public 方法返回 java.util.List<com.jiuyan.banquet.entity.PackageItem>，元素类型是实体 com.jiuyan.banquet.entity.PackageItem。调用方拿到的不是快照而是内部对象本身 —— 改一个字段就改了服务层的内部状态，绕过所有不变量检查。集合可变、实体也可变，封装在这一行彻底失效。（返回单个实体引用是 JPA 常规用法，不算泄漏；这里只认「实体的集合」。）

```json
{
  "class": "com.jiuyan.banquet.entity.BanquetPackage",
  "method": "getItems",
  "returns": "java.util.List<com.jiuyan.banquet.entity.PackageItem>",
  "entity_type": "com.jiuyan.banquet.entity.PackageItem",
  "file_carrier": {
    "detector": "encapsulation_leak",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.BanquetPackage",
        "method": "getItems",
        "return_type": "java.util.List<com.jiuyan.banquet.entity.PackageItem>",
        "element_type": "com.jiuyan.banquet.entity.PackageItem",
        "element_entity": true
      }
    ]
  }
}
```

## [HIGH] 4 条业务不变量在 DB / DTO / 服务层三层全都不存在

这些不是「某一层写错了」，而是三层都没有表达过 —— 没有任何一层承担校验责任，实测 API 也全部放行并落库。订单的业务语义（定金不超过成交价、预订时间在未来、联系方式是手机号、完成必有定金）目前只存在于人的脑子里，不存在于任何一行代码或迁移文件里。

```json
{
  "rules": [
    {
      "rule": "deposit_cent > final_price_cent（定金大于成交价）",
      "table": "orders",
      "db_blocked": false,
      "api_http": 200,
      "api_note": "定金 999999 > 成交价 1，落库无异常"
    },
    {
      "rule": "reservation_time 在过去（预订过去的日期）",
      "table": "orders",
      "db_blocked": false,
      "api_http": 200,
      "api_note": "预订时间取一年前，接口照常受理并落库"
    },
    {
      "rule": "customer_phone 非手机号格式",
      "table": "orders",
      "db_blocked": false,
      "api_http": 200,
      "api_note": "customer_phone 落库为 'not-a-phone'"
    },
    {
      "rule": "status=COMPLETED 但 deposit_cent=0（未付定金却已完成）",
      "table": "orders",
      "db_blocked": false,
      "api_http": 200,
      "api_note": "订单直接置为已完成，定金为 0"
    }
  ]
}
```

## [MEDIUM] 软删除的行会被同名重建「复活」，id 与 sort_order 沿用旧值

删除分类后用同名重建，命中的是同一条已软删记录（id 不变、sort_order 沿用旧的），而不是新建。DDL 的 UNIQUE(name) 与软删除语义冲突：删除不是删除，是可复活。id 复用会让历史引用（商品、订单快照）指向一个「看起来是新」的实体。

```json
{
  "category": "ai_probe_cat_6f42d7",
  "create": 200,
  "delete": 200,
  "recreate": 200,
  "db_rows": [
    [
      "291",
      "f"
    ]
  ]
}
```

## [MEDIUM] orders 表没有软删/归档，永久增长（当前 184 kB）

products/packages/categories 都有 is_deleted，orders 没有。订单是唯一不设生命周期的表，还带 2 个 jsonb 快照列和 6 个索引 —— 数据量上来后这张表会最先撑爆。

```json
{
  "orders_size": "184 kB",
  "table_sizes": {
    "products": "16 MB",
    "orders": "184 kB",
    "packages": "144 kB",
    "categories": "104 kB",
    "package_items": "104 kB",
    "package_types": "104 kB",
    "flyway_schema_history": "48 kB"
  }
}
```

## [MEDIUM] BanquetOrder 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.BanquetOrder",
  "simple_name": "BanquetOrder",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.BanquetOrder",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] BanquetPackage 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.BanquetPackage",
  "simple_name": "BanquetPackage",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.BanquetPackage",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] Category 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.Category",
  "simple_name": "Category",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.Category",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] PackageItem 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.PackageItem",
  "simple_name": "PackageItem",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.PackageItem",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] PackageType 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.PackageType",
  "simple_name": "PackageType",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.PackageType",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] Product 没有值相等：放进集合或当键，语义会变

实体没有声明 equals/hashCode，用的是 Object 的默认实现 —— 按对象身份比较，不是按业务主键。业务上「同一个客户」在两处会被当成两个对象：Set 去重失效、Map 键匹配不上、List.contains 永远 false。这不是性能问题而是正确性问题，而且编译期与运行期都不会报错。

```json
{
  "class": "com.jiuyan.banquet.entity.Product",
  "simple_name": "Product",
  "declares_equals": false,
  "declares_hashcode": false,
  "file_carrier": {
    "detector": "entity_value_equality",
    "observations": [
      {
        "class": "com.jiuyan.banquet.entity.Product",
        "has_equals": false,
        "has_hashcode": false,
        "two_instances_equal": false
      }
    ]
  }
}
```

## [MEDIUM] orders.address 声明 NOT NULL，实际用空串绕过 —— 非空约束只挡了 NULL

三方声明：DB 列 address 为 NOT NULL，实体 @Column(nullable=false)，但 DTO 的 address 没有任何约束。实测：客户端不传 address，接口返回 200，库里落的是空串 ''。NOT NULL 只证明列不是 NULL，空串照样入库 —— 约束拦不住业务上的空。后果：订单地址为空，宴会执行单拿不到地址，且没有任何一层会报错。

```json
{
  "dto": "CreateOrderRequest",
  "field": "address",
  "column": "address",
  "db_not_null": true,
  "dto_constraints": [],
  "entity_nullable": false,
  "http": 200,
  "db_value": ""
}
```

## [MEDIUM] orders.finalPriceCent 必填性校验落在服务层，DTO 少一条 @NotNull

DB 与实体都声明 NOT NULL，DTO 只写了 @Min(0) 而没有 @NotNull —— 字段在传输层是可空的，靠服务层补一句判断拦住。实测：传 null 返回 422 / PRICE_INVALID，文案是「成交价不能小于 0」。后果一：责任错层，任何绕过服务层的写入方（脚本、数据修复）都会撞 DB 报错；后果二：报错文案说的是「小于 0」，真实原因是「不能为空」，客户端无法据此定位。

```json
{
  "dto": "CreateOrderRequest",
  "field": "finalPriceCent",
  "column": "final_price_cent",
  "dto_constraints": [
    "Min(0)"
  ],
  "db_not_null": true,
  "http": 422,
  "code": "PRICE_INVALID",
  "message": "成交价不能小于 0"
}
```

## [MEDIUM] PUT 是全量覆盖：不传的 imageUrl 会被抹成 NULL

UpdateProductRequest.imageUrl 没有任何约束（可缺省），但 PUT 的语义是全量替换。实测：创建时带图片路径，随后一次不带 imageUrl 的 PUT 之后，image_url 变成 NULL。后果不是校验问题而是数据丢失 —— 磁盘上的图片文件还在，但数据库里的引用没了，文件成为孤儿，前端永远拿不到图。

```json
{
  "entity": "products",
  "id": 4606119,
  "changed_columns": [
    "description",
    "image_url",
    "is_active",
    "name",
    "price_cent",
    "unit",
    "updated_at"
  ],
  "final_row": {
    "id": 4606119,
    "name": "ai_probe_fp2",
    "unit": "克",
    "image_url": null,
    "is_active": false,
    "created_at": "2026-11-08T13:10:13.59774+00:00",
    "is_deleted": true,
    "price_cent": 9999,
    "updated_at": "2026-11-08T13:10:13.960528+00:00",
    "category_id": 2,
    "description": "changed"
  }
}
```

## [LOW] products.is_active 的默认值被声明了三遍

DB 列有 DEFAULT true，DTO 允许缺省，服务层再兜底成 true。实测：缺省时接口返回 200，落库 is_active=True。同一个默认值写三遍，改任何一处（比如把产品默认改成下架）另外两处不会跟着变，且不会有任何报错 —— 属于典型的静默漂移。

```json
{
  "table": "products",
  "column": "is_active",
  "db_default": "true",
  "dto_constraints": [],
  "observed_value": true,
  "http": 200
}
```

## [LOW] packages.is_active 的默认值被声明了三遍

DB 列有 DEFAULT true，DTO 允许缺省，服务层再兜底成 true。实测：缺省时接口返回 200，落库 is_active=True。同一个默认值写三遍，改任何一处（比如把产品默认改成下架）另外两处不会跟着变，且不会有任何报错 —— 属于典型的静默漂移。

```json
{
  "table": "packages",
  "column": "is_active",
  "db_default": "true",
  "dto_constraints": [],
  "observed_value": true,
  "http": 200
}
```

## [LOW] finalPriceCent=null 的报错文案说的是「小于 0」，真实原因是「不能为空」

null 触发的是必填缺失，却复用了一个数值区间的错误文案。客户端拿到 422 后按「价格太小」去提示用户，实际用户根本没填价格。这类错误文案漂移会让前端提示整体失真。

```json
{
  "http": 422,
  "code": "PRICE_INVALID",
  "message": "成交价不能小于 0",
  "label": "finalPriceCent=null（DTO 可空，DB NOT NULL）"
}
```

## [LOW] 订单状态枚举大小写敏感，小写 pending 直接 400

DTO 的 status 字段直接用 OrderStatus 枚举，Jackson 默认大小写敏感。实测传 'pending' 返回 400「订单状态不正确」。对外 API 的枚举值应当约定为大小写不敏感或明确写进 OpenAPI，否则每个新客户端都会踩一次。

```json
{
  "http": 400,
  "code": "ORDER_STATUS_INVALID",
  "message": "订单状态不正确"
}
```

## [INFO] OrderStatus 枚举与 DB CHECK 白名单完全一致（3/3）

这是一次「没有问题」的差分结论 —— 差分引擎既能报问题也能给出通过结论。

```json
{
  "java": [
    "PENDING",
    "DEPOSIT_PAID",
    "COMPLETED"
  ],
  "db": [
    "PENDING",
    "DEPOSIT_PAID",
    "COMPLETED"
  ]
}
```

## [INFO] 可空性三方一致 27/31 列（同向设防 21 · DTO 更严 0 · 都可空 6）

这是差分引擎的通过结论：DTO 的非空声明要么与 DB 一致，要么比 DB 更严（DTO 先拦，留给 DB 兜底），方向正确。不成立的只有下面单列出来的那几列。

```json
{
  "aligned_columns": 27,
  "total_columns": 31,
  "same_direction": 21,
  "dto_stricter": 0,
  "both_nullable": 6
}
```

## [INFO] 8/8 条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合

DB 的 CHECK 不是孤岛：DTO 层有等价声明，两层同时设防。这类对齐决定了改一条业务规则要不要同时动三个文件。

```json
{
  "aligned": [
    {
      "constraint": "chk_orders_table_count",
      "column": "table_count",
      "dto": "CreateOrderRequest.tableCount",
      "match": "tableCount @Min(1)"
    },
    {
      "constraint": "chk_orders_deposit_cent",
      "column": "deposit_cent",
      "dto": "CreateOrderRequest.depositCent",
      "match": "depositCent @Min(0)"
    },
    {
      "constraint": "chk_orders_status",
      "column": "status",
      "dto": "CreateOrderRequest.status",
      "match": "status @NotNull + 枚举 ['PENDING', 'DEPOSIT_PAID', 'COMPLETED']"
    },
    {
      "constraint": "chk_orders_final_price_cent",
      "column": "final_price_cent",
      "dto": "CreateOrderRequest.finalPriceCent",
      "match": "finalPriceCent @Min(0)"
    },
    {
      "constraint": "chk_orders_plan_total_cent",
      "column": "plan_total_cent",
      "dto": "CreateOrderRequest.planTotalCent",
      "match": "planTotalCent @Min(0)"
    },
    {
      "constraint": "chk_package_items_quantity",
      "column": "quantity",
      "dto": "PackageItemRequest.quantity",
      "match": "quantity @Min(1)"
    },
    {
      "constraint": "chk_packages_price_cent",
      "column": "price_cent",
      "dto": "CreatePackageRequest.priceCent",
      "match": "priceCent @Min(0)"
    },
    {
      "constraint": "chk_products_price_cent",
      "column": "price_cent",
      "dto": "CreateProductRequest.priceCent",
      "match": "priceCent @Min(0)"
    }
  ]
}
```

## [INFO] PUT 缺省必填外键被 DTO 的 @NotNull 挡住（400，DB 无脏数据）

categoryId 在 DTO 上有 @NotNull，实测缺省时接口返回 400 且行未被改动。这是可空性差分里方向正确的一例：声明在最靠近输入的地方设防。

```json
{
  "http": 400,
  "changed": []
}
```

## 第二载体复核（文件载体 × 类层载体）

文件载体观测 10 条｜被类层结论独立复现 7 条｜无人认领 0 条｜项目声明不适用 3 条

### 项目声明不适用的观测（决策留痕）

- `encapsulation_leak` → `com.jiuyan.banquet.repository.CategoryRepository.findAllByIsDeletedFalseOrderBySortOrderAscIdAsc` —— Spring Data 仓储方法返回实体是框架的既定用法 —— 仓储本身就是实体的对外接口；封装泄漏的担心针对的是服务层把内部状态交出去
- `encapsulation_leak` → `com.jiuyan.banquet.repository.PackageItemRepository.findByBanquetPackageId` —— Spring Data 仓储方法返回实体是框架的既定用法 —— 仓储本身就是实体的对外接口；封装泄漏的担心针对的是服务层把内部状态交出去
- `encapsulation_leak` → `com.jiuyan.banquet.repository.PackageTypeRepository.findAllByIsDeletedFalseOrderBySortOrderAscIdAsc` —— Spring Data 仓储方法返回实体是框架的既定用法 —— 仓储本身就是实体的对外接口；封装泄漏的担心针对的是服务层把内部状态交出去
