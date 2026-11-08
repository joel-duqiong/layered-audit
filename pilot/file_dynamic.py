"""文件层 · 动态侧：通用驱动（配置驱动）。

**通用骨架**（零项目字面量）：
  ``file_harness/GenericFileHarness*.java`` —— JVM 内整类实例化 + 依赖图 + 打桩 + 日志捕获
  ``file_http_probe.py``                     —— 真实容器探针（HTTP / DB / 裸 socket）

**项目声明**（换项目只换这个文件）：
  ``<project>/file_cases.json`` —— 探哪些类、怎么构造、怎么打桩、期望看到什么

文件层的价值在「同文件内多方法的配合」，所以用例文件里每个场景都是一串步骤
（构造 → 调 → 看中间态），而不是单方法断言。

输出 ``file_observation.json`` 的结构与旧版逐字一致 —— ``file_diff.py`` 的判定逻辑
一行不用改，所以改造前后的结论可比。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import project

PILOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PILOT))

import file_http_probe  # noqa: E402
import file_static  # noqa: E402  只借用「编译项目 + test 作用域依赖」两件事

WORK = project.work_dir() / "file"
HARNESS_CLASSES = WORK / "harness"
UPLOAD_DIR = WORK / "uploads"

HARNESS_META = "JVM 内整类实例化 + 仓库打桩 + 日志捕获；容器探针走真实 HTTP"


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=cwd)


# --------------------------------------------------------------------- 定位与编译

def cases_path(arg: str) -> Path:
    """用例文件：命令行优先，其次项目绑定 paths.file_cases，最后同名默认。"""
    if arg:
        p = Path(arg)
    else:
        p = Path(project.path("file_cases", "file_cases.json"))
    return p if p.is_absolute() else project.project_dir() / p


def build_classpath() -> list[str]:
    """编译项目 + 通用 harness，产出运行 classpath。

    依赖用 test 作用域：探针要 spring-test 的 Mock 请求/响应与 logback 的 ListAppender。
    """
    file_static.ensure_compiled()
    deps = file_static.ensure_test_deps()
    HARNESS_CLASSES.mkdir(parents=True, exist_ok=True)
    srcs = sorted(str(p) for p in (PILOT / "file_harness").glob("*.java"))
    run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(HARNESS_CLASSES),
         "-cp", deps, *srcs])
    return [str(HARNESS_CLASSES), str(file_static.CLASSES), deps]


def run_harness(cfg_path: Path, classpath: list[str]) -> dict[str, Any]:
    """跑通用 harness，拿回每个场景的观测与检查器结果。"""
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / "file_observation_raw.json"
    proc = subprocess.run(
        ["java", "-cp", os.pathsep.join(classpath), "GenericFileHarness",
         str(cfg_path), str(out), str(WORK), str(file_static.CLASSES)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"harness failed: {proc.returncode}")
    return json.loads(out.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- 归一化

def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """把 harness 的原始观测整理成稳定形状。

    两件事：
      · error_type 去掉包名（异常类全名对判定没用，简单名才可比）
      · logs / call_counts 不进 raw（原始量单独落 side 文件，观测层只留判定依据）
    """
    out = {k: v for k, v in raw.items() if k not in ("logs", "call_counts")}
    et = out.get("error_type")
    if isinstance(et, str) and "." in et:
        out["error_type"] = et.rsplit(".", 1)[1]
    return out


# --------------------------------------------------------------------- 判定

OPS = {
    "eq": lambda a, e: a == e,
    "ne": lambda a, e: a != e,
    "in": lambda a, e: e in (a if isinstance(a, (list, tuple, dict)) else [a]),
}


def resolve(ref: str, ctx: dict[str, Any]) -> Any:
    """按引用取值：``http:<探针id>.<键>`` / ``scenario:<场景id>.<键>`` / 本场景的键。"""
    if ref.startswith("http:"):
        _, rest = ref.split(":", 1)
        probe, _, key = rest.partition(".")
        return (ctx["http"].get(probe) or {}).get(key)
    if ref.startswith("scenario:"):
        _, rest = ref.split(":", 1)
        sid, _, key = rest.partition(".")
        return (ctx["scenarios"].get(sid) or {}).get(key)
    return ctx["current"].get(ref)


def eval_check(chk: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """跑一条判定，返回 {check, expected, actual, pass, note}。"""
    name = chk.get("check", "")
    op = chk.get("op", "eq")
    expect = chk.get("expect")
    src = chk.get("from")
    actual = resolve(src, ctx) if src else ctx["current"].get(name)
    if op == "differs":
        other = resolve(chk.get("other", ""), ctx)
        actual = actual != other
        ok = actual == expect
    else:
        ok = OPS[op](actual, expect)
    return {"check": name, "expected": expect, "actual": actual,
            "pass": bool(ok), "note": chk.get("note", "")}


# --------------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser(description="文件层动态侧：通用 harness + 容器探针")
    ap.add_argument("--cases-file", default="",
                    help="用例文件（含打桩、构造与期望观测）；不给就用项目绑定的 file_cases")
    args = ap.parse_args()

    cfg_path = cases_path(args.cases_file)
    if not cfg_path.exists():
        raise SystemExit(f"找不到用例文件：{cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    classpath = build_classpath()
    harness_out = run_harness(cfg_path, classpath)

    scenarios = {s["id"]: normalize(s) for s in harness_out.get("scenarios", [])}
    http_raw = file_http_probe.run(cfg.get("http", {})) if cfg.get("http") else {}

    ctx_scen: dict[str, Any] = {"scenarios": scenarios, "http": http_raw}

    results: list[dict[str, Any]] = []
    for spec in cfg.get("scenarios", []):
        sid = spec.get("id", "")
        raw = scenarios.get(sid, {})
        ctx = {**ctx_scen, "current": raw}
        checks = [eval_check(c, ctx) for c in spec.get("checks", [])]
        results.append({"id": sid, "checks": checks, "raw": raw})

    http_checks: list[dict[str, Any]] = []
    if cfg.get("http"):
        ctx = {**ctx_scen, "current": http_raw}
        http_checks = [eval_check(c, ctx) for c in cfg["http"].get("checks", [])]
        results.append({"id": "HTTP-PROBES", "checks": http_checks, "raw": http_raw})

    payload = {
        "meta": {
            "harness": HARNESS_META,
            "scenarios": len(cfg.get("scenarios", [])),
            "http_probes": len(http_checks),
            "upload_sandbox": str(UPLOAD_DIR),
            "cases_file": str(cfg_path),
        },
        "results": results,
    }
    out = project.artifact_dir() / "file_observation.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    detectors = harness_out.get("detectors") or {}
    det_out = project.artifact_dir() / "file_detectors.json"
    det_out.write_text(json.dumps(detectors, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(r["checks"]) for r in results)
    failed = [(r["id"], c) for r in results for c in r["checks"] if not c["pass"]]
    print(f"文件层动态 -> {out}")
    print(f"  用例 {len(results)} 个 / 判定 {total} 条 / 不符 {len(failed)} 条")
    for sid, c in failed:
        print(f"  !! {sid}.{c['check']}: 期望 {c['expected']} 实际 {c['actual']}")
    if detectors:
        print(f"  检查器 -> {det_out}")


if __name__ == "__main__":
    main()
