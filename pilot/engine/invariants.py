"""业务不变式求值器（通用引擎）。

一条不变式 = 「实体的一个字段关系必须恒成立」。它有两半：

- **声明**（项目配置）：字段之间是什么关系 —— 在 target.json / invariants.json 里
- **判定**（本模块）：通用原语，认得字段名、认得 JSONB 路径，不认得业务

换项目 = 换声明，判定器不动。

支持的断言原语：
  field_lte_field      字段 A <= 字段 B
  field_gte_field      字段 A >= 字段 B
  field_eq_field       字段 A == 字段 B
  field_lte_const      字段 A <= 常量
  field_gte_const      字段 A >= 常量
  field_gt_const       字段 A > 常量（严格大于；等于常量判违反，不得用 >= 冒充）
  field_in_set         字段 A 属于集合
  field_not_null       字段存在且值非 null（缺字段 -> unknown；null -> fail）
  field_not_blank      字段非 null 且非纯空白字符串（只认字符串，其余类型 -> unknown）
  field_size           字符串字符数或集合元素数落在 [min, max]（两种计数不得混）
  field_pattern        字符串全匹配正则（锚定语义，不得放宽成部分匹配）
  text_not_contains    字符串不得含任一禁用词（字面子串，逐字按声明）
  field_default_when_absent  条件契约「不传该字段时落默认值」：字段存在 -> not_applicable
  jsonb_sum_eq_field   JSONB 数组里 sum(单价*数量) == 某字段
  jsonb_subset_of      JSONB 数组的元素 id 集合 是 另一个 JSONB 数组 id 集合的子集

字段名用**数据库列名**（snake_case）—— 判定器不认映射，映射是项目侧的事。

## 三值判定（硬规矩）

`evaluate` 只返回三态：`pass` / `fail` / `unknown`，外加条件契约专用的
`not_applicable`（字段存在时「不传该字段时…」的契约不适用 —— 不适用不是成立，
也不是判不出来，汇总时按跳过计，绝不许洗成 pass）。

**任何「我不知道」的情况都返回 unknown，绝不返回 pass。** 曾经的实现有三处把
「不知道」当成了「成立」：

  * `field_eq_field` 两侧都是 None 时 `None == None` -> True
  * `jsonb_subset_of` 两列都取不到时两个空集 -> 没有 OUTSIDE -> True
  * 一行都没适用时 `ok = not failures` -> True

后果是**任何一个字段名拼错，都会得到一张「通过」的成绩单**。现在这三种情况
都是 unknown，并在 detail 里写清原因。同理，`jsonb_sum_eq_field` 取不到列时
以前会算成 `0 != None` -> 判违反（假红），现在也是 unknown。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

JSONB_OPS = ("jsonb_sum_eq_field", "jsonb_subset_of")

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"


class InvariantConfigError(ValueError):
    """不变式声明本身有错（字段名写错、算子不支持）—— 与「数据不满足」区分开。"""


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


def applies(row: dict[str, Any], invariant: dict[str, Any]) -> bool | None:
    """不变式的适用条件（when）。

    返回三态：
      True  = 适用
      False = 数据明确不满足适用条件（例如不是 VIP 客户）
      None  = **判不出来**（when 引用的字段在这一行里不存在，或值为空）

    None 与 False 必须分开：False 是「这行本来就不归它管」，
    None 是「配置里的字段名对不上数据」—— 后者会让整条不变式静默失效。
    """
    w = invariant.get("when")
    if not w:
        return True
    if isinstance(w, list):
        results = [applies(row, {**invariant, "when": cond}) for cond in w]
        if any(r is None for r in results):
            return None
        return all(results)
    field = w.get("field")
    if field not in row:
        return None
    actual = row.get(field)
    op = w.get("op", "eq")
    expect = w.get("value")
    if actual is None:
        # 条件字段是空值：判不出来它属不属于这个条件。
        # （「字段不存在就当不适用」同等危险，见模块文档。）
        return None
    if op == "eq":
        return actual == expect
    if op == "ne":
        return actual != expect
    if op == "in":
        return actual in (expect or [])
    if op == "not_in":
        return actual not in (expect or [])
    raise InvariantConfigError(f"未知的条件算子: {op}")


def _missing(detail: dict[str, Any], *fields: str) -> tuple[bool, str]:
    """检查判定所需的输入是否齐备；不齐备就说清缺什么。"""
    missing = [f for f in fields if detail.get(f) is None]
    if missing:
        return True, f"字段缺失或为空（{', '.join(missing)}）—— 无法判定"
    return False, ""


def _finite_number(value: Any) -> bool:
    """数值比较原语只认有限数值；bool 不是数，NaN/inf 判不出来。"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _size_of(value: Any) -> tuple[str, int] | None:
    """字符串数的是字符，集合数的是元素 —— 两种计数不得混。其余类型判不出来。"""
    if isinstance(value, str):
        return "chars", len(value)
    if isinstance(value, list):
        return "elements", len(value)
    return None


def _pattern_matches(text: str, pattern: str) -> bool:
    """全匹配：原文的 ^...$ 锚定语义逐字保留，放宽成部分匹配就是放宽判据。"""
    return re.fullmatch(pattern, text) is not None


def evaluate(row: dict[str, Any], invariant: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """对一行数据判定一条不变式。返回 (结论, 细节)；结论为 pass/fail/unknown/not_applicable。"""
    a = invariant["assert"]
    kind = a["type"]
    detail: dict[str, Any] = {"type": kind}
    why = ""

    if kind == "field_lte_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        gap, why = _missing(detail, "left", "right")
        verdict = UNKNOWN if gap else (PASS if left <= right else FAIL)
    elif kind == "field_gte_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        gap, why = _missing(detail, "left", "right")
        verdict = UNKNOWN if gap else (PASS if left >= right else FAIL)
    elif kind == "field_eq_field":
        left, right = row.get(a["left"]), row.get(a["right"])
        detail.update(left=left, right=right)
        gap, why = _missing(detail, "left", "right")
        # 两侧都缺失时 `None == None` 会是 True —— 这是最隐蔽的假绿。
        verdict = UNKNOWN if gap else (PASS if left == right else FAIL)
    elif kind == "field_lte_const":
        left = row.get(a["left"])
        detail.update(left=left, limit=a["value"])
        gap, why = _missing(detail, "left")
        verdict = UNKNOWN if gap else (PASS if left <= a["value"] else FAIL)
    elif kind == "field_gte_const":
        left = row.get(a["left"])
        detail.update(left=left, limit=a["value"])
        gap, why = _missing(detail, "left")
        verdict = UNKNOWN if gap else (PASS if left >= a["value"] else FAIL)
    elif kind == "field_in_set":
        left = row.get(a["left"])
        detail.update(left=left, allowed=a["value"])
        gap, why = _missing(detail, "left")
        verdict = UNKNOWN if gap else (PASS if left in a["value"] else FAIL)
    elif kind == "field_not_null":
        # 缺字段与 null 必须分开：缺字段是「判不出来」（字段名可能拼错），
        # null 是明确违反。缺字段若判 pass，拼错字段名就会得到一张通过的成绩单。
        if a["left"] not in row:
            detail.update(left=None)
            verdict = UNKNOWN
            why = f"字段缺失（{a['left']}）—— 无法判定"
        else:
            left = row[a["left"]]
            detail.update(left=left)
            verdict = FAIL if left is None else PASS
    elif kind == "field_not_blank":
        if a["left"] not in row:
            detail.update(left=None)
            verdict = UNKNOWN
            why = f"字段缺失（{a['left']}）—— 无法判定"
        else:
            left = row[a["left"]]
            detail.update(left=left)
            if left is None:
                verdict = FAIL
            elif not isinstance(left, str):
                verdict = UNKNOWN
                why = f"类型不兼容（{type(left).__name__}）—— not_blank 只认字符串"
            else:
                verdict = PASS if left.strip() else FAIL
    elif kind == "field_gt_const":
        left = row.get(a["left"])
        detail.update(left=left, limit=a["value"])
        gap, why = _missing(detail, "left")
        if gap:
            verdict = UNKNOWN
        elif not _finite_number(left) or not _finite_number(a["value"]):
            verdict = UNKNOWN
            why = "类型不兼容 —— field_gt_const 要求两侧都是有限数值（bool 不是数）"
        else:
            verdict = PASS if left > a["value"] else FAIL
    elif kind == "field_size":
        lo, hi = a.get("min"), a.get("max")
        if lo is None and hi is None:
            raise InvariantConfigError("field_size 声明缺 min/max —— 无界断言不构成判据")
        for bound in (lo, hi):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or bound < 0):
                raise InvariantConfigError(f"field_size 边界 {bound!r} 必须是非负整数")
        left = row.get(a["left"])
        detail.update(left=left, min=lo, max=hi)
        gap, why = _missing(detail, "left")
        if gap:
            verdict = UNKNOWN
        else:
            sized = _size_of(left)
            if sized is None:
                verdict = UNKNOWN
                why = f"类型不兼容（{type(left).__name__}）—— size 只认字符串（字符数）或数组（元素数）"
            else:
                kind_of_size, n = sized
                detail.update(size=n, size_kind=kind_of_size)
                ok = (lo is None or n >= lo) and (hi is None or n <= hi)
                verdict = PASS if ok else FAIL
    elif kind == "field_pattern":
        left = row.get(a["left"])
        detail.update(left=left, pattern=a["value"])
        gap, why = _missing(detail, "left")
        if gap:
            verdict = UNKNOWN
        elif not isinstance(left, str):
            verdict = UNKNOWN
            why = f"类型不兼容（{type(left).__name__}）—— pattern 只认字符串"
        else:
            try:
                matched = _pattern_matches(left, a["value"])
            except re.error as exc:
                raise InvariantConfigError(f"field_pattern 正则不合法: {exc}") from exc
            verdict = PASS if matched else FAIL
    elif kind == "text_not_contains":
        terms = a["value"]
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) for t in terms):
            raise InvariantConfigError("text_not_contains 的 value 必须是非空字符串列表")
        left = row.get(a["left"])
        detail.update(left=left, terms=terms)
        gap, why = _missing(detail, "left")
        if gap:
            verdict = UNKNOWN
        elif not isinstance(left, str):
            verdict = UNKNOWN
            why = f"类型不兼容（{type(left).__name__}）—— text_not_contains 只认字符串"
        else:
            hits = [t for t in terms if t in left]
            detail.update(hits=hits)
            verdict = FAIL if hits else PASS
    elif kind == "field_default_when_absent":
        # 条件契约「不传该字段时落默认值」：字段存在 ⇒ 条件不成立 ⇒ 不适用，
        # 不是 PASS（契约没被考验，谈不上成立）。字段缺失 ⇒ 契约被触发，但
        # 「落没落默认值」要看结果态观测，单行输入观测核验不了 → UNKNOWN，不猜。
        detail.update(default=a["value"])
        if a["left"] in row:
            detail.update(left=row[a["left"]])
            verdict = NOT_APPLICABLE
            why = f"字段 {a['left']!r} 存在 —— 「不传该字段时」的条件不成立，判据不适用"
        else:
            verdict = UNKNOWN
            why = f"字段 {a['left']!r} 缺失，契约被触发；默认值是否落地需结果态观测，当前判不出来"
    elif kind == "jsonb_sum_eq_field":
        items = _jsonb_path(row, a["items_path"])
        right = row.get(a["equals"])
        detail.update(items_count=None if items is None else len(items), field=right)
        gap, why = _missing(detail, "items_count", "field")
        if gap:
            detail["sum_total"] = None
            verdict = UNKNOWN
        else:
            try:
                total = sum(int(i.get(a["value_field"], 0)) * int(i.get(a["qty_field"], 1))
                            for i in items)
            except (TypeError, ValueError) as exc:
                detail["sum_total"] = None
                verdict = UNKNOWN
                why = f"明细元素类型不可计算（{exc}）"
            else:
                detail["sum_total"] = total
                verdict = PASS if total == right else FAIL
    elif kind == "jsonb_subset_of":
        items = _jsonb_path(row, a["items_path"])
        ref = _jsonb_path(row, a["ref_path"])
        detail.update(items_count=None if items is None else len(items),
                      ref_count=None if ref is None else len(ref))
        gap, why = _missing(detail, "items_count", "ref_count")
        if gap:
            detail.update(item_ids=[], ref_ids=[], outside_ref=[])
            verdict = UNKNOWN
        else:
            # 先取出原始值再看「有值的有几个」—— 元素里缺这个键时得到的是 None，
            # {None} 是个**非空集合**，直接判 truthy 会把「字段名对不上」漏过去。
            raw_item_ids = {i.get(a["id_field"]) for i in items if isinstance(i, dict)}
            raw_ref_ids = {i.get(a["ref_id_field"]) for i in ref if isinstance(i, dict)}
            item_ids = {x for x in raw_item_ids if x is not None}
            ref_ids = {x for x in raw_ref_ids if x is not None}
            # 元素全都没有这个 id 字段 ⇒ 字段名对不上，判不出来
            # （不是「集合为空所以成立」）
            if (items and not item_ids) or (ref and not ref_ids):
                detail.update(item_ids=[], ref_ids=[], outside_ref=[], missing_id_field=True)
                verdict = UNKNOWN
                why = f"明细元素里没有 {a['id_field']!r} / {a['ref_id_field']!r} 字段 —— 无法比对"
            else:
                outside = sorted(x for x in item_ids if x not in ref_ids)
                detail.update(item_ids=sorted(item_ids, key=str),
                              ref_ids=sorted(ref_ids, key=str),
                              outside_ref=outside)
                verdict = PASS if not outside else FAIL
    else:
        raise InvariantConfigError(f"未知的断言原语: {kind}")

    detail["verdict"] = verdict
    # ok 保留为兼容字段，但只在有明确结论时才有布尔值 —— unknown/not_applicable 时是 None，
    # 任何 `if detail["ok"]` 的旧读法都不会把未知或不适用当成成立。
    detail["ok"] = True if verdict == PASS else False if verdict == FAIL else None
    if verdict == UNKNOWN and why:
        detail["why_unknown"] = why
    if verdict == NOT_APPLICABLE and why:
        detail["why_not_applicable"] = why
    return verdict, detail


def deviation(detail: dict[str, Any]) -> float:
    """从一次判定的细节算出「偏离量」—— 违反得有多严重（0 = 没违反）。

    涌现裁决要用它比较量级（合起来错得是否比单独错得更狠）。

    unknown 的偏离量是 0 —— 它不是「没违反」，而是「量不出来」；
    调用方必须自己看 verdict，不能只看偏离量。
    """
    if detail.get("verdict") == UNKNOWN:
        return 0.0
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
        return 0.0 if detail.get("verdict") == PASS else 1.0
    if kind in ("field_not_null", "field_not_blank", "field_pattern", "text_not_contains"):
        return 0.0 if detail.get("verdict") == PASS else 1.0
    if kind == "field_gt_const":
        return max(0.0, num(detail.get("limit")) - num(detail.get("left")))
    if kind == "field_size":
        if detail.get("verdict") != FAIL:
            return 0.0
        n, lo, hi = num(detail.get("size")), detail.get("min"), detail.get("max")
        if lo is not None and n < lo:
            return float(lo - n)
        if hi is not None and n > hi:
            return float(n - hi)
        return 0.0
    return 0.0


def evaluate_all(rows: list[dict[str, Any]],
                 invariants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对一批行跑一批不变式，返回每条不变式的判定汇总。

    汇总的结论也是**三态**：
      fail    = 至少一行被判定为违反（有明确证据）
      pass    = 至少一行被判定为成立，且没有一行违反
      unknown = 没有一行被判定成 pass/fail（全部缺失、全部不适用、或无法判定）

    行级 not_applicable（条件契约的字段存在）按跳过计：不算违反也不算成立，
    全部不适用时汇总是 unknown —— 「没考验到」不是「通过了」。

    **`ok` 字段：pass -> True，fail -> False，unknown -> None。**
    unknown 时 ok 是 None 而不是 True —— 这是「没查到」与「查了没问题」的物理隔离。
    """
    out: list[dict[str, Any]] = []
    for inv in invariants:
        failures: list[dict[str, Any]] = []
        unknown_rows: list[dict[str, Any]] = []
        checked = 0
        skipped = 0
        undecidable = 0
        not_applicable = 0
        for row in rows:
            applicability = applies(row, inv)
            if applicability is None:
                undecidable += 1
                continue
            if applicability is False:
                skipped += 1
                continue
            checked += 1
            verdict, detail = evaluate(row, inv)
            if verdict == FAIL:
                failures.append({"row_id": row.get("id"), **detail})
            elif verdict == UNKNOWN:
                unknown_rows.append({"row_id": row.get("id"), **detail})
            elif verdict == NOT_APPLICABLE:
                not_applicable += 1

        decided = checked - len(unknown_rows) - not_applicable
        if failures:
            status = FAIL
        elif decided > 0:
            status = PASS
        else:
            status = UNKNOWN
        out.append({
            "id": inv["id"],
            "claim": inv.get("claim", ""),
            "severity": inv.get("severity", "MEDIUM"),
            "category": inv.get("category", ""),
            "why": inv.get("why", ""),
            "when": inv.get("when"),
            "oracle": inv.get("oracle"),
            "checked": checked,
            "skipped": skipped,
            "undecidable_rows": undecidable,
            "unknown_rows": len(unknown_rows),
            "not_applicable_rows": not_applicable,
            "violations": len(failures),
            "failure_samples": failures[:3],
            "unknown_samples": unknown_rows[:3],
            "status": status,
            "ok": True if status == PASS else False if status == FAIL else None,
            "applicable": checked - not_applicable > 0,
        })
    return out
