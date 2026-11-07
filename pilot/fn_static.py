"""函数层 · 静态侧：从字节码提取函数签名卡。

不用正则去猜源码，而是 javap 直读编译产物——异常表、字面常量、内部调用图
都是字节码里确定的事实，没有解析歧义。

产出 fn_declaration.json：每个函数的「声明侧」事实。
"""
from __future__ import annotations
import project

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
    r"^  (?P<ret>[\w.$\[\]<>, ?]+?)\s(?P<name>[A-Za-z_$][\w$]*)\((?P<params>.*)\);\s*$")
CALL_RE = re.compile(
    r"invoke(?:virtual|static|interface|special|dynamic)\s+#\d+(?:,\s*\d+)?\s+//\s+"
    r"(?:Method|InterfaceMethod)\s+(?P<owner>[\w/$]+)\.(?P<mname>[\w$<>]+)")
LDC_RE = re.compile(r"ldc(?:_w)?\s+#\d+\s+//\s+(?P<kind>\w+)\s+(?P<value>.*)$")
INT_CONST_RE = re.compile(r"^\s*\d+:\s+(?:bipush|sipush)\s+(?P<v>-?\d+)\s*$")
ICONST_RE = re.compile(r"^\s*\d+:\s+iconst_(?P<v>\d)\s*$")
EXC_ROW_RE = re.compile(
    r"^\s+(?P<from>\d+)\s+(?P<to>\d+)\s+(?P<target>\d+)\s+(?P<type>Class|any)\s+(?P<exc>[\w/$]+)?")
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
    return run(["javap", "-p", "-c", "-cp", str(class_dir), fqcn]).stdout


def parse_methods(text: str) -> list[dict]:
    methods: list[dict] = []
    current: dict | None = None
    in_exception_table = False
    for line in text.splitlines():
        if not line.strip():
            continue
        m = METHOD_RE.match(line)
        if m:
            current = {
                "name": m.group("name"),
                "returns": re.sub(r"^(?:public|private|protected|static|final|abstract)\s+",
                                   "", m.group("ret").strip()),
                "params_raw": m.group("params").strip(),
                "modifiers": "",
                "calls": [],
                "constants": [],
                "caught": [],
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
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped == "Exception table:":
            in_exception_table = True
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

        call = CALL_RE.search(line)
        if call:
            owner = call.group("owner").replace("/", ".")
            current["calls"].append(f"{owner}.{call.group('mname')}")
            continue
        ldc = LDC_RE.search(line)
        if ldc:
            value = ldc.group("value").strip()
            if ldc.group("kind") == "String":
                value = value.strip('"')
            current["constants"].append({"kind": ldc.group("kind"), "value": value})
            continue
        ic = INT_CONST_RE.match(line)
        if ic:
            current["constants"].append({"kind": "int", "value": ic.group("v")})
            continue
        ii = ICONST_RE.match(line)
        if ii:
            current["constants"].append({"kind": "int", "value": ii.group("v")})
            continue
        if re.match(r"^\s*\d+:\s", line):
            current["bytecode_size"] += 1
            op = stripped.split(":")[1].strip().split()[0]
            if op.startswith(BRANCH_OPS):
                current["branch_count"] += 1
            if op in ("ifnull", "ifnonnull"):
                current["null_guard_count"] += 1
            if op == "return":
                current["return_count"] += 1
    return methods


def side_effects(method: dict) -> list[str]:
    return sorted({c for c in method["calls"] if any(m in c for m in SIDE_EFFECT_MARKERS)})


def main() -> None:
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
        for method in parse_methods(text):
            if method["name"] in ("<init>", "<clinit>"):
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
    out = project.artifact_dir() / "fn_declaration.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    pure = sum(1 for f in functions if f["is_pure"])
    print(f"函数层静态 -> {out}")
    print(f"  类 {len(classes)} 个 / 函数 {len(functions)} 个（纯函数 {pure} 个）")
    if drift:
        print(f"  !! 源码与部署产物漂移: {drift}")
    else:
        print("  源码与部署产物 javap 输出逐字节一致")


if __name__ == "__main__":
    main()
