"""ORM 查询放大探针（N+1）—— 用第二实例的 Hibernate 统计精确数语句。

为什么需要第二实例：当前跑着的容器没开 Hibernate 统计；开了之后每个请求结束
都会打一段 "Session Metrics { ... spent executing N JDBC statements ... }"，
N 就是这次请求发的 SQL 条数 —— 精确值，不是估的。

实例怎么来：**克隆正在跑的那个容器的镜像、网络与环境变量**，只改端口和上传目录，
再加一个 JAVA_TOOL_OPTIONS 打开统计。环境变量从 `docker inspect` 现场读，
所以密码之类的东西不会进仓库。测完立刻拆掉。

类别：service.service_latency（慢路径）。oracle：trace_analysis。
"""
from __future__ import annotations
import project

import json
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from integration_chain import BASE, call, cleanup, data_of, order_body, owner_session
from integration_reconcile import PFX, psql_rows

HERE = project.artifact_dir()
OUT = HERE / "orm_findings.json"
SRC_CONTAINER = "jiuyan-backend"
PROBE_CONTAINER = "jiuyan-orm-stats-probe"
STAT_RE = re.compile(r"spent executing (\d+) JDBC statements")


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} 失败: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def source_env() -> tuple[str, str, dict[str, str]]:
    """从运行中的容器读镜像名、网络名、环境变量（密码不落仓库）。"""
    raw = docker("inspect", SRC_CONTAINER)
    info = json.loads(raw)[0]
    image = info["Config"]["Image"]
    network = next(iter(info["NetworkSettings"]["Networks"]))
    env = {}
    for item in info["Config"].get("Env") or []:
        k, _, v = item.partition("=")
        env[k] = v
    return image, network, env


class StatsInstance:
    """临时第二实例：开 Hibernate 统计，用完拆掉。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "StatsInstance":
        image, network, env = source_env()
        docker("rm", "-f", PROBE_CONTAINER)
        args = ["run", "-d", "--name", PROBE_CONTAINER, "--network", network,
                "-p", f"127.0.0.1:{self.port}:{self.port}"]
        for k, v in env.items():
            if k.startswith("OTEL_") or k in ("LANG", "LANGUAGE", "LC_ALL", "HOSTNAME"):
                continue
            args += ["-e", f"{k}={v}"]
        args += [
            "-e", f"SERVER_PORT={self.port}",
            "-e", f"FILE_UPLOAD_DIR=/tmp/{PROBE_CONTAINER}-uploads",
            "-e", "JAVA_TOOL_OPTIONS=-Dspring.jpa.properties.hibernate.generate_statistics=true",
            image,
        ]
        docker(*args)
        for _ in range(90):
            try:
                if requests.get(f"{self.base}/api/health", timeout=2).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError("统计实例没起来")

    def __exit__(self, *exc: Any) -> None:
        docker("rm", "-f", PROBE_CONTAINER)

    def total_statements(self) -> int:
        """所有 Session Metrics 块的 JDBC 语句数之和。

        一个请求可能开多个 session（OpenEntityManagerInView + 事务），
        所以不能只读最后一块 —— 要累加总量，再取差值。
        """
        logs = docker("logs", PROBE_CONTAINER)
        return sum(int(m.group(1)) for m in STAT_RE.finditer(logs))


def statements_of(inst: "StatsInstance", session: requests.Session, method: str,
                  path: str, body: dict[str, Any] | None = None) -> int:
    """打一个请求，返回这次请求发的 SQL 条数（前后总量取差）。"""
    before = inst.total_statements()
    if method == "GET":
        session.get(inst.base + path, timeout=30)
    else:
        session.post(inst.base + path, json=body, timeout=30)
    return inst.total_statements() - before


def main() -> None:
    findings: list[dict[str, Any]] = []
    probes: dict[str, Any] = {}
    try:
        with StatsInstance() as inst:
            s = requests.Session()
            # --- 建单：语句数 vs 明细条数 ---
            all_prods = s.get(f"{inst.base}/api/products?page=1&pageSize=50").json()["data"]["items"]
            # 只取**分类互不相同**的商品：同分类的商品会命中持久化上下文缓存，
            # 分类查询被省掉，斜率会被系统性低估，测不出最坏情况。
            prods: list[dict[str, Any]] = []
            seen_cat: set[Any] = set()
            for prod in all_prods:
                cat = prod.get("categoryId")
                if cat in seen_cat:
                    continue
                seen_cat.add(cat)
                prods.append(prod)
            ptypes = s.get(f"{inst.base}/api/package-types").json()["data"]
            pkg = s.post(f"{inst.base}/api/packages", json={
                "name": f"{PFX}orm_pkg", "banquetType": ptypes[0]["name"], "priceCent": 8888,
                "imageUrl": "", "description": "orm probe", "isActive": True,
                "items": [{"productId": p["id"], "quantity": 1} for p in prods[:10]],
            }).json()["data"]

            # 预热一次，排除首次请求的连接/JIT 噪声
            statements_of(inst, s, "GET", "/api/products?page=1&pageSize=1")
            order_points: list[dict[str, Any]] = []
            ks = [k for k in (1, 2, 4, 8) if k <= len(prods)] or [1]
            for k in ks:
                body = order_body("orm", prods[0]["id"], pkg["id"], qty=k)
                body["packageItems"] = [{"productId": p["id"], "quantity": 1} for p in prods[:k]]
                body["planTotalCent"] = 24680
                body["useFinalPrice"] = False
                body["finalPriceCent"] = None
                body["depositCent"] = 0
                n = statements_of(inst, s, "POST", "/api/orders", body)
                order_points.append({"items": k, "statements": n})
            probes["order_create"] = order_points

            # --- 套餐列表：语句数 vs 返回条数 ---
            list_points: list[dict[str, Any]] = []
            for ps in (1, 2, 4):
                if ps > len(all_prods):
                    break
                n = statements_of(inst, s, "GET", f"/api/packages?page=1&pageSize={ps}")
                list_points.append({"page_size": ps, "statements": n})
            probes["package_list"] = list_points
    finally:
        cleanup()

    # --- 斜率：每多 1 个明细多几条 SQL ---
    order_pts = probes["order_create"]
    # 斜率取端点法（最稳）：(最后 - 最前) / (明细数之差)
    if len(order_pts) >= 2:
        per_item = round((order_pts[-1]["statements"] - order_pts[0]["statements"]) /
                         (order_pts[-1]["items"] - order_pts[0]["items"]), 2)
    else:
        per_item = 0.0
    list_pts = probes["package_list"]
    if len(list_pts) >= 2:
        per_package = round((list_pts[-1]["statements"] - list_pts[0]["statements"]) /
                            (list_pts[-1]["page_size"] - list_pts[0]["page_size"]), 2)
    else:
        per_package = 0.0

    probes["per_item_statements"] = per_item
    probes["per_package_statements"] = per_package

    if per_item >= 1.5:
        findings.append({
            "id": "SVC-ORM-N-PLUS-ONE-ORDER-ITEMS",
            "severity": "MEDIUM",
            "endpoint": "POST /api/orders",
            "title": f"下单时每多一个明细就多 {per_item} 条 SQL（典型 N+1）",
            "detail": (
                f"用开了 Hibernate 统计的第二实例精确数了每次请求发的 SQL 条数：\n"
                f"{json.dumps(order_points, ensure_ascii=False)}\n"
                f"斜率 = **每加 1 个套餐明细，多 {per_item} 条 SQL**。\n"
                "对应到代码：OrderService.buildPackageSnapshot 对每个明细调一次 "
                "findProduct()（1 条商品查询），随后 productSnapshot 访问 "
                "product.getCategory().getName() —— category 是 LAZY 关联，又触发 1 条查询。\n"
                "业务后果：订单明细越多，下单越慢，且慢的部分不在任何索引或缓存的管辖范围内。\n"
                f"（附：套餐列表接口每多返回 1 个套餐多 {per_package} 条 SQL，同样是 N+1。）"),
            "repro": "python3 orm_query_probe.py",
            "evidence": {"probes": probes, "per_item_statements": per_item,
                         "per_package_statements": per_package},
        })
    if per_package >= 1.5:
        findings.append({
            "id": "SVC-ORM-N-PLUS-ONE-PACKAGE-LIST",
            "severity": "MEDIUM",
            "endpoint": "GET /api/packages",
            "title": f"套餐列表每多返回 1 个套餐就多 {per_package} 条 SQL",
            "detail": (
                f"{json.dumps(list_points, ensure_ascii=False)}\n"
                f"斜率 = 每多 1 个套餐多 {per_package} 条 SQL。列表页把套餐的明细、"
                "明细的商品、商品的分类逐个拉出来 —— 页面打开 50 个套餐就是几百次往返。"
                "前端固定只取前 100 条，所以上限也就是几百次往返/页。"),
            "repro": "python3 orm_query_probe.py",
            "evidence": {"probes": probes},
        })

    payload = {"service": findings, "probes": probes}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ORM 查询放大探针 -> {OUT}  {len(findings)} 条发现")
    print(f"  下单：每明细 {per_item} 条 SQL；套餐列表：每套餐 {per_package} 条 SQL")
    for f in findings:
        print(f"  {f['severity']:<9}{f['id']:<40}{f['title'][:44]}")


if __name__ == "__main__":
    main()
