"""函数层 · 静态侧：从字节码提取函数签名卡。

不用正则去猜源码，而是 javap 直读编译产物——异常表、字面常量、内部调用图
都是字节码里确定的事实，没有解析歧义。

产出 fn_declaration.json：每个函数的「声明侧」事实。
"""
from __future__ import annotations
import project

import argparse
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path


BACKEND = project.backend_root()
WORK = project.work_dir()
CLASSES = WORK / "classes"
DEPLOYED = WORK / "deployed_classes"
DEPS_FILE = WORK / "deps.txt"
JAR_COPY = WORK / "app.jar"

PKG = project.java_package()

METHOD_RE = re.compile(
    r"^  (?P<ret>[\w.$\[\]<>, ?]+?)\s(?P<name>[A-Za-z_$][\w$]*)\((?P<params>.*)\)"
    r"(?:\s+throws\s+[\w.$, ]+)?;\s*$")
CALL_RE = re.compile(
    r"invoke(?:virtual|static|interface|special|dynamic)\s+#\d+(?:,\s*\d+)?\s+//\s+"
    r"(?:Method|InterfaceMethod)\s+(?:(?P<owner>[\w/$]+)\.)?(?P<mname>[\w$<>]+)(?=[:(])")
LDC_RE = re.compile(r"ldc(?:_w)?\s+#\d+\s+//\s+(?P<kind>\w+)\s+(?P<value>.*)$")
INT_CONST_RE = re.compile(r"^\s*\d+:\s+(?:bipush|sipush)\s+(?P<v>-?\d+)\s*$")
ICONST_RE = re.compile(r"^\s*\d+:\s+iconst_(?P<v>\d)\s*$")
EXC_ROW_RE = re.compile(
    r"^\s+(?P<from>\d+)\s+(?P<to>\d+)\s+(?P<target>\d+)\s+(?P<type>Class|any)\s+(?P<exc>[\w/$]+)?")
MH_RE = re.compile(
    r"^\s*#(?P<idx>\d+) = MethodHandle\s+\d+:#\d+\s+//\s+"
    r"(?P<kind>REF_\w+)\s+(?P<owner>[\w/$]+)\.(?P<mname>[\w$<>]+):")
INDY_BOOT_RE = re.compile(r"//\s+InvokeDynamic\s+#(\d+):")
BOOT_ENTRY_RE = re.compile(r"^\s{2}(\d+):\s+#\d+\s+REF_")
BOOT_ARG_RE = re.compile(
    r"^\s+#(?P<idx>\d+)\s+(?P<kind>REF_\w+)\s+(?P<owner>[\w/$]+)\.(?P<mname>[\w$<>]+):")
ANNOT_TYPE_RE = re.compile(r"^\s+(?P<type>[\w$.]+)\($")
ANNOT_MARKER_RE = re.compile(r"^\s+(?P<type>[\w$.]+)$")
ATTR_HEADER_RE = re.compile(r"^\s*[A-Z][A-Za-z]+:")
BRANCH_OPS = ("if_icmp", "if_acmp", "ifnull", "ifnonnull", "goto", "tableswitch",
              "lookupswitch", "ifeq", "ifne", "iflt", "ifgt", "ifle", "ifge")

# 依赖外部世界的调用 -> 该函数不是纯函数，动态层必须打桩
SIDE_EFFECT_MARKERS = ("Repository", "Mapper", "Files", "File", "RestTemplate",
                       "EntityManager", "JdbcTemplate", "Client", "InputStream")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=cwd)


def ensure_compiled() -> None:
    """从源码新鲜编译一份 class，并用 maven 依赖拼出运行时 classpath。"""
    WORK.mkdir(parents=True, exist_ok=True)
    if not DEPS_FILE.exists():
        run(["mvn", "-o", "-q", "dependency:build-classpath",
             "-Dmdep.outputFile=" + str(DEPS_FILE), "-DincludeScope=runtime"], cwd=BACKEND)
    srcs = sorted(BACKEND.glob("src/main/java/**/*.java"))
    listing = WORK / "sources.txt"
    listing.write_text("\n".join(str(p) for p in srcs))
    shutil.rmtree(CLASSES, ignore_errors=True)
    CLASSES.mkdir(parents=True, exist_ok=True)
    run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(CLASSES),
         "-cp", DEPS_FILE.read_text().strip(), "@" + str(listing)], cwd=BACKEND)


def ensure_deployed_classes() -> bool:
    """取部署产物（容器里跑的 app.jar）的 class，比对源码与线上是否漂移。"""
    if not JAR_COPY.exists():
        rc = subprocess.run(["docker", "cp", f"{project.app_container()}:/app/app.jar", str(JAR_COPY)],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            return False
    shutil.rmtree(DEPLOYED, ignore_errors=True)
    with zipfile.ZipFile(JAR_COPY) as z:
        for name in z.namelist():
            if name.startswith("BOOT-INF/classes/") and name.endswith(".class"):
                target = DEPLOYED / name[len("BOOT-INF/classes/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    return True


def javap(class_dir: Path, fqcn: str) -> str:
    return run(["javap", "-v", "-p", "-cp", str(class_dir), fqcn]).stdout


def parse_methods(text: str, fqcn: str = "") -> list[dict]:
    methods: list[dict] = []
    current: dict | None = None
    in_exception_table = False
    in_code = False
    in_annotations = False
    annot_type = ""
    annot_lines: list[str] = []
    in_bootstrap = False
    boot_idx = -1
    handles: dict[str, dict] = {}
    boot_args: dict[int, list[dict]] = {}
    prev_op = ""

    def _flush_annotation() -> None:
        nonlocal annot_type, annot_lines
        if current is not None and annot_type:
            current["annotations"].append(
                {"type": annot_type, "raw": "\n".join(annot_lines).strip()})
        annot_type = ""
        annot_lines = []

    for line in text.splitlines():
        if not line.strip():
            continue
        mh = MH_RE.match(line)
        if mh:
            handles[mh.group("idx")] = {
                "kind": mh.group("kind"),
                "owner": mh.group("owner").replace("/", "."),
                "method": mh.group("mname"),
            }
            continue
        if line.startswith("BootstrapMethods:"):
            in_bootstrap = True
            continue
        if in_bootstrap:
            be = BOOT_ENTRY_RE.match(line)
            if be:
                boot_idx = int(be.group(1))
                boot_args.setdefault(boot_idx, [])
                continue
            ba = BOOT_ARG_RE.match(line)
            if ba and boot_idx >= 0:
                boot_args[boot_idx].append({
                    "cp": ba.group("idx"), "kind": ba.group("kind"),
                    "owner": ba.group("owner").replace("/", "."),
                    "method": ba.group("mname"),
                })
            continue
        m = METHOD_RE.match(line)
        if m:
            _flush_annotation()
            current = {
                "name": m.group("name"),
                "returns": re.sub(r"^(?:public|private|protected|static|final|abstract)\s+",
                                   "", m.group("ret").strip()),
                "params_raw": m.group("params").strip(),
                "modifiers": "",
                "calls": [],
                "constants": [],
                "caught": [],
                "annotations": [],
                "indy_boot": [],
                "branch_count": 0,
                "return_count": 0,
                "bytecode_size": 0,
                "null_guard_count": 0,
            }
            head = line.strip().split()
            mods = ("public", "private", "protected", "static", "final",
                    "synchronized", "abstract", "native")
            current["modifiers"] = " ".join(w for w in head if w in mods)
            methods.append(current)
            in_exception_table = False
            in_code = False
            in_annotations = False
            prev_op = ""
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped == "Exception table:":
            in_exception_table = True
            in_code = False
            continue
        if in_exception_table:
            if stripped.startswith("from"):
                continue
            row = EXC_ROW_RE.match(line)
            if row:
                current["caught"].append({
                    "type": (row.group("exc") or row.group("type")).replace("/", "."),
                    "from": int(row.group("from")),
                    "to": int(row.group("to")),
                    "target": int(row.group("target")),
                })
                continue
            in_exception_table = False
        if stripped in ("RuntimeVisibleAnnotations:", "RuntimeInvisibleAnnotations:"):
            in_annotations = True
            in_code = False
            continue
        if in_annotations:
            if re.match(r"^\s*\d+:\s+#", line):
                continue
            at = ANNOT_TYPE_RE.match(line)
            if at:
                _flush_annotation()
                annot_type = at.group("type")
                continue
            if annot_type:
                if stripped == ")":
                    _flush_annotation()
                else:
                    annot_lines.append(stripped)
                continue
            am = ANNOT_MARKER_RE.match(line)
            if am:
                _flush_annotation()
                annot_type = am.group("type")
                _flush_annotation()
                continue
            in_annotations = False
        if stripped == "Code:":
            in_code = True
            prev_op = ""
            continue
        if ATTR_HEADER_RE.match(line):
            in_code = False
            continue
        if not in_code:
            continue

        if "invokedynamic" in line:
            bm = INDY_BOOT_RE.search(line)
            if bm:
                current["indy_boot"].append(
                    {"boot": int(bm.group(1)), "prev_op": prev_op})
        call = CALL_RE.search(line)
        if call:
            # javap 对同类 invoke 省略 owner —— 缺 owner 即本类调用，
            # 归到当前类；丢掉它等于整张图没有自调用边。
            owner = (call.group("owner") or fqcn.replace(".", "/")).replace("/", ".")
            current["calls"].append(f"{owner}.{call.group('mname')}")
        else:
            ldc = LDC_RE.search(line)
            if ldc:
                value = ldc.group("value").strip()
                if ldc.group("kind") == "String":
                    value = value.strip('"')
                current["constants"].append({"kind": ldc.group("kind"), "value": value})
            else:
                ic = INT_CONST_RE.match(line)
                if ic:
                    current["constants"].append({"kind": "int", "value": ic.group("v")})
                else:
                    ii = ICONST_RE.match(line)
                    if ii:
                        current["constants"].append({"kind": "int", "value": ii.group("v")})
        if re.match(r"^\s*\d+:\s", line):
            current["bytecode_size"] += 1
            op = stripped.split(":")[1].strip().split()[0]
            if op.startswith(BRANCH_OPS):
                current["branch_count"] += 1
            if op in ("ifnull", "ifnonnull"):
                current["null_guard_count"] += 1
            if op == "return":
                current["return_count"] += 1
            prev_op = stripped.split(":")[1].strip()
    _flush_annotation()

    # 方法引用归因：invokedynamic -> BootstrapMethods 序号 -> 常量池 MethodHandle。
    # 接收者取 invokedynamic 前一条指令：aload_0=this，getfield=字段，其余=static/unknown。
    for meth in methods:
        refs = []
        for ib in meth["indy_boot"]:
            for h in boot_args.get(ib["boot"], []):
                if not h["kind"].startswith("REF_invoke"):
                    continue
                prev = ib["prev_op"]
                if prev == "aload_0":
                    receiver = "this"
                elif prev.startswith("getfield"):
                    fm = re.search(r"//\s+Field\s+([\w/$]+)\.([\w$]+):", prev)
                    receiver = f"field:{fm.group(2)}" if fm else "unknown"
                elif prev.startswith("getstatic"):
                    receiver = "unknown"
                else:
                    receiver = "static"
                refs.append({"target": f"{h['owner']}.{h['method']}",
                             "kind": h["kind"], "cp": f"#{h['cp']}",
                             "receiver": receiver})
        meth["method_reference_calls"] = refs
    return methods


def side_effects(method: dict) -> list[str]:
    return sorted({c for c in method["calls"] if any(m in c for m in SIDE_EFFECT_MARKERS)})


def main() -> None:
    global CLASSES
    ap = argparse.ArgumentParser(description="函数层静态声明：javap 直读字节码（异常表/常量/调用图无解析歧义）")
    ap.add_argument("--classes-dir", type=Path,
                    help="复用已有字节码目录（隐含 --no-compile；绝不编译、绝不删该目录）")
    ap.add_argument("--no-compile", action="store_true",
                    help="不跑 javac/maven、不 docker cp 部署产物，只读已有字节码")
    ap.add_argument("--out", type=Path, help="输出路径（默认 <产物目录>/fn_declaration.json）")
    args = ap.parse_args()
    if args.classes_dir:
        CLASSES = args.classes_dir
    if args.no_compile or args.classes_dir:
        # ensure_compiled 会 rmtree(CLASSES) 再 javac —— 指向被测仓库的字节码时那是破坏性操作。
        if not CLASSES.is_dir():
            raise SystemExit(f"字节码目录不存在: {CLASSES}（复用模式不编译，缺产物就是缺输入，不产空声明）")
        deployed_ok = False
    else:
        ensure_compiled()
        deployed_ok = ensure_deployed_classes()

    classes = []
    for path in sorted(CLASSES.glob(f"{PKG.replace('.', '/')}/**/*.class")):
        if "$" in path.name:
            continue
        classes.append(path.relative_to(CLASSES).with_suffix("").as_posix().replace("/", "."))

    functions: list[dict] = []
    drift: list[str] = []
    for fqcn in classes:
        text = javap(CLASSES, fqcn)
        if deployed_ok:
            try:
                deployed_text = javap(DEPLOYED, fqcn)
            except subprocess.CalledProcessError:
                deployed_text = None
            if deployed_text is not None and deployed_text != text:
                drift.append(fqcn)
        for method in parse_methods(text, fqcn):
            # $values 是枚举的编译器合成方法，源码里不存在 ——
            # 放进声明会让图里长出无源码证据的假单元/假边。
            if method["name"] in ("<init>", "<clinit>", "$values"):
                continue
            params = [p.strip() for p in method["params_raw"].split(",") if p.strip()]
            effects = side_effects(method)
            functions.append({
                "class": fqcn,
                "method": method["name"],
                "modifiers": method["modifiers"],
                "returns": method["returns"],
                "params": params,
                "arity": len(params),
                "caught_exceptions": method["caught"],
                "annotations": method["annotations"],
                "method_reference_calls": method["method_reference_calls"],
                "internal_calls": sorted({c for c in method["calls"] if c.startswith(PKG)}),
                "external_calls": sorted({c for c in method["calls"] if not c.startswith(PKG)}),
                "side_effects": effects,
                "is_pure": not effects,
                "constants": method["constants"],
                "branch_count": method["branch_count"],
                "return_count": method["return_count"],
                "bytecode_size": method["bytecode_size"],
                "null_guard_count": method["null_guard_count"],
                "throws_business_exception": any("BusinessException" in c for c in method["calls"]),
            })

    payload = {
        "meta": {
            "backend_repo": str(BACKEND),
            "compiled_classes": len(classes),
            "functions": len(functions),
            "deployed_artifact_compared": deployed_ok,
            "source_vs_deployed_drift": drift,
            "note": "声明侧事实来自 javap 字节码：异常表/常量/调用图无解析歧义",
        },
        "functions": functions,
    }
    out = args.out if args.out else project.artifact_dir() / "fn_declaration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    pure = sum(1 for f in functions if f["is_pure"])
    print(f"函数层静态 -> {out}")
    print(f"  类 {len(classes)} 个 / 函数 {len(functions)} 个（纯函数 {pure} 个）")
    if not deployed_ok:
        print("  部署产物未比对（复用字节码 / 取不到部署 jar）—— 漂移未知，不是无漂移")
    elif drift:
        print(f"  !! 源码与部署产物漂移: {drift}")
    else:
        print("  源码与部署产物 javap 输出逐字节一致")


if __name__ == "__main__":
    main()
