"""类层差分规则（通用实现）。

每条规则都是**语言级 / 结构级**的判断，不含项目字面量：
文案、涉及哪张表、哪个枚举文件、哪个索引名，全部由项目侧的 class_scenarios.json 声明。

项目配置里一条规则的形状：

    {"id": "enum_check_drift", "params": {"enum_file": "EnumFile.java", ...}}

`{{xxx}}` 是占位符：文案模板里换成实测值，证据模板里换成 JSON 值（保持类型）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import file_carrier


# ------------------------------------------------------------------ 模板渲染
def render_text(template: str, values: dict[str, Any]) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def render_json(template: Any, values: dict[str, Any]) -> Any:
    """证据模板：先换成哨兵，再 json.loads，最后把哨兵换回真值（保住类型）。"""
    import json
    tokens: dict[str, Any] = {}
    if not isinstance(template, str):
        template = json.dumps(template, ensure_ascii=False)
    text = template
    for i, (k, v) in enumerate(values.items()):
        tok = f"@@{i}@@"
        tokens[tok] = v
        text = text.replace("{{" + k + "}}", tok)
    obj = json.loads(text)

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            # 整个字符串就是一个占位符 -> 直接换成真值（保住 bool / int / list / dict 的类型）
            for tok, v in tokens.items():
                if o == tok:
                    return v
            for tok, v in tokens.items():
                if tok in o:
                    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    o = o.replace(tok, s)
            return o
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o

    return walk(obj)


def _finding(fid: str, sev: str, layer: str, title: str, detail: str,
             evidence: Any) -> dict:
    return {"id": fid, "severity": sev, "layer": layer, "title": title,
            "detail": detail, "evidence": evidence}


# ------------------------------------------------------------ 规则：枚举 vs CHECK
def enum_check_drift(rule: dict, ctx: dict) -> list[dict]:
    """Java 枚举取值 ↔ DB CHECK 白名单。"""
    p = rule["params"]
    decl = ctx["decl"]
    ddl = decl["ddl"]["tables"]
    src = (Path(ctx["entity_dir"]) / p["enum_file"]).read_text(encoding="utf-8")
    enum_vals = re.findall(r"^\s*([A-Z_]+)\s*[,;]?\s*$", src, re.M)
    enum_vals = [v for v in enum_vals if v and v not in ("public", "enum")]
    checks = [v for v in ddl[p["table"]]["constraints"].values()
              if v["kind"] == "CHECK" and p["column"] in v["expr"]]
    if not checks:
        return []
    db_vals = re.findall(r"'([A-Z_]+)'", checks[0]["expr"])
    only_java = sorted(set(enum_vals) - set(db_vals))
    only_db = sorted(set(db_vals) - set(enum_vals))
    values = {"only_java": only_java, "only_db": only_db,
              "java_n": len(enum_vals), "db_n": len(db_vals)}
    if only_java or only_db:
        return [_finding(p["drift_id"], p["severity"], "class",
                         render_text(p["drift_title"], values),
                         render_text(p["drift_detail"], values),
                         {"java": enum_vals, "db": db_vals})]
    return [_finding(p["aligned_id"], p["aligned_severity"], "class",
                     render_text(p["aligned_title"], values),
                     render_text(p["aligned_detail"], values),
                     {"java": enum_vals, "db": db_vals})]


# ---------------------------------------------------------- 规则：状态机缺失
def no_state_machine(rule: dict, ctx: dict) -> list[dict]:
    """实测：状态能不能随便跳。"""
    p = rule["params"]
    m = ctx["obs"]["transition"]["matrix"]
    allowed = [k for k, v in m.items() if v["accepted"]]
    backward = [k for k in allowed if k.split("->")[0] > k.split("->")[1]]
    values = {"allowed_n": len(allowed), "total": len(m)}
    return [_finding(p["finding_id"], p["severity"], p.get("layer", "class"),
                     render_text(p["title"], values),
                     render_text(p["detail"], values),
                     {"allowed": allowed, "backward_transitions": backward,
                      "ddl_check": p["ddl_check"]})]


# ---------------------------------------------------------- 规则：索引用不上
def index_unusable(rule: dict, ctx: dict) -> list[dict]:
    """声明的索引能不能被服务层的查询模式用上。"""
    p = rule["params"]
    sc = ctx["obs"]["scale_experiment"]
    like_ms = float(str(sc[p["like_label"]]["exec_ms"]).split()[0])
    exact_ms = float(str(sc[p["exact_label"]]["exec_ms"]).split()[0])
    ratio = like_ms / max(exact_ms, 0.001)
    values = {"like_ms": like_ms, "exact_ms": exact_ms, "ratio": f"{ratio:.0f}"}
    return [_finding(p["finding_id"], p["severity"], p.get("layer", "class"),
                     render_text(p["title"], values),
                     render_text(p["detail"], values),
                     {p["like_ms_key"]: like_ms, p["exact_ms_key"]: exact_ms,
                      "index_cond_present": bool(sc[p["exact_label"]]["index_cond"]),
                      "like_has_index_cond": bool(sc[p["like_label"]]["index_cond"]),
                      "scale_rows": sc["rows"]})]


# ------------------------------------------------------ 规则：软删 + 唯一约束
def soft_delete_resurrect(rule: dict, ctx: dict) -> list[dict]:
    """软删之后同名重建，命中的是旧行还是新行。"""
    p = rule["params"]
    sdu = ctx["obs"]["softdelete_unique"]
    return [_finding(p["finding_id"], p["severity"], p.get("layer", "class"),
                     render_text(p["title"], {}),
                     render_text(p["detail"], {}),
                     {"category": sdu["category"], "create": sdu["create_status"],
                      "delete": sdu["delete_status"],
                      "recreate": sdu["recreate_status"],
                      "db_rows": sdu["db_rows"]})]


# ---------------------------------------------------------- 规则：没有生命周期
def no_data_lifecycle(rule: dict, ctx: dict) -> list[dict]:
    """某张表没有软删 / 归档，只增不减。"""
    p = rule["params"]
    inv = ctx["obs"]["db_invariants"]
    if not inv.get(p["probe_label"]):
        return []
    sizes = dict(inv.get("table_sizes", []))
    size = sizes.get(p["table"], "?")
    values = {"table": p["table"], "size": size}
    return [_finding(p["finding_id"], p["severity"], p.get("layer", "class"),
                     render_text(p["title"], values),
                     render_text(p["detail"], values),
                     {f"{p['table']}_size": size, "table_sizes": sizes})]


# ------------------------------------------------------------ 规则：类形状
def class_shape(rule: dict, ctx: dict) -> list[dict]:
    """类形状：里氏替换 / 值相等 / 封装泄漏 / 缓存里的可变对象。

    数据来自 JVM 内反射（class_harness/ClassShape.java），探针不碰 DB、不碰容器。
    """
    p = rule.get("params", {})
    shape = ctx["obs"].get("class_shape") or {}
    out: list[dict] = []
    classes = shape.get("classes", [])

    # 1) 里氏替换：覆写方与继承方的后置条件不一致
    lsp_cfg = p.get("lsp")
    if lsp_cfg:
        seen: set[tuple[str, str]] = set()
        for v in shape.get("lsp_violations", []):
            key = (v["overrider"], v["method"])
            if key in seen:
                continue
            seen.add(key)
            states = v.get("field_states") or {}
            values = {"overrider": v["overrider"], "base": v["base"],
                      "method": v["method"], "fields": ", ".join(v["differing_fields"]),
                      "over_state": v["overrider_state"], "inh_state": v["inheritor_state"],
                      # 逐字段：字段名 + 覆写方取值 + 继承方取值（报告里能直接引用）
                      "field_diff": "; ".join(
                          f"{k}: 覆写方={st.get('overrider')} / 继承方={st.get('inheritor')}"
                          for k, st in states.items())}
            out.append(file_carrier.attach(_finding(
                lsp_cfg["id"], lsp_cfg["severity"], lsp_cfg.get("layer", "class"),
                render_text(lsp_cfg["title"], values),
                render_text(lsp_cfg["detail"], values),
                render_json(lsp_cfg["evidence"], values)),
                lsp_cfg.get("file_carrier_detector"), ctx))

    # 2) 实体没有值相等：放进集合 / 做键，语义会变
    eq_cfg = p.get("value_equality")
    if eq_cfg:
        for c in classes:
            if not c.get("is_entity"):
                continue
            if c.get("declares_equals") or c.get("declares_hashcode"):
                continue
            values = {"class": c["class"], "simple_name": c["simple_name"]}
            out.append(file_carrier.attach(_finding(
                eq_cfg["id"], eq_cfg["severity"], eq_cfg.get("layer", "class"),
                render_text(eq_cfg["title"], values),
                render_text(eq_cfg["detail"], values),
                render_json(eq_cfg["evidence"], values)),
                eq_cfg.get("file_carrier_detector"), ctx))

    # 3) 封装泄漏：public 方法把内部可变实体的集合交出去
    leak_cfg = p.get("encapsulation")
    if leak_cfg:
        for c in classes:
            for leak in c.get("encapsulation_leaks", []):
                values = {"class": leak["class"], "method": leak["method"],
                          "returns": leak["returns"], "entity": leak["entity_type"]}
                out.append(file_carrier.attach(_finding(
                    leak_cfg["id"], leak_cfg["severity"], leak_cfg.get("layer", "class"),
                    render_text(leak_cfg["title"], values),
                    render_text(leak_cfg["detail"], values),
                    render_json(leak_cfg["evidence"], values)),
                leak_cfg.get("file_carrier_detector"), ctx))

    # 4) 缓存里放可变对象：@Cacheable 直接缓存了集合实例
    cache_cfg = p.get("cacheable")
    if cache_cfg:
        for c in classes:
            for cm in c.get("cacheable_methods", []):
                if not cm.get("mutable"):
                    continue
                values = {"class": cm["class"], "method": cm["method"],
                          "returns": cm["returns"], "element": cm.get("element_type"),
                          "element_is_record": cm.get("element_is_record")}
                out.append(file_carrier.attach(_finding(
                    cache_cfg["id"], cache_cfg["severity"], cache_cfg.get("layer", "class"),
                    render_text(cache_cfg["title"], values),
                    render_text(cache_cfg["detail"], values),
                    render_json(cache_cfg["evidence"], values)),
                cache_cfg.get("file_carrier_detector"), ctx))
    return out


# -------------------------------------------------- 规则：条件装配（多实例）
def instance_pair(rule: dict, ctx: dict) -> list[dict]:
    """两个实例（不同 profile / 不同配置）行为不一致 —— 条件装配用错维度。"""
    p = rule["params"]
    ip = ctx["obs"].get("instance_pair") or {}
    div = ip.get("divergence") or []
    if not div:
        return []
    values = {"fields": ", ".join(d["field"] for d in div),
              "instances": ", ".join(i["instance"] + (f"({i['profile']})" if i.get("profile") else "")
                                     for i in ip.get("instances", [])),
              # 每个分叉字段在各实例上的实测取值
              "field_values": "; ".join(
                  f"{d['field']}: " + ", ".join(f"{k}={v}" for k, v in d.get("values", {}).items())
                  for d in div)}
    return [_finding(p["finding_id"], p["severity"], p.get("layer", "class"),
                     render_text(p["title"], values),
                     render_text(p["detail"], values),
                     render_json(p["evidence"], values))]


RULES = {
    "enum_check_drift": enum_check_drift,
    "no_state_machine": no_state_machine,
    "index_unusable": index_unusable,
    "soft_delete_resurrect": soft_delete_resurrect,
    "no_data_lifecycle": no_data_lifecycle,
    "class_shape": class_shape,
    "instance_pair": instance_pair,
}
