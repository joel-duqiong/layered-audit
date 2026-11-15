"""成员声明生产者 —— 把已有声明产物转录成 arch_rules 的规范 members 容器。

只读已有产物：不编译、不跑 javap、不碰被测仓库。存在的理由是**形状**而不是覆盖率：
零配置入口的默认 member_source 是 {records: "members"}，而上游声明产物的容器叫
functions / nodes，直接喂会被消费者的 select_source 拒掉。本生产者只做转录。

输入形状复用消费者自己的读取器（select_source / extract_records / member_row_ok），
不另立一套合格判据 —— 否则同一封闭集活在两处，改一处就静默漂移。

两条诚实性纪律：
- 缺 modifiers 的行跳过并计数，绝不补空串。空串在消费者那边是 package_private 的
  合法事实（javap 对包私有成员不打印修饰符），把「上游没给」补成空串等于伪造判定。
- 多载体同键修饰符冲突时不挑赢家：全部转录，由消费者合并成不可判定（unknown）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import arch_rules

SCHEMA = "member-declaration/1"
# 词汇表从消费者自己的默认形状派生，不在这里写第二份字面量（坑 11：封闭集不许活在两处）。
FIELDS = tuple(field for field in arch_rules.DEFAULT_MEMBER_SOURCE if field != "records")


def skip_reason(row: Any, spec: dict[str, str]) -> str:
    """只用于给被拒记录归因，合格与否仍由消费者的 member_row_ok 判。"""
    if not isinstance(row, dict):
        return "not_an_object"
    for field in ("class", "method"):
        value = row.get(spec[field])
        if not isinstance(value, str) or not value:
            return f"{field}_missing"
    if not isinstance(row.get(spec["modifiers"]), str):
        return "modifiers_missing"
    return "not_member_shaped"


def transcribe(data: Any, spec: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
    chosen = arch_rules.select_source(data, spec, "members")
    rows = arch_rules.extract_records(data, chosen, "members")
    members: list[dict[str, str]] = []
    skipped: dict[str, int] = {}
    empty: list[dict[str, str]] = []
    for row in rows:
        if not arch_rules.member_row_ok(row, chosen):
            reason = skip_reason(row, chosen)
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        record = {field: row[chosen[field]] for field in FIELDS}
        members.append(record)
        if not record["modifiers"].strip():
            empty.append({"class": record["class"], "method": record["method"]})
    return members, {"container": chosen["records"], "records_seen": len(rows),
                     "records_emitted": len(members), "records_skipped": sum(skipped.values()),
                     "skipped_reasons": dict(sorted(skipped.items())), "empty_modifiers": empty}


def build(inputs: list[tuple[str, Path]], spec: Any) -> dict[str, Any]:
    members: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []
    for label, path in inputs:
        rows, stats = transcribe(json.loads(path.read_text(encoding="utf-8")), spec)
        members.extend(rows)
        sources.append({"label": label, "path": str(path.resolve()),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **stats})
    by_key: dict[tuple[str, str], set[str]] = {}
    for row in members:
        by_key.setdefault((row["class"], row["method"]), set()).add(row["modifiers"])
    conflicts = [{"class": cls, "method": method, "modifiers": sorted(values)}
                 for (cls, method), values in sorted(by_key.items()) if len(values) > 1]
    return {
        "schema": SCHEMA,
        "members": members,
        "sources": sources,
        "coverage": {
            "records_emitted": len(members), "distinct_keys": len(by_key),
            "duplicate_records": len(members) - len(by_key),
            "conflicting_keys": len(conflicts), "conflicts": conflicts,
            "empty_modifiers": sum(len(s["empty_modifiers"]) for s in sources),
            "sources_without_members": [s["label"] for s in sources if not s["records_emitted"]],
        },
        "notes": [
            "members 是转录，不是新测量：每条修饰符都来自 sources 里声明的已有产物，sha256 可核",
            "本产物容器是规范 members 形状，供默认 member_source 的消费者（含零配置入口）使用；"
            "若消费者的 member_source 候选列表只声明 functions/nodes，请直接喂上游原产物，喂本文件会被 select_source 拒掉",
            "modifiers 为空串是 javap 对包私有成员的真实输出，消费者读作 package_private（纯空白同样读作 package_private）；"
            "empty_modifiers 列出这些键供复核，缺 modifiers 字段的行不在此列（已跳过并计入 skipped_reasons）",
            "同键重载全部保留，修饰符冲突不挑赢家：消费者合并成 None 后判 unknown",
            "conflicting_keys>0 或 sources_without_members 非空时，本产物的判定力低于完整输入，不得当成等价口径",
        ],
    }


def parse_input(raw: str) -> tuple[str, Path]:
    label, sep, path = raw.partition("=")
    if not sep or not label.strip() or not path.strip():
        raise ValueError(f"--input 必须是 LABEL=PATH，收到 {raw!r}")
    return label.strip(), Path(path.strip())


def clobbers(out: Path, inputs: list[Path]) -> bool:
    """按**文件身份**比，不只按路径字符串。

    resolve() 只覆盖字符串等价类（相对/绝对、符号链接）；硬链接是同一 inode 不同路径，
    resolve() 看不出来，写下去就是毁掉上游证据。out 尚不存在时只能比规范化路径。
    """
    resolved = out.resolve()
    for path in inputs:
        if resolved == path.resolve():
            return True
        if out.exists() and path.exists() and os.path.samefile(out, path):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe existing declaration artifacts into a members container")
    parser.add_argument("--input", action="append", required=True, metavar="LABEL=PATH",
                        help="已有声明产物，可重复；LABEL 用于出处归因")
    parser.add_argument("--rules", type=Path,
                        help="只读其 member_source 段确定输入形状（可为候选列表）；缺省用规范 members 形状")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    inputs = [parse_input(raw) for raw in args.input]
    protected = [path for _, path in inputs] + ([args.rules] if args.rules else [])
    if clobbers(args.out, protected):
        parser.error("--out 不得覆盖任何输入产物（含 --rules；按文件身份判，符号链接与硬链接同样拒绝）")
    raw_config = json.loads(args.rules.read_text(encoding="utf-8")) if args.rules else {}
    spec = arch_rules.validate_record_source(raw_config.get("member_source"), FIELDS,
                                             arch_rules.DEFAULT_MEMBER_SOURCE, "member")
    report = build(inputs, spec)
    if not report["members"]:
        detail = "；".join(
            f"{s['label']}: " + ("容器为空" if not s["records_seen"] else
                                f"容器 {s['container']} 有 {s['records_seen']} 条记录但全不合格"
                                f"（跳过归因 {s['skipped_reasons']}）")
            for s in report["sources"])
        print(f"成员声明生产失败：没有任何合格记录，拒绝写出空容器。{detail}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["coverage"], ensure_ascii=False))
    for source in report["sources"]:
        print(f"  {source['label']} 容器={source['container']} 见={source['records_seen']} "
              f"转录={source['records_emitted']} 跳过={source['records_skipped']} "
              f"空修饰符={len(source['empty_modifiers'])} {source['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())