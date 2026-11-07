"""类层探针 · 通用引擎（声明式）。

通用骨架在这里；**项目特定的东西全在项目侧的 class_scenarios.json 里**：
造什么临时实体、查什么表、什么端点、哪些字段带前缀、什么状态机、什么 SQL 探针。
换项目只换那个文件，引擎一行不改。

探针是「声明式」的：每个探针自己声明它要看什么（表 / 列 / 实体 / DTO / 端点），
引擎只负责执行 —— 不替项目猜业务语义。

三条硬规矩（都是本项目踩过的坑）：

1. **造出来的行必须带前缀**（``ai_probe_``）。没有前缀的行清理脚本删不掉，
   还会污染种子基线（亲眼见过 products 里留下叫 ``probe-str`` 的行）。
2. **探针跑完自己删干净**。引擎记下每一条它造的行，探针结束时统一清。
   「别人会帮我清」不是理由 —— 流水线里探针和清理脚本之间还有别的步骤。
3. **类型探针绝不能破坏前缀字段**。破坏了，等于造了一行清理不掉的垃圾。
   引擎在每个写入场景后强制把前缀字段盖回带前缀的值。

DB 通道（容器名 / 库名 / 用户）与 HTTP base_url 全部来自项目绑定的 ``paths`` 段，
本文件不含任何项目字面量。
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import requests

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Database:
    """DB 通道 —— 容器名 / 库名 / 用户全部来自项目绑定，不写死。"""

    def __init__(self, container: str, user: str, database: str, timeout: int = 120):
        self.container = container
        self.user = user
        self.database = database
        self.timeout = timeout

    def _psql(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec"]
        if stdin is not None:
            cmd.append("-i")
        cmd += [self.container, "psql", "-U", self.user, "-d", self.database, *args]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=self.timeout)

    def sql(self, query: str) -> list[list[str]]:
        """按 | 分隔取列。"""
        out = self._psql(["-tAF", "|", "-c", query])
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        return [r.split("|") for r in out.stdout.strip().splitlines() if r != ""]

    def raw(self, query: str) -> str:
        """原样取 stdout（EXPLAIN 之类的多行输出）。"""
        out = self._psql(["-tA", "-c", query])
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        return out.stdout

    def rows(self, query: str) -> list[dict]:
        """取行并按列名返回字典（整行快照差分用）。

        用 to_jsonb 把整行压成一个 JSON 字面量再逐行解析，
        避免用分隔符切列时 jsonb 里的逗号/换行把行切碎。
        """
        out = self._psql(["-tA", "-c", f"SELECT to_jsonb(_r) FROM ({query}) _r"])
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        res = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if line:
                res.append(json.loads(line))
        return res

    def script(self, script: str) -> dict:
        """在单个 psql 会话里跑一段脚本。脚本自带 BEGIN，session 结束即回滚 —— 零残留。

        返回 stdout / stderr；stderr 非空说明有语句被 DB 拒绝了（防线生效）。
        """
        out = self._psql(["-tAF", "|", "-v", "ON_ERROR_STOP=0", "-f", "-"], stdin=script)
        return {"stdout": out.stdout.strip(), "stderr": out.stderr.strip(),
                "returncode": out.returncode}


class Api:
    """HTTP 通道 —— base_url 来自项目绑定。"""

    def __init__(self, base_url: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        r = self.session.request(method, self.base_url + path, json=body,
                                timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None


class ClassProbe:
    """类层探针引擎。"""

    def __init__(self, cfg: dict, db: Database, api: Api):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.prefix: str = cfg.get("probe_prefix", "ai_probe_")
        self.prefix_fields: dict = cfg.get("prefix_fields", {})
        self.results: dict[str, Any] = {}
        self.errors: list[dict] = []
        self._created: list[dict] = []      # 本探针造出来的行
        self._cleanup_log: list[dict] = []

    # ------------------------------------------------------------------ 骨架
    def run(self) -> dict[str, Any]:
        for probe in self.cfg.get("probes", []):
            kind = str(probe.get("kind", ""))
            handler = getattr(self, "_p_" + kind, None)
            if handler is None:
                self.errors.append({"probe": probe.get("id"), "kind": kind,
                                    "error": f"未知探针类型 {kind}"})
                continue
            self._created = []
            try:
                self.results[str(probe["id"])] = handler(probe)
            except Exception as exc:                       # 探针失败不许带崩整轮
                self.errors.append({"probe": probe.get("id"), "kind": kind,
                                    "error": f"{type(exc).__name__}: {exc}"})
            self._cleanup(probe)
        self.results["_cleanup"] = self._cleanup_log
        self.results["_errors"] = self.errors
        return self.results

    # ------------------------------------------------------------- 通用小工具
    def _resolve(self, value: Any, ctx: dict) -> Any:
        """把配置里的值解析成实参。

        支持三种写法：
          "{{prefix}}" / "{{rand6}}" / "{{plus30d}}" —— 占位符
          {"$sql": "SELECT ...", "as": "int"}          —— 现查现用（造数要现查的 id）
          {"$sql": "...", "as": "int", "as_name": "x"} —— 顺带把结果存进上下文
        """
        if isinstance(value, str):
            return PLACEHOLDER_RE.sub(lambda m: str(self._placeholder(m.group(1), ctx)), value)
        if isinstance(value, dict):
            if "$sql" in value:
                rows = self.db.sql(str(value["$sql"]))
                cell = rows[0][0] if rows and rows[0] else ""
                cast = value.get("as", "str")
                out: Any = cell
                if cast in ("int", "int_or_null"):
                    out = int(cell) if str(cell).strip() != "" else None
                name = value.get("as_name")
                if name:
                    ctx[str(name)] = out
                return out
            return {k: self._resolve(v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, ctx) for v in value]
        return value

    def _placeholder(self, key: str, ctx: dict) -> Any:
        if key == "prefix":
            return self.prefix
        if key == "rand6":
            return uuid.uuid4().hex[:6]
        if key == "plus30d":
            return (_now() + datetime.timedelta(days=30)).isoformat()
        if key == "minus365d":
            return (_now() - datetime.timedelta(days=365)).isoformat()
        if key == "now":
            return _now().isoformat()
        if key in ctx:
            return ctx[key]
        return ""

    def _entity_prefix_dto_fields(self, entity: str | None) -> list[str]:
        spec = self.prefix_fields.get(entity or "", {})
        return list(spec.get("dto", []))

    def _enforce_prefix(self, body: Any, probe: dict) -> Any:
        """前缀字段必须始终带前缀。

        为什么每场景都要盖：某个场景会把垃圾值写进一个字段 —— 如果那个字段
        正好是实体的名字字段，造出来的行就**不带前缀**，清理脚本删不掉，
        还会污染种子基线。
        """
        if not isinstance(body, dict):
            return body
        for field in self._entity_prefix_dto_fields(probe.get("entity")):
            value = body.get(field)
            if isinstance(value, str) and not value.startswith(self.prefix):
                body[field] = self.prefix + "probe"
        return body

    def _track(self, table: str | None, row_id: Any) -> None:
        if table and row_id is not None:
            self._created.append({"table": table, "id": row_id})

    def _cleanup(self, probe: dict) -> None:
        """把本探针造出来的行删干净 —— 自己造的自己清，不指望别人。"""
        statements = list(probe.get("cleanup_sql", []))
        for row in self._created:
            ctx = {"id": row["id"], "table": row["table"]}
            for stmt in statements:
                filled = self._resolve(stmt, ctx)
                try:
                    self.db.raw(filled)
                    self._cleanup_log.append({"table": row["table"], "id": row["id"],
                                               "sql": filled, "ok": True})
                except Exception as exc:
                    self._cleanup_log.append({"table": row["table"], "id": row["id"],
                                               "sql": filled, "ok": False,
                                               "error": str(exc)[:200]})
        # 兜底：探针没写 cleanup_sql 时，按 id 删掉它造的行
        if not statements:
            for row in self._created:
                try:
                    self.db.raw(f"DELETE FROM {row['table']} WHERE id = {row['id']}")
                    self._cleanup_log.append({"table": row["table"], "id": row["id"],
                                               "sql": "DELETE BY ID", "ok": True})
                except Exception as exc:
                    self._cleanup_log.append({"table": row["table"], "id": row["id"],
                                               "sql": "DELETE BY ID", "ok": False,
                                               "error": str(exc)[:200]})

    def _created_id(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return data.get("id")
        return None

    # ------------------------------------------------------------ 状态迁移矩阵
    def _p_state_transition(self, probe: dict) -> dict:
        """状态迁移矩阵：每个状态能不能直接跳到另一个状态，DB 里落成什么。"""
        ctx: dict[str, Any] = {}
        create = probe["create"]
        body = self._enforce_prefix(self._resolve(create["body"], ctx), probe)
        st, payload = self.api.call(create["method"], create["path"], body)
        oid = self._created_id(payload)
        if oid is None:
            raise RuntimeError(f"创建失败: HTTP {st}")
        self._track(probe.get("entity"), oid)
        endpoint = probe["state_endpoint"]
        read = probe["read"]
        states = list(probe["states"])
        matrix: dict[str, Any] = {}
        for frm in states:
            self.api.call(endpoint["method"], endpoint["path"].format(id=oid),
                          {probe.get("state_field", "status"): frm})
            for to in states:
                st, _ = self.api.call(endpoint["method"], endpoint["path"].format(id=oid),
                                      {probe.get("state_field", "status"): to})
                actual = self.db.sql(f"SELECT {read['column']} FROM {read['table']} "
                                     f"WHERE id={oid}")
                matrix[f"{frm}->{to}"] = {
                    "http": st, "accepted": st == 200,
                    "db_status": actual[0][0] if actual else None}
        return {"order_id": oid, "matrix": matrix}

    # ------------------------------------------------------------ 索引可用性
    def _p_index_usage(self, probe: dict) -> dict:
        """声明的索引能不能被服务层的查询模式用上（默认走什么 + 禁用 seqscan 后走什么）。"""
        res: dict[str, Any] = {}
        for item in probe["queries"]:
            label, query = item["label"], item["sql"]
            plan_text = self.db.raw("EXPLAIN (ANALYZE) " + query)
            forced = self.db.raw("SET enable_seqscan=off; EXPLAIN (ANALYZE) " + query)
            lines = [l.strip() for l in plan_text.splitlines()]
            node = next((l for l in lines if "Scan on " in l or "Scan using " in l), "?")
            index = self._index_name(lines)
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

    @staticmethod
    def _index_name(lines: list[str]) -> str | None:
        """从执行计划里取索引名。

        Bitmap 计划写的是「Bitmap Index Scan on <索引名>」，没有 "using "；
        直接按 "using " 切会 IndexError —— 探针不该因为一条计划格式崩掉。
        """
        for line in lines:
            if "Index Scan using" in line:
                return line.split("using ")[1].split(" ")[0]
            if "Bitmap Index Scan on" in line:
                return line.split("on ")[-1].split(" ")[0]
        return None

    # -------------------------------------------------------- 软删 + 唯一约束
    def _p_soft_delete_unique(self, probe: dict) -> dict:
        """软删除 + 唯一约束：删掉后能否用同名重建。"""
        ctx: dict[str, Any] = {}
        create = probe["create"]
        name_field = probe.get("name_field", "name")
        name = self._resolve(create["body"].get(name_field, ""), ctx)
        ctx["name"] = name
        body = dict(create["body"])
        body[name_field] = name
        body = self._enforce_prefix(self._resolve(body, ctx), probe)
        st, payload = self.api.call(create["method"], create["path"], body)
        created = self._created_id(payload)
        self._track(probe.get("entity"), created)
        out: dict[str, Any] = {"category": name, "create_status": st}
        if created:
            delete = probe["delete"]
            st2, _ = self.api.call(delete["method"], delete["path"].format(id=created))
            out["delete_status"] = st2
            st3, payload3 = self.api.call(create["method"], create["path"], body)
            out["recreate_status"] = st3
            out["recreate_body"] = payload3
            read = probe["read"]
            where = read.get("where", "id = {id}").format(id=created)
            rows = self.db.sql(f"SELECT {read['columns']} FROM {read['table']} WHERE {where}")
            out["db_rows"] = rows
        return out

    # ------------------------------------------------------------ 等比放大实验
    def _p_scale_experiment(self, probe: dict) -> dict:
        """等比放大实验：在影子表上复刻同一份 DDL + 索引，看索引在数据量上来后是否还生效。

        只建 ai_probe_ 前缀的影子表，跑完即删，不动业务表。
        """
        shadow = probe["shadow_table"]
        rows = int(probe["rows"])
        try:
            self.db.raw(f"DROP TABLE IF EXISTS {shadow}")
            self.db.raw(f"CREATE TABLE {shadow} (LIKE {probe['source_table']} INCLUDING ALL)")
            self.db.raw(self._resolve(probe["insert_sql"], {"rows": rows}))
            out: dict[str, Any] = {"rows": rows}
            for item in probe["queries"]:
                txt = self.db.raw("EXPLAIN (ANALYZE) " + item["sql"])
                lines = [l.strip() for l in txt.splitlines()]
                node = next((l for l in lines if "Scan on " in l or "Scan using " in l), "?")
                t = next((l.split("actual time=")[1].split("..")[1].split("ms")[0]
                          for l in lines if "actual time=" in l and "Scan" in l), None)
                cond = next((l.strip() for l in lines if l.strip().startswith("Index Cond")), None)
                filt = next((l.strip() for l in lines if l.strip().startswith("Filter:")), None)
                rm = next((l.strip() for l in lines if "Rows Removed by Filter" in l), None)
                out[item["label"]] = {"node": node[:80], "index_cond": cond, "filter": filt,
                                      "rows_removed_by_filter": rm, "exec_ms": t}
            return out
        finally:
            try:
                self.db.raw(f"DROP TABLE IF EXISTS {shadow}")
            except Exception:
                pass

    # ---------------------------------------------------------------- DB 不变量
    def _p_db_invariants(self, probe: dict) -> dict:
        """DB 层现存数据是否违反表级声明。"""
        decl_path = Path(probe["declaration"]) if probe.get("declaration") \
            else self._declaration_path()
        decl = json.loads(decl_path.read_text(encoding="utf-8"))
        out: dict[str, Any] = {}
        for table, spec in decl["ddl"]["tables"].items():
            checks = []
            for col, c in spec["columns"].items():
                if c["not_null"]:
                    n = self.db.sql(f"SELECT count(*) FROM {table} WHERE {col} IS NULL")[0][0]
                    if int(n) != 0:
                        checks.append({"col": col, "null_rows": n})
            out[table] = checks
        for check in probe.get("orphan_checks", []):
            rows = self.db.sql(check["sql"])
            out[check["label"]] = rows[0][0] if rows else None
        soft = probe.get("soft_delete_probe")
        if soft:
            cols = decl["ddl"]["tables"].get(soft["table"], {}).get("columns", {})
            out[soft["label"]] = soft["column"] not in cols
        out["table_sizes"] = self.db.sql(probe["table_sizes_sql"])
        return out

    def _declaration_path(self) -> Path:
        import project
        return project.artifact_dir() / "class_declaration.json"

    # ---------------------------------------------------------------- DB 防线
    def _p_defense_audit(self, probe: dict) -> dict:
        """DB 是最后一道防线。先盘点防线清单，再逐条实测它到底拦不拦得住。

        所有写操作都在一个事务里，session 结束自动回滚 —— 不碰业务数据。
        """
        results = []
        for cp in probe.get("constraint_probes", []):
            r = self.db.script("BEGIN;\n" + cp["sql"] + ";\nROLLBACK;\n")
            results.append({"constraint": cp["name"], "table": cp["table"],
                            "blocked": bool(r["stderr"]), "error": r["stderr"][:300]})
        for np_ in probe.get("no_defense_probes", []):
            r = self.db.script("BEGIN;\n" + np_["sql"] + ";\nROLLBACK;\n")
            results.append({"constraint": None, "label": np_["label"], "table": np_["table"],
                            "blocked": bool(r["stderr"]), "error": r["stderr"][:300]})
        soft = []
        for sp in probe.get("soft_delete_propagation", []):
            r = self.db.script("BEGIN;\n" + sp["sql"] + "\n" + sp["probe_sql"] + ";\nROLLBACK;\n")
            soft.append({"label": sp["label"], "stdout": r["stdout"], "stderr": r["stderr"][:200]})
        return {"constraint_probes": results, "softdelete_propagation": soft}

    # ------------------------------------------------------------ 契约边界实测
    def _p_contract_boundary(self, probe: dict) -> list:
        """DTO 声明可空、DB 声明 NOT NULL 的字段，走真实 API 会怎样。"""
        ctx: dict[str, Any] = {}
        base = self._resolve(probe["body"], ctx)
        out = []
        for case in probe["cases"]:
            body = dict(base)
            body.update(self._resolve(case.get("override", {}), ctx))
            body = self._enforce_prefix(body, probe)
            st, payload = self.api.call(probe["method"], probe["path"], body)
            row = None
            oid = self._created_id(payload)
            if st == 200 and oid is not None:
                self._track(probe.get("entity"), oid)
                read = probe["read"]
                rows = self.db.rows(f"SELECT {read['columns']} FROM {read['table']} "
                                    f"WHERE id={oid}")
                row = rows[0] if rows else None
            out.append({"label": case["label"], "http": st,
                        "code": (payload or {}).get("code"),
                        "message": (payload or {}).get("message"),
                        "db_row": row})
        return out

    # ------------------------------------------------------------ 可空性穿透
    def _p_nullability(self, probe: dict) -> list:
        """可空性穿透：DB 声明 NOT NULL 的列，对应的 DTO 字段却允许缺省。

        问的是「谁负责兜底」：DTO 不管、服务层不管，就是 500。
        """
        cases = []
        for case in probe["cases"]:
            ctx: dict[str, Any] = {}
            body = self._enforce_prefix(self._resolve(case["body"], ctx), probe)
            st, payload = self.api.call(case["method"], case["path"], body)
            row = None
            oid = self._created_id(payload)
            if st == 200 and oid is not None:
                self._track(case.get("entity"), oid)
                rows = self.db.rows(f"SELECT {case['read_columns']} FROM {case['table']} "
                                    f"WHERE id={oid}")
                row = rows[0] if rows else None
            cases.append({"label": case["label"], "http": st,
                          "code": (payload or {}).get("code"),
                          "message": (payload or {}).get("message"),
                          "details": (payload or {}).get("details"),
                          "db_row": row})
        return cases

    # ------------------------------------------------------------ 列级写入足迹
    def _p_write_footprint(self, probe: dict) -> list:
        """列级写入足迹：每个写动作前后做整行快照，差分出真正被改的列。"""
        out = []
        for ent in probe["entities"]:
            ctx: dict[str, Any] = {}
            create = ent["create"]
            body = self._enforce_prefix(self._resolve(create["body"], ctx), probe)
            st, payload = self.api.call(create["method"], create["path"], body)
            oid = self._created_id(payload)
            if st != 200 or oid is None:
                continue
            self._track(ent.get("entity"), oid)
            ctx["id"] = oid
            if ent.get("alt_id_sql"):
                alt = self._resolve(ent["alt_id_sql"], ctx)
                ctx["alt_id"] = alt if alt not in ("", None) else self._resolve(
                    ent.get("alt_id_fallback", ""), ctx)

            def snap() -> dict:
                rows = self.db.rows(f"SELECT * FROM {ent['table']} WHERE id={oid}")
                return rows[0] if rows else {}

            def diff(a: dict, b: dict) -> list[str]:
                return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))

            steps = []
            previous = snap()
            for step in ent["steps"]:
                sbody = self._resolve(step.get("body"), ctx)
                sbody = self._enforce_prefix(sbody, probe)
                http, _ = self.api.call(step["method"], step["path"].format(id=oid), sbody)
                current = snap()
                record: dict[str, Any] = {"action": step["action"],
                                          "changed": diff(previous, current)}
                if step.get("record_http"):
                    record["http"] = http
                steps.append(record)
                previous = current
            out.append({"entity": ent["entity"], "id": oid, "steps": steps,
                        "final_row": previous})
        return out

    # ------------------------------------------------------- 装配差异（多实例）
    def _p_instance_pair(self, probe: dict) -> dict:
        """两个实例（不同 profile / 不同配置）跑同一个请求，行为是否一致。

        条件装配（@Profile / @Conditional）用错维度时，只有起两个实例对比才暴露。
        """
        request = probe["request"]
        results = []
        for inst in probe["instances"]:
            api = Api(inst["base_url"])
            ctx: dict[str, Any] = {}
            body = self._resolve(request.get("body"), ctx)
            body = self._enforce_prefix(body, probe)
            st, payload = api.call(request["method"], request["path"], body)
            results.append({"instance": inst["name"], "profile": inst.get("profile"),
                            "http": st, "body": payload})
        divergence = []
        if len(results) >= 2:
            fields: set[str] = set()
            for r in results:
                if isinstance(r["body"], dict):
                    fields |= set(r["body"].keys())
            for field in sorted(fields):
                values = {r["instance"]: (r["body"] or {}).get(field)
                          for r in results if isinstance(r["body"], dict)}
                if len({json.dumps(v, sort_keys=True, ensure_ascii=False)
                        for v in values.values()}) > 1:
                    divergence.append({"field": field, "values": values})
        return {"instances": results, "divergence": divergence}

    # ------------------------------------------------------------- 类形状（反射）
    def _p_class_shape(self, probe: dict) -> dict:
        """类形状：继承层次 / 覆写关系 / 值相等 / 封装泄漏 / 缓存里的可变对象。

        走 JVM 内反射（class_harness/ClassShape.java），类清单来自配置。
        """
        from class_shape_probe import run_shape_probe
        return run_shape_probe(probe)
