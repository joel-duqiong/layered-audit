# 分层审计 · 根因与覆盖度报告

生成物：/Users/june/.cc-weixin/workspace/layered-audit/pilot/root_causes.json　输入：/Users/june/.cc-weixin/workspace/layered-audit/pilot/all_findings.json

- 发现 83 条 → 根因 63 条（归并 20 条）
- 台账按 kind：确认缺陷 66 / 正面结论 15 / 观测 2（三者不混算）
- **确认缺陷 66 条 / 待确认疑点 12 条 / 已证伪只留观测 8 条** —— 后两者**不在台账 83 条内**，见第八、九节
- 硬违规 1 条；契约缺口（无法归类）9 条
- 闸门覆盖（15 个生产者）：A/B 判据二分 2 / 仅 oracle 4 / **未接 9**；未接名下**未裁定 defect 3 条**（见第十节）

## 一、修复顺序（按根因）

| # | 严重度 | 归属层 | 根因 | 标题 | 发现数 | 置信 | ground_truth | 可跑复现 |
|---|--------|--------|------|------|--------|------|---------------|----------|
| 1 | CRITICAL | service | `authz.no_authorization_layer` | 全系统零鉴权：无凭证即可读写全部业务数据 | 3 | 0.98 | verified | `curl http://127.0.0.1:8081/api/orders   # 无任何凭证` |
| 2 | CRITICAL | function | `upload.content_type_not_sniffed` | 上传只信客户端自报的扩展名与 contentType：任意字节都以 .png 落盘 | 2 | 0.95 | verified | `python3 fn_dynamic.py --case UPL-04` |
| 3 | HIGH | function | `order.item_quantity_unvalidated` | 明细归一化只查重复，不查 productId/quantity：数量可为空、为 0、为负 | 3 | 0.93 | verified | `python3 fn_dynamic.py --case NAI-04` |
| 4 | HIGH | file | `cors.allowed_origin_patterns_too_broad` | CORS 白名单等于「整个内网 + 所有本地端口」，且全是明文 http | 2 | 0.95 | verified | `python3 config_probe.py` |
| 5 | HIGH | service | `errhandler.client_error_mapped_to_500` | 客户端错误被当成服务端错误：返回 [500] | 2 | 0.94 | verified | `curl -X POST 见 observation.json 中 key=POST /api/uploads/p…` |
| 6 | HIGH | function | `order.snapshot.item_ownership_unchecked` | 订单快照：明细归属不校验，空明细静默回退成套餐默认明细 | 2 | 0.94 | verified | `python3 fn_dynamic.py --case APL-17` |
| 7 | HIGH | file | `audit.client_ip_trusted_from_header` | 审计日志里的 clientIp 完全由客户端伪造：X-Forwarded-For 首段直接采信 | 1 | 0.93 | verified | `python3 file_dynamic.py` |
| 8 | HIGH | class | `banquet_package.items_exposed` | com.jiuyan.banquet.entity.BanquetPackage.getItems 把内部实体集合直接交出去 | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 9 | HIGH | service | `cors.empty_whitelist_fails_open` | CORS 白名单配成空值时反而允许任意来源跨域（fail-open） | 1 | 0.93 | verified | `python3 config_probe.py` |
| 10 | HIGH | integration | `frontend.list_hardcoded_limit_100` | 前端列表固定只取前 100 条并丢弃 total，超出的数据永远不可见 | 1 | 0.95 | verified | `python3 integration_dynamic.py --scenario SC05_LIST_TRUNC…` |
| 11 | HIGH | service | `index.idx_products_name_unusable` | DDL 声明的 idx_products_name 对服务层查询模式无效（8.982ms vs 0.015ms，差 599 倍） | 1 | 0.88 | verified | `python3 class_dynamic.py` |
| 12 | HIGH | function | `order_no.no_collision_guarantee` | 订单号每秒只有 1000 个槽位，并发下单必然撞号（唯一键冲突 → 500） | 1 | 0.9 | verified | `python3 fn_dynamic.py --case GON-02` |
| 13 | HIGH | class | `rule.missing_in_db_dto_service` | 1 条业务不变量在 DB / DTO / 服务层三层全都不存在 | 1 | 0.85 | verified | `python3 class_dynamic.py` |
| 14 | MEDIUM | service | `errhandler.internal_detail_leak` | 错误响应把内部实现细节返回给客户端 | 7 | 0.92 | verified | `curl -X PATCH /api/orders/2596/status` |
| 15 | MEDIUM | class | `entity.no_value_equality` | BanquetOrder 没有值相等：放进集合或当键，语义会变 | 6 | 0.8 | verified | `python3 class_dynamic.py` |
| 16 | MEDIUM | integration | `api.snapshot_structure_undeclared` | 前端依赖的订单快照内部结构，在 API 契约里零声明 | 1 | 0.92 | verified | `grep -n 'packageSnapshot' backend/src/main/java/com/jiuya…` |
| 17 | MEDIUM | function | `apply.null_input.npe_instead_of_422` | apply 直接对入参 .trim()：空值触发 NPE（500）而不是业务异常（422） | 1 | 0.93 | verified | `python3 fn_dynamic.py --case APL-02` |
| 18 | MEDIUM | service | `config.binding_priority_surprise` | app.cors-allowed-origins 的绑定优先级与直觉相反：APP_CORS_ALLOWED_ORIGINS 赢了 CORS_ALLOWED_ORIGINS | 1 | 0.88 | verified | `python3 property_binding_probe.py` |
| 19 | MEDIUM | class | `dto.required_check_missing` | orders.finalPriceCent 必填性校验落在服务层，DTO 少一条 @NotNull | 1 | 0.88 | verified | `python3 class_dynamic.py` |
| 20 | MEDIUM | integration | `dto.unknown_field_silent_drop` | 字段名拼错会被静默吞掉，并被「套餐默认明细」悄悄顶替 | 1 | 0.93 | verified | `python3 integration_dynamic.py --scenario SC04_UNKNOWN_FI…` |
| 21 | MEDIUM | file | `errhandler.badrequest_echoes_body` | 400 错误把原始请求体片段原样回显 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 22 | MEDIUM | file | `errhandler.db_constraint_text_leak` | DB 约束冲突的原始错误文本被原样回给客户端 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 23 | MEDIUM | integration | `frontend.image_fallback_cross_domain` | 商品缺图时前端兜底成了套餐种子图 | 1 | 0.88 | verified | `POST /api/products（imageUrl 留空）→ 前端 mapProduct → image 字段…` |
| 24 | MEDIUM | integration | `frontend.item_count_semantics_divergent` | 同一个套餐，界面上有两个互不相同的「商品数」 | 1 | 0.92 | verified | `造一个套餐，明细含一个 is_active=false 的商品 → 对比卡片与弹层的数字` |
| 25 | MEDIUM | function | `oracle.assertion_weak_under_mutation` | 有变异改了实际行为但我们的 oracle 没抓住 —— 断言只看结果，不看「该发生什么」 | 1 | 0.92 | unverified | `python3 mutation_test.py` |
| 26 | MEDIUM | function | `order.apply.amount_invariants_weak` | apply 的金额/状态不变量只查了 >= 0：定金可以大于套餐总价，状态与预订时间可为空 | 1 | 0.92 | verified | `python3 fn_dynamic.py --case APL-12` |
| 27 | MEDIUM | service | `order.create.n_plus_one_per_item` | 下单时每多一个明细就多 2.0 条 SQL（典型 N+1） | 1 | 0.95 | verified | `python3 orm_query_probe.py` |
| 28 | MEDIUM | service | `order.update.no_concurrency_control` | 并发编辑同一订单会静默丢掉对方的修改：没有版本号、没有冲突检测 | 1 | 0.95 | verified | `python3 concurrency_props.py` |
| 29 | MEDIUM | class | `orders.no_data_lifecycle` | orders 表没有软删/归档，永久增长（当前 184 kB） | 1 | 0.8 | verified | `python3 class_dynamic.py` |
| 30 | MEDIUM | file | `package.applyItems.mutate_before_validate` | PackageService.applyItems 先删后校验：明细被清空后才去查商品 | 1 | 0.91 | verified | `python3 file_dynamic.py` |
| 31 | MEDIUM | service | `package.list.n_plus_one_per_package` | 套餐列表每多返回 1 个套餐就多 8.0 条 SQL | 1 | 0.93 | verified | `python3 orm_query_probe.py` |
| 32 | MEDIUM | class | `service.public_api_returns_jpa_entity` | service 层把 JPA 实体当 public API 返回：调用方能绕过全部业务校验直接改实体 | 1 | 0.88 | verified | `python3 java_feature_scan.py` |
| 33 | MEDIUM | class | `softdelete.resurrects_row` | 软删除的行会被同名重建「复活」，id 与 sort_order 沿用旧值 | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 34 | MEDIUM | class | `spring.self_invocation_bypasses_proxy` | 同类内部直接调用被切面标注的方法 → 绕过 Spring 代理，切面语义不生效 | 1 | 0.9 | verified | `python3 java_feature_scan.py` |
| 35 | MEDIUM | integration | `tooling.coverage_recording_gap` | 1/25 个接口既无真实流量样本、前端也不调用；17 个写接口的真实流量样本为 0 | 1 | 0.85 | verified | `见 /Users/june/tests/auto-api/recordings/jiuyan_readonly.j…` |
| 36 | MEDIUM | file | `traceid.client_controlled_no_validation` | X-Trace-Id 完全由客户端指定，无格式与长度白名单，原样进日志与响应头 | 1 | 0.88 | verified | `python3 file_dynamic.py` |
| 37 | MEDIUM | file | `upload.limit_declared_in_three_places` | 上传体积上限 5MB 这个数字散落三处，改一处另两处就开始说谎 | 1 | 0.95 | verified | `python3 file_dynamic.py` |
| 38 | MEDIUM | function | `upload.no_size_limit` | 上传无体积上限：单请求 20MB 被完整写盘（上传放大 DoS） | 1 | 0.91 | verified | `python3 fn_dynamic.py --case UPL-06` |
| 39 | MEDIUM | file | `webconfig.static_root_normalization_env_dependent` | 静态资源根目录的规范化依赖「目录在启动前就存在」这个环境偶然 | 1 | 0.72 | unverified | `python3 file_dynamic.py` |
| 40 | LOW | class | `column.default_declared_in_three_places` | products.is_active 的默认值被声明了三遍 | 2 | 0.86 | verified | `python3 class_dynamic.py` |
| 41 | LOW | integration | `api.dead_endpoints` | 后端 4 个接口前端从不调用 | 1 | 0.9 | verified | `grep -n 'api/' /Users/june/jiuyan/src/api/banquetApi.js` |
| 42 | LOW | service | `config.bad_upload_dir_kills_startup` | 上传目录配错会让整个后端起不来，而不是降级运行 | 1 | 0.85 | verified | `python3 config_probe.py` |
| 43 | LOW | file | `errhandler.response_shape_inconsistent` | 同一个异常处理文件里三种响应形状：信封 / 空体 / 未知 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 44 | LOW | class | `error.message_mismatch` | finalPriceCent=null 的报错文案说的是「小于 0」，真实原因是「不能为空」 | 1 | 0.85 | verified | `python3 class_dynamic.py` |
| 45 | LOW | function | `money_input.normalization_silently_mutates_value` | 金额输入框静默改变用户输入的数值：负号被吞、超过 2 位小数被截断 | 1 | 0.95 | verified | `python3 numeric_props.py` |
| 46 | LOW | function | `numeric.amount_exceeds_js_safe_integer` | 金额超过 2^53 分时，前端显示的金额与库里存的不一致（后端金额无上界是前提） | 1 | 0.85 | verified | `python3 numeric_props.py` |
| 47 | LOW | file | `service.helpers.duplicated` | 分页归一化等 4 组方法在 3 个 service 文件里逐字复制 | 1 | 0.93 | verified | `python3 file_dynamic.py` |
| 48 | INFO | class | `check.dto_aligned` | 8/8 条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合 | 1 | 0.92 | verified | `python3 class_dynamic.py` |
| 49 | INFO | file | `cors.negative_precision_proven` | CORS 白名单是精确匹配，不是通配全放 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 50 | INFO | file | `dead_private_method.none` | 没有真正无人调用的私有方法 | 1 | 0.88 | verified | `python3 file_dynamic.py` |
| 51 | INFO | class | `enum.check_aligned` | OrderStatus 枚举与 DB CHECK 白名单完全一致（3/3） | 1 | 0.95 | verified | `python3 class_dynamic.py` |
| 52 | INFO | function | `money_conversion.roundtrip_exact` | 前端金额换算在合理量级内精确闭合（分→元→分、元→分、显示精度三条性质全过） | 1 | 0.9 | verified | `python3 numeric_props.py` |
| 53 | INFO | class | `nullability.aligned` | 可空性三方一致 27/31 列（同向设防 21 · DTO 更严 0 · 都可空 6） | 1 | 0.93 | verified | `python3 class_dynamic.py` |
| 54 | INFO | function | `oracle.mutation_harness_operational` | 变异测试链路跑通：函数层 oracle 抓得住大部分变异 | 1 | 0.9 | verified | `python3 mutation_test.py` |
| 55 | INFO | service | `order.amount_consistent_when_not_overridden` | 未使用成交价时，订单金额是否等于套餐总价？ | 1 | 0.9 | verified | `python3 business_probe.py` |
| 56 | INFO | service | `order.update.transaction_boundary_holds` | 被拒的写请求不会在订单行留下部分写入（事务边界有效） | 1 | 0.9 | verified | `python3 concurrency_props.py` |
| 57 | INFO | class | `put.required_fk_guarded` | PUT 缺省必填外键被 DTO 的 @NotNull 挡住（400，DB 无脏数据） | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 58 | INFO | service | `spring.transaction_effectiveness_verified` | 静态声明的事务与运行时实际生效的事务完全一致（差分为空） | 1 | 0.93 | verified | `python3 transaction_probe.py` |
| 59 | INFO | file | `static_oracle.blind_spot_compile_time_constant` | 静态 oracle 的一处已知盲区：编译期常量内联 | 1 | 0.8 | unverified | `python3 file_dynamic.py` |
| 60 | INFO | file | `static_root.same_root` | 静态映射根与上传根同源（manifest 假设被证伪） | 1 | 0.85 | verified | `python3 file_dynamic.py` |
| 61 | INFO | file | `txn.boundary_complete` | 全仓写方法的事务边界一个不缺 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 62 | INFO | file | `txn.rollback_proven` | 事务边界实测有效：中途失败不留残局 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 63 | INFO | service | `upload.error.no_path_leak` | 上传失败时错误响应不泄露服务器文件系统路径 | 1 | 0.9 | verified | `python3 config_probe.py` |

## 二、硬违规（违反 attribution.rule / out_of_scope）

| 根因 | 归属层 | 违规 |
|------|--------|------|
| `index.idx_products_name_unusable` | service | 越界开单：class 层的 out_of_scope 明确含 service_latency，该条应由 service 层开单 |
| `index.idx_products_name_unusable` | service | 说明：合法上提：开单层 ['class'] → owner service（契约 escalation_to 允许） |

## 三、归并提示（同层同根因多开）

| 根因 | 归属层 | 说明 |
|------|--------|------|
| `errhandler.client_error_mapped_to_500` | service | service 层同一根因开了 2 条 own，可归并为 1 条根因 |
| `errhandler.internal_detail_leak` | service | service 层同一根因开了 7 条 own，可归并为 1 条根因 |
| `entity.no_value_equality` | class | class 层同一根因开了 6 条 own，可归并为 1 条根因 |
| `column.default_declared_in_three_places` | class | class 层同一根因开了 2 条 own，可归并为 1 条根因 |

## 四、跨根因关联

| 根因 | 关联到 |
|------|--------|
| `authz.no_authorization_layer` | `order.snapshot.item_ownership_unchecked` |
| `order.snapshot.item_ownership_unchecked` | `order.item_quantity_unvalidated` |

## 五、覆盖度矩阵（层 × 契约声明的 category）

**function**

| category | 命中 | 状态 |
|----------|------|------|
| boundary | 9 | ok |
| complexity | 0 | 零覆盖 —— 没人管 |
| exception_path | 1 | ok |
| numeric | 3 | ok |

**file**

| category | 命中 | 状态 |
|----------|------|------|
| state_consistency | 2 | ok |
| error_handling_pattern | 5 | ok |
| cohesion | 3 | ok |
| contract_alignment | 0 | 零覆盖 —— 没人管 |

**class**

| category | 命中 | 状态 |
|----------|------|------|
| encapsulation_leak | 2 | ok |
| invariant | 10 | ok |
| contract_violation | 6 | ok |
| inheritance_design | 0 | 零覆盖 —— 没人管 |
| architecture_contract | 0 | 零覆盖 —— 没人管 |

**service**

| category | 命中 | 状态 |
|----------|------|------|
| interface_contract | 11 | ok |
| resilience | 1 | ok |
| authz | 3 | ok |
| data_consistency | 4 | ok |
| resource_leak | 0 | 零覆盖 —— 没人管 |
| service_latency | 2 | ok |

**integration**

| category | 命中 | 状态 |
|----------|------|------|
| end_to_end_business | 9 | ok |
| cross_service_performance | 0 | 零覆盖 —— 没人管 |
| cross_service_attack_chain | 2 | ok |
| cross_layer_correlation | 0 | 零覆盖 —— 没人管 |
| emergent_masking | 0 | 零覆盖 —— 没人管 |

## 六、契约缺口（category 装不下）

合计 9 条，按层：{'file': 6, 'class': 2, 'integration': 1}

| uid | 建议类别 |
|-----|----------|
| `file:FILE-CORS-INTRANET-WIDE#0` | 见 all_findings.json 的 suggested_category |
| `file:FILE-XFF-CLIENT-IP-SPOOF#1` | 见 all_findings.json 的 suggested_category |
| `file:FILE-TRACEID-CLIENT-CONTROLLED#6` | 见 all_findings.json 的 suggested_category |
| `file:FILE-CORS-NEGATIVE-PRECISION#10` | 见 all_findings.json 的 suggested_category |
| `file:FILE-STATIC-ORACLE-BLIND-SPOT#13` | 见 all_findings.json 的 suggested_category |
| `file:FILE-STATIC-SAME-ROOT#14` | 见 all_findings.json 的 suggested_category |
| `class:CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES#12` | 见 all_findings.json 的 suggested_category |
| `class:CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES#13` | 见 all_findings.json 的 suggested_category |
| `integration:RECORDING_COVERAGE_GAP#8` | 见 all_findings.json 的 suggested_category |

## 七、同一文件承载多个根因（文件层内聚信号）

| 文件 | 根因数 | 根因 |
|------|--------|------|
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/OrderController.java` | 9 | `api.snapshot_structure_undeclared`, `dto.unknown_field_silent_drop`, `errhandler.internal_detail_leak`, `order.amount_consistent_when_not_overridden`, `order.create.n_plus_one_per_item`, `order.item_quantity_unvalidated`, `order.snapshot.item_ownership_unchecked`, `order.update.no_concurrency_control`, `order.update.transaction_boundary_holds` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/OrderService.java` | 6 | `apply.null_input.npe_instead_of_422`, `order.apply.amount_invariants_weak`, `order.item_quantity_unvalidated`, `order.snapshot.item_ownership_unchecked`, `order_no.no_collision_guarantee`, `service.helpers.duplicated` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/BanquetOrder.java` | 4 | `dto.required_check_missing`, `entity.no_value_equality`, `error.message_mismatch`, `orders.no_data_lifecycle` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/UploadService.java` | 3 | `upload.content_type_not_sniffed`, `upload.limit_declared_in_three_places`, `upload.no_size_limit` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/exception/GlobalExceptionHandler.java` | 3 | `errhandler.badrequest_echoes_body`, `errhandler.db_constraint_text_leak`, `errhandler.response_shape_inconsistent` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/PackageService.java` | 3 | `package.applyItems.mutate_before_validate`, `service.public_api_returns_jpa_entity`, `spring.self_invocation_bypasses_proxy` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/PackageController.java` | 3 | `errhandler.internal_detail_leak`, `package.list.n_plus_one_per_package`, `tooling.coverage_recording_gap` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/config/WebConfig.java` | 2 | `cors.allowed_origin_patterns_too_broad`, `webconfig.static_root_normalization_env_dependent` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/config/TraceIdFilter.java` | 2 | `audit.client_ip_trusted_from_header`, `traceid.client_controlled_no_validation` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/ProductController.java` | 2 | `errhandler.internal_detail_leak`, `frontend.list_hardcoded_limit_100` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/Product.java` | 2 | `column.default_declared_in_three_places`, `index.idx_products_name_unusable` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/BanquetPackage.java` | 2 | `banquet_package.items_exposed`, `column.default_declared_in_three_places` |
| `/Users/june/jiuyan/src/api/banquetApi.js` | 2 | `money_conversion.roundtrip_exact`, `numeric.amount_exceeds_js_safe_integer` |

## 八、待确认疑点（规范层风险 · 需业务方确认）

这些结论的**观测是真的**，但「所以这是缺陷」所依赖的业务预期**没有文档/业务方依据**。它们是「不知道是否成立，需业务方确认」——**既不是缺陷，也不是已排除**。不得计入缺陷分子，也不得当成已澄清而忽略。

合计 **12 条**（不在缺陷台账内）；按层：class 3、file 1、function 2、integration 2、ledger 1、service 3


### [function] `FN-PRICE-CLIENT-CONTROLLED`

- **预期原文**：成交价由客户端决定且不与套餐价比对（文档化设计；价格篡改是规范层风险，待业务确认）
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（现有文档只规定金额 >= 0）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：「成交价不得高于套餐标价 / 不得由客户端决定」是推断的业务预期，不是规范违背。项目对同一条预期的权威声明在 invariants.json 的 BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE，同样 source=inferred / confirmation=unconfirmed，服务层已正确压进 business_findings.json 的 unconfirmed_suspects —— 函数层与集成层此前绕过该声明直接开 CRITICAL/HIGH defect。severity 保留 CRITICAL 作为**风险评级**（进待确认疑点清单），不是缺陷严重度；服务层对同一预期评 HIGH，跨层评级口径不一致待编排者裁定。价格篡改风险真实存在，不得吞掉：观测与证据原样留痕，等业务确认是否要求上界。
- **风险评级**：CRITICAL（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
[
  {
    "case_id": "APL-15",
    "function": "com.jiuyan.banquet.service.OrderService#apply",
    "invariant": "落库的成交价不得高于套餐总价",
    "actual": "期望 finalPriceCent <= 46800，实际 999999",
    "exception": null
  },
  {
    "case_id": "RFP-05",
    "function": "com.jiuyan.banquet.service.OrderService#resolveFinalPrice",
    "invariant": "成交价不得低于套餐总价（客户端不能自降定价权）",
    "actual": "期望 >= 46800，实际 1",
    "exception": null
  },
  {
    "case_id": "RFP-06",
    "function": "com.jiuyan.banquet.service.OrderService#resolveFinalPrice",
    "invariant": "成交价不得高于套餐总价",
    "actual": "期望 <= 46800，实际 999999",
    "exception": null
  },
  {
    "case_id": "RFP-07",
    "function": "com.jiuyan.banquet.service.OrderService#resolveFinalPrice",
    "invariant": "成交价不得为 0（0 元成交无任何业务依据）",
    "actual": "期望 >= 1，实际 0",
    "exception": null
  }
]
```
- **来源**：`fn_rule_suspects.json`

### [function] `OB-RESOLVE-PRICE-UPPER-BOUND`

- **预期原文**：职责断言（待确认）：成交价不得高于套餐标价 —— 无文档依据，属规范层风险
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（现有文档只规定金额 >= 0）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：source=declared 只表示这条职责是人写下来的，不表示业务方确认过。本条断言的「成交价不得高于套餐标价」与 invariants.json 的 BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE 是同一条预期，后者的 oracle 明确是 inferred/unconfirmed/「无文档依据」，服务层已压进 unconfirmed_suspects。规范违背不成立：业务校验对照.md:20 在「已确认一致的规则」表里记 resolveFinalPrice「一致」，API.md:338 规定 useFinalPrice=true 时保存请求中的 finalPriceCent。断言继续执行以产出观测，但结论进待确认疑点清单，不以 defect 进台账。
- **风险评级**：HIGH（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
[
  {
    "case_id": "OB-RESOLVE-PRICE-UPPER-BOUND",
    "function": "com.jiuyan.banquet.service.OrderService#resolveFinalPrice",
    "invariant": "计算订单最终成交价：客户端报的价格高于套餐标价时，必须被拒绝或限到套餐价以内",
    "actual": "期望 <= 46800，实际 999999",
    "exception": null
  }
]
```
- **来源**：`fn_rule_suspects.json`

### [file] `FILE-NULL-BODY-WRAPPED-AS-SUCCESS`

- **预期原文**：null 响应体也被包成 success=true 的信封（data 字段缺失）
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无明文依据（AGENTS.md:100 只要求统一 JSON；:104-113 成功示例含 data，但未明文规定 data 必须存在）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：实测链路无害：499 走 ResponseEntity<Void>，协商不出 JSON content type，不会进包装分支。观测（success=true 但缺 data）保留，是否要求 data 恒存在需业务方确认。
- **风险评级**：LOW（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "null_body_json": "{\"success\":true,\"code\":\"OK\",\"message\":\"成功\",\"traceId\":\"FILELAYER-CONTEXT-TRACE\",\"timestamp\":1794675282.600868000}",
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
- **来源**：`file_rule_suspects.json`

### [class] `（无 id）status=COMPLETED 但 deposit_cent=0（未付定金却已完成）`

- **预期原文**：status=COMPLETED 但 deposit_cent=0（未付定金却已完成）
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：（未给出处）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：文档未定义此规则；第 64 行反而把「订单状态流转限制：当前允许任意状态直接切换」列为未定义。属推断预期，待业务确认
- **观测证据**：

```json
{
  "table": "orders",
  "api_http": 200,
  "api_note": "订单直接置为已完成，定金为 0"
}
```
- **来源**：`class_rule_suspects.json`

### [class] `CLASS-PUT-WIPES-UNSET-FIELD`

- **预期原文**：PUT 是全量覆盖：不传的 imageUrl 会被抹成 NULL
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（API.md:202 定义 PUT 请求体同新增；未规定不传字段须保留原值）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：实测：创建时带图片路径，一次不带 imageUrl 的 PUT 之后 image_url 变 NULL，磁盘文件成为孤儿。观测为真且后果值得业务方知道，但「PUT 应保留未传字段」等价于要求 PATCH 语义，文档未规定 —— 进待确认疑点，由业务方决定是否改契约或改用 PATCH。不以 defect 进台账。
- **风险评级**：MEDIUM（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "entity": "products",
  "id": 5857503,
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
    "id": 5857503,
    "name": "ai_probe_fp2",
    "unit": "克",
    "image_url": null,
    "is_active": false,
    "created_at": "2026-11-14T16:55:33.771199+00:00",
    "is_deleted": true,
    "price_cent": 9999,
    "updated_at": "2026-11-14T16:55:34.119342+00:00",
    "category_id": 2,
    "description": "changed"
  }
}
```
- **来源**：`class_rule_suspects.json`

### [class] `CLASS-ENUM-CASE-SENSITIVE`

- **预期原文**：订单状态枚举大小写敏感，小写 pending 直接 400
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（API.md 只定义大写枚举值，未规定大小写不敏感）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：实测传 'pending' 返回 400 ORDER_STATUS_INVALID。原 detail 自己写的是「应当约定为大小写不敏感或明确写进 OpenAPI」—— 这是建议而非既有规则。观测保留，是否放宽容错需业务方确认。
- **风险评级**：LOW（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "http": 400,
  "code": "ORDER_STATUS_INVALID",
  "message": "订单状态不正确"
}
```
- **来源**：`class_rule_suspects.json`

### [service] `BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE`

- **预期原文**：订单金额是否不超过所选套餐的标价？
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（现有文档只规定金额 >= 0）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：推断：成交价不应高于套餐标价。听起来合理，但文档没写 —— 进待确认清单
- **风险评级**：HIGH（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "invariant": {
    "id": "BI-FINAL-AMOUNT-LTE-PACKAGE-PRICE",
    "severity": "HIGH",
    "category": "service.data_consistency",
    "claim": "订单金额是否不超过所选套餐的标价？",
    "why": "套餐价是系统里唯一的商品价格基准。订单金额高于套餐标价，等于凭空多收钱。",
    "assert": {
      "type": "field_lte_field",
      "left": "final_price_cent",
      "right": "package_price_cent"
    },
    "oracle": {
      "source": "inferred",
      "confirmation": "unconfirmed",
      "reference": "无文档依据（现有文档只规定金额 >= 0）",
      "note": "推断：成交价不应高于套餐标价。听起来合理，但文档没写 —— 进待确认清单"
    }
  },
  "checked": 6,
  "violations": 1,
  "samples": [
    {
      "row_id": 2633,
      "type": "field_lte_field",
      "left": 999999999,
      "right": 46800,
      "verdict": "fail",
      "ok": false,
      "scenario": "over_pkg_price"
    }
  ],
  "scenarios": [
    {
      "scenario": "clean",
      "order_id": 2630
    },
    {
      "scenario": "deposit_over",
      "order_id": 2631
    },
    {
      "scenario": "amount_ne_detail",
      "order_id": 2632
    },
    {
      "scenario": "over_pkg_price",
      "order_id": 2633
    },
    {
      "scenario": "wrong_btype",
      "order_id": 2634
    },
    {
      "scenario": "foreign_item",
      "order_id": 2635
    }
  ]
}
```
- **复现**：`python3 business_probe.py`
- **来源**：`business_findings.json`

### [service] `BI-BANQUET-TYPE-MATCH-PACKAGE`

- **预期原文**：订单的宴会类型是否与所选套餐的宴会类型一致？
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：推断：订单宴会类型应与套餐一致。文档没写 —— 进待确认清单
- **风险评级**：MEDIUM（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "invariant": {
    "id": "BI-BANQUET-TYPE-MATCH-PACKAGE",
    "severity": "MEDIUM",
    "category": "service.data_consistency",
    "claim": "订单的宴会类型是否与所选套餐的宴会类型一致？",
    "why": "套餐带 banquetType（婚宴 / 满月酒 / 寿宴这类分类），订单也带 banquetType。两者指向同一场宴席就必须一致 —— 不一致就是数据自相矛盾（订单说婚宴、套餐说寿宴）。",
    "assert": {
      "type": "field_eq_field",
      "left": "banquet_type",
      "right": "package_banquet_type"
    },
    "oracle": {
      "source": "inferred",
      "confirmation": "unconfirmed",
      "reference": "无文档依据",
      "note": "推断：订单宴会类型应与套餐一致。文档没写 —— 进待确认清单"
    }
  },
  "checked": 6,
  "violations": 1,
  "samples": [
    {
      "row_id": 2634,
      "type": "field_eq_field",
      "left": "寿宴",
      "right": "升学宴",
      "verdict": "fail",
      "ok": false,
      "scenario": "wrong_btype"
    }
  ],
  "scenarios": [
    {
      "scenario": "clean",
      "order_id": 2630
    },
    {
      "scenario": "deposit_over",
      "order_id": 2631
    },
    {
      "scenario": "amount_ne_detail",
      "order_id": 2632
    },
    {
      "scenario": "over_pkg_price",
      "order_id": 2633
    },
    {
      "scenario": "wrong_btype",
      "order_id": 2634
    },
    {
      "scenario": "foreign_item",
      "order_id": 2635
    }
  ]
}
```
- **复现**：`python3 business_probe.py`
- **来源**：`business_findings.json`

### [service] `BI-SNAPSHOT-ITEMS-SUBSET-OF-PACKAGE`

- **预期原文**：订单快照里的明细是否都属于所选套餐？
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：文档只说「订单保存商品快照」，未说明细必须属于套餐
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：推断：快照明细应属于所选套餐。相关文档把「无套餐订单误传套餐商品」列为「疑似缺陷，需产品确认」—— 同样是待确认
- **风险评级**：HIGH（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "invariant": {
    "id": "BI-SNAPSHOT-ITEMS-SUBSET-OF-PACKAGE",
    "severity": "HIGH",
    "category": "service.data_consistency",
    "claim": "订单快照里的明细是否都属于所选套餐？",
    "why": "快照是订单金额与交付内容的凭据。混进不属于该套餐的商品，快照就不再代表这份套餐。",
    "assert": {
      "type": "jsonb_subset_of",
      "items_path": "package_snapshot.items",
      "id_field": "productId",
      "ref_path": "package_items",
      "ref_id_field": "productId"
    },
    "oracle": {
      "source": "inferred",
      "confirmation": "unconfirmed",
      "reference": "文档只说「订单保存商品快照」，未说明细必须属于套餐",
      "note": "推断：快照明细应属于所选套餐。相关文档把「无套餐订单误传套餐商品」列为「疑似缺陷，需产品确认」—— 同样是待确认"
    }
  },
  "checked": 6,
  "violations": 1,
  "samples": [
    {
      "row_id": 2635,
      "type": "jsonb_subset_of",
      "items_count": 1,
      "ref_count": 10,
      "item_ids": [
        30
      ],
      "ref_ids": [
        1,
        10,
        11,
        13,
        19,
        21,
        25,
        26,
        4,
        7
      ],
      "outside_ref": [
        30
      ],
      "verdict": "fail",
      "ok": false,
      "scenario": "foreign_item"
    }
  ],
  "scenarios": [
    {
      "scenario": "clean",
      "order_id": 2630
    },
    {
      "scenario": "deposit_over",
      "order_id": 2631
    },
    {
      "scenario": "amount_ne_detail",
      "order_id": 2632
    },
    {
      "scenario": "over_pkg_price",
      "order_id": 2633
    },
    {
      "scenario": "wrong_btype",
      "order_id": 2634
    },
    {
      "scenario": "foreign_item",
      "order_id": 2635
    }
  ]
}
```
- **复现**：`python3 business_probe.py`
- **来源**：`business_findings.json`

### [integration] `PRICE_CLIENT_CONTROLLED_XL`

- **预期原文**：同一订单里并存两个互相矛盾的价格：成交价 0.01 元、套餐价 468.00 元
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（现有文档只规定金额 >= 0）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：「成交价不得高于套餐价 / 多个价格必须相等」是推断的业务预期。业务校验对照.md:20 在「已确认一致的规则」表里记 resolveFinalPrice「一致」，API.md:337-338 与 AGENTS.md:425-427 规定 useFinalPrice=true 时保存请求中的 finalPriceCent，且 AGENTS.md:422-424 要求界面同时保留「方案合计」与「成交价」—— 两个价格并存且不等是设计，不是矛盾。价格篡改是真实的规范层风险，进待确认疑点清单等业务确认，不以 defect 进台账。
- **风险评级**：CRITICAL（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "final_price_cent": 1,
  "snapshot_price_cent": 46800,
  "order_id": 2614,
  "root_cause": "OrderService.resolveFinalPrice 直接采纳客户端 finalPriceCent",
  "related_lower_layer": "服务层 test_price_is_not_client_controlled 已报根因，本条只补集成症状"
}
```
- **复现**：`python3 integration_dynamic.py --scenario SC01_CLIENT_CONTROLLED_PRICE`
- **来源**：`integration_rule_suspects.json`

### [integration] `INT-THREE-PRICES-NO-DETAIL-TOTAL`

- **预期原文**：同一订单并存三个互不相等的价格，界面只展示其中两个，明细合计从不显示
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（现有文档只规定金额 >= 0）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：「成交价不得高于套餐价 / 多个价格必须相等」是推断的业务预期。业务校验对照.md:20 在「已确认一致的规则」表里记 resolveFinalPrice「一致」，API.md:337-338 与 AGENTS.md:425-427 规定 useFinalPrice=true 时保存请求中的 finalPriceCent，且 AGENTS.md:422-424 要求界面同时保留「方案合计」与「成交价」—— 两个价格并存且不等是设计，不是矛盾。价格篡改是真实的规范层风险，进待确认疑点清单等业务确认，不以 defect 进台账。
- **风险评级**：HIGH（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "chain_steps": [
    {
      "n": 1,
      "method": "PUT",
      "path": "/api/orders/2621",
      "status": 200,
      "evidence": "匿名把明细数量改成 99，成交价保持 1 分"
    },
    {
      "n": 2,
      "method": "GET",
      "path": "/api/orders/2621",
      "status": 200,
      "evidence": "订单金额 0.01 元 / 明细合计 4950.0 元 / 套餐标价 88.88 元 —— 三个数字互不相等"
    },
    {
      "n": 3,
      "method": "前端",
      "path": "App.jsx 订单详情抽屉",
      "status": null,
      "evidence": "抽屉只渲染 已选套餐 / 加购商品 / 订单金额 / 定金，明细单价与明细合计一律不渲染，员工在界面上无法核对订单金额的构成"
    }
  ],
  "evidence": "订单金额 0.01 元 vs 明细合计 4950.0 元 vs 套餐标价 88.88 元"
}
```
- **复现**：`python3 integration_chain.py --chain CHAIN-D`
- **来源**：`integration_rule_suspects.json`

### [ledger] `SVC-POST-NOT-IDEMPOTENT`

- **预期原文**：下单接口没有幂等：同一个请求重发一次就产生两个订单
- **oracle 状态**：`unconfirmed`（source=`inferred`）
- **reference**：无文档依据（全语料未声明下单幂等性要求；uk_orders_order_no 只保证订单号唯一，不保证业务去重）
- **为何不进台账**：推断的业务预期，没有文档/业务方依据 —— **不知道是否成立，需业务方确认**。既不是缺陷，也不是已排除：观测为真，但「所以这是缺陷」这一步缺来源。
- **裁定说明**：实测同一 POST 重发两次产生两张订单（2638/2639）为真，零鉴权下也可被灌单。这是真实的规范层风险，但是否要求幂等键/业务去重需业务方确认 —— 进待确认疑点，不以 defect 进台账，也不当成已排除。
- **风险评级**：MEDIUM（这是风险优先级，**不是缺陷严重度**，不参与缺陷计数）
- **观测证据**：

```json
{
  "evidence_refs": [
    "concurrency_findings.json#SVC-POST-NOT-IDEMPOTENT"
  ],
  "location": {
    "file": "/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/OrderController.java",
    "line": 52,
    "symbol": "POST /api/orders",
    "endpoint": "POST /api/orders"
  },
  "source": {
    "file": "concurrency_findings.json",
    "index": 2
  }
}
```
- **复现**：`python3 concurrency_props.py`
- **来源**：`ledger_rule_suspects.json`

## 九、已证伪（文档明确否定 · 只保留观测）

这些结论所依赖的业务预期被**文档明确否定**（例如文档写明该字段可为空、或该规则「当前未定义」）。观测保留以便复核，**不判缺陷、也不再等业务确认**。

合计 **8 条**（不在缺陷台账内）；按层：class 4、file 1、ledger 2、service 1


### [file] `FILE-VALIDATION-STATUS-SPLIT`

- **预期原文**：参数校验分散在 DTO 与 service 两个文件，分别返回 400 与 422（文档定义的两种不同类别，非缺陷）
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/jiuyan/AGENTS.md:153-156
- **原文引用**：- 400: 参数错误 / - 404: 资源不存在 / - 409: 业务冲突，例如订单号重复 / - 422: 业务校验失败
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：权威对照表两处明确记「一致」：业务校验对照.md:14「HTTP 400 参数错误 / 404 资源不存在 / 422 业务校验失败 / 409 冲突 | GlobalExceptionHandler 实测 | 一致」；:24「套餐商品不可重复（同套餐同商品唯一）| DB unique + Service 422 | 一致」。原结论的前提「两者在语义上都是请求参数不合法」被文档明确否定 —— 422 那侧是业务校验失败，不是参数错误。观测（两个状态码并存）保留，缺陷断言撤销。
- **原结论严重度（已撤销）**：MEDIUM（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

```json
{
  "dto_validation_status": 400,
  "dto_validation_body": "{\"success\":false,\"code\":\"VALIDATION_ERROR\",\"message\":\"参数错误\",\"details\":{\"items[0].quantity\":\"must be greater than or equal to 1\"},\"traceId\":\"f0adf04f-e73b-4c12-a1f9-5f474d24ad5c\",\"timestamp\":\"2026-11-15T00:54:50.337735131+08:00\"}",
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
- **来源**：`file_rule_suspects.json`

### [class] `（无 id）deposit_cent > final_price_cent（定金大于成交价）`

- **预期原文**：deposit_cent > final_price_cent（定金大于成交价）
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/tests/auto-api/docs/业务校验对照.md 第 65 行
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：文档「当前未定义、实现也未校验的规则」明列：定金与成交价的大小关系，当前只要求各自 >= 0
- **观测证据**：

```json
{
  "table": "orders",
  "api_http": 200,
  "api_note": "定金 999999 > 成交价 1，落库无异常"
}
```
- **来源**：`class_rule_suspects.json`

### [class] `（无 id）reservation_time 在过去（预订过去的日期）`

- **预期原文**：reservation_time 在过去（预订过去的日期）
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/tests/auto-api/docs/业务校验对照.md 第 62 行
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：文档「当前未定义」明列：预约时间是否允许过去时间，当前只要求可解析的 OffsetDateTime
- **观测证据**：

```json
{
  "table": "orders",
  "api_http": 200,
  "api_note": "预订时间取一年前，接口照常受理并落库"
}
```
- **来源**：`class_rule_suspects.json`

### [class] `CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING`

- **预期原文**：orders.address 的 NOT NULL 只挡 NULL：缺省地址落空串（文档与验收用例声明的有意设计）
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/jiuyan/docs/API.md:307
- **原文引用**：`address` 可为空；为空时后端保存为空字符串，避免前端非必填地址导致订单生成失败。
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：带理由的有意设计，不是疏漏。技术约束（NOT NULL / nullable=false）被空串满足，不等于缺陷；业务意图（地址非空白）没有任何来源声明，反而被文档与可执行验收用例否定。原结论还编造了后果「宴会执行单拿不到地址」——「执行单」在被测仓库全文 0 命中，前端 App.jsx:2411 反而显式把空地址渲染成「无」。观测（缺省地址落空串）保留，缺陷断言撤销。
- **原结论严重度（已撤销）**：INFO（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

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
- **来源**：`class_rule_suspects.json`

### [class] `no_state_machine`

- **预期原文**：状态机缺失：9/9 种迁移全部被接受，含回退
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/tests/auto-api/docs/业务校验对照.md 第 64 行
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：文档「当前未定义、实现也未校验的规则」明列：订单状态流转限制，当前允许任意状态直接切换。9/9 迁移被接受是客观观测，但「所以这是缺陷」需要业务预期，而该预期被文档明确否定。
- **原结论严重度（已撤销）**：HIGH（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

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
- **来源**：`class_rule_suspects.json`

### [service] `BI-DEPOSIT-LTE-FINAL-AMOUNT`

- **预期原文**：订单定金是否不得超过订单本身的金额？
- **oracle 状态**：`refuted`（source=`document`）
- **reference**：/Users/june/tests/auto-api/docs/业务校验对照.md（「当前未定义、实现也未校验的规则」：定金与成交价的大小关系：当前只要求各自 >= 0）
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：项目文档**明确说明**定金与成交价的大小关系尚未定义。用这条判定会得到「编造的业务预期」—— 只保留观测，不进缺陷台账
- **原结论严重度（已撤销）**：HIGH（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

```json
{
  "invariant": {
    "id": "BI-DEPOSIT-LTE-FINAL-AMOUNT",
    "severity": "HIGH",
    "category": "service.data_consistency",
    "claim": "订单定金是否不得超过订单本身的金额？",
    "why": "定金是订单金额里的一部分。定金大于订单金额，业务上不可能成立 —— 系统没有任何折扣或溢价模型能解释它。",
    "assert": {
      "type": "field_lte_field",
      "left": "deposit_cent",
      "right": "final_price_cent"
    },
    "oracle": {
      "source": "document",
      "confirmation": "refuted",
      "reference": "/Users/june/tests/auto-api/docs/业务校验对照.md（「当前未定义、实现也未校验的规则」：定金与成交价的大小关系：当前只要求各自 >= 0）",
      "note": "项目文档**明确说明**定金与成交价的大小关系尚未定义。用这条判定会得到「编造的业务预期」—— 只保留观测，不进缺陷台账"
    }
  },
  "checked": 6,
  "violations": 1,
  "samples": [
    {
      "row_id": 2631,
      "type": "field_lte_field",
      "left": 999999,
      "right": 1,
      "verdict": "fail",
      "ok": false,
      "scenario": "deposit_over"
    }
  ],
  "scenarios": [
    {
      "scenario": "clean",
      "order_id": 2630
    },
    {
      "scenario": "deposit_over",
      "order_id": 2631
    },
    {
      "scenario": "amount_ne_detail",
      "order_id": 2632
    },
    {
      "scenario": "over_pkg_price",
      "order_id": 2633
    },
    {
      "scenario": "wrong_btype",
      "order_id": 2634
    },
    {
      "scenario": "foreign_item",
      "order_id": 2635
    }
  ]
}
```
- **复现**：`python3 business_probe.py`
- **来源**：`business_findings.json`

### [ledger] `SVC-PAGINATION-COUNT-DISCARDED`

- **预期原文**：分页 total 是纯开销：后端每次列表请求都跑 count(*) 全表扫描，前端从不读这个数
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/jiuyan/AGENTS.md:139-140
- **原文引用**："total": 100, "totalPages": 5
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：原结论「真正该修的不是加索引，而是别让后端算一个没人要的数」被明文契约否定：total 是契约要求返回的字段，前端不读不等于后端不该算。观测（count(*) 走 Seq Scan、50030 行时占端到端 36.1%/97.3%）为真且保留，但该成本的根因已由 index.idx_products_name_unusable 认领，不重复开单。缺陷断言撤销。
- **原结论严重度（已撤销）**：MEDIUM（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

```json
{
  "evidence_refs": [
    "perf_findings.json#SVC-PAGINATION-COUNT-DISCARDED"
  ],
  "location": {
    "file": "/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/ProductController.java",
    "line": 35,
    "symbol": "GET /api/products, GET /api/packages, GET /api/orders",
    "endpoint": "GET /api/products, GET /api/packages, GET /api/orders"
  },
  "source": {
    "file": "perf_findings.json",
    "index": 0
  }
}
```
- **复现**：`python3 perf_load.py --scales 0,2000,10000,50000`
- **来源**：`ledger_rule_suspects.json`

### [ledger] `INT-LIST-COUNT-END-TO-END-WASTE`

- **预期原文**：三个列表接口每次都为前端丢弃的 total 付一次全表扫描
- **oracle 状态**：`refuted`（source=`project_doc`）
- **reference**：/Users/june/jiuyan/AGENTS.md:139-140
- **原文引用**："total": 100, "totalPages": 5
- **为何不进台账**：文档明确否定该预期 —— 只保留观测，不判缺陷，也不再等业务确认。
- **裁定说明**：count(*) 占端到端 36.1%~97.3% 的观测为真并保留，但该成本的根因已由 index.idx_products_name_unusable 认领。「收益为零」被契约否定。detail 里另有一条独立观察「前端把 page 固定 1、pageSize 固定 100，超出部分不可见」——那是另一条预期（用户应能翻页），本轮未裁定，随观测留痕，不随本条一起当作已澄清。
- **原结论严重度（已撤销）**：MEDIUM（预期已被文档否定，此严重度仅留档，**不是缺陷也不是风险**）
- **观测证据**：

```json
{
  "evidence_refs": [
    "perf_findings.json#INT-LIST-COUNT-END-TO-END-WASTE"
  ],
  "location": {
    "file": "/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/ProductController.java",
    "line": 35,
    "symbol": "GET /api/products, GET /api/packages, GET /api/orders",
    "endpoint": "GET /api/products, GET /api/packages, GET /api/orders"
  },
  "source": {
    "file": "perf_findings.json",
    "index": 0
  }
}
```
- **复现**：`python3 perf_load.py --scales 0,2000,10000,50000`
- **来源**：`ledger_rule_suspects.json`

## 十、判据闸门覆盖清单（已接 / 未接 / 未裁定）

gate=warrant 表示已接 A/B 判据来源二分闸门；oracle 表示只有业务预期 oracle 闸门（未区分技术可自证）；none/unknown 表示未接。unadjudicated 只统计未接闸门生产者名下、且不在登记册里的 defect。**三个口径不得混算，未接闸门的条数必须与已接的并排报。**

生产者 15 个：A/B 判据二分 **2** / 仅 oracle **4** / 未接 **9** / 未声明 **0**

登记册条目 12 条；台账漏斗拦下 3 条；**未接闸门名下未裁定 defect 3 条**


| 生产者产物 | 生产脚本 | 闸门 | 生产条数 | defect | 登记册覆盖 | 漏斗拦下 | 未裁定 |
|---|---|---|---|---|---|---|---|
| `business_findings.json` | `engine/business_verdict.py` | oracle | 1 | 0 | 0 | 0 | 0 |
| `class_findings.json` | `class_diff.py` | oracle | 19 | 15 | 0 | 0 | 0 |
| `concurrency_findings.json` | `concurrency_props.py` | none | 3 | 2 | 2 | 1 | 0 |
| `config_findings.json` | `config_probe.py` | none | 4 | 3 | 3 | 0 | 0 |
| `feature_findings.json` | `java_feature_scan.py` | none | 2 | 2 | 1 | 0 | 1 |
| `file_findings.json` | `engine/file_diff.py` | warrant | 16 | 10 | 0 | 0 | 0 |
| `findings.json` | `contract_diff.py` | warrant | 10 | 10 | 0 | 0 | 0 |
| `fn_findings.json` | `engine/fn_diff.py` | oracle | 8 | 8 | 0 | 0 | 0 |
| `integration_findings.json` | `integration_diff.py` | oracle | 12 | 11 | 0 | 0 | 0 |
| `mutation_findings.json` | `mutation_test.py` | none | 2 | 1 | 0 | 0 | 1 |
| `numeric_findings.json` | `numeric_props.py` | none | 3 | 2 | 1 | 0 | 1 |
| `orm_findings.json` | `orm_query_probe.py` | none | 2 | 2 | 2 | 0 | 0 |
| `perf_findings.json` | `perf_load.py` | none | 2 | 2 | 2 | 2 | 0 |
| `property_binding_findings.json` | `property_binding_probe.py` | none | 1 | 1 | 1 | 0 | 0 |
| `transaction_findings.json` | `transaction_probe.py` | none | 1 | 0 | 0 | 0 | 0 |

「生产条数」是该生产者产出的条数（含被漏斗拦下的）；进台账的 = 生产条数 − 漏斗拦下。「未裁定」只统计**未接闸门**生产者名下、且不在登记册里的 defect —— 它们既没被裁定为技术可自证，也没被裁定为业务预期，**不得读成已澄清**。
