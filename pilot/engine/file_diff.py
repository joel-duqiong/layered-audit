"""文件层 · 差分判定（通用引擎，零项目字面量）。

把「字节码声明」↔「整类实例化 + 真实容器实测」对账：
两边对不上的地方是缺陷；两边对得上的地方给出**通过结论**
（避免只报问题、不给「查过没问题」的证据）。

**通用的是判定机制**：证据取值、模板渲染、派生事实的计算、排序、输出。
**项目的是判定内容**：哪些差异算缺陷、严重度多高、根因落在哪个文件、
关联到高层哪条单 —— 全在项目侧的 `file_findings_config.json`。

证据的四种来源（模板里的占位符）：
    {{raw:<场景id>.<字段>}}        文件层动态实测的原始观测
    {{http:<场景id>.<字段>}}       容器 HTTP 探针的观测
    {{check:<场景id>.<检查名>}}    某个场景里的单项检查结果
    {{static:<类名>#<方法>.<字段>}} 静态声明里的类/方法事实
    {{static:<类名>.<字段>}}       静态声明里的类级事实
    {{decl:<路径>}}                静态声明的根（meta.files 等）
    {{derived:<名字>}}             由配置里的派生事实算出来的列表/字典

派生事实（配置里的 `derived` 段）是「对一组字典做投影/过滤/聚合」的小表达：
    source   取数的路径
    filter   按字段过滤（等值）
    select   取一个字段 / 取若干字段（投影）
    limit    取前 N 条
    slice    取片段 [起, 止)
    sum_len  对字段长度求和
    group_by 按「类简名」分组，值取某个字段
    keys_where 按字段过滤后取键名

输出：file_findings.json / file_findings.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import findings_schema
import oracle_gate


def project_dir() -> Path:
    return findings_schema.target_path().parent


def artifacts_dir() -> Path:
    target = findings_schema.load_target()
    raw = (target.get("paths") or {}).get("artifacts_dir")
    return Path(raw) if raw else project_dir()


# ------------------------------------------------------------------ 上下文
class Context:
    """证据取值的四类来源 + 声明/观测根。"""

    def __init__(self, decl: dict, obs: dict) -> None:
        self.decl = decl
        self.obs = obs
        self.raw: dict[str, dict] = {}
        self.checks: dict[tuple[str, str], dict] = {}
        for r in obs.get("results", []):
            self.raw[r["id"]] = r.get("raw", {}) or {}
            for c in r.get("checks", []):
                self.checks[(r["id"], c.get("check"))] = c
        self.static: dict[str, dict] = {}
        for f in decl.get("files", []):
            simple = f.get("class", "").split(".")[-1]
            self.static[simple] = f
            for m in f.get("methods", []):
                self.static[f"{simple}#{m.get('name')}"] = m

    def http(self, scenario: str) -> dict:
        """HTTP 探针的观测：文件层用 HTTP-PROBES 这一个场景承载，里面再按场景 id 分组。"""
        probes = self.raw.get("HTTP-PROBES", {})
        return probes.get(scenario, {}) or {}


def _walk(obj: Any, path: str) -> Any:
    """按点号路径取值；取不到返回 None（不猜、不报错）。"""
    cur = obj
    for part in path.split(".") if path else []:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def resolve_path(ctx: Context, path: str) -> Any:
    """解析一个证据路径。"""
    source, _, rest = path.partition(":")
    if source == "raw":
        key, _, sub = rest.partition(".")
        return _walk(ctx.raw.get(key, {}), sub)
    if source == "http":
        key, _, sub = rest.partition(".")
        return _walk(ctx.http(key), sub)
    if source == "check":
        key, _, name = rest.partition(".")
        return ctx.checks.get((key, name), {})
    if source == "static":
        key, _, sub = rest.partition(".")
        return _walk(ctx.static.get(key, {}), sub)
    if source == "decl":
        return _walk(ctx.decl, rest)
    if source == "obs":
        return _walk(ctx.obs, rest)
    return None


def resolve_derived(spec: dict, ctx: Context) -> Any:
    """按配置里的派生事实规格算出一个列表/字典。"""
    rows = resolve_path(ctx, spec["source"]) if spec.get("source") else None
    if rows is None:
        rows = []
    if isinstance(rows, dict):
        rows = [rows]

    if "group_by" in spec:
        out: dict[str, Any] = {}
        has = spec.get("where_has")
        for r in rows:
            if not isinstance(r, dict):
                continue
            if has and not r.get(has):
                continue
            simple = r.get("class", "").split(".")[-1]
            out[simple] = r.get(spec["group_by"])
        return out

    if "keys_where" in spec:
        where = spec["keys_where"]
        src = resolve_path(ctx, spec.get("source", "")) or {}
        return [k for k, v in src.items()
                if isinstance(v, dict) and all(v.get(f) == val for f, val in where.items())]

    if "filter" in spec:
        rows = [r for r in rows if isinstance(r, dict)
                and all(r.get(f) == val for f, val in spec["filter"].items())]

    if "sum_len" in spec:
        return sum(len(r.get(spec["sum_len"]) or []) for r in rows
                   if isinstance(r, dict))

    if "select" in spec:
        sel = spec["select"]
        if isinstance(sel, list):
            rows = [{k: r.get(k) for k in sel} for r in rows if isinstance(r, dict)]
        else:
            rows = [r.get(sel) for r in rows if isinstance(r, dict)]

    if "limit" in spec:
        rows = rows[: spec["limit"]]
    if "slice" in spec:
        start, end = spec["slice"]
        rows = rows[start:end]
    return rows


# ------------------------------------------------------------------ 模板渲染
def render_value(template: Any, ctx: Context, derived_values: dict[str, Any]) -> Any:
    """渲染一个字段。

    · 散文（title / detail / note）：直接替换，值转成字符串。
    · 结构化（evidence 字典/列表）：走 JSON 往返 —— 整段就是一个占位符时
      **保留原类型**（列表还是列表、布尔还是布尔），嵌在文本里才转字符串。
    """
    if isinstance(template, str):
        if "{{" not in template:
            return template
        text = template
        for key in _placeholders(template):
            value = _lookup(key, ctx, derived_values)
            text = text.replace("{{" + key + "}}",
                                value if isinstance(value, str) else str(value))
        return text
    if isinstance(template, (dict, list)):
        text = json.dumps(template, ensure_ascii=False)
        tokens: dict[str, Any] = {}
        for i, key in enumerate(_placeholders(text)):
            value = _lookup(key, ctx, derived_values)
            tok = f"@@{i}@@"
            tokens[tok] = value
            text = text.replace("{{" + key + "}}", tok)
        return _restore(json.loads(text), tokens)
    return template


def _placeholders(template: str) -> list[str]:
    """找出模板里的占位符（按出现顺序，去重）。"""
    out: list[str] = []
    i = template.find("{{")
    while i >= 0:
        j = template.find("}}", i)
        if j < 0:
            break
        key = template[i + 2:j].strip()
        if key not in out:
            out.append(key)
        i = template.find("{{", j + 2)
    return out


def _restore(obj: Any, tokens: dict[str, Any]) -> Any:
    """把哨兵换回真值（整段相等时保住类型，嵌在文本里就转字符串）。"""
    if isinstance(obj, str):
        for tok, value in tokens.items():
            if obj == tok:
                return value
        for tok, value in tokens.items():
            if tok in obj:
                # 嵌在文本里的值用 Python 的写法（True / [1, 2]），
                # 与改造前 %-格式化的结果一致
                obj = obj.replace(tok, value if isinstance(value, str) else str(value))
        return obj
    if isinstance(obj, list):
        return [_restore(o, tokens) for o in obj]
    if isinstance(obj, dict):
        return {k: _restore(v, tokens) for k, v in obj.items()}
    return obj


def build_context(art: Path, cfg: dict) -> Context:
    decl = json.loads((art / "file_declaration.json").read_text(encoding="utf-8"))
    obs = json.loads((art / "file_observation.json").read_text(encoding="utf-8"))
    ctx = Context(decl, obs)
    ctx.derived = cfg.get("derived", {})
    return ctx


def resolve_derived_values(ctx: Context) -> dict[str, Any]:
    return {name: resolve_derived(spec, ctx) for name, spec in ctx.derived.items()}


def render_with(template: Any, ctx: Context, derived_values: dict[str, Any]) -> Any:
    """带派生事实的渲染：占位符先按路径解析，derived 的用预计算值。"""
    if isinstance(template, str):
        if "{{" not in template:
            return template
        tokens: dict[str, Any] = {}
        text = template
        for i, key in enumerate(_placeholders(template)):
            value = _lookup(key, ctx, derived_values)
            tok = f"@@{i}@@"
            tokens[tok] = value
            text = text.replace("{{" + key + "}}", tok)
        obj = json.loads(text)
        return _restore(obj, tokens)
    if isinstance(template, dict):
        return {k: render_with(v, ctx, derived_values) for k, v in template.items()}
    if isinstance(template, list):
        return [render_with(v, ctx, derived_values) for v in template]
    return template


def _lookup(key: str, ctx: Context, derived_values: dict[str, Any]) -> Any:
    if key.startswith("derived:"):
        return derived_values.get(key.partition(":")[2])
    return resolve_path(ctx, key)


# ------------------------------------------------------------------ 输出
SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def main() -> None:
    proj = project_dir()
    art = artifacts_dir()

    cfg = json.loads((proj / "file_findings_config.json").read_text(encoding="utf-8"))
    ctx = build_context(art, cfg)
    derived_values = resolve_derived_values(ctx)

    findings: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for rec in cfg.get("findings", []):
        out: dict[str, Any] = {}
        for field, value in rec.items():
            out[field] = render_value(value, ctx, derived_values)
        # 只有 defect 需要判据来源：positive / observation 不是开单，不受闸门影响。
        # technical（通用安全/正确性不变式）直接放行；business 必须有 confirmed oracle。
        if out.get("kind") == "defect":
            kept, gated = oracle_gate.warrant_split(
                rec.get("warrant"), [out], rule_id=out.get("id", ""))
            findings.extend(kept)
            held.extend(gated)
        else:
            findings.append(out)

    findings.sort(key=lambda f: (SEV.get(f.get("severity"), 9), f.get("id", "")))

    out_json = art / "file_findings.json"
    out_json.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")

    # 判据来源不足的业务预期：不进缺陷台账，但必须留痕（硬约束 5）。无条件写。
    hb = oracle_gate.buckets(held)
    (art / "file_rule_suspects.json").write_text(json.dumps({
        "unconfirmed": hb["unconfirmed"], "unconfirmed_count": len(hb["unconfirmed"]),
        "refuted": hb["refuted"], "refuted_count": len(hb["refuted"]),
        "note": "文件层开单门的判据来源分流：technical（通用安全/正确性不变式）直接开单，"
                "business 必须有 confirmed oracle。未经确认的进待确认疑点（规范层风险），"
                "被文档明确否定的只保留观测；未声明 warrant 的按「未裁定」保守留痕。"
                "positive / observation 不是开单，不受此闸门影响。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    defects = [f for f in findings if f.get("kind") == "defect"]
    positives = [f for f in findings if f.get("kind") == "positive"]
    dmeta = ctx.decl.get("meta", {})
    ometa = ctx.obs.get("meta", {})

    L = [f"# 文件层差分报告（{cfg.get('target_name', '目标')}）", "",
         f"文件 {dmeta.get('files')} 个 / 方法 {dmeta.get('methods')} 个 | "
         f"动态用例 {ometa.get('scenarios')} + 探针 {ometa.get('http_probes')} | "
         f"缺陷 {len(defects)} 条 / 通过结论 {len(positives)} 条", "",
         "| 级别 | 缺陷 | 根因文件 |", "|---|---|---|"]
    for f in defects:
        L.append(f"| {f['severity']} | {f['title']} | `{f['root_cause_file']}` |")
    L += ["", "## 通过结论（差分也能给「查过没问题」）", "",
          "| 级别 | 结论 |", "|---|---|"]
    for f in positives:
        L.append(f"| {f['severity']} | {f['title']} |")
    L.append("")
    for f in findings:
        L += [f"## [{f['severity']}] {f['title']}", "",
              f"- id: `{f['id']}`", f"- kind: {f['kind']}",
              f"- 根因文件: `{f['root_cause_file']}`"]
        if f.get("cwe"):
            L.append(f"- CWE: {f['cwe']}")
        if f.get("related"):
            L.append(f"- 相关: {', '.join(f['related'])}")
        L += ["", f["detail"], "", "```json",
              json.dumps(f["evidence"], ensure_ascii=False, indent=2), "```", ""]
    out_md = art / "file_findings.md"
    out_md.write_text("\n".join(L), encoding="utf-8")

    print(f"文件层差分 -> {out_json} / {out_md}")
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        n = sum(1 for f in findings if f.get("severity") == s)
        if n:
            print(f"  {s:8} {n}")


if __name__ == "__main__":
    main()
