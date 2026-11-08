# 分层审计 · 根因与覆盖度报告

生成物：/Users/june/.cc-weixin/workspace/layered-audit/pilot/root_causes.json　输入：/Users/june/.cc-weixin/workspace/layered-audit/pilot/all_findings.json

- 发现 101 条 → 根因 73 条（归并 28 条）
- 硬违规 1 条；契约缺口（无法归类）9 条

## 一、修复顺序（按根因）

| # | 严重度 | 归属层 | 根因 | 标题 | 发现数 | 置信 | ground_truth | 可跑复现 |
|---|--------|--------|------|------|--------|------|---------------|----------|
| 1 | CRITICAL | function | `price.final_price.upper_bound_unvalidated` | 成交价由客户端说了算：resolveFinalPrice 只做下界检查，完全不与套餐价比对 | 6 | 0.97 | verified | `python3 business_probe.py` |
| 2 | CRITICAL | service | `authz.no_authorization_layer` | 全系统零鉴权：无凭证即可读写全部业务数据 | 3 | 0.98 | verified | `curl http://127.0.0.1:8081/api/orders   # 无任何凭证` |
| 3 | CRITICAL | function | `upload.content_type_not_sniffed` | 上传只信客户端自报的扩展名与 contentType：任意字节都以 .png 落盘 | 2 | 0.95 | verified | `python3 fn_dynamic.py --case UPL-04` |
| 4 | HIGH | function | `order.item_quantity_unvalidated` | 明细归一化只查重复，不查 productId/quantity：数量可为空、为 0、为负 | 3 | 0.93 | verified | `python3 fn_dynamic.py --case NAI-04` |
| 5 | HIGH | function | `order.snapshot.item_ownership_unchecked` | 订单快照：明细归属不校验，空明细静默回退成套餐默认明细 | 3 | 0.94 | verified | `python3 business_probe.py` |
| 6 | HIGH | file | `cors.allowed_origin_patterns_too_broad` | CORS 白名单等于「整个内网 + 所有本地端口」，且全是明文 http | 2 | 0.95 | verified | `python3 config_probe.py` |
| 7 | HIGH | service | `errhandler.client_error_mapped_to_500` | 客户端错误被当成服务端错误：返回 [500] | 2 | 0.94 | verified | `curl -X POST 见 observation.json 中 key=POST /api/uploads/p…` |
| 8 | HIGH | function | `order.apply.amount_invariants_weak` | apply 的金额/状态不变量只查了 >= 0：定金可以大于套餐总价，状态与预订时间可为空 | 2 | 0.92 | verified | `python3 business_probe.py` |
| 9 | HIGH | file | `audit.client_ip_trusted_from_header` | 审计日志里的 clientIp 完全由客户端伪造：X-Forwarded-For 首段直接采信 | 1 | 0.93 | verified | `python3 file_dynamic.py` |
| 10 | HIGH | class | `banquet_package.items_exposed` | com.jiuyan.banquet.entity.BanquetPackage.getItems 把内部实体集合直接交出去 | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 11 | HIGH | service | `cors.empty_whitelist_fails_open` | CORS 白名单配成空值时反而允许任意来源跨域（fail-open） | 1 | 0.93 | verified | `python3 config_probe.py` |
| 12 | HIGH | integration | `frontend.list_hardcoded_limit_100` | 前端列表固定只取前 100 条并丢弃 total，超出的数据永远不可见 | 1 | 0.95 | verified | `python3 integration_dynamic.py --scenario SC05_LIST_TRUNC…` |
| 13 | HIGH | service | `index.idx_products_name_unusable` | DDL 声明的 idx_products_name 对服务层查询模式无效（8.094ms vs 0.013ms，差 623 倍） | 1 | 0.88 | verified | `python3 class_dynamic.py` |
| 14 | HIGH | class | `order.status.transition_unconstrained` | 状态机缺失：9/9 种迁移全部被接受，含回退 | 1 | 0.96 | verified | `python3 class_dynamic.py` |
| 15 | HIGH | function | `order_no.no_collision_guarantee` | 订单号每秒只有 1000 个槽位，并发下单必然撞号（唯一键冲突 → 500） | 1 | 0.9 | verified | `python3 fn_dynamic.py --case GON-02` |
| 16 | HIGH | class | `rule.missing_in_db_dto_service` | 4 条业务不变量在 DB / DTO / 服务层三层全都不存在 | 1 | 0.85 | verified | `python3 class_dynamic.py` |
| 17 | MEDIUM | service | `errhandler.internal_detail_leak` | 错误响应把内部实现细节返回给客户端 | 7 | 0.92 | verified | `curl -X PATCH /api/orders/2192/status` |
| 18 | MEDIUM | class | `entity.no_value_equality` | BanquetOrder 没有值相等：放进集合或当键，语义会变 | 6 | 0.8 | verified | `python3 class_dynamic.py` |
| 19 | MEDIUM | service | `pagination.count_total_computed_but_discarded` | 分页 total 是纯开销：后端每次列表请求都跑 count(*) 全表扫描，前端从不读这个数 | 2 | 0.93 | verified | `python3 perf_load.py --scales 0,2000,10000,50000` |
| 20 | MEDIUM | class | `api.put_full_overwrite` | PUT 是全量覆盖：不传的 imageUrl 会被抹成 NULL | 1 | 0.91 | verified | `python3 class_dynamic.py` |
| 21 | MEDIUM | integration | `api.snapshot_structure_undeclared` | 前端依赖的订单快照内部结构，在 API 契约里零声明 | 1 | 0.92 | verified | `grep -n 'packageSnapshot' backend/src/main/java/com/jiuya…` |
| 22 | MEDIUM | function | `apply.null_input.npe_instead_of_422` | apply 直接对入参 .trim()：空值触发 NPE（500）而不是业务异常（422） | 1 | 0.93 | verified | `python3 fn_dynamic.py --case APL-02` |
| 23 | MEDIUM | service | `config.binding_priority_surprise` | app.cors-allowed-origins 的绑定优先级与直觉相反：APP_CORS_ALLOWED_ORIGINS 赢了 CORS_ALLOWED_ORIGINS | 1 | 0.88 | verified | `python3 property_binding_probe.py` |
| 24 | MEDIUM | class | `db.notnull_bypassed_by_empty_string` | orders.address 声明 NOT NULL，实际用空串绕过 —— 非空约束只挡了 NULL | 1 | 0.92 | verified | `python3 class_dynamic.py` |
| 25 | MEDIUM | class | `dto.required_check_missing` | orders.finalPriceCent 必填性校验落在服务层，DTO 少一条 @NotNull | 1 | 0.88 | verified | `python3 class_dynamic.py` |
| 26 | MEDIUM | integration | `dto.unknown_field_silent_drop` | 字段名拼错会被静默吞掉，并被「套餐默认明细」悄悄顶替 | 1 | 0.93 | verified | `python3 integration_dynamic.py --scenario SC04_UNKNOWN_FI…` |
| 27 | MEDIUM | file | `errhandler.badrequest_echoes_body` | 400 错误把原始请求体片段原样回显 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 28 | MEDIUM | file | `errhandler.db_constraint_text_leak` | DB 约束冲突的原始错误文本被原样回给客户端 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 29 | MEDIUM | integration | `frontend.image_fallback_cross_domain` | 商品缺图时前端兜底成了套餐种子图 | 1 | 0.88 | verified | `POST /api/products（imageUrl 留空）→ 前端 mapProduct → image 字段…` |
| 30 | MEDIUM | integration | `frontend.item_count_semantics_divergent` | 同一个套餐，界面上有两个互不相同的「商品数」 | 1 | 0.92 | verified | `造一个套餐，明细含一个 is_active=false 的商品 → 对比卡片与弹层的数字` |
| 31 | MEDIUM | function | `oracle.assertion_weak_under_mutation` | 有变异改了实际行为但我们的 oracle 没抓住 —— 断言只看结果，不看「该发生什么」 | 1 | 0.92 | unverified | `python3 mutation_test.py` |
| 32 | MEDIUM | service | `order.banquet_type_not_checked_against_package` | 订单的宴会类型是否与所选套餐的宴会类型一致？ | 1 | 0.93 | verified | `python3 business_probe.py` |
| 33 | MEDIUM | service | `order.create.n_plus_one_per_item` | 下单时每多一个明细就多 2.0 条 SQL（典型 N+1） | 1 | 0.95 | verified | `python3 orm_query_probe.py` |
| 34 | MEDIUM | service | `order.create.not_idempotent` | 下单接口没有幂等：同一个请求重发一次就产生两个订单 | 1 | 0.92 | verified | `python3 concurrency_props.py` |
| 35 | MEDIUM | service | `order.update.no_concurrency_control` | 并发编辑同一订单会静默丢掉对方的修改：没有版本号、没有冲突检测 | 1 | 0.95 | verified | `python3 concurrency_props.py` |
| 36 | MEDIUM | class | `orders.no_data_lifecycle` | orders 表没有软删/归档，永久增长（当前 184 kB） | 1 | 0.8 | verified | `python3 class_dynamic.py` |
| 37 | MEDIUM | file | `package.applyItems.mutate_before_validate` | PackageService.applyItems 先删后校验：明细被清空后才去查商品 | 1 | 0.91 | verified | `python3 file_dynamic.py` |
| 38 | MEDIUM | service | `package.list.n_plus_one_per_package` | 套餐列表每多返回 1 个套餐就多 8.0 条 SQL | 1 | 0.93 | verified | `python3 orm_query_probe.py` |
| 39 | MEDIUM | class | `service.public_api_returns_jpa_entity` | service 层把 JPA 实体当 public API 返回：调用方能绕过全部业务校验直接改实体 | 1 | 0.88 | verified | `python3 java_feature_scan.py` |
| 40 | MEDIUM | class | `softdelete.resurrects_row` | 软删除的行会被同名重建「复活」，id 与 sort_order 沿用旧值 | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 41 | MEDIUM | class | `spring.self_invocation_bypasses_proxy` | 同类内部直接调用被切面标注的方法 → 绕过 Spring 代理，切面语义不生效 | 1 | 0.9 | verified | `python3 java_feature_scan.py` |
| 42 | MEDIUM | integration | `tooling.coverage_recording_gap` | 1/25 个接口既无真实流量样本、前端也不调用；17 个写接口的真实流量样本为 0 | 1 | 0.85 | verified | `见 /Users/june/tests/auto-api/recordings/jiuyan_readonly.j…` |
| 43 | MEDIUM | file | `traceid.client_controlled_no_validation` | X-Trace-Id 完全由客户端指定，无格式与长度白名单，原样进日志与响应头 | 1 | 0.88 | verified | `python3 file_dynamic.py` |
| 44 | MEDIUM | file | `upload.limit_declared_in_three_places` | 上传体积上限 5MB 这个数字散落三处，改一处另两处就开始说谎 | 1 | 0.95 | verified | `python3 file_dynamic.py` |
| 45 | MEDIUM | function | `upload.no_size_limit` | 上传无体积上限：单请求 20MB 被完整写盘（上传放大 DoS） | 1 | 0.91 | verified | `python3 fn_dynamic.py --case UPL-06` |
| 46 | MEDIUM | file | `validation.status_code_split` | 同一类「参数非法」返回两种状态码：DTO 校验给 400，服务自校验给 422 | 1 | 0.94 | verified | `python3 file_dynamic.py` |
| 47 | MEDIUM | file | `webconfig.static_root_normalization_env_dependent` | 静态资源根目录的规范化依赖「目录在启动前就存在」这个环境偶然 | 1 | 0.72 | unverified | `python3 file_dynamic.py` |
| 48 | LOW | class | `column.default_declared_in_three_places` | products.is_active 的默认值被声明了三遍 | 2 | 0.86 | verified | `python3 class_dynamic.py` |
| 49 | LOW | integration | `api.dead_endpoints` | 后端 4 个接口前端从不调用 | 1 | 0.9 | verified | `grep -n 'api/' /Users/june/jiuyan/src/api/banquetApi.js` |
| 50 | LOW | file | `apiresponse.null_body_wrapped_as_success` | null 响应体也被包成 success=true 的信封（data 字段缺失） | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 51 | LOW | service | `config.bad_upload_dir_kills_startup` | 上传目录配错会让整个后端起不来，而不是降级运行 | 1 | 0.85 | verified | `python3 config_probe.py` |
| 52 | LOW | class | `enum.case_sensitive` | 订单状态枚举大小写敏感，小写 pending 直接 400 | 1 | 0.82 | verified | `python3 class_dynamic.py` |
| 53 | LOW | file | `errhandler.response_shape_inconsistent` | 同一个异常处理文件里三种响应形状：信封 / 空体 / 未知 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 54 | LOW | class | `error.message_mismatch` | finalPriceCent=null 的报错文案说的是「小于 0」，真实原因是「不能为空」 | 1 | 0.85 | verified | `python3 class_dynamic.py` |
| 55 | LOW | function | `money_input.normalization_silently_mutates_value` | 金额输入框静默改变用户输入的数值：负号被吞、超过 2 位小数被截断 | 1 | 0.95 | verified | `python3 numeric_props.py` |
| 56 | LOW | file | `service.helpers.duplicated` | 分页归一化等 4 组方法在 3 个 service 文件里逐字复制 | 1 | 0.93 | verified | `python3 file_dynamic.py` |
| 57 | INFO | class | `check.dto_aligned` | 8/8 条 DB CHECK 在 DTO 层都有同义约束，防线链条闭合 | 1 | 0.92 | verified | `python3 class_dynamic.py` |
| 58 | INFO | file | `cors.negative_precision_proven` | CORS 白名单是精确匹配，不是通配全放 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 59 | INFO | file | `dead_private_method.none` | 没有真正无人调用的私有方法 | 1 | 0.88 | verified | `python3 file_dynamic.py` |
| 60 | INFO | class | `enum.check_aligned` | OrderStatus 枚举与 DB CHECK 白名单完全一致（3/3） | 1 | 0.95 | verified | `python3 class_dynamic.py` |
| 61 | INFO | function | `money_conversion.roundtrip_exact` | 前端金额换算在合理量级内精确闭合（分→元→分、元→分、显示精度三条性质全过） | 1 | 0.9 | verified | `python3 numeric_props.py` |
| 62 | INFO | class | `nullability.aligned` | 可空性三方一致 27/31 列（同向设防 21 · DTO 更严 0 · 都可空 6） | 1 | 0.93 | verified | `python3 class_dynamic.py` |
| 63 | INFO | function | `oracle.mutation_harness_operational` | 变异测试链路跑通：函数层 oracle 抓得住大部分变异 | 1 | 0.9 | verified | `python3 mutation_test.py` |
| 64 | INFO | service | `order.added_items_existence_checked` | 加购明细里的商品是否都真实存在且未软删？ | 1 | 0.9 | verified | `python3 business_probe.py` |
| 65 | INFO | service | `order.amount_consistent_when_not_overridden` | 未使用成交价时，订单金额是否等于套餐总价？ | 1 | 0.9 | verified | `python3 business_probe.py` |
| 66 | INFO | service | `order.update.transaction_boundary_holds` | 被拒的写请求不会在订单行留下部分写入（事务边界有效） | 1 | 0.9 | verified | `python3 concurrency_props.py` |
| 67 | INFO | class | `put.required_fk_guarded` | PUT 缺省必填外键被 DTO 的 @NotNull 挡住（400，DB 无脏数据） | 1 | 0.9 | verified | `python3 class_dynamic.py` |
| 68 | INFO | service | `spring.transaction_effectiveness_verified` | 静态声明的事务与运行时实际生效的事务完全一致（差分为空） | 1 | 0.93 | verified | `python3 transaction_probe.py` |
| 69 | INFO | file | `static_oracle.blind_spot_compile_time_constant` | 静态 oracle 的一处已知盲区：编译期常量内联 | 1 | 0.8 | unverified | `python3 file_dynamic.py` |
| 70 | INFO | file | `static_root.same_root` | 静态映射根与上传根同源（manifest 假设被证伪） | 1 | 0.85 | verified | `python3 file_dynamic.py` |
| 71 | INFO | file | `txn.boundary_complete` | 全仓写方法的事务边界一个不缺 | 1 | 0.9 | verified | `python3 file_dynamic.py` |
| 72 | INFO | file | `txn.rollback_proven` | 事务边界实测有效：中途失败不留残局 | 1 | 0.92 | verified | `python3 file_dynamic.py` |
| 73 | INFO | service | `upload.error.no_path_leak` | 上传失败时错误响应不泄露服务器文件系统路径 | 1 | 0.9 | verified | `python3 config_probe.py` |

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
| `price.final_price.upper_bound_unvalidated` | `frontend.detail_total_not_surfaced` |
| `authz.no_authorization_layer` | `order.snapshot.item_ownership_unchecked` |
| `order.snapshot.item_ownership_unchecked` | `order.item_quantity_unvalidated` |
| `pagination.count_total_computed_but_discarded` | `index.idx_products_name_unusable` |

## 五、覆盖度矩阵（层 × 契约声明的 category）

**function**

| category | 命中 | 状态 |
|----------|------|------|
| boundary | 11 | ok |
| complexity | 0 | 零覆盖 —— 没人管 |
| exception_path | 1 | ok |
| numeric | 3 | ok |

**file**

| category | 命中 | 状态 |
|----------|------|------|
| state_consistency | 2 | ok |
| error_handling_pattern | 7 | ok |
| cohesion | 3 | ok |
| contract_alignment | 0 | 零覆盖 —— 没人管 |

**class**

| category | 命中 | 状态 |
|----------|------|------|
| encapsulation_leak | 2 | ok |
| invariant | 12 | ok |
| contract_violation | 8 | ok |
| inheritance_design | 0 | 零覆盖 —— 没人管 |

**service**

| category | 命中 | 状态 |
|----------|------|------|
| interface_contract | 11 | ok |
| resilience | 2 | ok |
| authz | 3 | ok |
| data_consistency | 9 | ok |
| resource_leak | 0 | 零覆盖 —— 没人管 |
| service_latency | 3 | ok |

**integration**

| category | 命中 | 状态 |
|----------|------|------|
| end_to_end_business | 11 | ok |
| cross_service_performance | 1 | ok |
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
| `file:FILE-CORS-NEGATIVE-PRECISION#12` | 见 all_findings.json 的 suggested_category |
| `file:FILE-STATIC-ORACLE-BLIND-SPOT#15` | 见 all_findings.json 的 suggested_category |
| `file:FILE-STATIC-SAME-ROOT#16` | 见 all_findings.json 的 suggested_category |
| `class:CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES#15` | 见 all_findings.json 的 suggested_category |
| `class:CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES#16` | 见 all_findings.json 的 suggested_category |
| `integration:RECORDING_COVERAGE_GAP#10` | 见 all_findings.json 的 suggested_category |

## 七、同一文件承载多个根因（文件层内聚信号）

| 文件 | 根因数 | 根因 |
|------|--------|------|
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/OrderController.java` | 14 | `api.snapshot_structure_undeclared`, `dto.unknown_field_silent_drop`, `errhandler.internal_detail_leak`, `order.added_items_existence_checked`, `order.amount_consistent_when_not_overridden`, `order.apply.amount_invariants_weak`, `order.banquet_type_not_checked_against_package`, `order.create.n_plus_one_per_item`, `order.create.not_idempotent`, `order.item_quantity_unvalidated`, `order.snapshot.item_ownership_unchecked`, `order.update.no_concurrency_control`, `order.update.transaction_boundary_holds`, `price.final_price.upper_bound_unvalidated` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/OrderService.java` | 7 | `apply.null_input.npe_instead_of_422`, `order.apply.amount_invariants_weak`, `order.item_quantity_unvalidated`, `order.snapshot.item_ownership_unchecked`, `order_no.no_collision_guarantee`, `price.final_price.upper_bound_unvalidated`, `service.helpers.duplicated` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/BanquetOrder.java` | 6 | `db.notnull_bypassed_by_empty_string`, `dto.required_check_missing`, `entity.no_value_equality`, `error.message_mismatch`, `order.status.transition_unconstrained`, `orders.no_data_lifecycle` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/UploadService.java` | 3 | `upload.content_type_not_sniffed`, `upload.limit_declared_in_three_places`, `upload.no_size_limit` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/exception/GlobalExceptionHandler.java` | 3 | `errhandler.badrequest_echoes_body`, `errhandler.db_constraint_text_leak`, `errhandler.response_shape_inconsistent` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/service/PackageService.java` | 3 | `package.applyItems.mutate_before_validate`, `service.public_api_returns_jpa_entity`, `spring.self_invocation_bypasses_proxy` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/PackageController.java` | 3 | `errhandler.internal_detail_leak`, `package.list.n_plus_one_per_package`, `tooling.coverage_recording_gap` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/controller/ProductController.java` | 3 | `errhandler.internal_detail_leak`, `frontend.list_hardcoded_limit_100`, `pagination.count_total_computed_but_discarded` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/Product.java` | 3 | `api.put_full_overwrite`, `column.default_declared_in_three_places`, `index.idx_products_name_unusable` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/config/WebConfig.java` | 2 | `cors.allowed_origin_patterns_too_broad`, `webconfig.static_root_normalization_env_dependent` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/config/TraceIdFilter.java` | 2 | `audit.client_ip_trusted_from_header`, `traceid.client_controlled_no_validation` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/BanquetPackage.java` | 2 | `banquet_package.items_exposed`, `column.default_declared_in_three_places` |
| `/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/OrderStatus.java` | 2 | `enum.case_sensitive`, `enum.check_aligned` |
| `/Users/june/jiuyan/src/api/banquetApi.js` | 2 | `money_conversion.roundtrip_exact`, `price.final_price.upper_bound_unvalidated` |
