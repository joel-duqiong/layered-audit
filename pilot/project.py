"""项目绑定的读取入口（项目侧，不是引擎）。

**层脚本的路径全部从这里来。** 换项目只改 `target.json` 的 `paths` 段，脚本一行不改。

为什么单独一个模块：路径散落在 10 个脚本里时，「换项目」就意味着改 10 个文件；
集中到这里之后，改动面收敛成一个配置段，且引擎侧仍然零项目字面量。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGET_FILE = HERE / "target.json"
ENV_TARGET = "LAYERED_AUDIT_TARGET"


def binding() -> dict[str, Any]:
    """读项目绑定（target.json）。可用 LAYERED_AUDIT_TARGET 换目标。"""
    path = Path(os.environ.get(ENV_TARGET, str(TARGET_FILE))).resolve()
    return json.loads(path.read_text(encoding="utf-8"))


def paths() -> dict[str, str]:
    return binding().get("paths", {})


def path(key: str, default: str = "") -> str:
    return paths().get(key, default)


def backend_root() -> Path:
    return Path(path("backend"))


def frontend_root() -> Path:
    """前端源码根（没有前端的目标返回空路径）。"""
    return Path(path("frontend", ""))


def java_package() -> str:
    """java 包前缀（点分形式，如 com.jiuyan.banquet；相对 src/main/java）。"""
    return path("java_package", "").replace("/", ".")


def java_source_root() -> Path:
    return backend_root() / "src/main/java"


def controller_dir() -> Path:
    return java_source_root() / path("controller_dir", f"{java_package().replace('.', '/')}/controller")


def entity_dir() -> Path:
    return java_source_root() / path("entity_dir", f"{java_package().replace('.', '/')}/entity")


def migration_dir() -> Path:
    return backend_root() / path("migration_dir", "src/main/resources/db/migration")


def backend_package_dir() -> Path:
    return java_source_root() / java_package().replace(".", "/")


def app_container() -> str:
    return path("app_container", "")


def base_url() -> str:
    return os.environ.get("JIUYAN_BASE_URL", path("base_url", "http://127.0.0.1:8081"))


def artifact_dir() -> Path:
    """产物目录 —— 各层脚本的输入输出都落在这里。

    默认与脚本同目录（jiuyan 的行为不变）；换目标时可指向独立目录，
    这样两个目标的产物不会互相覆盖。
    """
    d = Path(path("artifacts_dir", str(HERE)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir() -> Path:
    return Path(path("work_dir", "/tmp/layered_audit_work"))


def recordings_dir() -> Path:
    return Path(path("recordings", ""))


def target_name() -> str:
    return str(binding().get("target", ""))
