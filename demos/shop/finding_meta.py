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

# 其余四层：demo 目前还没有这四层的 findings，等真跑出来再标注。
FUNCTION: dict[str, Meta] = {}
FILE: dict[str, Meta] = {}
SERVICE: dict[str, Meta] = {}
CLASS: dict[str, Meta] = {}

META: dict[str, dict[str, Meta]] = {
    "function": FUNCTION,
    "file": FILE,
    "service": SERVICE,
    "class": CLASS,
    "integration": INTEGRATION,
}

# 类层实体定位提示（demo 暂无类层 findings，先留空，接口与 pilot 一致）
CLASS_FILE_HINTS: dict[str, str] = {}
COLUMN_FILE_HINTS: dict[str, str] = {}


def lookup(layer: str, finding_id: str) -> Meta | None:
    return META.get(layer, {}).get(finding_id)
