"""Oracle 采集器 —— 从仓库自身已有的产物里挖出「已确认的判据」。

为什么需要它：业务不变式原先靠人手写（实测某目标 6 条，其中 confirmed 只有 1 条），
于是「功能对不对」这一半长期卡在 oracle 供给上。但仓库里本来就躺着大量
**已经被人确认过**的约束：写进迁移的 DDL、写在 DTO 上的校验注解、OpenAPI 的
required/enum。它们不需要再找业务方确认 —— 作者写下它们的那一刻就是确认。

本引擎只做「转录 + 溯源」，不做推断：
  · 每条判据都带 reference（文件:行）与原文，缺原文的不产出
  · 不猜业务意图，不从命名推断规则（那属于 L5，只进疑点清单，本版不做）
  · 产出 confirmation=confirmed 但 gate_eligible=false —— 采集到的是**声明**，
    「声明有没有被实现真正 enforce」是差分层的事（DDL 写了 NOT NULL，
    空串照样能绕过，那是缺陷不是判据失效）

分级：
  L1 executable_constraint  DDL 约束 / Bean Validation 注解
  L2 interface_contract     OpenAPI required / enum / type

零项目字面量：所有输入路径由调用方显式给出。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

LEVEL_BUSINESS = "L0_business_rule"
LEVEL_CONSTRAINT = "L1_executable_constraint"
LEVEL_CONTRACT = "L2_interface_contract"

CONFIRMED = "confirmed"
REFUTED = "refuted"
NEEDS_CONFIRMATION = "needs_confirmation"

# PRD 里能确定性转录的条款形态。转录不了的散文一律进 untranscribed，不猜语义。
_RE_DOC_CMP = re.compile(r"^`?(\w+)`?\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
_RE_DOC_ONLY = re.compile(r"`?(\w+)`?\s*只能是\s*(.+?)\s*$")
_RE_DOC_REGEX = re.compile(r"`?/(\^[^`/\n]+\$)/`?")
_RE_DOC_ALLOW = re.compile(r"(?:只允许|仅支持)\s*(`?[A-Za-z]+`?(?:[、,，/]\s*`?[A-Za-z]+`?)+)")
_RE_DOC_MAXMB = re.compile(r"(?:单文件)?最大\s*(\d+)\s*MB")
_RE_DOC_FORBID = re.compile(r"不要出现[“\"]([^”\"]+)[”\"]等.*字样")
_RE_DOC_DEFAULT = re.compile(r"`?(\w+)`?\s*[:：].*?默认\s*`?(\w+)`?")
_RE_DOC_NULLABLE = re.compile(r"`?([\w.]+)`?\s*(?:可为空|选填|非必填|可以为空|允许为空)")
_RE_VALUE_SEMANTICS = re.compile(r"默认\s*`?\w|>=|<=|>\s*\d|<\s*\d|\d+\s*(?:字符|MB|条|个|天)")
_RE_NOISE = re.compile(r"附和|推理|判断|不确定|压缩|重启|线程|上下文|记忆|工具调用|不要只|明确说|独立验证|科学家")
_RE_BUSINESS = re.compile(r"`\w|默认|\d|必须|不得|只能|禁止|仅|上传|订单|价格|金额|状态|分类|套餐|商品|快照|错误码")
_RE_DOC_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_DOC_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_RE_BACKTICK = re.compile(r"`([^`]+)`")
_RE_PROBLEM_HEADING = re.compile(r"^P(\d+)\s*(.*)")
_RE_RULE_HINT = re.compile(r"必须|不得|不要|只能|应该|应当|要求|优先|至少|仅|不允许|禁止|需要|确保")
_RE_TABLE_CMP = re.compile(r"^([A-Za-z0-9_一-鿿]+)\s*(>=|<=|>|<)\s*(-?\d+)")
_RE_TABLE_ENUM = re.compile(r"^(.+?)仅\s*([A-Z_]+(?:/[A-Z_]+)+)\s*$")
_RE_TABLE_ONLY = re.compile(r"([一-鿿A-Za-z]+?)仅\s*([A-Za-z]+(?:/[A-Za-z]+)+)")
_RE_TABLE_MAX = re.compile(r"最大\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)")
_RE_TABLE_COUNT = re.compile(r"(>=|>)\s*(\d+)\s*([一-鿿]{1,2})")
_RE_TABLE_SUBJECT = re.compile(r"^([一-鿿A-Za-z]+?)(?=仅|最大|>=|<=|>|<|，|,|：|$)")
_RE_TABLE_FIELDS = re.compile(r"^(.+?)（([A-Za-z0-9_一-鿿]+(?:/[A-Za-z0-9_一-鿿]+)+)）")
_RE_TABLE_COVERS = re.compile(r"覆盖\s*([A-Z_]+(?:/[A-Z_]+)+)")
_RE_TABLE_NOTNULL = re.compile(r"`?([\w.]+)`?\s*必须有值")
_RE_TABLE_UNIQUE = re.compile(r"同([一-鿿A-Za-z]+?)同([一-鿿A-Za-z]+?)唯一")
_RE_IF_CMP = re.compile(r"([A-Za-z一-鿿][\w一-鿿]*)\s*(>=|<=|>|<)\s*(\d+)")
_RE_IF_RANGE = re.compile(r"([A-Za-z]\w*)\s*限制在\s*(\d+)\s*-\s*(\d+)")
_RE_IF_ENUM = re.compile(r"([A-Za-z]\w*)\s*必须是\s*([A-Z_]+(?:/[A-Z_]+)+)")
_RE_IF_SIZE = re.compile(r"([A-Za-z]\w*).*?不超过\s*(\d+)\s*字符")
_RE_IF_REQUIRED = re.compile(r"([A-Za-z][\w/]*)\s*必填")
_RE_IF_EXISTS = re.compile(r"([A-Za-z][\w/]*)\s*必须存在")
_RE_IF_ONLY = re.compile(r"仅\s*([a-z]+(?:/[a-z]+)+)")
_RE_IF_MAXMB = re.compile(r"最大\s*(\d+)\s*MB")
_RE_IF_DEFAULT = re.compile(r"([A-Za-z]\w*)\s*默认\s*(true|false|[A-Za-z]\w*)")

# DDL 里能转录成可执行原语的约束形态
_RE_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)\s*\((.*?)\)\s*;",
                        re.IGNORECASE | re.DOTALL)
_RE_CONSTRAINT_CHECK = re.compile(r"CONSTRAINT\s+(\w+)\s+CHECK\s*\((.+?)\)\s*(?:,|$)", re.IGNORECASE)
_RE_CONSTRAINT_UNIQUE = re.compile(r"CONSTRAINT\s+(\w+)\s+UNIQUE\s*\(([^)]+)\)", re.IGNORECASE)
_RE_CONSTRAINT_FK = re.compile(
    r"CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY\s*\((\w+)\)\s*REFERENCES\s+(\w+)\s*\((\w+)\)", re.IGNORECASE)
_RE_INLINE_CHECK = re.compile(r"^\s*(\w+)\s+[^,]*?\bCHECK\s*\((.+?)\)", re.IGNORECASE)
_RE_CMP = re.compile(r"^\s*(\w+)\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")
_RE_IN = re.compile(r"^\s*(\w+)\s+IN\s*\((.+)\)\s*$", re.IGNORECASE)
_RE_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_RE_STRING_TYPE = re.compile(r"\b(?:VARCHAR|CHARACTER|CHAR|TEXT|CLOB|STRING)\b", re.IGNORECASE)
_RE_UNIQUE_INLINE = re.compile(r"\bUNIQUE\b", re.IGNORECASE)
_RE_DEFAULT = re.compile(r"\bDEFAULT\s+('(?:[^']|'')*'|-?\d+(?:\.\d+)?|TRUE|FALSE|NULL)\b", re.IGNORECASE)
_RE_COL = re.compile(r"^\s*(\w+)\s+([A-Za-z]+(?:\s*\([^)]*\))?(?:\s+[A-Za-z]+)*)")
_RE_COL_SKIP = re.compile(r"^\s*(CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK)\b", re.IGNORECASE)

# Bean Validation：注解 → 可执行原语。只收声明式校验，不收 @Transactional 之类
_ANNOTATIONS: dict[str, str] = {
    "NotNull": "not_null", "NotBlank": "not_blank", "NotEmpty": "not_empty",
    "Positive": "positive", "PositiveOrZero": "positive_or_zero",
    "AssertTrue": "assert_true", "AssertFalse": "assert_false",
}
_RE_ANNO_FIELD = re.compile(
    r"((?:@\w+(?:\([^)]*\))?\s*)+)(?:private|protected|public)\s+[\w.<>,\[\]\s]+?\s+(\w+)\s*[;=]")
# Java record 的组件参数：注解直接挂在 record 头上，没有访问修饰符、以逗号分隔。
# 现代 Spring Boot 3 的 DTO 普遍是 record，只解析 class 字段会整类漏采。
_RE_RECORD = re.compile(r"\brecord\s+(\w+)\s*\((.*?)\)\s*\{", re.DOTALL)
_RE_COMPONENT = re.compile(r"^\s*((?:@\w+(?:\([^)]*\))?\s*)*)([\w.<>,\[\]\s]+?)\s+(\w+)\s*$")
_RE_ANNO_VALUE = re.compile(r"@(\w+)\s*(?:\(([^)]*)\))?")
_RE_CLASS = re.compile(r"\b(?:class|record)\s+(\w+)")
_RE_ALTER_ADD = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+([^;]+);", re.IGNORECASE)


def _split_components(body: str) -> list[tuple[str, int]]:
    """按顶层逗号切 record 组件，返回 (组件文本, 在 body 内的起始偏移)。"""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(body)))
    parts: list[tuple[str, int]] = []
    for s, e in spans:
        seg = body[s:e]
        stripped = seg.strip()
        if stripped:
            parts.append((stripped, s + len(seg) - len(seg.lstrip())))
    return parts


def _literal(text: str) -> Any:
    text = text.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.upper() in ("TRUE", "FALSE"):
        return text.upper() == "TRUE"
    return text


def _expr_to_assert(expr: str) -> dict[str, Any] | None:
    """把 CHECK 表达式转录成封闭原语。转录不了就返回 None —— 不猜。"""
    m = _RE_CMP.match(expr)
    if m:
        return {"type": "field_cmp", "field": m.group(1), "op": m.group(2), "value": _literal(m.group(3))}
    m = _RE_IN.match(expr)
    if m:
        values = [_literal(v) for v in m.group(2).split(",")]
        return {"type": "in", "field": m.group(1), "value": values}
    if re.fullmatch(r"\s*\w+\s+IS\s+NOT\s+NULL\s*", expr, re.IGNORECASE):
        return {"type": "not_null", "field": expr.split()[0]}
    return None


def _oracle(oid: str, level: str, kind: str, reference: str, text: str,
            target: dict[str, Any], assertion: dict[str, Any], note: str) -> dict[str, Any]:
    return {
        "id": oid, "level": level, "confirmation": CONFIRMED, "gate_eligible": False,
        "source": {"kind": kind, "reference": reference, "text": text.strip()},
        "target": target, "assert": assertion, "note": note,
    }


def _emit_column_constraints(table: str, col: str, line: str, ref: str,
                             out: list[dict[str, Any]]) -> None:
    """列定义上的约束：NOT NULL / UNIQUE / DEFAULT / 列级 CHECK。"""
    if _RE_NOT_NULL.search(line):
        out.append(_oracle(f"ddl:{table}.{col}.not_null", LEVEL_CONSTRAINT, "ddl", ref,
                           line, {"table": table, "field": col},
                           {"type": "not_null", "field": col},
                           "非空约束。注意：NOT NULL 挡不住空串，enforce 与否要差分"))
        if _RE_STRING_TYPE.search(line):
            out.append({
                "id": f"ddl:{table}.{col}.not_blank_candidate", "level": LEVEL_CONSTRAINT,
                "confirmation": NEEDS_CONFIRMATION, "gate_eligible": False,
                "source": {"kind": "inferred", "reference": ref, "text": line},
                "target": {"table": table, "field": col},
                "assert": {"type": "not_blank", "field": col},
                "note": "DDL 只声明 NOT NULL，空串可绕过（address:'' 判 PASS）；非空白是推断的"
                        "业务意图，需业务方确认。从原 DDL 行派生的候选而非原文，确认前不得进判定",
            })
    if _RE_UNIQUE_INLINE.search(line):
        out.append(_oracle(f"ddl:{table}.{col}.unique", LEVEL_CONSTRAINT, "ddl", ref,
                           line, {"table": table, "field": col},
                           {"type": "unique", "fields": [col]}, "列级唯一约束"))
    dm = _RE_DEFAULT.search(line)
    if dm:
        out.append(_oracle(f"ddl:{table}.{col}.default", LEVEL_CONSTRAINT, "ddl", ref,
                           line, {"table": table, "field": col},
                           {"type": "default", "field": col, "value": _literal(dm.group(1))},
                           "默认值契约：不传该字段时应落这个值"))
    ic = _RE_INLINE_CHECK.match(line)
    if ic:
        assertion = _expr_to_assert(ic.group(2))
        if assertion is not None:
            out.append(_oracle(f"ddl:{table}.{col}.check", LEVEL_CONSTRAINT, "ddl", ref,
                               line, {"table": table, "field": col}, assertion,
                               "列级 CHECK 约束"))


def harvest_ddl(paths: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _RE_CREATE.finditer(text):
            table, body = m.group(1), m.group(2)
            body_start = m.start(2)
            for cm in _RE_CONSTRAINT_CHECK.finditer(body):
                assertion = _expr_to_assert(cm.group(2))
                if assertion is None:
                    continue
                ref = f"{path.name}:{text[:body_start + cm.start()].count("\n") + 1}"
                out.append(_oracle(f"ddl:{table}.{cm.group(1)}", LEVEL_CONSTRAINT, "ddl", ref,
                                   cm.group(0), {"table": table}, assertion,
                                   "迁移里写明的 CHECK 约束，作者已确认"))
            for um in _RE_CONSTRAINT_UNIQUE.finditer(body):
                fields = [f.strip() for f in um.group(2).split(",")]
                ref = f"{path.name}:{text[:body_start + um.start()].count("\n") + 1}"
                out.append(_oracle(f"ddl:{table}.{um.group(1)}", LEVEL_CONSTRAINT, "ddl", ref,
                                   um.group(0), {"table": table},
                                   {"type": "unique", "fields": fields},
                                   "唯一性约束：重复即违反，不需要业务预期"))
            for fm in _RE_CONSTRAINT_FK.finditer(body):
                ref = f"{path.name}:{text[:body_start + fm.start()].count("\n") + 1}"
                out.append(_oracle(f"ddl:{table}.{fm.group(1)}", LEVEL_CONSTRAINT, "ddl", ref,
                                   fm.group(0), {"table": table},
                                   {"type": "references", "field": fm.group(2),
                                    "target_table": fm.group(3), "target_field": fm.group(4)},
                                   "引用完整性：孤儿行即违反"))
            offset = 0
            for raw in body.split("\n"):
                line_pos = body_start + offset
                offset += len(raw) + 1
                line = raw.strip().rstrip(",")
                if not line or _RE_COL_SKIP.match(line):
                    continue
                cm = _RE_COL.match(line)
                if not cm:
                    continue
                col = cm.group(1)
                ref = f"{path.name}:{text[:line_pos].count("\n") + 1}"
                _emit_column_constraints(table, col, line, ref, out)
        # 表级 ALTER 追加的约束（迁移常见）
        for am in re.finditer(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+CONSTRAINT\s+(\w+)\s+CHECK\s*\((.+?)\)\s*;",
                              text, re.IGNORECASE):
            assertion = _expr_to_assert(am.group(3))
            if assertion is None:
                continue
            line_no = text[:am.start(3)].count("\n") + 1
            out.append(_oracle(f"ddl:{am.group(1)}.{am.group(2)}", LEVEL_CONSTRAINT, "ddl",
                               f"{path.name}:{line_no}", am.group(0), {"table": am.group(1)},
                               assertion, "ALTER 追加的 CHECK 约束"))
        # 迁移里常见的 ALTER ADD COLUMN —— 只解析 CREATE TABLE 会整批漏采
        for am in _RE_ALTER_ADD.finditer(text):
            line_no = text[:am.start(3)].count("\n") + 1
            _emit_column_constraints(am.group(1), am.group(2), am.group(3).strip(),
                                     f"{path.name}:{line_no}", out)
    return out


def _annotation_assertion(name: str, args: str, field: str) -> dict[str, Any] | None:
    kind = _ANNOTATIONS.get(name)
    if kind:
        return {"type": kind, "field": field}
    if name == "Valid":
        return {"type": "validate_nested", "field": field}
    if name in ("Min", "Max", "DecimalMin", "DecimalMax"):
        vm = re.search(r"(?:value\s*=\s*)?(-?\d+(?:\.\d+)?)", args)
        if not vm:
            return None
        return {"type": "field_cmp", "field": field,
                "op": ">=" if name in ("Min", "DecimalMin") else "<=",
                "value": _literal(vm.group(1))}
    if name == "Size":
        lo = re.search(r"min\s*=\s*(\d+)", args)
        hi = re.search(r"max\s*=\s*(\d+)", args)
        if not (lo or hi):
            return None
        return {"type": "size", "field": field,
                **({"min": int(lo.group(1))} if lo else {}),
                **({"max": int(hi.group(1))} if hi else {})}
    if name == "Pattern":
        pm = re.search(r'regexp\s*=\s*"([^"]+)"', args)
        if not pm:
            return None
        return {"type": "pattern", "field": field, "value": pm.group(1)}
    return None


def _emit_annotations(entity: str, field: str, block: str, text: str,
                      block_pos: int, filename: str, out: list[dict[str, Any]]) -> None:
    for am in _RE_ANNO_VALUE.finditer(block):
        name, args = am.group(1), (am.group(2) or "").strip()
        assertion = _annotation_assertion(name, args, field)
        if assertion is None:
            continue
        line_no = text[:block_pos + am.start()].count("\n") + 1
        out.append(_oracle(f"anno:{entity}.{field}.{name.lower()}", LEVEL_CONSTRAINT,
                           "annotation", f"{filename}:{line_no}", am.group(0),
                           {"entity": entity, "field": field}, assertion,
                           "DTO/实体上声明的校验注解，作者已确认"))


def harvest_annotations(paths: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        cls = _RE_CLASS.search(text)
        entity = cls.group(1) if cls else path.stem
        for m in _RE_ANNO_FIELD.finditer(text):
            _emit_annotations(entity, m.group(2), m.group(1), text, m.start(1), path.name, out)
        for rm in _RE_RECORD.finditer(text):
            record_name, body = rm.group(1), rm.group(2)
            for component, cpos in _split_components(body):
                cm = _RE_COMPONENT.match(component)
                if not cm or not cm.group(1).strip():
                    continue
                _emit_annotations(record_name, cm.group(3), cm.group(1), text,
                                  rm.start(2) + cpos, path.name, out)
    return out


def _ptr_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def harvest_openapi(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    schemas = (doc.get("components") or {}).get("schemas") or {}
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        required = set(schema.get("required") or [])
        for field, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict):
                continue
            ref = (f"{path.name}:#/components/schemas/{_ptr_token(name)}"
                   f"/properties/{_ptr_token(field)}")
            if field in required:
                out.append(_oracle(f"api:{name}.{field}.required", LEVEL_CONTRACT, "openapi", ref,
                                   f"required: {field}", {"schema": name, "field": field},
                                   {"type": "not_null", "field": field},
                                   "接口契约声明必填 —— 与 DTO 注解、DDL 非空三方对账"))
            if "enum" in spec and isinstance(spec["enum"], list):
                out.append(_oracle(f"api:{name}.{field}.enum", LEVEL_CONTRACT, "openapi", ref,
                                   f"enum: {spec['enum']}", {"schema": name, "field": field},
                                   {"type": "in", "field": field, "value": spec["enum"]},
                                   "接口契约声明的取值域"))
            if spec.get("type") in ("integer", "number"):
                bounds = {k: spec[k] for k in ("minimum", "maximum") if k in spec}
                if bounds:
                    out.append(_oracle(f"api:{name}.{field}.range", LEVEL_CONTRACT, "openapi", ref,
                                       json.dumps(bounds), {"schema": name, "field": field},
                                       {"type": "range", "field": field, **bounds},
                                       "接口契约声明的数值范围"))
    return out


def _table_rule_assert(rule: str) -> list[dict[str, Any]]:
    """把确认表规则转录成封闭原语列表。复合规则拆成多条；转录不了返回空。

    值与字段名一律取自原文，不猜业务意图。has_fields/set_covers 超出求值器
    当前封闭集，仍忠实转录为结构判据（gate_eligible=false，待补原语，unknown 非 impossible）。
    """
    m = _RE_TABLE_CMP.match(rule)
    if m:
        return [{"type": "field_cmp", "field": m.group(1).strip(),
                 "op": m.group(2), "value": _literal(m.group(3))}]
    m = _RE_TABLE_ENUM.match(rule)
    if m:
        return [{"type": "in", "field": m.group(1).strip(), "value": m.group(2).split("/")}]
    out: list[dict[str, Any]] = []
    subject_m = _RE_TABLE_SUBJECT.match(rule)
    subject = subject_m.group(1) if subject_m else rule
    om = _RE_TABLE_ONLY.search(rule)
    if om:
        out.append({"type": "in", "field": om.group(1), "value": om.group(2).split("/")})
    mm = _RE_TABLE_MAX.search(rule)
    if mm:
        a: dict[str, Any] = {"type": "field_cmp", "field": subject, "op": "<=",
                             "value": _literal(mm.group(1))}
        if mm.group(2):
            a["unit"] = mm.group(2)
        out.append(a)
    for cm in _RE_TABLE_COUNT.finditer(rule):
        out.append({"type": "field_cmp", "field": cm.group(3), "op": cm.group(1),
                    "value": _literal(cm.group(2))})
    fm = _RE_TABLE_FIELDS.match(rule)
    if fm:
        out.append({"type": "has_fields", "target": fm.group(1).strip(),
                    "fields": fm.group(2).split("/")})
    cv = _RE_TABLE_COVERS.search(rule)
    if cv:
        out.append({"type": "set_covers", "field": "错误码", "value": cv.group(1).split("/")})
    nm = _RE_TABLE_NOTNULL.search(rule)
    if nm:
        out.append({"type": "not_null", "field": nm.group(1)})
    um = _RE_TABLE_UNIQUE.search(rule)
    if um:
        out.append({"type": "unique", "fields": [um.group(1), um.group(2)]})
    return out


def harvest_document(paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """从 PRD / 需求文档采集**业务规则**（L0）—— 这是「功能对不对」的唯一权威来源。

    返回 (oracles, undefined, untranscribed)。
    undefined 是文档明确写「当前未定义」的条款：标 refuted，永不许推断成预期。
    untranscribed 是转录不了的散文规则与需产品确认/文档陈旧条目：可见登记，不静默丢弃。
    """
    out: list[dict[str, Any]] = []
    undefined: list[dict[str, Any]] = []
    untranscribed: list[dict[str, Any]] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        heading = ""
        in_undefined = False
        in_acceptance = False
        in_table = False
        table_cols: list[str] = []
        table_kind = "other"
        problem_class: str | None = None
        for idx, raw in enumerate(lines, start=1):
            hm = _RE_DOC_HEADING.match(raw)
            if hm:
                heading = hm.group(2).strip()
                in_undefined = "未定义" in heading
                in_acceptance = "验收" in heading
                in_table = False
                pm = _RE_PROBLEM_HEADING.match(heading)
                if pm:
                    title = pm.group(2)
                    if "需产品确认" in title or "疑似" in title:
                        problem_class = "needs_confirmation"
                        out.append({
                            "id": f"doc:{path.stem}:{idx}.needs_confirmation",
                            "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                            "gate_eligible": False,
                            "source": {"kind": "document", "reference": f"{path.name}:{idx}",
                                       "text": heading},
                            "target": {}, "assert": {"type": "needs_confirmation", "text": title},
                            "note": "文档标「需产品确认/疑似」——不得当成 confirmed 规则判缺陷",
                        })
                    elif "文档陈旧" in title or "以代码为准" in title:
                        problem_class = "stale"
                    else:
                        problem_class = "confirmed"
                else:
                    problem_class = None
                continue
            ref = f"{path.name}:{idx}"

            stripped = raw.strip()
            if stripped.startswith("|"):
                inner = stripped.strip("|")
                if in_table and table_cols:
                    cells = _split_row(inner, len(table_cols))
                else:
                    cells = [c.strip() for c in inner.split("|")]
                if not in_table:
                    nxt = lines[idx].strip() if idx < len(lines) else ""
                    if nxt.startswith("|") and re.match(r"^\|[\s:|-]+\|$", nxt):
                        in_table = True
                        table_cols = cells
                        table_kind = _table_kind(cells)
                    continue
                if all(set(c) <= set("-: ") for c in cells):
                    continue
                if len(cells) != len(table_cols):
                    untranscribed.append({
                        "id": f"doc:untranscribed:{path.stem}:{idx}",
                        "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                        "gate_eligible": False,
                        "source": {"kind": "document", "reference": ref, "text": stripped},
                        "note": f"表格列数不匹配（期望 {len(table_cols)} 得 {len(cells)}），显式登记不静默截断",
                    })
                    continue
                row = dict(zip(table_cols, cells))
                if table_kind == "confirmed" and "已确认" in heading:
                    rule = row.get("规则", "")
                    conclusion = row.get("结论", "")
                    expected_src = row.get("预期来源", "")
                    impl_fact = row.get("实现事实", "")
                    if not rule or conclusion.strip() != "一致":
                        untranscribed.append({
                            "id": f"doc:untranscribed:{path.stem}:{idx}",
                            "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                            "gate_eligible": False,
                            "source": {"kind": "document", "reference": ref, "text": rule},
                            "note": f"表格行结论非「一致」，不得当 confirmed；预期来源={expected_src}",
                        })
                        continue
                    assertions = _table_rule_assert(rule)
                    base_note = f"人工确认表「{heading}」；预期来源={expected_src}；实现事实={impl_fact}"
                    if not assertions:
                        out.append(_oracle(f"doc:{path.stem}:{idx}.table", LEVEL_BUSINESS,
                                           "document", ref, rule, {"rule": rule},
                                           {"type": "confirmed_rule", "text": rule},
                                           base_note + "；结构/散文规则，求值器封闭原语集"
                                           "（field_*/in/jsonb_*）暂无对应原语，需物化层补原语或绑定，不猜语义"))
                        continue
                    for n, assertion in enumerate(assertions, start=1):
                        suffix = ".table" if n == 1 else f".table.{n}"
                        note = base_note
                        if assertion["type"] in ("has_fields", "set_covers"):
                            note += ("；结构取自原文，求值器封闭集暂无 has_fields/set_covers，"
                                     "待补原语后方可判定（当前 unknown，非 impossible）")
                        elif len(assertions) > 1:
                            note += f"；复合规则第 {n}/{len(assertions)} 个可执行原语（值取自原文）"
                        out.append(_oracle(f"doc:{path.stem}:{idx}{suffix}", LEVEL_BUSINESS,
                                           "document", ref, rule, {"rule": rule}, assertion, note))
                elif table_kind == "interface":
                    cell = row.get("校验要点", "")
                    path_cell = row.get("路径", "")
                    assertions, unmatched = _interface_asserts(cell)
                    for n, assertion in enumerate(assertions, start=1):
                        suffix = ".iface" if n == 1 else f".iface.{n}"
                        out.append(_oracle(f"doc:{path.stem}:{idx}{suffix}", LEVEL_BUSINESS,
                                           "document", ref, cell,
                                           {"field": assertion.get("field", ""), "path": path_cell},
                                           assertion,
                                           f"接口清单「{heading}」校验要点（路径={path_cell}），字段名取自原文"))
                    for c, clause in enumerate(unmatched, start=1):
                        untranscribed.append({
                            "id": f"doc:untranscribed:{path.stem}:{idx}.c{c}",
                            "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                            "gate_eligible": False,
                            "source": {"kind": "document", "reference": ref, "text": clause},
                            "note": f"接口表「{heading}」校验要点子句转录不了，规则粒度可见登记；路径={path_cell}",
                        })
                    if not assertions and not unmatched:
                        untranscribed.append({
                            "id": f"doc:untranscribed:{path.stem}:{idx}",
                            "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                            "gate_eligible": False,
                            "source": {"kind": "document", "reference": ref, "text": cell},
                            "note": f"接口表「{heading}」校验要点为空，可见登记；路径={path_cell}",
                        })
                else:
                    untranscribed.append({
                        "id": f"doc:untranscribed:{path.stem}:{idx}",
                        "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                        "gate_eligible": False,
                        "source": {"kind": "document", "reference": ref, "text": " | ".join(cells)},
                        "note": f"表格「{heading}」非判据表（{'/'.join(table_cols)}），可见登记不静默丢弃",
                    })
                continue
            in_table = False

            bm = _RE_DOC_BULLET.match(raw)
            body = bm.group(1).strip() if bm else raw.strip()
            if not body:
                continue
            if body.startswith("`") and body.endswith("`") and body.count("`") == 2:
                body = body[1:-1].strip()

            if in_undefined:
                undefined.append({
                    "id": f"doc:undefined:{path.stem}:{idx}", "level": LEVEL_BUSINESS,
                    "confirmation": REFUTED, "gate_eligible": False,
                    "source": {"kind": "document", "reference": ref, "text": body},
                    "note": "文档明确列为「当前未定义」——只保留观测，不许推断预期、不许判缺陷",
                })
                continue

            if problem_class == "stale":
                untranscribed.append({
                    "id": f"doc:untranscribed:{path.stem}:{idx}",
                    "level": LEVEL_BUSINESS, "confirmation": REFUTED,
                    "gate_eligible": False,
                    "source": {"kind": "document", "reference": ref, "text": body},
                    "note": "文档陈旧点（以代码为准）——不构成判据，不得当 confirmed 规则",
                })
                continue
            if problem_class == "needs_confirmation":
                untranscribed.append({
                    "id": f"doc:untranscribed:{path.stem}:{idx}",
                    "level": LEVEL_BUSINESS, "confirmation": NEEDS_CONFIRMATION,
                    "gate_eligible": False,
                    "source": {"kind": "document", "reference": ref, "text": body},
                    "note": "需产品确认条目的佐证——不得当 confirmed 规则",
                })
                continue

            cm = _RE_DOC_CMP.match(body)
            if cm:
                out.append(_oracle(f"doc:{path.stem}:{idx}.{cm.group(1)}", LEVEL_BUSINESS,
                                   "document", ref, body, {"field": cm.group(1)},
                                   {"type": "field_cmp", "field": cm.group(1),
                                    "op": cm.group(2), "value": _literal(cm.group(3))},
                                   f"PRD 章节「{heading}」明写的约束"))
                continue
            om = _RE_DOC_ONLY.search(body)
            if om:
                values = [_literal(v) for v in re.split(r"[、,，]", _RE_BACKTICK.sub(r"\1", om.group(2)))
                          if v.strip()]
                if values:
                    out.append(_oracle(f"doc:{path.stem}:{idx}.{om.group(1)}", LEVEL_BUSINESS,
                                       "document", ref, body, {"field": om.group(1)},
                                       {"type": "in", "field": om.group(1), "value": values},
                                       f"PRD 章节「{heading}」明写的取值域"))
                    continue
            rm = _RE_DOC_REGEX.search(body)
            if rm:
                out.append(_oracle(f"doc:{path.stem}:{idx}.pattern", LEVEL_BUSINESS,
                                   "document", ref, body, {},
                                   {"type": "pattern", "value": rm.group(1)},
                                   f"文档明写的格式规则（章节「{heading}」）"))
                continue
            prose: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
            am = _RE_DOC_ALLOW.search(body)
            if am:
                values = [v.strip().strip("`") for v in re.split(r"[、,，/]", am.group(1)) if v.strip()]
                prose.append((".allow", {"field": "上传格式"},
                              {"type": "in", "field": "上传格式", "value": values},
                              "文档明写的允许取值域，字段为散文主语待绑定"))
            mx = _RE_DOC_MAXMB.search(body)
            if mx:
                prose.append((".maxmb", {"field": "文件大小"},
                              {"type": "field_cmp", "field": "文件大小", "op": "<=",
                               "value": int(mx.group(1)), "unit": "MB"},
                              "文档明写的大小上限，带单位待绑定观测单位"))
            fb = _RE_DOC_FORBID.search(body)
            if fb:
                terms = [t.strip() for t in re.split(r"[、,，]", fb.group(1)) if t.strip()]
                prose.append((".forbid", {},
                              {"type": "forbidden_terms", "value": terms},
                              "文档明写的禁用字样，封闭集无对应原语待补"))
            dm = _RE_DOC_DEFAULT.search(body)
            if dm:
                prose.append((".default", {"field": dm.group(1)},
                              {"type": "default", "field": dm.group(1), "value": _literal(dm.group(2))},
                              "文档明写的默认值契约，DDL/OpenAPI 未必有此默认，独知载体待绑定"))
            if prose:
                for n, (suffix, target, assertion, why) in enumerate(prose, start=1):
                    sid = suffix if len(prose) == 1 else f"{suffix}.{n}"
                    out.append(_oracle(f"doc:{path.stem}:{idx}{sid}", LEVEL_BUSINESS,
                                       "document", ref, body, target, assertion,
                                       f"{why}（章节「{heading}」）"))
                continue
            if in_acceptance and bm:
                out.append(_oracle(f"doc:{path.stem}:{idx}.acceptance", LEVEL_BUSINESS,
                                   "document", ref, body, {},
                                   {"type": "acceptance_scenario", "text": body},
                                   "验收条款：需物化成场景才能执行，本版只登记不判定"))
                continue

            if problem_class == "confirmed" and ("预期" in body or "应" in body):
                out.append(_oracle(f"doc:{path.stem}:{idx}.confirmed_defect", LEVEL_BUSINESS,
                                   "document", ref, body, {},
                                   {"type": "confirmed_rule", "text": body},
                                   f"已确认缺陷章节「{heading}」明写的预期"))
                continue

            rule_like = (bool(_RE_RULE_HINT.search(body))
                         or bool(_RE_VALUE_SEMANTICS.search(body))
                         or (bm is not None and any(k in heading for k in ("规则", "规范", "约束", "要求", "校验"))))
            if rule_like:
                untranscribed.append({
                    "id": f"doc:untranscribed:{path.stem}:{idx}",
                    "level": LEVEL_BUSINESS, "confirmation": CONFIRMED,
                    "gate_eligible": False,
                    "source": {"kind": "document", "reference": ref, "text": body},
                    "note": f"散文规则（章节「{heading}」）转录不了——可见登记，需物化，不静默丢弃",
                })
    assign_buckets(untranscribed)
    return out, undefined, untranscribed


def assign_buckets(untranscribed: list[dict[str, Any]]) -> None:
    for u in untranscribed:
        note = u.get("note", "")
        text = u["source"]["text"]
        if "非判据表" in note or "列数不匹配" in note or "为空" in note:
            u["bucket"] = "untranscribable"
        elif _RE_NOISE.search(text):
            u["bucket"] = "noise"
        elif _RE_BUSINESS.search(text) or _RE_RULE_HINT.search(text):
            u["bucket"] = "business_rule_pending"
        else:
            u["bucket"] = "noise"


def openapi_parameter_names(path: Path) -> set[str]:
    """OpenAPI 路径参数名（page/pageSize 等真实字段，不在 components/schemas 里）。"""
    if not path or not path.is_file():
        return set()
    doc = json.loads(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for item in (doc.get("paths") or {}).values():
        if not isinstance(item, dict):
            continue
        for op in item.values():
            if isinstance(op, dict):
                for p in op.get("parameters") or []:
                    if isinstance(p, dict) and isinstance(p.get("name"), str):
                        names.add(p["name"])
    return names


def drop_unverifiable_defaults(oracles: list[dict[str, Any]], inventory: set[str],
                               untranscribed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """default 断言的字段名必须可核实于 DDL/DTO/OpenAPI（含参数）。不可核实（如技术栈
    说明「Java: 默认使用 Java 21」被误解析成 field=Java value=使用）→ 不产出荒谬断言，
    进 untranscribed。只门控 default，不误伤 in/field_cmp 的合法中文描述字段判据。"""
    kept: list[dict[str, Any]] = []
    for o in oracles:
        if o["assert"].get("type") == "default" and o["assert"].get("field") not in inventory:
            untranscribed.append({
                "id": f"doc:untranscribed:default:{o['id']}",
                "level": o["level"], "confirmation": NEEDS_CONFIRMATION, "gate_eligible": False,
                "source": o["source"],
                "note": "default 断言字段名无法核实于 DDL/DTO/OpenAPI（疑技术栈/散文误解析），"
                        "不产出荒谬字段断言，进 untranscribed 待人工核",
            })
        else:
            kept.append(o)
    return kept


def dedupe(oracles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for o in oracles:
        seen.setdefault(o["id"], o)
    return [seen[k] for k in sorted(seen)]


def _field_subjects(o: dict[str, Any]) -> list[str]:
    a = o["assert"]
    if "field" in a:
        return [a["field"]]
    if "fields" in a:
        return list(a["fields"])
    if "target" in a and isinstance(a["target"], str):
        return [a["target"]]
    return []


def demote_unverified_fields(oracles: list[dict[str, Any]],
                             verified_fields: set[str]) -> list[dict[str, Any]]:
    """字段可核实性分级：散文名词（如「金额字段」）在 DDL/DTO/OpenAPI 核实不到 →
    文档判据降为 needs_confirmation，不得当成 confirmed 可执行判据。

    采集器只发原始词汇（field_cmp/in/range…），映射到消费方封闭集是 oracle_bridge
    的职责，不在此越权对齐（单方面改发射词汇会让端到端计数漂移）。
    """
    for o in oracles:
        subjects = _field_subjects(o)
        if not subjects:
            continue
        if all(s in verified_fields for s in subjects):
            continue
        if o["source"]["kind"] == "document" and o["confirmation"] == CONFIRMED:
            o["confirmation"] = NEEDS_CONFIRMATION
            o["note"] += "；字段未核实，降为 needs_confirmation，不得当 confirmed 可执行判据"
    return oracles


def field_inventory(oracles: list[dict[str, Any]]) -> set[str]:
    fields: set[str] = set()
    for o in oracles:
        if o["source"]["kind"] in ("ddl", "annotation", "openapi"):
            f = o["target"].get("field")
            if isinstance(f, str):
                fields.add(f)
    return fields


def ddl_column_tables(paths: list[Path]) -> dict[str, list[dict[str, str]]]:
    """列→表归属（DDL 客观事实，与 NOT NULL 同档，非猜）。返回 列名→[{table,reference}]。"""
    colmap: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _RE_CREATE.finditer(text):
            table, body = m.group(1), m.group(2)
            body_start = m.start(2)
            offset = 0
            for raw in body.split("\n"):
                line_pos = body_start + offset
                offset += len(raw) + 1
                line = raw.strip().rstrip(",")
                if not line or _RE_COL_SKIP.match(line):
                    continue
                cm = _RE_COL.match(line)
                if not cm:
                    continue
                ref = f"{path.name}:{text[:line_pos].count(chr(10)) + 1}"
                colmap.setdefault(cm.group(1), []).append({"table": table, "reference": ref})
        for am in _RE_ALTER_ADD.finditer(text):
            ref = f"{path.name}:{text[:am.start(3)].count(chr(10)) + 1}"
            colmap.setdefault(am.group(2), []).append({"table": am.group(1), "reference": ref})
    return colmap


def bind_l0_scope(oracles: list[dict[str, Any]],
                  colmap: dict[str, list[dict[str, str]]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """给 L0 判据绑定 DB scope（列→表客观归属）。单表→绑定；多表→按表拆分各带证据；
    非 DDL 列→scope 留空并显式标不可绑定。不改 confirmation（不提升 needs_confirmation）。"""
    out: list[dict[str, Any]] = []
    ambiguous = 0
    for o in oracles:
        if o["level"] != LEVEL_BUSINESS:
            out.append(o)
            continue
        field = o["assert"].get("field") or o["target"].get("field")
        entries = colmap.get(field) if isinstance(field, str) else None
        if not entries:
            o["target"]["scope_bindable"] = False
            o["note"] += "；字段非 DDL 列名（DTO 字段/散文），无 DB scope 绑定，显式不可绑定"
            out.append(o)
            continue
        tables = {e["table"]: e for e in entries}
        if len(tables) > 1:
            ambiguous += 1
        for table, e in sorted(tables.items()):
            no = dict(o)
            no["target"] = dict(o["target"])
            no["target"]["table"] = table
            no["target"]["scope_evidence"] = e["reference"]
            no["target"]["scope_bindable"] = True
            if len(tables) > 1:
                no["id"] = f"{o['id']}.{table}"
                no["note"] = o["note"] + f"；字段属多表按表拆分，scope={table}（DDL 证据 {e['reference']}）"
            else:
                no["note"] = o["note"] + f"；scope 绑定 {table}（DDL 证据 {e['reference']}，列→表客观归属）"
            out.append(no)
    l0 = [o for o in out if o["level"] == LEVEL_BUSINESS]
    stats = {
        "l0_total": len(l0),
        "l0_scope_bindable": sum(1 for o in l0 if o["target"].get("scope_bindable")),
        "l0_scope_unbindable": sum(1 for o in l0 if not o["target"].get("scope_bindable")),
        "l0_scope_ambiguous_split": ambiguous,
    }
    return out, stats


def doc_nullable_fields(paths: list[Path]) -> dict[str, dict[str, str]]:
    """扫文档明写「可为空/选填/非必填」的字段→{reference,text}。派生候选前的否定证据。"""
    nullable: dict[str, dict[str, str]] = {}
    for path in paths:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for idx, raw in enumerate(lines, start=1):
            m = _RE_DOC_NULLABLE.search(raw)
            if m:
                field = m.group(1).split(".")[-1]
                nullable.setdefault(field, {"reference": f"{path.name}:{idx}", "text": raw.strip()})
    return nullable


def refute_negated_candidates(oracles: list[dict[str, Any]],
                              nullable: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """派生 not_blank 候选若被文档明写「可为空」否定 → refuted（不是 needs_confirmation）。
    文档否定的预期不得留在待确认，更不得判缺陷（与假 HIGH 同族教训）。"""
    for o in oracles:
        if not o["id"].endswith(".not_blank_candidate"):
            continue
        ev = nullable.get(o["target"].get("field"))
        if ev:
            o["confirmation"] = REFUTED
            o["source"] = {"kind": "document", "reference": ev["reference"], "text": ev["text"]}
            o["note"] = (f"文档明写 {o['target'].get('field')} 可为空（{ev['reference']}），"
                         "非空白预期被否定；refuted 只保留观测，不判缺陷")
    return oracles


def _split_row(inner: str, ncols: int) -> list[str]:
    """按列数拆分表格行，末列吸收字面 |（如 image/jpeg|png|webp），不静默截断。"""
    return [c.strip() for c in inner.split("|", ncols - 1)]


def _table_kind(cols: list[str]) -> str:
    if "规则" in cols and "结论" in cols:
        return "confirmed"
    if "校验要点" in cols:
        return "interface"
    return "other"


def _interface_asserts(cell: str) -> tuple[list[dict[str, Any]], list[str]]:
    """把接口表「校验要点」单元格按子句转录成封闭原语。字段名取自原文（多为真实
    DTO/OpenAPI 字段）。返回 (断言列表, 未匹配子句列表)——未匹配子句由调用方逐条
    登记 untranscribed，保证规则粒度零静默丢弃（不只行粒度）。"""
    out: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for clause in re.split(r"[；;。]", cell):
        clause = clause.strip()
        if not clause:
            continue
        before = len(out)
        m = _RE_IF_CMP.search(clause)
        if m:
            out.append({"type": "field_cmp", "field": m.group(1), "op": m.group(2),
                        "value": _literal(m.group(3))})
        m = _RE_IF_RANGE.search(clause)
        if m:
            out.append({"type": "range", "field": m.group(1),
                        "minimum": int(m.group(2)), "maximum": int(m.group(3))})
        m = _RE_IF_ENUM.search(clause)
        if m:
            out.append({"type": "in", "field": m.group(1), "value": m.group(2).split("/")})
        m = _RE_IF_SIZE.search(clause)
        if m:
            out.append({"type": "size", "field": m.group(1), "max": int(m.group(2))})
        m = _RE_IF_REQUIRED.search(clause)
        if m:
            for f in m.group(1).split("/"):
                out.append({"type": "not_null", "field": f})
        m = _RE_IF_EXISTS.search(clause)
        if m:
            for f in m.group(1).split("/"):
                out.append({"type": "references", "field": f})
        m = _RE_IF_ONLY.search(clause)
        if m:
            out.append({"type": "in", "field": "上传格式", "value": m.group(1).split("/")})
        m = _RE_IF_MAXMB.search(clause)
        if m:
            out.append({"type": "field_cmp", "field": "文件大小", "op": "<=",
                        "value": int(m.group(1)), "unit": "MB"})
        m = _RE_IF_DEFAULT.search(clause)
        if m:
            out.append({"type": "default", "field": m.group(1), "value": _literal(m.group(2))})
        if len(out) == before:
            unmatched.append(clause)
    return out, unmatched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--migrations", type=Path, nargs="*", default=[])
    ap.add_argument("--java-root", type=Path, nargs="*", default=[])
    ap.add_argument("--openapi", type=Path)
    ap.add_argument("--docs", type=Path, nargs="*", default=[],
                    help="PRD / 需求文档（业务规则的权威来源，L0）")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    # 归一化一次，下游全用同一批 Path：reference 的 basename 与 inputs 记录才不会撕裂
    # （符号链接下 args.openapi.name 与 resolve() 后的 name 可能不同）。
    ddl_files: list[Path] = []
    for root in args.migrations:
        root = root.resolve()
        ddl_files.extend(sorted(root.rglob("*.sql")) if root.is_dir() else [root])
    java_files: list[Path] = []
    for root in args.java_root:
        root = root.resolve()
        java_files.extend(sorted(root.rglob("*.java")) if root.is_dir() else [root])
    doc_files: list[Path] = []
    for root in args.docs:
        root = root.resolve()
        doc_files.extend(sorted(root.rglob("*.md")) if root.is_dir() else [root])
    openapi = args.openapi.resolve() if args.openapi else None

    oracles: list[dict[str, Any]] = []
    doc_oracles, undefined, untranscribed = harvest_document(doc_files)
    oracles += doc_oracles
    oracles += harvest_ddl(ddl_files)
    oracles += harvest_annotations(java_files)
    if openapi and openapi.is_file():
        oracles += harvest_openapi(openapi)
    oracles = dedupe(oracles)
    oracles = demote_unverified_fields(oracles, field_inventory(oracles))
    oracles, scope_stats = bind_l0_scope(oracles, ddl_column_tables(ddl_files))
    oracles = refute_negated_candidates(oracles, doc_nullable_fields(doc_files))
    expanded_inventory = field_inventory(oracles) | openapi_parameter_names(openapi)
    oracles = drop_unverifiable_defaults(oracles, expanded_inventory, untranscribed)
    assign_buckets(untranscribed)

    by_level: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_confirmation: dict[str, int] = {}
    for o in oracles:
        by_level[o["level"]] = by_level.get(o["level"], 0) + 1
        kind = o["source"]["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_confirmation[o["confirmation"]] = by_confirmation.get(o["confirmation"], 0) + 1

    oracle_ids = {o["id"] for o in oracles}
    undefined_ids = {u["id"] for u in undefined}
    by_bucket: dict[str, int] = {}
    for u in untranscribed:
        by_bucket[u["bucket"]] = by_bucket.get(u["bucket"], 0) + 1
    report = {
        "schema_version": 1, "engine": "oracle_harvest",
        "inputs": {"migrations": [str(p) for p in ddl_files],
                   "java_roots": [str(p) for p in java_files],
                   "docs": [str(p) for p in doc_files],
                   "openapi": str(openapi) if openapi else None},
        "counts": {"total": len(oracles),
                   "oracles_count": len(oracles),
                   "explicitly_undefined_count": len(undefined),
                   "total_declarations": len(oracles) + len(undefined),
                   "denominators_disjoint": not (oracle_ids & undefined_ids),
                   "by_level": by_level, "by_source": by_kind,
                   "by_confirmation": by_confirmation,
                   "explicitly_undefined": len(undefined),
                   "untranscribed": len(untranscribed),
                   "untranscribed_by_bucket": by_bucket,
                   **scope_stats},
        "denominator_caliber": (
            "oracles 数组（confirmed/needs_confirmation）与 explicitly_undefined 数组（refuted）"
            "是两个不重叠集合：oracles_count + explicitly_undefined_count = total_declarations。"
            "counts.total == oracles_count（仅 oracles 数组）；桥把 refuted 单列排除桶，"
            "故桥处理总数 = total_declarations，比 counts.total 多 explicitly_undefined_count。"),
        "limitations": [
            "采集到的是**声明**，不是「声明已被实现 enforce」——后者要差分层实测",
            "不推断业务意图，不从命名猜规则；转录不了的散文规则进 untranscribed 可见登记",
            "文档明确写「当前未定义」的条款标 refuted，永远不许被推断成预期",
            "需产品确认/疑似缺陷标 needs_confirmation，不得当 confirmed 规则判缺陷",
            "gate_eligible 恒为 false：判据权威性与观测有效性尚未独立验证",
            "验收条款只登记为 acceptance_scenario，需物化成场景才能执行",
            "任何 markdown 表格不得静默丢弃：采到或进 untranscribed，覆盖缺口可见",
            "L3 测试断言 / L4 修复历史本版未采集",
        ],
        "explicitly_undefined": undefined,
        "untranscribed": untranscribed,
        "oracles": oracles,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"采集判据 {len(oracles)} 条 → {args.out}")
    for k, v in sorted(by_kind.items()):
        print(f"  {k}: {v}")
    print(f"  未定义(refuted): {len(undefined)}  未转录: {len(untranscribed)}")
    print("  可执行分子分母由 oracle_bridge 映射后报出（采集器不越权对齐词汇）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())