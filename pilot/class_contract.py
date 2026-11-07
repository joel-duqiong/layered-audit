"""类层契约声明（v2）：一个字段从「传输 → 实体 → 存储」的完整声明，三方各取一份。

与 class_static.py 的分工：
  class_static.py  读 Flyway 迁移文件 + 实体源码   -> 迁移文件「声称」的契约
  class_contract.py 反射读 DTO/实体 + 直查 DB      -> 运行中「实际」的契约

两者对不上的地方就是迁移漂移；本文件的三方声明对不上的地方就是契约缺陷。

为什么要反射：源码正则分不清 record 组件上的约束注解落在字段、访问器还是构造参数上，
而这正是本层要比对的东西（DTO 到底声明了什么约束）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fn_static import BACKEND, CLASSES, PKG, ensure_compiled, run

D = Path(__file__).parent
WORK = Path("/tmp/jiuyan_class")
HARNESS = WORK / "harness"
DEPS_FILE = D / "file_deps.txt"
OUT = D / "class_contract.json"

SRC_ROOTS = ("dto", "entity")


def psql(sql: str) -> str:
    rc = subprocess.run(
        ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan", "-tAF", "|", "-c", sql],
        capture_output=True, text=True, timeout=60)
    if rc.returncode != 0:
        raise RuntimeError(rc.stderr.strip()[:300])
    return rc.stdout.strip()


def db_actual() -> dict:
    """直接问 DB 自己：列、约束、索引、默认值。DB 是运行时的事实，不是迁移文件的转述。"""
    cols = psql("""
        SELECT table_name, column_name, data_type, is_nullable,
               COALESCE(column_default,''), character_maximum_length
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name NOT LIKE 'ai_probe_%'
          AND table_name <> 'flyway_schema_history'
        ORDER BY table_name, ordinal_position
    """).splitlines()
    tables: dict[str, dict] = {}
    for line in cols:
        t, c, dt, nullable, default, maxlen = line.split("|")
        tb = tables.setdefault(t, {"columns": {}, "constraints": {}, "indexes": []})
        tb["columns"][c] = {"type": dt, "nullable": nullable == "YES",
                            "default": default or None, "max_length": maxlen or None}

    cons = psql("""
        SELECT rel.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname='public' AND rel.relname NOT LIKE 'ai_probe_%'
          AND rel.relname <> 'flyway_schema_history'
        ORDER BY rel.relname, con.conname
    """).splitlines()
    kind = {"c": "CHECK", "u": "UNIQUE", "f": "FOREIGN KEY", "p": "PRIMARY KEY"}
    for line in cons:
        t, name, ct, cdef = line.split("|", 3)
        tb = tables.setdefault(t, {"columns": {}, "constraints": {}, "indexes": []})
        tb["constraints"][name] = {"kind": kind.get(ct, ct), "def": cdef}

    idx = psql("""
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname='public' AND tablename NOT LIKE 'ai_probe_%'
          AND tablename <> 'flyway_schema_history'
        ORDER BY tablename, indexname
    """).splitlines()
    for line in idx:
        t, name, idef = line.split("|", 2)
        tb = tables.setdefault(t, {"columns": {}, "constraints": {}, "indexes": []})
        tb["indexes"].append({"name": name, "def": idef})

    return {"tables": tables,
            "note": "直接查 information_schema / pg_constraint / pg_indexes，不读迁移文件"}


def reflection() -> list[dict]:
    """反射导出 DTO 与实体的精确声明。"""
    ensure_compiled()
    HARNESS.mkdir(parents=True, exist_ok=True)
    src = D / "class_harness" / "ClassContract.java"
    out_dir = WORK / "classes"
    out_dir.mkdir(parents=True, exist_ok=True)
    deps = DEPS_FILE.read_text().strip() if DEPS_FILE.exists() else ""
    run(["javac", "-cp", f"{CLASSES}:{deps}", "-d", str(out_dir), str(src)])

    classes = []
    for root in SRC_ROOTS:
        for path in sorted(CLASSES.glob(f"{PKG.replace('.', '/')}/{root}/*.class")):
            if "$" in path.name:
                continue
            classes.append(path.relative_to(CLASSES).with_suffix("").as_posix().replace("/", "."))
    listing = WORK / "classes.txt"
    listing.write_text("\n".join(classes))
    rc = subprocess.run(
        ["java", "-cp", f"{CLASSES}:{out_dir}:{deps}", "ClassContract"],
        stdin=listing.open(), capture_output=True, text=True)
    if rc.returncode != 0:
        raise RuntimeError(rc.stderr.strip()[:500])
    return json.loads(rc.stdout)


def main() -> None:
    contract = {"reflection": reflection(), "db_actual": db_actual()}
    OUT.write_text(json.dumps(contract, ensure_ascii=False, indent=2))
    dtos = [c for c in contract["reflection"] if c["kind"] == "record" and c["components"]]
    ents = [c for c in contract["reflection"] if c["kind"] == "class"]
    print(f"类层契约 -> {OUT}")
    print(f"  DTO 记录 {len(dtos)} 个 / 实体类 {len(ents)} 个 / "
          f"DB 表 {len(contract['db_actual']['tables'])} 张")


if __name__ == "__main__":
    main()
