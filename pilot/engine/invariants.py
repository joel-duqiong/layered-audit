"""业务不变式求值器（通用引擎）。

一条不变式 = 「实体的一个字段关系必须恒成立」。它有两半：

- **声明**（项目配置）：字段之间是什么关系 —— 在 target.json / invariants.json 里
- **判定**（本模块）：通用原语，认得字段名、认得 JSONB 路径，不认得业务

换项目 = 换声明，判定器不动。

支持的断言原语：
  field_lte_field      字段 A ≤ 字段 B
  field_gte_field      字段 A ≥ 字段 B
  field_eq_field       字段 A == 字段 B
  field_lte_const      字段 A ≤ 常量
  field_gte_const      字段 A ≥ 常量
  field_in_set         字段 A ∈ 集合
  jsonb_sum_eq_field   JSONB 数组里 Σ(单价×数量) == 某字段
  jsonb_subset_of      JSONB 数组的元素 id 集合 ⊆ 另一个 JSONB 数组的 id 集合

字段名用**数据库列名**（snake_case）—— 判定器不认映射，映射是项目侧的事。
"""
from __future__ import annotations

import json
from typing import Any

JSONB_OPS = ("jsonb_sum_eq_field", "jsonb_subset_of")


def _jsonb_path(row: dict[str, Any], path: str) -> Any:
    """按 'package_snapshot.items' 取嵌套值；row 里该列是 JSON 字符串时先解一次。"""
    cur: Any = row
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, str):
            try:
                cur = json.loads(cur)
            except ValueError:
                return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, str):
        try:
            return json.loads(cur)
        except ValueError:
            return None
    return cur


def applies(row: dict[str, Any], invariant: dict[str, Any]) -> bool:
    """不变式的适用条件（when）。不满足则该行不适用这条不变式。"""
    w = invariant.get("when")
    if not w:
        return True
    actual = row.get(w["field"])
    op = w.get("op", "eq")
    expect = w.get("value")
    if op == "eq":
        return actual == expect
    if op == "ne":
        return actual != expect
    if op == "in":
        return actual in (expect or [])
    if op == "not_in":
        return actual not in (expect or [])
    raise ValueError(f"未知的条件算子: {op}")


def evaluate(row: dict[str, Any], invariant: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """对一行数据判定一条不变式。返回 (是否成立, 细节)。"""
    a = invariant["assert"]
    kind = a["type"]
    detail: dict[str, Any] = {"type": kind}

    if kind == "field_lte_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        ok = left is not None and right is not None and left <= right
    elif kind == "field_gte_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        ok = left is not None and right is not None and left >= right
    elif kind == "field_eq_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        ok = left == right
    elif kind == "field_lte_const":
        left = row.get(a["left"])
        detail.update(left=left, limit=a["value"])
        ok = left is not None and left <= a["value"]
    elif kind == "field_gte_const":
        left = row.get(a["left"])
        detail.update(left=left, limit=a["value"])
        ok = left is not None and left >= a["value"]
    elif kind == "field_in_set":
        left = row.get(a["left"])
        detail.update(left=left, allowed=a["value"])
        ok = left in a["value"]
    elif kind == "jsonb_sum_eq_field":
        items = _jsonb_path(row, a["items_path"]) or []
        total = sum(int(i.get(a["value_field"], 0)) * int(i.get(a["qty_field"], 1)) for i in items)
        right = row.get(a["equals"])
        detail.update(items_count=len(items), sum_total=total, field=right)
        ok = total == right
    elif kind == "jsonb_subset_of":
        items = _jsonb_path(row, a["items_path"]) or []
        ref = _jsonb_path(row, a["ref_path"]) or []
        item_ids = {i.get(a["id_field"]) for i in items}
        ref_ids = {i.get(a["ref_id_field"]) for i in ref}
        outside = sorted(x for x in item_ids if x not in ref_ids)
        detail.update(item_ids=sorted(x for x in item_ids if x is not None),
                      ref_ids=sorted(x for x in ref_ids if x is not None),
                      outside_ref=outside)
        ok = not outside
    else:
        raise ValueError(f"未知的断言原语: {kind}")

    detail["ok"] = ok
    return ok, detail


def deviation(detail: dict[str, Any]) -> float:
    """从一次判定的细节算出「偏离量」—— 违反得有多严重（0 = 没违反）。

    涌现裁决要用它比较量级（合起来错得是否比单独错得更狠）。判定细节里的
    left/right/value 已经是具体数值，这里只做差，不做解释。
    """
    kind = detail.get("type")

    def num(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    if kind == "field_eq_field":
        return abs(num(detail.get("left")) - num(detail.get("right")))
    if kind == "field_lte_field":
        return max(0.0, num(detail.get("left")) - num(detail.get("right")))
    if kind == "field_gte_field":
        return max(0.0, num(detail.get("right")) - num(detail.get("left")))
    if kind == "field_lte_const":
        return max(0.0, num(detail.get("left")) - num(detail.get("value")))
    if kind == "field_gte_const":
        return max(0.0, num(detail.get("value")) - num(detail.get("left")))
    if kind == "field_in_set":
        return 0.0 if detail.get("ok") else 1.0
    return 0.0


def evaluate_all(rows: list[dict[str, Any]],
                 invariants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对一批行跑一批不变式，返回每条不变式的判定汇总。"""
    out: list[dict[str, Any]] = []
    for inv in invariants:
        failures: list[dict[str, Any]] = []
        checked = 0
        skipped = 0
        for row in rows:
            if not applies(row, inv):
                skipped += 1
                continue
            checked += 1
            ok, detail = evaluate(row, inv)
            if not ok:
                failures.append({"row_id": row.get("id"), **detail})
        out.append({
            "id": inv["id"],
            "claim": inv.get("claim", ""),
            "severity": inv.get("severity", "MEDIUM"),
            "category": inv.get("category", ""),
            "why": inv.get("why", ""),
            "when": inv.get("when"),
            "checked": checked,
            "skipped": skipped,
            "violations": len(failures),
            "failure_samples": failures[:3],
            "ok": not failures,
            "applicable": checked > 0,
        })
    return out
