"""demo-shop 的人工标注表（项目侧）。

与 pilot/finding_meta.py 同一套结构 —— 归一化器只认这张表的接口：
`lookup(layer, finding_id)` + `Meta` 字段。demo 是**另一个目标**，
所以它有自己的一份标注表（引擎不认项目，只认绑定）。

为什么 demo 需要自己的一份：
pilot/finding_meta.py 标的是 jiuyan 的发现；demo 的发现如果塞进那份表，
两个仓库的 finding 会共用一套 category/owner 判定 —— 归属会串。
按「换项目只改绑定 + 标注表」的原则，各目标各带一份。
"""
from __future__ import annotations

from dataclasses import dataclass


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


# 涌现裁决出来的集成层发现：单独注入都不触发，只有组合才触发。
# 根因不可归给任何单层（额度检查那一层和记账那一层各自都对），
# 故 owner = integration（own），低层根因以 related_root_causes 引用。
INTEGRATION: dict[str, Meta] = {
    "EMERG-QUOTA-LEDGER-DIVERGENCE": _m(
        "integration.end_to_end_business", "exact",
        "额度占用额与账本记录额是否在同一条下单链路上出现口径分裂？",
        "integration", 0.95, "defect", ("CWE-840",),
        "order.quota_ledger_amount_divergence",
        relation="own",
        ground_truth="verified",
        related_root_causes=(
            "order.quota_checked_on_quoted_amount_ledger_on_settled",
            "pricing.member_tier_bound_to_deployment_profile",
        ),
        note="涌现链（EMERGENT_CONJUNCTIVE）：单独注入 INJ-TIER-VIP / INJ-PROFILE-VIP "
             "都不触发，只有两条合起来才违反「额度占用额 == 账本记录额」。"
             "两个低层各自都对，根因落在它们的组合上，故由集成层开单。"),
}

# 类层：demo 的类层探针真跑出来了（15 条），逐条标注。
CLASS: dict[str, Meta] = {
    "CLASS-LSP-POSTCONDITION-CHANGED": _m(
        "class.inheritance_design", "exact",
        "ShopOrder 覆写 touch() 后是否把父类承诺的审计信息抹掉了？",
        "class", 0.95, "defect", ("CWE-840",),
        "inheritance.touch_postcondition_changed",
        note="BaseEntity <- AuditableEntity <- ShopOrder 三层继承；"
             "AuditableEntity.touch(actor) 承诺 updatedBy=actor，ShopOrder 覆写后置成 null。"
             "反射实测：覆写方 updatedBy=null，继承方 updatedBy=actor"),
    "CLASS-ENCAPSULATION-LEAK-ENTITY-COLLECTION": _m(
        "class.encapsulation_leak", "exact",
        "public 方法是否把内部可变实体集合直接交出去？",
        "class", 0.92, "defect", ("CWE-495",),
        "service.product_service_returns_internal_entities",
        note="ProductService.snapshot() 返回 List<ShopProduct>（JPA 实体本身），"
             "调用方改一个字段就改了服务层内部状态"),
    "CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE": _m(
        "class.invariant", "exact",
        "同一请求在两个 profile 实例上是否算出不同的结算金额？",
        "class", 0.94, "defect", ("CWE-840",),
        "pricing.member_tier_bound_to_deployment_profile",
        note="VIP 是客户等级（业务维度），却用 @Profile（部署维度）表达。"
             "实测：default 实例 settled=1000，vip 实例 settled=900。"
             "本条是集成层涌现链（EMERG-QUOTA-LEDGER-DIVERGENCE）的低层根因之一"),
    "CLASS-RULE-MISSING-IN-ALL-LAYERS": _m(
        "class.invariant", "exact",
        "那 3 条业务不变量在 DB / DTO / 服务层是否全都不存在？",
        "class", 0.85, "defect", ("CWE-840",),
        "rule.missing_in_db_dto_service"),
    "CLASS-BUSINESS-ERROR-AS-500": _m(
        "class.contract_violation", "exact",
        "业务规则失败是否被当成服务器错误抛出去？",
        "class", 0.90, "defect", ("CWE-755",),
        "error.business_error_as_500",
        note="客户不存在是业务错误，实测却返回 HTTP 500，响应体里没有业务错误码"),
    "CLASS-ENTITY-WITHOUT-VALUE-EQUALITY": _m(
        "class.invariant", "stretched",
        "实体是否没有值相等（equals/hashCode）？",
        "class", 0.80, "defect", ("CWE-595",),
        "entity.no_value_equality",
        note="按对象身份比较而不是按业务主键：Set 去重失效、Map 键匹配不上"),
    "CLASS-CACHE-HOLDS-MUTABLE-CONTAINER": _m(
        "class.encapsulation_leak", "exact",
        "@Cacheable 是否缓存了可变容器本身？",
        "class", 0.88, "defect", ("CWE-471",),
        "cache.holds_mutable_container",
        note="ProductService.listCached() 缓存 List<ProductResponse> 实例本身，"
             "调用方 add/remove 就是在改缓存，后续所有请求都读到被改过的值"),
    "NO_DATA_LIFECYCLE": _m(
        "class.invariant", "stretched",
        "shop_order 表是否没有软删或归档、永久增长？",
        "class", 0.80, "defect", (),
        "shop_order.no_data_lifecycle",
        note="订单是持续增长的事实表；customer_name 只是字符串引用，没有外键兜底"),
    "CLASS-PRIMITIVE-NULLABLE-BY-DESIGN": _m(
        "class.contract_violation", "exact",
        "DTO 用原始类型 long 承载 DB 的 NOT NULL 列，缺省是否会静默变成 0？",
        "class", 0.90, "defect", ("CWE-20",),
        "dto.primitive_hides_missing_required_field",
        note="原始类型表达不了「没传」：缺省与显式 null 都变成 0，而 0 恰好满足 @Min(0)，"
             "DTO 的校验看着像防御，实际拦不住任何东西"),
    "CLASS-CHECK-WITHOUT-DTO-COUNTERPART": _m(
        "class.invariant", "stretched",
        "那 3 条 DB CHECK 是否在 DTO 层找不到同义约束？",
        "class", 0.75, "defect", (),
        "rule.check_without_dto_counterpart",
        note="规则只写在迁移文件里，Java 侧读不到、也无法在报错前拦截"),
    "CLASS-NULLABILITY-ALIGNED": _m(
        "class.contract_violation", "exact",
        "可空性三方声明是否一致？",
        "class", 0.95, "positive", (),
        "nullability.three_way_aligned"),
    "CLASS-CHECK-DTO-ALIGNED": _m(
        "class.invariant", "exact",
        "DB CHECK 与 DTO 约束是否对齐？",
        "class", 0.95, "positive", (),
        "check.dto_aligned"),
}

# 其余三层：demo 目前还没有这三层的 findings，等真跑出来再标注。
FUNCTION: dict[str, Meta] = {}

# 文件层（demos/shop/file_findings_config.json）。demo 之前没有文件层的开单，
# 因为差分判定层还是 jiuyan 专属的。现在判定层通用化了，demo 自己的语义在这里声明。
FILE: dict[str, Meta] = {
    "FILE-QUOTA-BASIS-QUOTED-VS-SETTLED": _m(
        "file.contract_alignment", "exact",
        "OrderService 同一文件里，额度检查与记账是否用了同一个订单金额？",
        "file", 0.93, "defect", ("CWE-682",),
        "order.quota_checked_on_quoted_amount_ledger_on_settled",
        note="额度按报价卡、账按结算记 —— 结算口径下过得去的单被拒了。"
             "函数层看不到（两处单看都对），类层也看不到（跨了两个类）—— 只有文件层能同时看见。"
             "服务层的 BI-QUOTA-LEDGER-PARITY 是同一根因的端到端差分证据（单条件不违反、"
             "双条件才违反），它引用本条不开单"),
    "FILE-FX-FALLBACK-WRONG-SCALE": _m(
        "file.error_handling_pattern", "exact",
        "跨仓库汇率取不到时的兜底值，与正常刻度是不是同一个量纲？",
        "file", 0.90, "defect", ("CWE-682",),
        "cross_repo.fx_fallback_returns_wrong_scale",
        note="正常刻度 100（=1.0），catch 分支兜底成 1 —— 按 settled*rate/100 折算，"
             "rate=1 意味着结算额只剩 1%。跨仓库契约没声明刻度，调用方默默假设，"
             "兜底值又写错了量纲。对方不可达时才现形，编译期与运行期都不报错"),
}

# 服务层：业务不变式的实测判定（demos/shop/business_probe.py）。
# 两个不变式、两个不同的归属 —— 这不是随手填的：
#   · 额度/账本的口径分裂需要两个条件同时成立才出现，服务层能给出可复现的
#     差分证据（3 个场景，只有「VIP 客户 + 会员装配」这一组违反），
#     所以 owner = service（own）。
#   · VIP 拿不到会员价，根因是「业务维度被绑到部署维度」—— 那是类层的结构问题，
#     类层已经 own（CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE）。
#     服务层这��只是行为上的佐证，**不重复开单**（relation = cite）。
SERVICE: dict[str, Meta] = {
    "BI-QUOTA-LEDGER-PARITY": _m(
        "service.data_consistency", "exact",
        "额度占用额（通过授信额度检查的报价）是否等于账本记录额（实付）？",
        "file", 0.93, "defect", ("CWE-840",),
        "order.quota_checked_on_quoted_amount_ledger_on_settled",
        relation="cite",
        note="服务层 OrderService.create 里额度检查用 quotedAmountCent、账本记录用 settled —— "
             "两个口径。单条件都不分裂：普通客户 + 会员装配不分裂，VIP 客户 + 标准装配也不分裂；"
             "两个条件同时成立才分裂（实测 3 个场景里只有这一组违反）。"
             "服务层是能给可复现证据的最低层，故 owner 归 service"),
    "BI-VIP-MEMBER-PRICE": _m(
        "service.data_consistency", "exact",
        "VIP 客户的结算额是否享受了会员价（报价的 9/10）？",
        "class", 0.92, "defect", ("CWE-840",),
        "pricing.member_tier_bound_to_deployment_profile",
        relation="cite",
        note="服务层实测到：VIP 客户在标准装配下拿不到九折（结算额 = 报价额）。"
             "根因是「业务维度（客户等级）被绑到部署维度（@Profile）」—— 那是类层的结构问题，"
             "类层已经 own（CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE）。本层只作行为佐证，不重复开单"),
}

META: dict[str, dict[str, Meta]] = {
    "function": FUNCTION,
    "file": FILE,
    "service": SERVICE,
    "class": CLASS,
    "integration": INTEGRATION,
}

# 类层实体定位提示（demo 暂无类层 findings，先留空，接口与 pilot 一致）
CLASS_FILE_HINTS: dict[str, str] = {
    "CLASS-LSP-POSTCONDITION-CHANGED": "entity/ShopOrder.java",
    "CLASS-ENCAPSULATION-LEAK-ENTITY-COLLECTION": "service/ProductService.java",
    "CLASS-CONDITIONAL-ASSEMBLY-DIVERGENCE": "service/MemberPricingStrategy.java",
    "CLASS-RULE-MISSING-IN-ALL-LAYERS": "",
    "CLASS-BUSINESS-ERROR-AS-500": "service/OrderService.java",
    "CLASS-ENTITY-WITHOUT-VALUE-EQUALITY": "entity/ShopOrder.java",
    "CLASS-CACHE-HOLDS-MUTABLE-CONTAINER": "service/ProductService.java",
    "NO_DATA_LIFECYCLE": "entity/ShopOrder.java",
    "CLASS-PRIMITIVE-NULLABLE-BY-DESIGN": "web/dto/CreateOrderRequest.java",
    "CLASS-CHECK-WITHOUT-DTO-COUNTERPART": "",
    "CLASS-NULLABILITY-ALIGNED": "",
    "CLASS-CHECK-DTO-ALIGNED": "",
}
COLUMN_FILE_HINTS: dict[str, str] = {}


def lookup(layer: str, finding_id: str) -> Meta | None:
    return META.get(layer, {}).get(finding_id)
