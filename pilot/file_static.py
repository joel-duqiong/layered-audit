"""文件层 · 静态侧：以「一个 .java 文件 = 一个类」为单位提取事实。

函数层给的是「一个函数」的签名卡；文件层给的是「一个文件」的编排图：

  - 类内方法调用图（谁调谁；哪些私有方法根本没人调）
  - 私有字段使用矩阵（每个字段被哪些方法写、被哪些方法读；只写不读 / 只读不写）
  - @Transactional 边界（写方法是否落在事务里；readOnly 语义）
  - 跨文件重复方法体（同一段逻辑被复制到几个文件 —— 分叉风险）
  - 跨文件调用边 + 实体字段写入矩阵（哪个文件改了哪个实体的哪些列）

同样只用 javap 字节码，不做源码正则解析：注解、异常表、调用图都是字节码里的确定事实。
产出 file_declaration.json。
"""
from __future__ import annotations
import project

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fn_static import BACKEND, CLASSES, PKG, WORK, ensure_compiled, run  # noqa: E402

DEPS_TEST = WORK / "deps_test.txt"

# 构造器没有「返回类型 + 空格」（javap 印成 类名(...)），所以 name 组可选
METHOD_RE = re.compile(
    r"^  (?P<ret>[\w.$\[\]<>, ?]+?)(?:\s(?P<name>[A-Za-z_$][\w$]*))?\((?P<params>.*)\)"
    r"(?:\s+throws\s+[\w., $]+)?;\s*$")
CLINIT_RE = re.compile(r"^  static \{\};\s*$")
FIELD_RE = re.compile(
    r"^  (?P<mods>(?:public|private|protected|static|final|transient|volatile)\s+)*"
    r"(?P<type>[\w.$\[\]<>, ]+?)\s+(?P<name>[A-Za-z_$][\w$]*);\s*$")
# 注意：调用「本类」方法时 javap 会省略 owner，只打印 // Method applyBase:(...)
CALL_RE = re.compile(
    r"invoke(?:virtual|static|interface|special|dynamic)\s+#\d+(?:,\s*\d+)?\s+//\s+"
    r"(?:Method|InterfaceMethod)\s+(?:(?P<owner>[\w/$]+)\.)?(?P<mname>[\w$<>]+):")
FIELDREF_RE = re.compile(
    r"(?P<kind>getfield|putfield|getstatic|putstatic)\s+#\d+\s+//\s+Field\s+"
    r"(?:(?P<owner>[\w/$]+)\.)?(?P<name>[\w$]+):(?P<type>[\w/$\[\];]+)")
INSTR_RE = re.compile(r"^\s+\d+:\s+(?P<op>\S.*)$")
# 方法引用（this::foo / Type::foo）在字节码里是 BootstrapMethods 的 REF_invoke* 条目
REF_RE = re.compile(
    r"REF_invoke\w+\s+(?:#\d+\s+)?(?P<owner>[\w/$]+)\.(?P<mname>[\w$<>]+):")

RESOURCE_MARKERS = {
    "db": ("Repository", "Mapper", "EntityManager", "JdbcTemplate"),
    "fs": ("Files", "File", "InputStream", "OutputStream", "Path"),
    "http": ("RestTemplate", "WebClient", "HttpClient"),
    "thread_local": ("MDC", "ThreadLocal"),
    "log": ("Logger", "LoggerFactory"),
}


def ensure_test_deps() -> str:
    """文件层要用 spring-test（MockHttpServletRequest）与 logback（ListAppender），需要 test 作用域 classpath。"""
    if not DEPS_TEST.exists():
        run(["mvn", "-o", "-q", "dependency:build-classpath",
             "-Dmdep.outputFile=" + str(DEPS_TEST), "-DincludeScope=test"], cwd=BACKEND)
    return DEPS_TEST.read_text().strip()


def javap_v(fqcn: str) -> str:
    return run(["javap", "-v", "-p", "-c", "-cp", str(CLASSES), fqcn]).stdout


def split_blocks(text: str) -> tuple[list[dict], list[dict]]:
    """把 javap -v 输出切成「字段声明」与「方法块」两份。"""
    fields: list[dict] = []
    methods: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if CLINIT_RE.match(line):
            current = {"header": line, "name": "<clinit>", "lines": []}
            methods.append(current)
            continue
        m = METHOD_RE.match(line)
        if m:
            name = m.group("name")
            current = {"header": line, "name": name if name else "<ctor>", "lines": []}
            methods.append(current)
            continue
        if current is None:
            fm = FIELD_RE.match(line)
            if fm and "descriptor:" not in line:
                fields.append({"name": fm.group("name"), "type": fm.group("type").strip(),
                               "mods": (fm.group("mods") or "").strip()})
            continue
        current["lines"].append(line)
    return fields, methods


def analyse_method(fqcn: str, block: dict) -> dict:
    header = block["header"]
    m = METHOD_RE.match(header)
    method_name = block.get("name") or m.group("name")
    body = "\n".join(block["lines"])
    calls = []
    field_reads: set[str] = set()
    field_writes: set[str] = set()
    entity_writes: set[str] = set()
    instructions: list[str] = []
    call_order: list[str] = []
    for line in block["lines"]:
        c = CALL_RE.search(line)
        if c:
            owner = (c.group("owner") or fqcn).replace("/", ".")
            calls.append(f"{owner}.{c.group('mname')}")
            call_order.append(f"{owner}.{c.group('mname')}")
        fr = FIELDREF_RE.search(line)
        if fr:
            owner = (fr.group("owner") or fqcn).replace("/", ".")
            key = f"{owner}.{fr.group('name')}"
            if fr.group("kind") in ("getfield", "getstatic"):
                field_reads.add(key)
            else:
                field_writes.add(key)
                if owner.startswith(PKG + ".entity"):
                    entity_writes.add(f"{owner.split('.')[-1]}.{fr.group('name')}")
        ins = INSTR_RE.match(line)
        if ins:
            # 归一化：去掉常量池下标与偏移，保留指令与成员名
            instructions.append(re.sub(r"\s+#\d+", "", ins.group("op")).strip())

    transactional = "org.springframework.transaction.annotation.Transactional" in body
    read_only = bool(re.search(r"Transactional\(\s*readOnly=true", body))
    body_hash = hashlib.sha1("\n".join(instructions).encode()).hexdigest()[:12]

    params = [p.strip() for p in m.group("params").split(",") if p.strip()]
    modifiers = " ".join(w for w in header.strip().split() if w in (
        "public", "private", "protected", "static", "final", "abstract", "synchronized"))
    return {
        "name": method_name,
        "modifiers": modifiers,
        "returns": re.sub(r"^(?:public|private|protected|static|final|abstract)\s+", "",
                           m.group("ret").strip()),
        "params": params,
        "arity": len(params),
        "calls": sorted(set(calls)),
        "calls_internal": sorted({c for c in calls if c.startswith(fqcn + ".")}),
        "calls_internal_ordered": [c for c in call_order if c.startswith(fqcn + ".")],
        "calls_external_ordered": [c for c in call_order if not c.startswith(fqcn + ".")],
        "calls_external": sorted({c for c in calls if not c.startswith(fqcn + ".")}),
        "field_reads": sorted(field_reads),
        "field_writes": sorted(field_writes),
        "entity_field_writes": sorted(entity_writes),
        "transactional": transactional,
        "transactional_read_only": read_only,
        "throws_business_exception": any("BusinessException" in c for c in calls),
        "body_hash": body_hash,
        "body_size": len(instructions),
        "instruction_count": len(instructions),
    }


def analyse_file(fqcn: str) -> dict:
    text = javap_v(fqcn)
    fields, blocks = split_blocks(text)
    # 方法引用（::）是类级的可达性证据
    method_refs = sorted({f"{m.group('owner').replace('/', '.')}.{m.group('mname')}"
                          for m in REF_RE.finditer(text)})
    # 构造器里的字段赋值也要算「写」——依赖注入的 final 字段就是在 <init> 里写的
    ctor_writes: set[str] = set()
    for b in blocks:
        if b.get("name") not in ("<ctor>", "<init>", "<clinit>"):
            continue
        for line in b["lines"]:
            fr = FIELDREF_RE.search(line)
            if fr and fr.group("kind") in ("putfield", "putstatic"):
                owner = (fr.group("owner") or fqcn).replace("/", ".")
                ctor_writes.add(f"{owner}.{fr.group('name')}")
    methods = [analyse_method(fqcn, b) for b in blocks
               if b.get("name") not in ("<ctor>", "<init>", "<clinit>")]
    # record 的访问器是编译器生成的（字段同名、零参），不是人写的复制
    is_record = "extends java.lang.Record" in text
    field_names = {f["name"] for f in fields}
    for m in methods:
        m["record_accessor"] = bool(is_record and m["arity"] == 0 and m["name"] in field_names)
    for m in methods:
        m["synthetic"] = m["name"].startswith("lambda$") or "$deserializeLambda$" in m["name"]

    own = fqcn + "."
    called_from: dict[str, set[str]] = {m["name"]: set() for m in methods}
    for m in methods:
        for call in m["calls_internal"]:
            called_from.setdefault(call.split(".")[-1], set()).add(m["name"])

    private_methods = [m["name"] for m in methods
                       if "private" in m["modifiers"] and not m["synthetic"]]
    ref_names = {c.rsplit(".", 1)[1] for c in method_refs}
    unreachable = sorted(n for n in private_methods
                         if not called_from.get(n) and n not in ref_names)

    field_usage: dict[str, dict] = {}
    for f in fields:
        key = own + f["name"]
        field_usage[f["name"]] = {
            "type": f["type"],
            "mods": f["mods"],
            "written_by": sorted({m["name"] for m in methods if key in m["field_writes"]}
                                  | ({"<init>"} if key in ctor_writes else set())),
            "written_by_ctor": key in ctor_writes,
            "read_by": sorted({m["name"] for m in methods if key in m["field_reads"]}),
        }
    for name, usage in field_usage.items():
        usage["write_only"] = bool(usage["written_by"]) and not usage["read_by"]
        usage["read_only_never_written"] = bool(usage["read_by"]) and not usage["written_by"]
        usage["unused"] = not usage["written_by"] and not usage["read_by"]

    REPO_WRITE = ("save", "saveAll", "delete", "deleteAll", "flush")
    is_service = ".service." in fqcn
    mutating = []
    for m in methods:
        writes_entity = bool(m["entity_field_writes"])
        writes_repo = any(
            c.rsplit(".", 1)[0].endswith("Repository") and c.rsplit(".", 1)[1] in REPO_WRITE
            for c in m["calls"])
        if is_service and (writes_entity or writes_repo):
            mutating.append(m)
    fs_writes = sorted(m["name"] for m in methods
                       if any("java.nio.file.Files" in c for c in m["calls"]))
    resources = sorted({r for r, marks in RESOURCE_MARKERS.items()
                        for m in methods for c in m["calls"]
                        if any(mark in c for mark in marks)})

    return {
        "class": fqcn,
        "is_record": is_record,
        "methods": methods,
        "fields": field_usage,
        "private_methods": private_methods,
        "method_reference_targets": method_refs,
        "unreachable_private_methods": unreachable,
        "resources": resources,
        "write_methods": [m["name"] for m in mutating],
        "write_methods_without_transaction": sorted(
            m["name"] for m in mutating if not m["transactional"]),
        "non_transactional_fs_writes": fs_writes,
    }


def main() -> None:
    global CLASSES
    ap = argparse.ArgumentParser(description="文件层静态声明：javap 直读字节码（一个 .java 文件 = 一个类）")
    ap.add_argument("--classes-dir", type=Path,
                    help="复用已有字节码目录（隐含 --no-compile；绝不编译、绝不删该目录）")
    ap.add_argument("--no-compile", action="store_true",
                    help="不跑 javac/maven，只读已有字节码与已有 test classpath")
    ap.add_argument("--out", type=Path, help="file_declaration.json 输出路径（默认产物目录）")
    args = ap.parse_args()
    if args.classes_dir:
        CLASSES = args.classes_dir
    if args.no_compile or args.classes_dir:
        # ensure_compiled 会 rmtree(CLASSES) 再 javac、ensure_test_deps 会跑 mvn —— 复用模式一个都不碰。
        if not CLASSES.is_dir():
            raise SystemExit(f"字节码目录不存在: {CLASSES}（复用模式不编译，缺产物就是缺输入，不产空声明）")
        deps = DEPS_TEST.read_text().strip() if DEPS_TEST.exists() else ""
        if not deps:
            print(f"  test classpath 未知（{DEPS_TEST} 不存在，复用模式不跑 mvn）—— 不是空 classpath")
    else:
        ensure_compiled()
        deps = ensure_test_deps()
    out = args.out if args.out else project.artifact_dir() / "file_declaration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # file_deps.txt 跟着声明产物走，换目录时两份产物不分散。
    deps_out = out.parent / "file_deps.txt" if args.out else project.artifact_dir() / "file_deps.txt"
    deps_out.write_text(deps)

    classes = []
    for path in sorted(CLASSES.glob(f"{PKG.replace('.', '/')}/**/*.class")):
        if "$" in path.name:
            continue
        classes.append(path.relative_to(CLASSES).with_suffix("").as_posix().replace("/", "."))

    files = []
    for fqcn in classes:
        info = analyse_file(fqcn)
        src = BACKEND / "src/main/java" / Path(*fqcn.split(".")).with_suffix(".java")
        info["file"] = str(src.relative_to(BACKEND)) if src.exists() else None
        files.append(info)

    # 跨文件重复方法体
    ACCESSOR_RE = re.compile(r"^(get|set|is)[A-Z]")
    by_hash: dict[str, list[str]] = {}
    for f in files:
        for m in f["methods"]:
            if m["synthetic"] or m["record_accessor"] or m["instruction_count"] < 4:
                continue
            if ACCESSOR_RE.match(m["name"]) and m["arity"] <= 1:
                continue
            by_hash.setdefault(m["body_hash"], []).append(f"{f['class']}#{m['name']}")
    duplicates = []
    for h, ms in by_hash.items():
        if len(ms) < 2:
            continue
        names = {m.split("#")[1] for m in ms}
        duplicates.append({
            "body_hash": h,
            "members": sorted(ms),
            "copies": len(ms),
            "distinct_files": len({m.split("#")[0] for m in ms}),
            "same_name": len(names) == 1,
            "kind": "同名复制" if len(names) == 1 else "改名复制",
        })
    duplicates.sort(key=lambda d: (-d["distinct_files"], -d["copies"]))

    # 跨文件调用边
    edges: dict[tuple[str, str], int] = {}
    for f in files:
        for m in f["methods"]:
            for call in m["calls_external"]:
                owner = call.rsplit(".", 1)[0]
                edges[(f["class"], owner)] = edges.get((f["class"], owner), 0) + 1

    payload = {
        "meta": {
            "backend_repo": str(BACKEND),
            "files": len(files),
            "methods": sum(len(f["methods"]) for f in files),
            "note": "文件级事实来自 javap -v 字节码：注解/字段引用/调用图无解析歧义",
        },
        "files": files,
        "duplicate_method_groups": sorted(duplicates, key=lambda d: -d["copies"]),
        "cross_file_edges": sorted(
            [{"from": a, "to": b, "calls": c} for (a, b), c in edges.items()],
            key=lambda e: -e["calls"]),
    }
    # 产物跟着目标走：默认目标��� artifacts_dir 就是本目录，行为不变；
    # 换目标时落到目标自己的 artifacts 目录，不会覆盖别人的产物。
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print(f"文件层静态 -> {out}")
    print(f"  文件 {len(files)} 个 / 方法 {payload['meta']['methods']} 个")
    for d in payload["duplicate_method_groups"]:
        print(f"  重复方法体 x{d['copies']}（{d['kind']}，跨 {d['distinct_files']} 个文件）: "
              f"{' , '.join(d['members'])}")
    for f in files:
        if f["unreachable_private_methods"]:
            print(f"  !! {f['file']} 无人调用的私有方法: {f['unreachable_private_methods']}")
    print("  写方法缺事务边界: " + "; ".join(
        f"{f['file']}:{f['write_methods_without_transaction']}"
        for f in files if f["write_methods_without_transaction"]))


if __name__ == "__main__":
    main()
