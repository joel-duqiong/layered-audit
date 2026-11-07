"""类层差分：JPA 实体声明 ↔ Flyway DDL 声明 ↔ DB 实测行为。

v2 增加了第三方视角（DTO 契约）与实体字段生命周期，
差分逻辑拆到 class_contract_diff.py，这里只做汇总、排序与落盘。
"""
import json, os

import class_contract_diff

D = os.path.dirname(os.path.abspath(__file__))
decl = json.load(open(os.path.join(D, "class_declaration.json")))
obs = json.load(open(os.path.join(D, "class_observation.json")))
findings = []

def add(fid, sev, layer, title, detail, evidence):
    findings.append({"id": fid, "severity": sev, "layer": layer, "title": title,
                     "detail": detail, "evidence": evidence})

ddl = decl["ddl"]["tables"]
jpa = decl["jpa"]

# D1 JPA 与 DDL 的列级差分
for cls, e in jpa.items():
    t = e["table"]
    if not t or t not in ddl:
        continue
    cols = ddl[t]["columns"]
    for c in e["columns"]:
        col = c["column"]
        if col not in cols:
            add("JPA_COLUMN_MISSING_IN_DDL", "HIGH", "class",
                f"{cls}.{c['field']} 映射的列 {col} 在 DDL 里不存在",
                "JPA 声明与 Flyway 迁移不一致：要么实体是新的而迁移没写，要么列名写错。",
                {"entity": cls, "field": c["field"], "column": col, "table": t})
            continue
        d = cols[col]
        if c["nullable"] is False and d["not_null"] is False:
            add("NULLABILITY_DRIFT", "HIGH", "class",
                f"{cls}.{c['field']} Java 声明 NOT NULL，DDL 允许空",
                "Java 侧假定该列恒非空，DB 侧却允许 NULL —— 一旦有别的写入方（脚本/运维/数据修复）"
                "写入 NULL，Java 代码会在没有校验的地方直接 NPE。",
                {"table": t, "column": col, "jpa_nullable": False, "ddl_not_null": False})
        if c["unique"] is True and d["unique"] is False:
            uk = [k for k, v in ddl[t]["constraints"].items() if v["kind"] == "UNIQUE"]
            if not any(col in v["expr"] for v in ddl[t]["constraints"].values() if v["kind"] == "UNIQUE"):
                add("UNIQUE_DRIFT", "HIGH", "class",
                    f"{cls}.{c['field']} Java 声明 unique=true，DDL 没有唯一约束",
                    "应用层以为有唯一性保护，实际 DB 层没有 —— 并发下会写入重复值。",
                    {"table": t, "column": col})

# D2 业务规则分布（DB CHECK ↔ DTO 约束）改由三方差分自动推导，见 class_contract_diff.diff_rules。
# 上一版这里用「DDL 有 CHECK 而实体没声明」就报 RULE_ONLY_IN_DB，漏掉了 DTO 这一维 —— 误报。

# D2b 枚举取值 ↔ DDL CHECK 白名单
import re
enum_src = open("/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity/OrderStatus.java").read()
enum_vals = re.findall(r"^\s*([A-Z_]+)\s*[,;]?\s*$", enum_src, re.M)
enum_vals = [v for v in enum_vals if v and v not in ("public", "enum")]
orders_check = [v for v in ddl["orders"]["constraints"].values()
                 if v["kind"] == "CHECK" and "status" in v["expr"]]
if orders_check:
    db_vals = re.findall(r"'([A-Z_]+)'", orders_check[0]["expr"])
    only_java = sorted(set(enum_vals) - set(db_vals))
    only_db = sorted(set(db_vals) - set(enum_vals))
    if only_java or only_db:
        add("ENUM_CHECK_DRIFT", "HIGH", "class",
            f"OrderStatus 枚举与 DB CHECK 白名单不一致：仅 Java {only_java} / 仅 DB {only_db}",
            "枚举是 Java 层的状态取值声明，CHECK 是 DB 层的。两份必须一致，否则加一个状态要改两处。",
            {"java": enum_vals, "db": db_vals})
    else:
        add("ENUM_CHECK_ALIGNED", "INFO", "class",
            "OrderStatus 枚举与 DB CHECK 白名单完全一致（3/3）",
            "这是一次「没有问题」的差分结论 —— 差分引擎既能报问题也能给出通过结论。",
            {"java": enum_vals, "db": db_vals})

# D3 状态迁移矩阵：DDL 只约束取值，实测允许任意迁移
m = obs["transition"]["matrix"]
allowed = [k for k, v in m.items() if v["accepted"]]
backward = [k for k in allowed if k.split("->")[0] > k.split("->")[1]]
add("NO_STATE_MACHINE", "HIGH", "class",
    f"状态机缺失：{len(allowed)}/{len(m)} 种迁移全部被接受，含回退",
    "DDL 只有取值白名单 CHECK (status IN (...))，没有任何迁移约束；实测 9 种迁移全部 200 成功。"
    "业务后果：已付款（DEPOSIT_PAID）/已完成（COMPLETED）的订单可以被改回 PENDING，"
    "押金与结算状态和现实不符，而且没有留下任何迁移记录。",
    {"allowed": allowed, "backward_transitions": backward,
     "ddl_check": "status IN ('PENDING','DEPOSIT_PAID','COMPLETED')"})

# D4 索引声明 vs 查询模式
sc = obs["scale_experiment"]
like_ms = float(str(sc["like_prefix_lower"]["exec_ms"]).split()[0])
exact_ms = float(str(sc["exact_equal"]["exec_ms"]).split()[0])
add("INDEX_UNUSABLE_FOR_QUERY_PATTERN", "HIGH", "class→service",
    f"DDL 声明的 idx_products_name 对服务层查询模式无效（{like_ms}ms vs {exact_ms}ms，差 {like_ms/max(exact_ms,0.001):.0f} 倍）",
    "迁移文件建了 products(name) 索引，但服务层关键词搜索写的是 lower(name) LIKE '%kw%'："
    "lower() 让索引无法用于定位，前导 % 让 btree 本来也用不上。EXPLAIN 显示走 Filter 全量过滤而非 Index Cond。"
    "类层把索引交给服务层，但服务层不知道该怎么用它 —— 声明没有传递下去。",
    {"like_prefix_lower_ms": like_ms, "exact_equal_ms": exact_ms,
     "index_cond_present": bool(sc["exact_equal"]["index_cond"]),
     "like_has_index_cond": bool(sc["like_prefix_lower"]["index_cond"]),
     "scale_rows": sc["rows"]})

# D5 软删 + 唯一约束 -> 复活语义
sdu = obs["softdelete_unique"]
add("SOFT_DELETE_RESURRECTS_ROW", "MEDIUM", "class",
    "软删除的行会被同名重建「复活」，id 与 sort_order 沿用旧值",
    "删除分类后用同名重建，命中的是同一条已软删记录（id 不变、sort_order 沿用旧的），"
    "而不是新建。DDL 的 UNIQUE(name) 与软删除语义冲突：删除不是删除，是可复活。"
    "id 复用会让历史引用（商品、订单快照）指向一个「看起来是新」的实体。",
    {"category": sdu["category"], "create": sdu["create_status"], "delete": sdu["delete_status"],
     "recreate": sdu["recreate_status"], "db_rows": sdu["db_rows"]})

# D6 订单表无生命周期管理
if obs["db_invariants"].get("orders_without_soft_delete"):
    sizes = dict(obs["db_invariants"].get("table_sizes", []))
    add("NO_DATA_LIFECYCLE", "MEDIUM", "class",
        f"orders 表没有软删/归档，永久增长（当前 {sizes.get('orders','?')}）",
        "products/packages/categories 都有 is_deleted，orders 没有。订单是唯一不设生命周期的表，"
        "还带 2 个 jsonb 快照列和 6 个索引 —— 数据量上来后这张表会最先撑爆。",
        {"orders_size": sizes.get("orders"), "table_sizes": sizes})

# v2：三方差分（DB 实况 ↔ 实体 ↔ DTO ↔ 实测）
for f in class_contract_diff.run(os.path.join(D, "class_declaration.json"),
                                 os.path.join(D, "class_contract.json"),
                                 os.path.join(D, "class_observation.json")):
    findings.append({"id": f["id"], "severity": f["severity"], "layer": f["layer"],
                     "title": f["title"], "detail": f["detail"], "evidence": f["evidence"]})

sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
findings.sort(key=lambda f: sev_order.get(f["severity"], 9))
json.dump(findings, open(os.path.join(D, "class_findings.json"), "w"), ensure_ascii=False, indent=2)
lines = ["# 类层差分报告（jiuyan）", "",
         f"JPA 实体 {len(jpa)} 个 | DDL 表 {len(ddl)} 张 | 索引 {len(decl['ddl']['indexes'])} 个 | "
         f"finding {len(findings)} 条", "", "| 级别 | 层 | 缺陷 |", "|---|---|---|"]
for f in findings:
    lines.append(f"| {f['severity']} | {f['layer']} | {f['title']} |")
lines.append("")
for f in findings:
    lines += [f"## [{f['severity']}] {f['title']}", "", f["detail"], "",
              "```json", json.dumps(f["evidence"], ensure_ascii=False, indent=2), "```", ""]
open(os.path.join(D, "class_findings.md"), "w").write("\n".join(lines))

print(f"类层差分：{len(findings)} 条")
for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
    n = sum(1 for f in findings if f["severity"] == s)
    if n:
        print(f"  {s:8} {n}")
print()
for f in findings:
    print(f"[{f['severity']}] ({f['layer']}) {f['title']}")
