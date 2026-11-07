"""删除上传目录里没有被任何数据库记录引用的文件（探针上传的残留）。

判定依据是「数据库里还有没有引用」——种子图（/uploads/seed/...）被引用，一律保留。

注意：/uploads/seed/ 由 SeedAssetInitializer 从 classpath 托管，不属于上传流。
即使某个种子图当前没被数据库引用，也绝不能删——它可能是前端代码里的硬编码兜底图。
"""
import re
import subprocess

SEED_DIR_PREFIX = "/uploads/seed/"


def psql(sql: str) -> str:
    return subprocess.run(
        ["docker", "exec", "jiuyan-postgres", "psql", "-U", "jiuyan", "-d", "jiuyan", "-tAc", sql],
        check=True, capture_output=True, text=True).stdout


def main() -> None:
    referenced = set()
    for sql in [
        "SELECT image_url FROM products WHERE image_url LIKE '/uploads/%'",
        "SELECT image_url FROM packages WHERE image_url LIKE '/uploads/%'",
        "SELECT package_snapshot::text FROM orders",
        "SELECT added_items_snapshot::text FROM orders",
    ]:
        referenced |= set(re.findall(r"/uploads/[A-Za-z0-9._/-]+", psql(sql)))

    files = subprocess.run(
        ["docker", "exec", "jiuyan-backend", "find", "/app/uploads", "-type", "f"],
        check=True, capture_output=True, text=True).stdout.split()
    removed = []
    skipped = []
    for f in files:
        url = f.replace("/app", "")
        if url.startswith(SEED_DIR_PREFIX):
            skipped.append(url)
            continue
        if url in referenced:
            continue
        subprocess.run(["docker", "exec", "jiuyan-backend", "rm", "-f", f], check=True)
        removed.append(url)
    print(f"upload cleanup: 引用中 {len(referenced)} 个路径，删除 {len(removed)} 个未被引用的文件，"
          f"跳过种子目录 {len(skipped)} 个文件")
    for u in removed:
        print(f"  removed {u}")


if __name__ == "__main__":
    main()
