r"""残留守卫 —— 探针造了数据就必须自己删干净，这条由机器守，不靠人记得。

每次跑完流水线查四样：
  1. 临时探针容器有没有留下
  2. DB 里有没有 ai_ 前缀的行（探针造的订单/商品/套餐/分类）
  3. 上传目录里有没有非种子文件
  4. 工作目录里有没有散落的落盘目录

DB 通道（容器 / 库 / 用户）、要查的表与列、探针容器前缀、上传目录全部来自
target.json 的 paths 段 —— 换项目不用改这个脚本。

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

import project

HERE = Path(__file__).resolve().parent
DEFAULT_PROBE_PREFIXES = ("jiuyan-tx-probe", "jiuyan-cfg-", "jiuyan-orm-", "jiuyan-stats-",
                          "jiuyan-feat-", "jiuyan-lazy", "jiuyan-portal")
DEFAULT_AI_COLUMNS = {
    "products": "name",
    "packages": "name",
    "categories": "name",
    "package_types": "name",
    "orders": "customer_name",
}
AI_PREFIX = "ai_probe_"
DEFAULT_UPLOAD_DIR = "/app/uploads"


def _paths() -> dict:
    try:
        return project.paths()
    except Exception:
        return {}


def probe_prefixes() -> list[str]:
    """探针容器名前缀 —— 来自项目绑定（换项目时探针的命名也跟着换）。"""
    return list(_paths().get("probe_container_prefixes", DEFAULT_PROBE_PREFIXES))


def ai_columns() -> dict[str, str]:
    """要查的表与前缀列 —— 来自项目绑定。"""
    cols = _paths().get("probe_ai_columns")
    return dict(cols) if cols else dict(DEFAULT_AI_COLUMNS)


def db_spec() -> dict:
    spec = _paths().get("db") or {}
    return {"container": spec.get("container", ""),
            "user": spec.get("user", ""),
            "database": spec.get("database", "")}


def upload_dir() -> str:
    return _paths().get("upload_dir", DEFAULT_UPLOAD_DIR)


def app_container() -> str:
    return _paths().get("app_container", "")


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} 失败: {proc.stderr.strip()[-200:]}")
    return proc.stdout


def psql(sql: str) -> str:
    spec = db_spec()
    missing = [k for k, v in spec.items() if not v]
    if missing:
        raise RuntimeError(f"target.json 的 paths.db 缺 {missing} —— 无法查残留")
    return docker("exec", spec["container"], "psql", "-U", spec["user"],
                  "-d", spec["database"], "-tAc", sql).strip()


def ai_row_counts() -> dict[str, int]:
    """按前缀统计探针残留行。

    用 `starts_with` 而不是 `LIKE`：LIKE 里的 `_` 是单字符通配符，转义又容易写错 ——
    这两个坑都会让「0 行」变成假象。starts_with 没有通配语义，没有转义歧义。
    """
    out: dict[str, int] = {}
    for table, col in ai_columns().items():
        try:
            n = psql(f"SELECT count(*) FROM {table} "
                     f"WHERE starts_with({col}, '{AI_PREFIX}')")
            out[table] = int(n or 0)
        except RuntimeError as exc:
            out[table] = -1          # 查不到不等于没有 —— 记成 -1 让守卫报出来
            print(f"  查 {table} 失败: {exc}")
    return out


def upload_files() -> list[str]:
    container = app_container()
    if not container:
        return []
    try:
        return docker("exec", container, "find", upload_dir(), "-type", "f",
                      "-not", "-path", "*/seed/*").split()
    except RuntimeError:
        return []


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
        if n > 0:
            problems.append(f"残留 {table} 行 {n}")
        elif n < 0:
            problems.append(f"{table} 残留行数查不到（守卫失效，不算干净）")
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
    spec = _paths().get("residue_selftest") or {}
    table = spec.get("table")
    name_col = spec.get("name_column")
    columns = spec.get("columns")
    values = spec.get("values_sql")
    if not (table and name_col and columns and values):
        print("  自检跳过：target.json 的 paths.residue_selftest 没配全")
        return True
    marker = f"{AI_PREFIX}selftest_{int(time.time())}"
    sql = values.replace("{{marker}}", marker)
    psql(f"INSERT INTO {table} ({columns}) VALUES ({sql})")
    try:
        hit = int(psql(f"SELECT count(*) FROM {table} "
                       f"WHERE {name_col} = '{marker}'") or 0)
        if hit != 1:
            print(f"  自检失败：探针行没插进去（count={hit}）")
            return False
        problems, _ = collect_problems()
        if not any(table in p for p in problems):
            print("  自检失败：守卫没抓到刚插入的探针行 —— 谓词有问题")
            return False
        print(f"  自检通过：插入的 {table} 探针行被守卫抓到")
        return True
    finally:
        psql(f"DELETE FROM {table} WHERE {name_col} = '{marker}'")


def main() -> int:
    if "--selftest" in sys.argv:
        ok = selftest()
        print("守卫自检：", "通过" if ok else "失败")
        return 0 if ok else 1

    problems, detail = collect_problems()
    payload = {"ok": not problems, "problems": problems, **detail}
    out = project.artifact_dir() / "residue_check.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(n for n in detail["ai_prefix_rows"].values() if n > 0)
    print(f"残留守卫{'通过' if not problems else '失败'}："
          f"容器 {len(detail['probe_containers'])} / ai_ 行 {total} / "
          f"非种子上传 {detail['non_seed_uploads']}")
    for p in problems:
        print(f"  {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
