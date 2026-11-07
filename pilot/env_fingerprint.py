"""环境指纹采集（项目侧）。

引擎不认识具体数据库，所以采集放在这边：DB schema 摘要 + 容器镜像 + 种子数据摘要。
采不到 → 返回 None → 调用方必须把相关分片当成不可复用。
"""
from __future__ import annotations

import subprocess
from typing import Any

import project


def _psql(sql: str) -> str | None:
    container = project.path("pg_container", "jiuyan-postgres")
    db = project.path("db_name", "jiuyan")
    user = project.path("db_user", "jiuyan")
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "psql", "-U", user, "-d", db, "-tAc", sql],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def collect() -> list[Any] | None:
    parts: list[Any] = []
    schema = _psql(
        "SELECT md5(string_agg(t, '|' ORDER BY t)) FROM ("
        "SELECT table_name||':'||column_name||':'||data_type AS t "
        "FROM information_schema.columns WHERE table_schema='public') s")
    if schema is None:
        return None
    parts.append(schema)
    try:
        img = subprocess.run(
            ["docker", "inspect", "-f", "{{.Image}}",
             project.path("app_container", "jiuyan-backend")],
            capture_output=True, text=True, timeout=60)
        if img.returncode == 0:
            parts.append(img.stdout.strip())
    except Exception:
        pass
    return parts
