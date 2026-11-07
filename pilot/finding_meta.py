"""人工标注的 finding 元数据表 —— 把各层自说自话的字段补齐到 layers.yaml 的证据 schema。

为什么要有这张表：五个层脚本各自输出自己发明的字段名（fn 有 function、file 有
root_cause_file、service 有 endpoint、class 什么都没有），聚合器无法把它们变成
同一张表。本模块是**人工判定**的结果，不是脚本推断的结果：

- category：落到该层 layers.yaml 允许的类别上；装不下就写 UNMAPPED，并给出
  suggested_category —— 这是契约本身的缺口，由 aggregate_findings.py 汇总上报。
- fit：exact（类别描述与现象一致）/ stretched（能归进去但要解释）/ unmapped。
- claim：一句话「怀疑什么」，不是结论（契约原文）。
- owner_layer：按 attribution.rule，能给出可复现证据的最低层。
- relation：own（本层自己的根因）/ cited（引低层根因，本层拥有的是链级后果）。
- root_cause_key：根因指纹。同 key 的多条发现 = 同一个根因被重复开单。
- ground_truth：verified = 结论已被实测或可复算证据确认。
- confidence：0-1 的人工置信度；<0.6 按契约算 weakEdge，不丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Meta:
    category: str
    fit: str
    claim: str
    owner_layer: str
    confidence: float
    kind: str
    cwe: tuple[str, ...]
    root_cause_key: str
    relation: str = "own"
    ground_truth: str = "verified"
    suggested_category: str | None = None
    related_root_causes: tuple[str, ...] = ()
    note: str = ""


def _m(category: str, fit: str, claim: str, owner: str, conf: float, kind: str,
       cwe: tuple[str, ...], root: str, **kw) -> Meta:
    return Meta(category=category, fit=fit, claim=claim, owner_layer=owner,
                confidence=conf, kind=kind, cwe=cwe, root_cause_key=root, **kw)


FUNCTION: dict[str, Meta] = {
    "FN-PRICE-CLIENT-CONTROLLED": _m(
        "function.boundary", "stretched",
        "成交价是否只有下界校验、完全没有上界约束？",
        "function", 0.97, "defect", ("CWE-840",),
        "price.final_price.upper_bound_unvalidated",
        note="属业务边界缺失而非纯入参边界，故归 boundary 而非 numeric"),
    "FN-UPLOAD-NO-CONTENT-SNIFF": _m(
        "function.boundary", "exact",
        "上传内容类型是否只信客户端自报的扩展名与 contentType？",
        "function", 0.95, "defect", ("CWE-434",),
        "upload.content_type_not_sniffed"),
    "FN-ORDER-NO-COLLISION": _m(
        "function.boundary", "stretched",
        "订单号是否每秒只有 1000 个槽位、并发下单必然撞号？",
        "function", 0.90, "defect", ("CWE-330", "CWE-400"),
        "order_no.no_collision_guarantee",
        note="容量上界问题；并发写库后果在 service 层，但唯一决策点在函数"),
    "FN-SNAPSHOT-SEMANTICS": _m(
        "function.boundary", "stretched",
        "明细归属是否未校验，空明细是否静默回退成套餐默认明细？",
        "function", 0.94, "defect", ("CWE-20",),
        "order.snapshot.item_ownership_unchecked",
        related_root_causes=("order.item_quantity_unvalidated",)),
    "FN-APPLY-AMOUNT-INVARIANTS": _m(
        "function.boundary", "exact",
        "金额与状态不变式是否只查了 >= 0，定金能否大于套餐总价？",
        "function", 0.92, "defect", ("CWE-840",),
        "order.apply.amount_invariants_weak"),
    "FN-APPLY-NULL-CONTRACT": _m(
        "function.exception_path", "exact",
        "空入参是否在 .trim() 处抛 NPE（500）而不是业务异常（422）？",
        "function", 0.93, "defect", ("CWE-20", "CWE-755"),
        "apply.null_input.npe_instead_of_422"),
    "FN-ITEM-QUANTITY-NOVALIDATION": _m(
        "function.boundary", "exact",
        "明细归一化是否只查重复、不查 productId 与 quantity？",
        "function", 0.93, "defect", ("CWE-20",),
        "order.item_quantity_unvalidated"),
    "FN-UPLOAD-NO-SIZE-LIMIT": _m(
        "function.boundary", "exact",
        "上传体积是否完全没有上限，单请求被完整写盘？",
        "function", 0.91, "defect", ("CWE-400",),
        "upload.no_size_limit",
        note="服务层还有一条 FILE-UPLOAD-LIMIT-SPLIT 是「上限写在哪」的问题，根因不同"),
}

FILE: dict[str, Meta] = {
    "FILE-CORS-INTRANET-WIDE": _m(
        "UNMAPPED", "unmapped",
        "CORS 允许源是否覆盖整个内网与任意本地端口？",
        "file", 0.95, "defect", ("CWE-942",),
        "cors.allowed_origin_patterns_too_broad",
        suggested_category="service.authz",
        note="file 层的类别表里没有「安全策略配置」这一类 —— 契约缺口"),
    "FILE-XFF-CLIENT-IP-SPOOF": _m(
        "UNMAPPED", "unmapped",
        "审计日志里的 clientIp 是否完全由客户端伪造？",
        "file", 0.93, "defect", ("CWE-348", "CWE-290"),
        "audit.client_ip_trusted_from_header",
        suggested_category="service.authz",
        note="契约缺口：file 层无「可信输入判定」类别"),
    "FILE-BADREQUEST-ECHO": _m(
        "file.error_handling_pattern", "exact",
        "400 错误是否把原始请求体片段原样回显？",
        "file", 0.90, "defect", ("CWE-209",),
        "errhandler.badrequest_echoes_body"),
    "FILE-DB-CONSTRAINT-LEAK": _m(
        "file.error_handling_pattern", "exact",
        "DB 约束冲突原文是否被原样回给客户端？",
        "file", 0.92, "defect", ("CWE-209",),
        "errhandler.db_constraint_text_leak"),
    "FILE-PKG-MUTATE-BEFORE-VALIDATE": _m(
        "file.state_consistency", "stretched",
        "applyItems 是否先删后校验，出错时明细已被清空？",
        "file", 0.91, "defect", ("CWE-665",),
        "package.applyItems.mutate_before_validate",
        note="顺序依赖 —— state_consistency 的 desc 只提「文件内共享状态」，此处是持久化副作用顺序"),
    "FILE-STATIC-ROOT-NO-TRAILING-SLASH": _m(
        "file.state_consistency", "stretched",
        "静态根目录的规范化是否依赖「目录在启动前就存在」这个环境偶然？",
        "file", 0.72, "defect", (),
        "webconfig.static_root_normalization_env_dependent",
        ground_truth="unverified",
        note="证据是路径推导，未在真实容器里观察到目录缺失导致的失败"),
    "FILE-TRACEID-CLIENT-CONTROLLED": _m(
        "UNMAPPED", "unmapped",
        "X-Trace-Id 是否无格式与长度白名单就原样进日志与响应头？",
        "file", 0.88, "defect", ("CWE-117",),
        "traceid.client_controlled_no_validation",
        suggested_category="service.interface_contract"),
    "FILE-UPLOAD-LIMIT-SPLIT-ACROSS-THREE-FILES": _m(
        "file.cohesion", "stretched",
        "上传体积上限这个数字是否散落三处，改一处另两处就开始说谎？",
        "file", 0.95, "defect", (),
        "upload.limit_declared_in_three_places",
        note="跨文件重复声明，不是单文件内 cohesion"),
    "FILE-VALIDATION-STATUS-SPLIT": _m(
        "file.error_handling_pattern", "exact",
        "同一类「参数非法」是否返回两种状态码（400 / 422）？",
        "file", 0.94, "defect", (),
        "validation.status_code_split"),
    "FILE-DUPLICATE-HELPERS-ACROSS-SERVICES": _m(
        "file.cohesion", "exact",
        "分页归一化等方法是否在多个 service 文件里逐字复制？",
        "file", 0.93, "defect", (),
        "service.helpers.duplicated"),
    "FILE-ERROR-BODY-SHAPE-INCONSISTENT": _m(
        "file.error_handling_pattern", "exact",
        "同一个异常处理文件里是否并存三种响应形状？",
        "file", 0.92, "defect", (),
        "errhandler.response_shape_inconsistent"),
    "FILE-NULL-BODY-WRAPPED-AS-SUCCESS": _m(
        "file.error_handling_pattern", "stretched",
        "null 响应体是否也被包成 success=true 的信封？",
        "file", 0.90, "defect", (),
        "apiresponse.null_body_wrapped_as_success"),
    "FILE-CORS-NEGATIVE-PRECISION": _m(
        "UNMAPPED", "unmapped",
        "CORS 白名单是精确匹配、而不是通配全放？",
        "file", 0.90, "positive", (),
        "cors.negative_precision_proven",
        suggested_category="service.authz"),
    "FILE-NO-DEAD-PRIVATE-METHOD": _m(
        "file.cohesion", "exact",
        "文件层是否真的没有无人调用的私有方法？",
        "file", 0.88, "positive", (),
        "dead_private_method.none"),
    "FILE-ROLLBACK-PROVEN": _m(
        "file.error_handling_pattern", "exact",
        "事务边界是否实测有效、中途失败不留残局？",
        "file", 0.92, "positive", (),
        "txn.rollback_proven"),
    "FILE-STATIC-ORACLE-BLIND-SPOT": _m(
        "UNMAPPED", "unmapped",
        "静态 oracle 是否存在编译期常量内联这类盲区？",
        "file", 0.80, "observation", (),
        "static_oracle.blind_spot_compile_time_constant",
        ground_truth="unverified",
        note="这是对工具盲区的记录，不是被测系统的性质"),
    "FILE-STATIC-SAME-ROOT": _m(
        "UNMAPPED", "unmapped",
        "静态映射根与上传根是否同源？",
        "file", 0.85, "positive", (),
        "static_root.same_root"),
    "FILE-TXN-BOUNDARY-COMPLETE": _m(
        "file.error_handling_pattern", "exact",
        "全仓写方法的事务边界是否一个不缺？",
        "file", 0.90, "positive", (),
        "txn.boundary_complete"),
}

SERVICE: dict[str, Meta] = {
    "NO_AUTHZ": _m(
        "service.authz", "exact",
        "全系统是否根本没有授权不变量，无凭证即可读写全部业务数据？",
        "service", 0.98, "defect", ("CWE-862", "CWE-306"),
        "authz.no_authorization_layer"),
    "UNHANDLED_HTTP_EXCEPTION": _m(
        "service.interface_contract", "exact",
        "客户端错误是否被当成服务端错误返回 500？",
        "service", 0.94, "defect", ("CWE-755",),
        "errhandler.client_error_mapped_to_500"),
    "TYPE_COERCION": _m(
        "service.interface_contract", "exact",
        "错误 JSON 类型是否被隐式转换后接受？",
        "service", 0.90, "defect", ("CWE-20",),
        "jackson.lenient_type_coercion"),
    "SVC-PAGINATION-COUNT-DISCARDED": _m(
        "service.service_latency", "exact",
        "分页 count(*) 是否每次列表请求都全表扫描，而前端根本不看 total？",
        "service", 0.93, "defect", (),
        "pagination.count_total_computed_but_discarded",
        related_root_causes=("index.idx_products_name_unusable",),
        note="实测 5 万行时 count(*) 占端到端 31%（带 keyword）/ 40%（不带 keyword）；"
             "前端 listProducts/listPackages/listOrders 三个函数都只取 items，total 从未被读"),
    "INTERNAL_LEAK": _m(
        "service.interface_contract", "stretched",
        "错误响应是否把内部实现细节返回给客户端？",
        "service", 0.92, "defect", ("CWE-209",),
        "errhandler.internal_detail_leak",
        note="25 条同根因；类别取 interface_contract（错误响应形状属接口契约），非 authz"),
}

CLASS: dict[str, Meta] = {
    "NO_STATE_MACHINE": _m(
        "class.invariant", "exact",
        "订单状态迁移是否没有任何约束，9/9 种迁移全被接受？",
        "class", 0.96, "defect", ("CWE-840",),
        "order.status.transition_unconstrained"),
    "INDEX_UNUSABLE_FOR_QUERY_PATTERN": _m(
        "service.service_latency", "stretched",
        "DDL 声明的 idx_products_name 对服务层查询模式是否真的可用？",
        "service", 0.88, "defect", (),
        "index.idx_products_name_unusable",
        note="检测在 class 层（实体与 DDL 对齐），归属层是 service（查询模式在服务层）"),
    "CLASS-RULE-MISSING-IN-ALL-LAYERS": _m(
        "class.invariant", "exact",
        "那 4 条业务不变量在 DB / DTO / 服务层是否全都不存在？",
        "class", 0.85, "defect", ("CWE-840",),
        "rule.missing_in_db_dto_service"),
    "SOFT_DELETE_RESURRECTS_ROW": _m(
        "class.invariant", "exact",
        "软删除的行被同名重建时是否会「复活」并沿用旧 id？",
        "class", 0.90, "defect", ("CWE-840",),
        "softdelete.resurrects_row"),
    "NO_DATA_LIFECYCLE": _m(
        "class.invariant", "stretched",
        "orders 表是否没有软删或归档、永久增长？",
        "class", 0.80, "defect", (),
        "orders.no_data_lifecycle",
        note="类层无「生命周期」类别；资源上限语义更接近 service.resource_leak"),
    "CLASS-NOTNULL-BYPASSED-BY-EMPTY-STRING": _m(
        "class.invariant", "exact",
        "NOT NULL 列是否可以用空串绕过？",
        "class", 0.92, "defect", ("CWE-20",),
        "db.notnull_bypassed_by_empty_string"),
    "CLASS-REQUIRED-CHECKED-IN-SERVICE-LAYER": _m(
        "class.contract_violation", "stretched",
        "必填性校验是否落在服务层、DTO 少一条 @NotNull？",
        "class", 0.88, "defect", (),
        "dto.required_check_missing"),
    "CLASS-PUT-WIPES-UNSET-FIELD": _m(
        "class.contract_violation", "exact",
        "PUT 全量覆盖是否会把不传的字段抹成 NULL？",
        "class", 0.91, "defect", ("CWE-20",),
        "api.put_full_overwrite"),
    "CLASS-DEFAULT-VALUE-DECLARED-IN-THREE-PLACES": _m(
        "UNMAPPED", "unmapped",
        "同一列的默认值是否被声明了三遍？",
        "class", 0.86, "defect", (),
        "column.default_declared_in_three_places",
        suggested_category="file.cohesion",
        note="缺陷跨三个文件（cohesion 信号），但 class 层的 escalation_to 只有 service，"
             "不能把归属上提到 file；owner 留在 class，与 manifest 的 attribution 一致"),
    "CLASS-ERROR-MESSAGE-MISMATCH": _m(
        "class.contract_violation", "stretched",
        "报错文案是否与真实触发条件不一致？",
        "class", 0.85, "defect", (),
        "error.message_mismatch"),
    "CLASS-ENUM-CASE-SENSITIVE": _m(
        "class.contract_violation", "stretched",
        "状态枚举是否大小写敏感，小写 pending 直接 400？",
        "class", 0.82, "defect", (),
        "enum.case_sensitive"),
    "ENUM_CHECK_ALIGNED": _m(
        "class.contract_violation", "exact",
        "枚举与 DB CHECK 白名单是否完全一致？",
        "class", 0.95, "positive", (),
        "enum.check_aligned"),
    "CLASS-NULLABILITY-ALIGNED": _m(
        "class.invariant", "exact",
        "可空性是否三方一致？",
        "class", 0.93, "positive", (),
        "nullability.aligned"),
    "CLASS-CHECK-DTO-ALIGNED": _m(
        "class.contract_violation", "exact",
        "DB CHECK 是否在 DTO 层都有同义约束？",
        "class", 0.92, "positive", (),
        "check.dto_aligned"),
    "CLASS-PUT-REQUIRED-FK-GUARDED": _m(
        "class.contract_violation", "exact",
        "PUT 缺省必填外键是否被 DTO 挡住？",
        "class", 0.90, "positive", (),
        "put.required_fk_guarded"),
}

INTEGRATION: dict[str, Meta] = {
    "INT-ANON-WRITE-REWRITES-SNAPSHOT": _m(
        "integration.cross_service_attack_chain", "exact",
        "匿名写入是否连带重写订单快照、让历史快照失去证据效力？",
        "service", 0.96, "defect", ("CWE-506", "CWE-829"),
        "authz.no_authorization_layer",
        relation="cited",
        related_root_causes=("order.snapshot.item_ownership_unchecked",),
        note="链级后果归集权在 integration 层，根因（零鉴权）归 service 层"),
    "INT-UPLOAD-ANONYMOUS-READBACK": _m(
        "integration.cross_service_attack_chain", "exact",
        "上传内容是否匿名可读回、落盘字节与上传字节逐字节一致？",
        "function", 0.95, "defect", ("CWE-434", "CWE-829"),
        "upload.content_type_not_sniffed",
        relation="cited",
        note="链级后果归集成层，根因（不做内容嗅探）归函数层"),
    "PRICE_CLIENT_CONTROLLED_XL": _m(
        "integration.end_to_end_business", "exact",
        "同一订单里是否并存两个互相矛盾的价格？",
        "function", 0.95, "defect", ("CWE-840",),
        "price.final_price.upper_bound_unvalidated",
        relation="cited",
        note="源数据把根因归 service 层；实测唯一决策点在 OrderService.resolveFinalPrice，"
             "函数层已给出可复现证据，故 owner 修正为 function"),
    "INT-LIST-COUNT-END-TO-END-WASTE": _m(
        "integration.cross_service_performance", "exact",
        "三个列表接口是否每次都为前端丢弃的 total 付一次全表扫描？",
        "service", 0.90, "defect", (),
        "pagination.count_total_computed_but_discarded",
        relation="cited",
        note="链级后果归集成层，根因（后端白算一次 count）归服务层"),
    "INT-PII-MASKED-ONLY-IN-FRONTEND": _m(
        "integration.end_to_end_business", "exact",
        "手机号脱敏是否只做在渲染层，接口与库里都是明文？",
        "service", 0.95, "defect", ("CWE-359",),
        "authz.no_authorization_layer",
        relation="cited"),
    "INT-THREE-PRICES-NO-DETAIL-TOTAL": _m(
        "integration.end_to_end_business", "exact",
        "同一订单是否并存三个互不相等的价格，界面只展示其中两个？",
        "function", 0.93, "defect", ("CWE-840",),
        "price.final_price.upper_bound_unvalidated",
        relation="cited",
        related_root_causes=("frontend.detail_total_not_surfaced",)),
    "LIST_TRUNCATED_AT_100": _m(
        "integration.end_to_end_business", "exact",
        "前端列表是否固定只取前 100 条并丢弃 total？",
        "integration", 0.95, "defect", (),
        "frontend.list_hardcoded_limit_100"),
    "PACKAGE_ITEM_NOT_VALIDATED": _m(
        "integration.end_to_end_business", "exact",
        "能否把不属于该套餐的商品写进套餐明细？",
        "function", 0.93, "defect", ("CWE-20",),
        "order.snapshot.item_ownership_unchecked",
        relation="cited"),
    "PACKAGE_QUANTITY_NOT_VALIDATED": _m(
        "integration.end_to_end_business", "exact",
        "明细数量能否改成 99 而价格仍按套餐原价结算？",
        "function", 0.93, "defect", ("CWE-20",),
        "order.item_quantity_unvalidated",
        relation="cited"),
    "INT-PACKAGE-COUNT-INCONSISTENT": _m(
        "integration.end_to_end_business", "exact",
        "同一个套餐的界面上是否有两个互不相同的「商品数」？",
        "integration", 0.92, "defect", (),
        "frontend.item_count_semantics_divergent"),
    "INT-PRODUCT-IMAGE-DOMAIN-FALLBACK": _m(
        "integration.end_to_end_business", "exact",
        "商品缺图时前端兜底是否兜成了套餐种子图？",
        "integration", 0.88, "defect", (),
        "frontend.image_fallback_cross_domain"),
    "RECORDING_COVERAGE_GAP": _m(
        "UNMAPPED", "unmapped",
        "是否有一批接口既无真实流量样本、前端也不调用？",
        "integration", 0.85, "observation", (),
        "tooling.coverage_recording_gap",
        note="覆盖度缺口，不是被测系统的缺陷；契约的 integration 类别里没有这一类"),
    "SNAPSHOT_CONTRACT_UNDECLARED": _m(
        "integration.end_to_end_business", "stretched",
        "前端依赖的订单快照内部结构是否在 API 契约里零声明？",
        "integration", 0.92, "defect", (),
        "api.snapshot_structure_undeclared"),
    "UNKNOWN_FIELD_SILENT_DROP": _m(
        "integration.end_to_end_business", "exact",
        "字段名拼错是否被静默吞掉并被套餐默认明细顶替？",
        "integration", 0.93, "defect", ("CWE-20",),
        "dto.unknown_field_silent_drop"),
    "DEAD_BACKEND_ENDPOINT": _m(
        "integration.end_to_end_business", "stretched",
        "后端是否有前端从不调用的接口？",
        "integration", 0.90, "defect", (),
        "api.dead_endpoints"),
}

META: dict[str, dict[str, Meta]] = {
    "function": FUNCTION,
    "file": FILE,
    "service": SERVICE,
    "class": CLASS,
    "integration": INTEGRATION,
}


# 类层实体的源码定位提示：finding id -> 相对 java 包根的文件路径（空串 = 跨实体，无法单点定位）
CLASS_FILE_HINTS: dict[str, str] = {
    "NO_STATE_MACHINE": "entity/BanquetOrder.java",
    "INDEX_UNUSABLE_FOR_QUERY_PATTERN": "entity/Product.java",
    "CLASS-RULE-MISSING-IN-ALL-LAYERS": "",
    "NO_DATA_LIFECYCLE": "entity/BanquetOrder.java",
    "CLASS-ERROR-MESSAGE-MISMATCH": "entity/BanquetOrder.java",
    "CLASS-ENUM-CASE-SENSITIVE": "entity/OrderStatus.java",
    "ENUM_CHECK_ALIGNED": "entity/OrderStatus.java",
    "CLASS-NULLABILITY-ALIGNED": "",
    "CLASS-CHECK-DTO-ALIGNED": "",
}

# 只知道列名时的兜底：列 -> 所属实体文件
COLUMN_FILE_HINTS: dict[str, str] = {
    "address": "entity/BanquetOrder.java",
    "final_price_cent": "entity/BanquetOrder.java",
    "customer_phone": "entity/BanquetOrder.java",
    "status": "entity/BanquetOrder.java",
    "is_active": "entity/Product.java",
}


def lookup(layer: str, finding_id: str) -> Meta | None:
    return META.get(layer, {}).get(finding_id)
