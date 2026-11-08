"""文件层通用代码纯净度检查 —— 文件层的「引擎」换项目时必须零改动。

文件层的通用骨架（通用 harness / 容器探针 / 静态抽取）散在 pilot/ 下，
不在 engine/ 里，所以 engine/check_engine_purity.py 扫不到它们；
类层有 check_class_probe_purity.py，文件层此前**一个守卫都没有** ——
通用文件层里写个 com.demo 不会被 CI 拦。这个文件补上这一段。

检查器自己也不写死任何项目名：词全部来自 target.json 与项目自己的源码。

退出码：0 = 干净；1 = 发现项目耦合。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import project

HERE = Path(__file__).resolve().parent

# 文件层的通用骨架：这些文件换项目时必须零改动
GENERIC_FILES = (
    "file_static.py",
    "file_dynamic.py",
    "file_http_probe.py",
    "file_harness/GenericFileHarness.java",
    "file_harness/GenericFileHarnessBase.java",
    "file_harness/GenericFileHarnessObserve.java",
    "file_harness/GenericFileHarnessValues.java",
    "file_harness/GenericFileHarnessDetectors.java",
    "file_harness/ClassConstPool.java",
)

# 形态类禁词：与具体项目无关，但同样是「写死」的痕迹
FORMS: tuple[tuple[str, str], ...] = (
    ("绝对路径", r"/Users/|/home/"),
    ("内网地址", r"127\.0\.0\.1|localhost:\d"),
    ("HTTP 路径字面量", r"/api/"),
)

# Java 通用词汇：出现在任何项目里，不算项目耦合
STOPWORDS = {
    "Entity", "Service", "Controller", "Repository", "Component", "Configuration",
    "Application", "Exception", "Config", "Util", "Utils", "Common", "Base",
    "List", "Map", "Set", "Optional", "String", "Integer", "Long", "Double",
    "Boolean", "Object", "Class", "Override", "Nullable", "NotNull", "NotBlank",
    "Min", "Max", "Size", "Pattern", "Valid", "Request", "Response", "Data",
    "Builder", "Getter", "Setter", "NoArgsConstructor", "AllArgsConstructor",
    "JsonNode", "Duration", "Instant", "LocalDate", "LocalDateTime", "UUID",
}


def project_tokens() -> list[tuple[str, str]]:
    """抽出「这个项目特有的词」——来自绑定与项目源码，不是硬编码。"""
    tokens: list[tuple[str, str]] = []
    binding = project.binding()
    paths = binding.get("paths", {})
    name = str(binding.get("target", "")).strip()
    if name:
        tokens.append(("项目名", name))
    for key in ("backend", "app_container", "base_url"):
        value = paths.get(key)
        if value:
            tokens.append((f"绑定项 {key}", str(value)))
    db = paths.get("db") or {}
    for key in ("container", "user", "database"):
        if db.get(key):
            tokens.append((f"绑定项 db.{key}", str(db[key])))
    pkg = paths.get("java_package")
    if pkg:
        tokens.append(("Java 包前缀", str(pkg)))
        tokens.append(("Java 包前缀", str(pkg).replace("/", ".")))

    mig = project.migration_dir()
    if mig.exists():
        for sql in sorted(mig.glob("*.sql")):
            text = sql.read_text(encoding="utf-8")
            for m in re.finditer(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", text, re.I):
                tokens.append(("表名", m.group(1)))

    for path in _domain_sources():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:class|record|interface|enum)\s+(\w+)", text):
            name_ = m.group(1)
            if name_ in STOPWORDS or len(name_) < 4:
                continue
            tokens.append(("类名", name_))
    return tokens


def _domain_sources() -> list[Path]:
    """实体目录 + DTO 目录里的源文件。"""
    out: list[Path] = []
    entity = project.entity_dir()
    if entity.exists():
        out.extend(sorted(entity.glob("*.java")))
    pkg_dir = project.backend_package_dir()
    if pkg_dir.exists():
        for path in sorted(pkg_dir.rglob("*.java")):
            if "dto" in path.parts:
                out.append(path)
    return out


def main() -> None:
    tokens = project_tokens()
    findings: list[str] = []
    scanned: list[str] = []

    for rel in GENERIC_FILES:
        path = HERE / rel
        if not path.exists():
            findings.append(f"{rel}: 文件不存在")
            continue
        scanned.append(rel)
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORMS:
            for m in re.finditer(pattern, text):
                line = text[:m.start()].count("\n") + 1
                findings.append(f"{rel}:{line}  {label}: {m.group(0)!r}")
        for label, token in tokens:
            if not token or len(token) < 4:
                continue
            for m in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(token)
                                 + r"(?![A-Za-z0-9_])", text):
                line = text[:m.start()].count("\n") + 1
                findings.append(f"{rel}:{line}  {label} {token!r} 出现在通用层里")

    if findings:
        print(f"文件层通用代码纯净度检查失败：{len(findings)} 处项目耦合")
        for f in findings:
            print(f"  {f}")
        raise SystemExit(1)
    print(f"文件层通用代码纯净度检查通过：{len(scanned)} 个文件零项目耦合"
          f"（{'、'.join(scanned)}）")
    print(f"  已比对的项目特有词 {len(tokens)} 个"
          f"（项目名 / 绑定项 / 表名 / 实体与 DTO 类名）")


if __name__ == "__main__":
    main()
