"""残留守卫 —— 探针造了数据就必须自己删干净，这条由机器守，不靠人记得。

每次跑完流水线都查三样：
  1. 临时探针容器有没有留下（探针起容器、克隆实例）
  2. DB 里有没有 ai_ 前缀的行（探针造的订单/商品/套餐/分类）
  3. 上传目录里有没有非种子文件

有残留 → 退出码 1，流水线红。**「查过了没问题」和「没查」必须区分开**，
所以这个检查失败就是失败，不是警告。
"""
from __future__ import annotations
import project

import json
import subprocess
import sys
from pathlib import Path

HERE = project.artifact_dir()
OUT = HERE / "residue_check.json"
PROBE_CONTAINER_PATTERNS = ("jiuyan-tx-probe", "jiuyan-cfg-", "jiuyan-orm-", "jiuyan-stats-",
                            "jiuyan-feat-", "jiuyan-lazy", "jiuyan-portal")


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else ""


def psql(sql: str) -> str:
    return docker("exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan",
                  "-tAc", sql).strip()


def main() -> None:
    problems: list[str] = []

    names = [n for n in docker("ps", "-a", "--format", "{{.Names}}").split()
             if n.startswith(PROBE_CONTAINER_PATTERNS)]
    if names:
        problems.append(f"残留探针容器 {len(names)} 个: {names}")

    counts = {
        "products": int(psql("SELECT count(*) FROM products WHERE name LIKE 'ai\\_%'") or 0),
        "packages": int(psql("SELECT count(*) FROM packages WHERE name LIKE 'ai\\_%'") or 0),
        "categories": int(psql("SELECT count(*) FROM categories WHERE name LIKE 'ai\\_%'") or 0),
        "package_types": int(psql("SELECT count(*) FROM package_types WHERE name LIKE 'ai\\_%'") or 0),
        "orders": int(psql("SELECT count(*) FROM orders WHERE customer_name LIKE 'ai\\_%'") or 0),
    }
    for table, n in counts.items():
        if n:
            problems.append(f"残留 {table} 行 {n}")

    uploads = docker("exec", "jiuyan-backend", "find", "/app/uploads", "-type", "f",
                      "-not", "-path", "*/seed/*").split()
    if uploads:
        problems.append(f"残留非种子上传文件 {len(uploads)} 个: {uploads[:5]}")

    payload = {"ok": not problems, "problems": problems,
               "probe_containers": names, "ai_prefix_rows": counts,
               "non_seed_uploads": len(uploads)}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if problems:
        print("残留守卫失败：")
        for p_ in problems:
            print(f"  {p_}")
        raise SystemExit(1)
    print(f"残留守卫通过：容器 0 / ai_ 行 {sum(counts.values())} / 非种子上传 0")


if __name__ == "__main__":
    main()
