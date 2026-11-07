"""类层静态声明：从 Flyway DDL 抽「表级契约」，从 JPA 实体抽「Java 级契约」。
两份声明互为参照 —— 它们的差分本身就是一类缺陷。
"""
import json, os, re, glob, datetime

D = os.path.dirname(os.path.abspath(__file__))
MIG = "/Users/june/jiuyan/backend/src/main/resources/db/migration"
ENT = "/Users/june/jiuyan/backend/src/main/java/com/jiuyan/banquet/entity"
OUT = os.path.join(D, "class_declaration.json")

CREATE = re.compile(r"CREATE TABLE\s+(\w+)\s*\((.*?)\n\);", re.S | re.I)
CONSTRAINT = re.compile(r"CONSTRAINT\s+(\w+)\s+(UNIQUE|CHECK|FOREIGN KEY|PRIMARY KEY)\s*(\([^)]*\)|[^,]*)", re.I)
COLUMN = re.compile(r"^\s*(\w+)\s+([A-Z]+(?:\([^)]*\))?)((?:\s+NOT NULL|\s+UNIQUE|\s+PRIMARY KEY|\s+DEFAULT [^,\n]+)*)\s*,?\s*$", re.I | re.M)
CREATE_INDEX = re.compile(r"CREATE INDEX\s+(\w+)\s+ON\s+(\w+)\s*\(([^)]*)\)", re.I)
ALTER_ADD = re.compile(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)\s+([A-Z]+(?:\([^)]*\))?)((?:\s+NOT NULL|\s+DEFAULT [^,\n]+)*)", re.I)

def parse_ddl():
    tables, indexes = {}, []
    for f in sorted(glob.glob(os.path.join(MIG, "*.sql"))):
        sql = open(f).read()
        for m in CREATE.finditer(sql):
            name, body = m.group(1), m.group(2)
            t = {"columns": {}, "constraints": {}}
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.upper().startswith("CONSTRAINT"):
                    continue
                cm = COLUMN.match(line)
                if cm:
                    col, typ, mods = cm.group(1), cm.group(2), (cm.group(3) or "").upper()
                    t["columns"][col] = {"type": typ,
                                         "not_null": "NOT NULL" in mods,
                                         "unique": "UNIQUE" in mods,
                                         "primary_key": "PRIMARY KEY" in mods,
                                         "default": (re.search(r"DEFAULT\s+([^,\s]+)", mods).group(1)
                                                     if "DEFAULT" in mods else None)}
            for cm in CONSTRAINT.finditer(body):
                t["constraints"][cm.group(1)] = {"kind": cm.group(2).upper(),
                                                "expr": (cm.group(3) or "").strip()}
            tables[name] = t
        for m in ALTER_ADD.finditer(sql):
            tbl, col, typ, mods = m.group(1), m.group(2), m.group(3), (m.group(4) or "").upper()
            tables.setdefault(tbl, {"columns": {}, "constraints": {}})
            tables[tbl]["columns"][col] = {"type": typ, "not_null": "NOT NULL" in mods,
                                           "unique": "UNIQUE" in mods,
                                           "primary_key": "PRIMARY KEY" in mods,
                                         "primary_key": "PRIMARY KEY" in mods,
                                           "default": (re.search(r"DEFAULT\s+([^,\s]+)", mods).group(1)
                                                       if "DEFAULT" in mods else None)}
        for m in CREATE_INDEX.finditer(sql):
            indexes.append({"name": m.group(1), "table": m.group(2), "cols": m.group(3).strip()})
    return tables, indexes

def parse_entities():
    out = {}
    for f in sorted(glob.glob(os.path.join(ENT, "*.java"))):
        src = open(f).read()
        cls = re.search(r"public (?:abstract )?class (\w+)", src)
        if not cls:
            continue
        tbl = re.search(r'@Table\(name\s*=\s*"(\w+)"\)', src)
        cols = []
        for cm in re.finditer(
                r'@Column\(([^)]*)\)\s*private\s+([\w<>]+)\s+(\w+)\s*;', src):
            args, jtype, field = cm.group(1), cm.group(2), cm.group(3)
            nm = re.search(r'name\s*=\s*"(\w+)"', args)
            cols.append({"field": field, "column": nm.group(1) if nm else field,
                         "java_type": jtype,
                         "nullable": "nullable = false" not in args,
                         "unique": "unique = true" in args,
                         "length": (re.search(r"length\s*=\s*(\d+)", args).group(1)
                                    if re.search(r"length\s*=\s*(\d+)", args) else None),
                         "enum": "Enumerated" in src.split(field)[0].split("@Column")[0][-120:]})
        base = "BaseEntity" in src
        out[cls.group(1)] = {"file": os.path.basename(f), "table": tbl.group(1) if tbl else None,
                             "columns": cols, "extends_base_entity": base}
    return out

def main():
    tables, indexes = parse_ddl()
    entities = parse_entities()
    decl = {"generated_at": datetime.datetime.now().isoformat(),
            "ddl": {"tables": tables, "indexes": indexes},
            "jpa": entities}
    json.dump(decl, open(OUT, "w"), ensure_ascii=False, indent=2)
    print(f"类层声明 -> {OUT}")
    for t, spec in tables.items():
        nn = sum(1 for c in spec["columns"].values() if c["not_null"])
        chk = [k for k, v in spec["constraints"].items() if v["kind"] == "CHECK"]
        print(f"  表 {t:14} 列 {len(spec['columns']):2}  NOT NULL {nn:2}  CHECK {len(chk)}  实体映射 "
              f"{'有' if any(e['table'] == t for e in entities.values()) else '无'}")
    print(f"  索引 {len(indexes)} 个 | JPA 实体 {len(entities)} 个")

if __name__ == "__main__":
    main()
