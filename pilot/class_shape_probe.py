"""类形状探针（通用）：编译 class_harness/ClassShape.java，跑反射，把类的形状导成 JSON。

形状 = 继承层次 / 覆写关系 / 里氏替换的后置条件比对 / 值相等 / 封装泄漏 /
缓存里的可变对象。

通用骨架在这里；**类清单**（哪些类参与比对）在项目侧的 class_scenarios.json 里 ——
换项目只换那个文件，本文件不含任何项目字面量。

harness 只做反射与比对，不碰 DB、不碰容器：审计不该顺手改业务数据。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import project

HERE = Path(__file__).resolve().parent
HARNESS_SRC = HERE / "class_harness" / "ClassShape.java"


def _harness_classes() -> Path:
    return project.work_dir() / "class_shape_classes"


def ensure_compiled() -> list[str]:
    """编译 harness —— classpath 来自目标项目的编译产物与依赖。

    目标项目的编译走 fn_static.ensure_compiled()（从源码新鲜编译），
    依赖 classpath 走 maven 的 dependency:build-classpath —— 都在项目绑定里，
    本文件不写死任何路径。
    """
    import fn_static

    fn_static.ensure_compiled()
    deps = fn_static.DEPS_FILE.read_text().strip()
    out_dir = _harness_classes()
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(out_dir),
         "-cp", f"{fn_static.CLASSES}:{deps}", str(HARNESS_SRC)],
        check=True, capture_output=True, text=True)
    return [str(out_dir), str(fn_static.CLASSES), deps]


def run_shape_probe(probe: dict) -> dict:
    """跑一次形状探针。类清单从配置来（stdin 交给 harness）。"""
    classes = list(probe.get("classes") or [])
    if not classes:
        return {"classes": [], "lsp_violations": []}
    classpath = ensure_compiled()
    proc = subprocess.run(
        ["java", "-cp", os.pathsep.join(classpath), "ClassShape"],
        input="\n".join(classes), capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError("ClassShape harness 失败: "
                           + (proc.stderr.strip() or proc.stdout.strip())[-500:])
    result: dict[str, Any] = json.loads(proc.stdout)
    result["_requested"] = classes
    return result
