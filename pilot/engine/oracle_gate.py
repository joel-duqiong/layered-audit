"""oracle 来源闸门（通用引擎，零项目字面量）。

硬约束 5：每条业务不变式必须声明 `oracle` 来源，只有 `confirmation: confirmed`
的才允许进缺陷台账。三态：

    confirmed    有文档/业务方明确依据  -> 可以开单
    unconfirmed  推断的预期            -> 只进待确认疑点（规范层风险）
    refuted      文档明确说这条规则未定义 -> 只保留观测，不判缺陷

**闸门只对声明了 oracle 的条目生效。** 语言级 / 平台级客观事实（空入参 NPE、
IEEE754 精度丢失、并发撞唯一键）不需要业务预期，闸门不能误伤它们。
缺声明时放行 —— 这是**显式声明的口径**，不是被忽略的洞；要收紧成「缺声明一律
unconfirmed」是一次有意的契约变更，必须同时裁定该门现存的全部条目。

被压下来的条目必须留痕（`held`），由调用方写进各自的疑点清单：
观测是真的，撤掉的只是「所以这是缺陷」这个断言。
"""
from __future__ import annotations

from typing import Any

STATES = ("confirmed", "unconfirmed", "refuted")


def confirmation(oracle: dict[str, Any] | None) -> str:
    """oracle 的确认状态。未声明返回 ""；声明了但取值非法一律按 unconfirmed。"""
    if not oracle:
        return ""
    state = str(oracle.get("confirmation") or "")
    return state if state in STATES else "unconfirmed"


def split(oracle: dict[str, Any] | None, items: list[dict[str, Any]],
          rule_id: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 oracle 把条目分成 (可开单, 只留痕)。

    留痕记录带上原结论与条目 id，人工复核才看得到被压下来的是什么。
    """
    state = confirmation(oracle)
    if state in ("", "confirmed"):
        return list(items), []
    held = [{"rule_id": rule_id,
             "oracle": {**(oracle or {}), "confirmation": state},
             "finding": item} for item in items]
    return [], held


def buckets(held: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把留痕按确认状态分桶，供调用方写疑点清单。"""
    out: dict[str, list[dict[str, Any]]] = {"unconfirmed": [], "refuted": []}
    for h in held:
        state = h["oracle"]["confirmation"]
        if state in out:
            out[state].append(h)
    return out


# ------------------------------------------------- 判据来源二分（A 技术 / B 业务）
WARRANT_KINDS = ("technical", "business")

UNWARRANTED_ORACLE = {
    "source": "unwarranted",
    "confirmation": "unconfirmed",
    "reference": "",
    "note": "未声明判据来源（warrant）：无法区分「技术可自证」与「业务预期」，"
            "保守不开单。补 warrant 后自动恢复判定 —— 这不是已排除，是未裁定。",
}


def warrant_split(warrant: dict[str, Any] | None, items: list[dict[str, Any]],
                  rule_id: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按**判据来源**分流，而不是按有没有 oracle 一刀切。

    分类判据（逐条可复核，声明即记录）：
      technical  违反通用安全/正确性不变式（空指针、注入、越权、日志伪造、
                 平台语义、并发丢失更新…）。判据出自代码本身或通用 CWE/规范，
                 **域替换测试成立**（换个业务域仍是缺陷）→ 直接开单，不需业务 oracle。
      business   断言「应满足某业务规则」（价格上下界、字段非空白、状态流转、
                 响应契约…）。域替换测试不成立 → 必须有 confirmed oracle。

    **A 类绝不被压**：压掉技术缺陷是漏报，比假阳性更危险。
    **未声明 warrant 一律保守压住**并写明「未裁定」，不静默开单也不静默丢弃。
    """
    kind = (warrant or {}).get("kind")
    if kind == "technical":
        return list(items), []
    if kind == "business":
        # 已明确声明是业务预期，缺 oracle 就是「未确认」，
        # **不得**退化成 split() 的「无声明即客观事实」放行分支。
        oracle = (warrant or {}).get("oracle") or {
            "source": "unspecified", "confirmation": "unconfirmed", "reference": "",
            "note": "声明为业务预期但未给 oracle 来源 —— 按未确认处理，需业务方确认。"}
        return split(oracle, items, rule_id=rule_id)
    return split(UNWARRANTED_ORACLE, items, rule_id=rule_id)