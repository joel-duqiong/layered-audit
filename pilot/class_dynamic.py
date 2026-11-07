"""类层动态实测：类的真实行为从 DB 里读（持久化后的对象状态）。
三个执行器：状态迁移矩阵 / 索引可用性 / 软删与唯一约束。
"""
import json, os, subprocess, uuid, datetime, requests

D = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("JIUYAN_BASE_URL", "http://127.0.0.1:8081")
S = requests.Session()
MARK = "ai_probe_"
STATUSES = ["PENDING", "DEPOSIT_PAID", "COMPLETED"]

def sql(q):
    out = subprocess.run(["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan",
                          "-d", "jiuyan", "-tAF", "|", "-c", q],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return [r.split("|") for r in out.stdout.strip().splitlines() if r != ""]

def api(method, path, body=None):
    r = S.request(method, BASE + path, json=body, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None

def make_order():
    now = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat()
    pkg = sql("SELECT id FROM packages WHERE is_deleted=false LIMIT 1")
    pid = pkg[0][0] if pkg else None
    body = {"customerName": MARK + "cls", "customerPhone": "13800000001", "banquetName": MARK + "cls",
            "banquetType": "婚宴", "reservationTime": now, "tableCount": 1,
            "packageId": int(pid) if pid else None, "planTotalCent": 1, "useFinalPrice": True,
            "finalPriceCent": 1, "depositCent": 0, "status": "PENDING"}
    st, j = api("POST", "/api/orders", body)
    return j["data"]["id"] if st == 200 and j and j.get("data") else None

def transition_matrix():
    oid = make_order()
    matrix = {}
    for frm in STATUSES:
        st, j = api("PATCH", f"/api/orders/{oid}/status", {"status": frm})
        for to in STATUSES:
            st, j = api("PATCH", f"/api/orders/{oid}/status", {"status": to})
            actual = sql(f"SELECT status FROM orders WHERE id={oid}")
            matrix[f"{frm}->{to}"] = {"http": st, "accepted": st == 200,
                                      "db_status": actual[0][0] if actual else None}
    return {"order_id": oid, "matrix": matrix}

def sql_raw(q):
    out = subprocess.run(["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan",
                          "-d", "jiuyan", "-tA", "-c", q],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout

def index_usage():
    res = {}
    for label, q in [
        ("prefix_like_lower", "SELECT count(*) FROM products WHERE lower(name) LIKE '%五香%'"),
        ("exact_equal", "SELECT count(*) FROM products WHERE name = '五香卤牛肉'"),
        ("prefix_like_raw", "SELECT count(*) FROM products WHERE name LIKE '%五香%'"),
        ("orders_status", "SELECT count(*) FROM orders WHERE status = 'PENDING'"),
        ("orders_status_range", "SELECT count(*) FROM orders WHERE status='PENDING' AND created_at > now() - interval '1 day'"),
    ]:
        plan_text = sql_raw("EXPLAIN (ANALYZE) " + q)
        forced = sql_raw("SET enable_seqscan=off; EXPLAIN (ANALYZE) " + q)
        lines = [l.strip() for l in plan_text.splitlines()]
        node = next((l for l in lines if "Scan on " in l or "Scan using " in l), "?")
        index = next((l.split("using ")[1].split(" ")[0] for l in lines
                      if "Index Scan using" in l or "Bitmap Index Scan on" in l), None)
        rows = next((l.split("rows=")[1].split(" ")[0] for l in lines
                     if "rows=" in l and "Scan" in l), None)
        flines = [l.strip() for l in forced.splitlines() if l.strip()]
        fnode = next((l for l in flines if "Scan on " in l or "Scan using " in l), "?")
        res[label] = {"node": node, "index": index,
                      "seq_scan": "Seq Scan" in node,
                      "rows": rows,
                      "forced_seqscan_off_node": fnode,
                      "index_usable": "Index Scan" in fnode or "Bitmap Index" in fnode,
                      "plan": lines}
    return res

def softdelete_unique():
    """软删除 + 唯一约束：删掉后能否用同名重建。"""
    name = MARK + "cat_" + uuid.uuid4().hex[:6]
    st, j = api("POST", "/api/categories", {"name": name})
    created = j.get("data", {}).get("id") if j and j.get("success") else None
    out = {"category": name, "create_status": st}
    if created:
        st2, j2 = api("DELETE", f"/api/categories/{created}")
        out["delete_status"] = st2
        st3, j3 = api("POST", "/api/categories", {"name": name})
        out["recreate_status"] = st3
        out["recreate_body"] = j3
        rows = sql(f"SELECT id, is_deleted FROM categories WHERE name='{name}'")
        out["db_rows"] = rows
    return out

def scale_experiment(rows=50000):
    """等比放大实验：在影子表上复刻同一份 DDL + 索引，看索引在数据量上来后是否还生效。
    只建 ai_probe_ 前缀的影子表，跑完即删，不动业务表。"""
    sql_raw("DROP TABLE IF EXISTS ai_probe_products")
    sql_raw("CREATE TABLE ai_probe_products (LIKE products INCLUDING ALL)")
    sql_raw(f"INSERT INTO ai_probe_products (name, category_id, price_cent, unit, is_active, is_deleted) "
            f"SELECT '菜品'||g, 1, 100, '份', true, false FROM generate_series(1,{rows}) g")
    out = {"rows": rows}
    for label, q in [("like_prefix_lower", "SELECT count(*) FROM ai_probe_products WHERE lower(name) LIKE '%菜品%'"),
                     ("like_prefix_raw", "SELECT count(*) FROM ai_probe_products WHERE name LIKE '%菜品%'"),
                     ("exact_equal", "SELECT count(*) FROM ai_probe_products WHERE name = '菜品1'")]:
        txt = sql_raw("EXPLAIN (ANALYZE) " + q)
        lines = [l.strip() for l in txt.splitlines()]
        node = next((l for l in lines if "Scan on " in l or "Scan using " in l), "?")
        t = next((l.split("actual time=")[1].split("..")[1].split("ms")[0] for l in lines
                  if "actual time=" in l and "Scan" in l), None)
        cond = next((l.strip() for l in lines if l.strip().startswith("Index Cond")), None)
        filt = next((l.strip() for l in lines if l.strip().startswith("Filter:")), None)
        rm = next((l.strip() for l in lines if "Rows Removed by Filter" in l), None)
        out[label] = {"node": node[:80], "index_cond": cond, "filter": filt,
                      "rows_removed_by_filter": rm, "exec_ms": t}
    sql_raw("DROP TABLE ai_probe_products")
    return out

def db_invariants():
    """DB 层现存数据是否违反表级声明。"""
    out = {}
    for t, spec in json.load(open(os.path.join(D, "class_declaration.json")))["ddl"]["tables"].items():
        checks = []
        for col, c in spec["columns"].items():
            if c["not_null"]:
                n = sql(f"SELECT count(*) FROM {t} WHERE {col} IS NULL")[0][0]
                if int(n) != 0:
                    checks.append({"col": col, "null_rows": n})
        out[t] = checks
    orphans = sql("SELECT count(*) FROM orders o LEFT JOIN packages p ON o.package_id=p.id "
                  "WHERE o.package_id IS NOT NULL AND p.id IS NULL")
    out["orphan_orders_package"] = orphans[0][0]
    out["orders_without_soft_delete"] = "is_deleted" not in \
        json.load(open(os.path.join(D, "class_declaration.json")))["ddl"]["tables"]["orders"]["columns"]
    out["table_sizes"] = sql("SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) "
                             "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC")
    return out


# ============================================================ 类层 v2：字段生命周期

MARK = "ai_probe_"


def psql_script(script: str) -> dict:
    """在单个 psql 会话里跑一段脚本。脚本自带 BEGIN，session 结束即回滚 —— 零残留。

    返回 stdout / stderr；stderr 非空说明有语句被 DB 拒绝了（防线生效）。
    """
    out = subprocess.run(["docker", "exec", "-i", "jiuyan-postgres", "psql", "-U", "jiuyan",
                           "-d", "jiuyan", "-tAF", "|", "-v", "ON_ERROR_STOP=0", "-f", "-"],
                          input=script, capture_output=True, text=True, timeout=60)
    return {"stdout": out.stdout.strip(), "stderr": out.stderr.strip(),
            "returncode": out.returncode}


def rows(query: str) -> list[dict]:
    """取行并按列名返回字典（用于整行快照差分）。

    用 to_jsonb 把整行压成一个 JSON 字面量再逐行解析，
    避免用分隔符切列时 jsonb 里的逗号/换行把行切碎。
    """
    out = subprocess.run(["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan",
                          "-d", "jiuyan", "-tA", "-c", f"SELECT to_jsonb(_r) FROM ({query}) _r"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    res = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            res.append(json.loads(line))
    return res


def defense_audit():
    """DB 是最后一道防线。先盘点防线清单，再逐条实测它到底拦不拦得住。

    所有写操作都在一个事务里，session 结束自动回滚 —— 不碰业务数据。
    """
    decl = json.load(open(os.path.join(D, "class_contract.json")))
    db = decl["db_actual"]["tables"]
    results = []

    # 1) CHECK 约束：故意写违规值，看拦不拦
    check_probes = [
        ("chk_orders_table_count", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_c1','ai_probe_c1','1','ai_probe_c1','x','a', now(), 0,"
         " '{}'::jsonb, '[]'::jsonb, 0, false, 0, 0, 'PENDING')"),
        ("chk_orders_deposit_cent", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_c2','ai_probe_c2','1','ai_probe_c2','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 0, false, 0, -1, 'PENDING')"),
        ("chk_orders_status", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_c3','ai_probe_c3','1','ai_probe_c3','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 0, false, 0, 0, 'NOT_A_STATUS')"),
        ("chk_orders_final_price_cent", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_c7','ai_probe_c7','1','ai_probe_c7','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 0, false, -1, 0, 'PENDING')"),
        ("chk_orders_plan_total_cent", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_c8','ai_probe_c8','1','ai_probe_c8','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, -1, false, 0, 0, 'PENDING')"),
        ("chk_package_items_quantity", "package_items",
         "INSERT INTO package_items (package_id, product_id, quantity)"
         " SELECT id, id, 0 FROM packages LIMIT 1"),
        ("chk_packages_price_cent", "packages",
         "INSERT INTO packages (name, banquet_type, price_cent) VALUES ('ai_probe_c4','x',-1)"),
        ("chk_products_price_cent", "products",
         "INSERT INTO products (name, category_id, price_cent, unit)"
         " VALUES ('ai_probe_c5', (SELECT id FROM categories LIMIT 1), -1, '份')"),
    ]
    for cname, table, stmt in check_probes:
        r = psql_script("BEGIN;\n" + stmt + ";\nROLLBACK;\n")
        results.append({"constraint": cname, "table": table,
                        "blocked": bool(r["stderr"]), "error": r["stderr"][:300]})

    # 2) 没有防线、但业务上该有的规则：直接写 DB 能不能写成
    no_defense = [
        ("deposit_cent > final_price_cent（定金大于成交价）", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_n1','ai_probe_n1','1','ai_probe_n1','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 100, true, 1, 999999, 'PENDING')"),
        ("reservation_time 在过去（预订过去的日期）", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_n2','ai_probe_n2','1','ai_probe_n2','x','a', now() - interval '1 year', 1,"
         " '{}'::jsonb, '[]'::jsonb, 100, true, 100, 0, 'PENDING')"),
        ("customer_phone 非手机号格式", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_n3','ai_probe_n3','not-a-phone','ai_probe_n3','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 100, true, 100, 0, 'PENDING')"),
        ("status=COMPLETED 但 deposit_cent=0（未付定金却已完成）", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " address, reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_n4','ai_probe_n4','1','ai_probe_n4','x','a', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 100, true, 100, 0, 'COMPLETED')"),
        ("orders.address 为 NULL（DB 声明 NOT NULL）", "orders",
         "INSERT INTO orders (order_no, customer_name, customer_phone, banquet_name, banquet_type,"
         " reservation_time, table_count, package_snapshot, added_items_snapshot,"
         " plan_total_cent, use_final_price, final_price_cent, deposit_cent, status)"
         " VALUES ('ai_probe_n5','ai_probe_n5','1','ai_probe_n5','x', now(), 1,"
         " '{}'::jsonb, '[]'::jsonb, 100, true, 100, 0, 'PENDING')"),
        ("products.category_id 指向一个不存在的分类（FK 应拦）", "products",
         "INSERT INTO products (name, category_id, price_cent, unit)"
         " VALUES ('ai_probe_n6', 999999, 100, '份')"),
    ]
    for label, table, stmt in no_defense:
        r = psql_script("BEGIN;\n" + stmt + ";\nROLLBACK;\n")
        results.append({"constraint": None, "label": label, "table": table,
                        "blocked": bool(r["stderr"]), "error": r["stderr"][:300]})

    # 3) 软删语义是否沿外键传播：引用一个已软删的父行
    soft = []
    for label, stmt, probe in [
        ("product -> 已软删的 category",
         "UPDATE categories SET is_deleted=true WHERE id=(SELECT id FROM categories WHERE is_deleted=false LIMIT 1);",
         "SELECT count(*) FROM products p JOIN categories c ON c.id=p.category_id"
         " WHERE c.is_deleted=true AND p.is_deleted=false"),
        # orders 表没有 is_deleted，所以这条看的是「订单仍指向已软删的套餐」这种悬空引用
        ("order -> 已软删的 package",
         "UPDATE packages SET is_deleted=true WHERE id=(SELECT id FROM packages WHERE is_deleted=false LIMIT 1);",
         "SELECT count(*) FROM orders o JOIN packages p ON p.id=o.package_id"
         " WHERE p.is_deleted=true"),
    ]:
        r = psql_script("BEGIN;\n" + stmt + "\n" + probe + ";\nROLLBACK;\n")
        soft.append({"label": label, "stdout": r["stdout"], "stderr": r["stderr"][:200]})

    return {"constraint_probes": results, "softdelete_propagation": soft}


def contract_boundary_probe():
    """DTO 声明可空、DB 声明 NOT NULL 的字段，走真实 API 会怎样。"""
    base = {
        "customerName": "ai_probe_cls", "customerPhone": "13800000001",
        "banquetName": "ai_probe_cls", "banquetType": "婚宴",
        "reservationTime": (datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(days=30)).isoformat(),
        "tableCount": 1, "packageId": None, "planTotalCent": 100,
        "useFinalPrice": True, "finalPriceCent": 100, "depositCent": 0,
        "status": "PENDING",
    }
    pkg = sql("SELECT id FROM packages WHERE is_deleted=false LIMIT 1")
    pid = int(pkg[0][0]) if pkg else None
    cases = [
        ("address 缺省（DTO 无约束，DB NOT NULL）", {**base, "packageId": pid}),
        ("finalPriceCent=null（DTO 可空，DB NOT NULL）", {**base, "packageId": pid, "finalPriceCent": None}),
        ("reservationTime 在过去", {**base, "packageId": pid,
                                    "reservationTime": (datetime.datetime.now(datetime.timezone.utc)
                                                        - datetime.timedelta(days=365)).isoformat()}),
        ("customerPhone 非手机号", {**base, "packageId": pid, "customerPhone": "not-a-phone"}),
        ("status 小写 pending", {**base, "packageId": pid, "status": "pending"}),
        ("depositCent > finalPriceCent", {**base, "packageId": pid,
                                           "depositCent": 999999, "finalPriceCent": 1}),
        ("tableCount=0", {**base, "packageId": pid, "tableCount": 0}),
        ("status=COMPLETED 且 depositCent=0", {**base, "packageId": pid, "status": "COMPLETED"}),
    ]
    out = []
    for label, body in cases:
        st, j = api("POST", "/api/orders", body)
        row = None
        if st == 200 and j and j.get("data"):
            oid = j["data"]["id"]
            r = rows(f"SELECT status, deposit_cent, final_price_cent, table_count,"
                     f" reservation_time, customer_phone, address, plan_total_cent"
                     f" FROM orders WHERE id={oid}")
            row = r[0] if r else None
        out.append({"label": label, "http": st,
                    "code": (j or {}).get("code"),
                    "message": (j or {}).get("message"),
                    "db_row": row})
    return out


def nullability_probe():
    """可空性穿透：DB 声明 NOT NULL 的列，对应的 DTO 字段却允许缺省。

    问的是「谁负责兜底」：DTO 不管、服务层不管，就是 500。
    """
    cases = []
    cat = sql("SELECT id FROM categories WHERE is_deleted=false LIMIT 1")
    cid = int(cat[0][0])
    probes = [
        ("products.is_active 缺省（DB NOT NULL, DTO 无 @NotNull）", "POST", "/api/products",
         {"name": MARK + "nn", "categoryId": cid, "priceCent": 1, "unit": "份"}),
        ("packages.is_active 缺省（DB NOT NULL, DTO 无 @NotNull）", "POST", "/api/packages",
         {"name": MARK + "nn_pkg", "banquetType": "婚宴", "priceCent": 1,
          "items": [{"productId": 1, "quantity": 1}]}),
        ("products.categoryId 缺省（DB NOT NULL, DTO 有 @NotNull）", "POST", "/api/products",
         {"name": MARK + "nn2", "priceCent": 1, "unit": "份"}),
    ]
    for label, method, path, body in probes:
        st, j = api(method, path, body)
        row = None
        if st == 200 and j and j.get("data"):
            oid = j["data"]["id"]
            tbl = "products" if "products" in path else "packages"
            r = rows(f"SELECT id, is_active, name FROM {tbl} WHERE id={oid}")
            row = r[0] if r else None
        cases.append({"label": label, "http": st,
                      "code": (j or {}).get("code"), "message": (j or {}).get("message"),
                      "details": (j or {}).get("details"),
                      "db_row": row})
        if st == 200 and j and j.get("data"):
            oid = j["data"]["id"]
            tbl = "products" if "products" in path else "packages"
            sql(f"DELETE FROM package_items WHERE package_id={oid}")
            sql(f"DELETE FROM {tbl} WHERE id={oid}")
    return cases


def write_footprint():
    """列级写入足迹：每个写动作前后做整行快照，差分出真正被改的列。"""
    out = []

    def snap(table, id_):
        r = rows(f"SELECT * FROM {table} WHERE id={id_}")
        return r[0] if r else {}

    def diff(a, b):
        return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))

    # product: create -> update -> status -> delete
    cat = sql("SELECT id FROM categories WHERE is_deleted=false LIMIT 1")
    cid = int(cat[0][0])
    st, j = api("POST", "/api/products", {"name": "ai_probe_fp", "categoryId": cid,
                                          "priceCent": 1234, "unit": "份",
                                          "imageUrl": "/uploads/seed/products/product-01.jpg",
                                          "description": "probe", "isActive": True})
    if st == 200 and j and j.get("data"):
        pid = j["data"]["id"]
        steps = []
        s0 = snap("products", pid)
        api("PUT", f"/api/products/{pid}", {"name": "ai_probe_fp2", "categoryId": cid,
                                            "priceCent": 9999, "unit": "克",
                                            "description": "changed", "isActive": False})
        s1 = snap("products", pid)
        steps.append({"action": "PUT /api/products/{id}", "changed": diff(s0, s1)})
        api("PATCH", f"/api/products/{pid}/status", {"isActive": True})
        s2 = snap("products", pid)
        steps.append({"action": "PATCH /api/products/{id}/status", "changed": diff(s1, s2)})
        # PUT 换外键：证明 category_id 也在 PUT 的可写范围内
        cid2 = sql("SELECT id FROM categories WHERE is_deleted=false AND id<>%d LIMIT 1" % cid)
        cid2 = int(cid2[0][0]) if cid2 else cid
        api("PUT", f"/api/products/{pid}", {"name": "ai_probe_fp2", "categoryId": cid2,
                                            "priceCent": 9999, "unit": "克",
                                            "description": "changed", "isActive": False})
        s3 = snap("products", pid)
        steps.append({"action": "PUT /api/products/{id} 换 categoryId",
                      "changed": diff(s2, s3)})
        # PUT 缺省必填外键：DTO 的 @NotNull 能不能挡住
        st_put, _ = api("PUT", f"/api/products/{pid}", {"name": "ai_probe_fp3", "priceCent": 1,
                                                       "unit": "克"})
        steps.append({"action": "PUT /api/products/{id} 缺省 categoryId",
                      "http": st_put, "changed": diff(s3, snap("products", pid))})
        api("DELETE", f"/api/products/{pid}")
        s4 = snap("products", pid)
        steps.append({"action": "DELETE /api/products/{id}", "changed": diff(s3, s4)})
        out.append({"entity": "products", "id": pid, "steps": steps,
                    "final_row": s4})
        sql(f"DELETE FROM products WHERE id={pid}")

    # package: create -> update -> delete
    st, j = api("POST", "/api/packages", {"name": "ai_probe_fp_pkg", "banquetType": "婚宴",
                                          "priceCent": 1000, "items": []})
    if st == 200 and j and j.get("data"):
        pid = j["data"]["id"]
        steps = []
        s0 = snap("packages", pid)
        api("PUT", f"/api/packages/{pid}", {"name": "ai_probe_fp_pkg2", "banquetType": "婚宴",
                                            "priceCent": 2000, "items": []})
        s1 = snap("packages", pid)
        steps.append({"action": "PUT /api/packages/{id}", "changed": diff(s0, s1)})
        api("DELETE", f"/api/packages/{pid}")
        s2 = snap("packages", pid)
        steps.append({"action": "DELETE /api/packages/{id}", "changed": diff(s1, s2)})
        out.append({"entity": "packages", "id": pid, "steps": steps, "final_row": s2})
        sql(f"DELETE FROM package_items WHERE package_id={pid}")
        sql(f"DELETE FROM packages WHERE id={pid}")

    return out

def main():
    res = {"transition": transition_matrix(), "index_usage": index_usage(),
           "nullability": nullability_probe(),
           "softdelete_unique": softdelete_unique(), "db_invariants": db_invariants(),
           "scale_experiment": scale_experiment(),
           "defense_audit": defense_audit(),
           "contract_boundary": contract_boundary_probe(),
           "write_footprint": write_footprint()}
    json.dump(res, open(os.path.join(D, "class_observation.json"), "w"), ensure_ascii=False, indent=2)
    print("状态迁移矩阵（实测允许?）:")
    for k, v in res["transition"]["matrix"].items():
        print(f"  {k:26} http={v['http']} db={v['db_status']}")
    print("索引可用性:")
    for k, v in res["index_usage"].items():
        print(f"  {k:22} 默认={v['node'][:34]:34} 禁用seqscan后={v['forced_seqscan_off_node'][:34]:34} 索引可用={v['index_usable']}")
    print("软删+唯一:", {k: v for k, v in res["softdelete_unique"].items() if k != "recreate_body"})
    print("等比放大实验(影子表 5 万行):")
    for k, v in res["scale_experiment"].items():
        if k == "rows":
            continue
        print(f"  {k:18} {v['exec_ms']:>8} ms  cond={v['index_cond']}  {v['rows_removed_by_filter']}")
    print("DB 不变量:", {k: v for k, v in res["db_invariants"].items() if k != "table_sizes"})
    print("DB 防线盘点:")
    for d in res["defense_audit"]["constraint_probes"]:
        tag = d.get("constraint") or d.get("label")
        print(f"  {'拦住' if d['blocked'] else '没拦住'}  {tag}")
    print("契约边界实测:")
    for d in res["contract_boundary"]:
        print(f"  http={d['http']:4} {d['label']}")
    print("可空性穿透:")
    for d in res["nullability"]:
        print(f"  http={d['http']:4} {d['label']}  -> {d['db_row']}")
    print("列级写入足迹:")
    for w in res["write_footprint"]:
        for st in w["steps"]:
            print(f"  {w['entity']:9} {st['action']:32} -> {st['changed']}")

if __name__ == "__main__":
    main()
