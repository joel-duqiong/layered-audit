"""引擎纯净度检查 —— 防止通用引擎被写回项目专属的东西。

engine/ 里的三个脚本换项目时必须零改动。任何项目专属字面量（被测项目名、
绝对路径、容器/数据库调用、内网地址）混进去，就说明「通用」只是文档里的一句话。

退出码：0 = 干净；1 = 发现项目耦合。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import findings_schema

ENGINE_DIR = Path(__file__).resolve().parent

# 硬编码即违规的形态：项目专属字面量
FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("绝对路径", r"/Users/|/home/|C:\\\\"),
    ("容器/数据库调用", r"docker\s+exec|\bpsql\s+-[a-zA-Z]"),
    ("内网地址", r"127\.0\.0\.1|localhost:\d"),
    ("HTTP 客户端", r"\brequests\.|\burllib\b"),
    ("SQL 字面量", r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET)\b"),
    # 检测器必须从配置取名字，不许在引擎里写死项目脚本
    ("写死的脚本调用", r"""['"]python3?\s+[\w./-]+\.py"""),
)


def main() -> None:
    target = findings_schema.load_target()
    name = str(target.get("target", "")).strip()
    findings: list[str] = []

    for path in sorted(ENGINE_DIR.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN:
            for m in re.finditer(pattern, text):
                line = text[:m.start()].count("\n") + 1
                findings.append(f"{path.name}:{line}  {label}: {m.group(0)!r}")
        if name:
            for m in re.finditer(re.escape(name), text, re.IGNORECASE):
                line = text[:m.start()].count("\n") + 1
                findings.append(f"{path.name}:{line}  项目名 {name!r} 出现在通用引擎里")

    if findings:
        print(f"引擎纯净度检查失败：{len(findings)} 处项目耦合")
        for f in findings:
            print(f"  {f}")
        raise SystemExit(1)
    files = sorted(p.name for p in ENGINE_DIR.glob("*.py") if p.name != Path(__file__).name)
    print(f"引擎纯净度检查通过：{len(files)} 个文件零项目耦合（{', '.join(files)}）")
    print("  允许：按 target.json 的配置调用项目侧检测器（subprocess 本身不算耦合）")


if __name__ == "__main__":
    main()
