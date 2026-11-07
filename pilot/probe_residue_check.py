r"""残留守卫 —— 探针造了数据就必须自己删干净，这条由机器守，不靠人记得。

每次跑完流水线查三样：
  1. 临时探针容器有没有留下
  2. DB 里有没有 ai_ 前缀的行（探针造的订单/商品/套餐/分类）
  3. 上传目录里有没有非种子文件

**守卫必须能证明自己抓得到**（`--selftest`）：
一个报「0 行」的检查，如果谓词写错了（转义、通配符、列名拼错），永远是绿的 ——
机器看着放心，库里其实留着垃圾。亲眼见过另一个组件栽在这上面：
`LIKE 'ai_probe\_%'` 的转义让它要求名字里有两个下划线，于是永远匹配不到任何行，
「0 残留」是假的。所以本守卫自带阳性对照：插一行探针数据，必须被抓到。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "residue_check.json"
DEFAULT_PROBE_PREFIXES = ("jiuyan-tx-probe", "jiuyan-cfg-", "jiuyan-orm-", "jiuyan-stats-",
                          "jiuyan-feat-", "jiuyan-lazy", "jiuyan-portal")
AI_COLUMNS = {
    "products": "name",
    "packages": "name",
    "categories": "name",
    "package_types": "name",
    "orders": "customer_name",
}
AI_PREFIX = "ai_probe_"


def probe_prefixes() -> list[str]:
    """探针容器名前缀 —— 来自项目绑定（换项目时探针的命名也跟着换）。"""
    try:
        cfg = json.loads((HERE / "target.json").read_text(encoding="utf-8"))
        return list(cfg.get("paths", {}).get("probe_container_prefixes",
                                             DEFAULT_PROBE_PREFIXES))
    except (OSError, ValueError):
        return list(DEFAULT_PROBE_PREFIXES)


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} 失败: {proc.stderr.strip()[-200:]}")
    return proc.stdout


def psql(sql: str) -> str:
    return docker("exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan",
                  "-tAc", sql).strip()


def ai_row_counts() -> dict[str, int]:
    """按前缀统计探针残留行。

    用 `starts_with` 而不是 `LIKE`：LIKE 里的 `_` 是单字符通配符，转义又容易写错 ——
    这两个坑都会让「0 行」变成假象。starts_with 没有通配语义，没有转义歧义。
    """
    return {table: int(psql(f"SELECT count(*) FROM {table} "
                             f"WHERE starts_with({col}, '{AI_PREFIX}')") or 0)
            for table, col in AI_COLUMNS.items()}


def upload_files() -> list[str]:
    return docker("exec", "jiuyan-backend", "find", "/app/uploads", "-type", "f",
                  "-not", "-path", "*/seed/*").split()


def stray_dirs() -> list[str]:
    """工作目录里不该出现散落的落盘目录（探针写错上传路径时的泄漏形态）。"""
    return sorted(str(q) for q in HERE.glob("*")
                  if q.is_dir() and q.name in ("products", "packages", "uploads"))


def collect_problems() -> tuple[list[str], dict]:
    problems: list[str] = []
    prefixes = tuple(probe_prefixes())
    names = [n for n in docker("ps", "-a", "--format", "{{.Names}}").split()
             if n.startswith(prefixes)]
    if names:
        problems.append(f"残留探针容器 {len(names)} 个: {names}")
    counts = ai_row_counts()
    for table, n in counts.items():
        if n:
            problems.append(f"残留 {table} 行 {n}")
    uploads = upload_files()
    if uploads:
        problems.append(f"残留非种子上传文件 {len(uploads)} 个: {uploads[:5]}")
    stray = stray_dirs()
    if stray:
        problems.append(f"工作目录里出现散落的落盘目录（探针写错了上传路径）: {stray}")
    detail = {"probe_containers": names, "ai_prefix_rows": counts,
              "non_seed_uploads": len(uploads), "stray_dirs": stray}
    return problems, detail


def selftest() -> bool:
    """阳性对照：插一行探针数据，必须被抓到；删掉后必须报干净。"""
    cat = psql("SELECT id FROM categories ORDER BY id LIMIT 1")
    if not cat:
        print("  自检跳过：拿不到分类 id")
        return True
    marker = f"{AI_PREFIX}selftest_{int(time.time())}"
    psql(f"INSERT INTO products (name, category_id, price_cent, unit) "
         f"VALUES ('{marker}', {cat}, 1, 'selftest')")
    try:
        hit = int(psql(f"SELECT count(*) FROM products WHERE name = '{marker}'") or 0)
        if hit != 1:
            print(f"  自检失败：探针行没插进去（count={hit}）")
            return False
        problems, _ = collect_problems()
        if not any("products" in p for p in problems):
            print("  自检失败：守卫没抓到刚插入的探针行 —— 谓词有问题")
            return False
        print("  自检通过：插入的探针行被守卫抓到")
        return True
    finally:
        psql(f"DELETE FROM products WHERE name = '{marker}'")


def main() -> int:
    if "--selftest" in sys.argv:
        ok = selftest()
        print("守卫自检：", "通过" if ok else "失败")
        return 0 if ok else 1

    problems, detail = collect_problems()
    payload = {"ok": not problems, "problems": problems, **detail}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(detail["ai_prefix_rows"].values())
    print(f"残留守卫{'通过' if not problems else '失败'}："
          f"容器 {len(detail['probe_containers'])} / ai_ 行 {total} / "
          f"非种子上传 {detail['non_seed_uploads']}")
    for p in problems:
        print(f"  {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
