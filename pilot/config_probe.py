"""配置绑定失败模式探针（@Value 决定的语义）。

`@Value` 注入的三个配置项决定着行为：上传目录、CORS 白名单。配置写错/写空时
系统怎么反应，是**只有配好时才正确**还是**配错了会静默放宽** —— 后者是安全问题。

做法：克隆正在跑的容器，逐个改配置项，起临时实例，用真实请求观察行为。
（不动用户正在跑的实例，也不动他的配置文件。）

类别：service.interface_contract / service.authz。oracle：chaos_experiment。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import requests

from probe_instance import ProbeInstance

HERE = Path(__file__).resolve().parent
OUT = HERE / "config_findings.json"


def cors_probe(base: str, origin: str = "http://evil.example.com") -> dict[str, Any]:
    s = requests.Session()
    resp = s.get(f"{base}/api/products?page=1&pageSize=1",
                 headers={"Origin": origin}, timeout=20)
    return {
        "status": resp.status_code,
        "allow_origin": resp.headers.get("Access-Control-Allow-Origin"),
        "vary": resp.headers.get("Vary"),
    }


def upload_probe(base: str) -> dict[str, Any]:
    """往上传接口扔一个文件，看错误响应里有没有泄露文件系统路径。"""
    s = requests.Session()
    resp = s.post(f"{base}/api/uploads/product-image",
                  files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")}, timeout=20)
    try:
        body = resp.json()
    except ValueError:
        body = {"_raw": resp.text[:400]}
    text = json.dumps(body, ensure_ascii=False)
    leaks = [seg for seg in ("/tmp/", "/app/", "/proc/", "java.io", "NoSuchFile",
                              "Permission denied", "/var/") if seg in text]
    return {"status": resp.status_code, "body": body, "path_leaks": leaks}


VARIANTS = ("cors_empty", "cors_star", "upload_dir_bad", "upload_dir_broken", "upload_relative")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="配置失败模式探针")
    parser.add_argument("--only", action="append", default=[], metavar="VARIANT",
                        help="只跑指定的配置变体（可重复）")
    args = parser.parse_args()
    only = set(args.only) or set(VARIANTS)
    unknown = sorted(only - set(VARIANTS))
    if unknown:
        raise SystemExit(f"未知配置变体: {unknown}；可用: {list(VARIANTS)}")

    probes: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []

    # --- CORS 白名单为空 ---
    if "cors_empty" in only:
        with ProbeInstance("jiuyan-cfg-empty-cors",
                           {"CORS_ALLOWED_ORIGINS": ""}) as inst:
            probes["cors_empty"] = cors_probe(inst.base)
    # --- CORS 白名单为 * ---
    if "cors_star" in only:
        with ProbeInstance("jiuyan-cfg-star-cors",
                           {"CORS_ALLOWED_ORIGINS": "*"}) as inst:
            probes["cors_star"] = cors_probe(inst.base)
    # --- 上传目录配成不可用的路径：应用还能起来吗 ---
    if "upload_dir_bad" in only:
        try:
            with ProbeInstance("jiuyan-cfg-bad-upload-dir",
                               {"FILE_UPLOAD_DIR": "/proc/self/nonexistent-dir"}) as inst:
                probes["upload_dir_bad"] = upload_probe(inst.base)
        except RuntimeError as exc:
            probes["upload_dir_bad"] = {"started": False, "error": str(exc)}
            # 实例起不来时它已经自己收尾了，这里再确认一次没有残留
            subprocess.run(["docker", "rm", "-f", "jiuyan-cfg-bad-upload-dir"], check=False)

    # --- 上传目录先可用、跑起来后再破坏掉：看上传接口的错误响应会不会泄露路径 ---
    if "upload_dir_broken" in only:
      with ProbeInstance("jiuyan-cfg-broken-upload-dir") as inst:
        upload_dir = f"/tmp/{inst.name}-uploads"
        # 把 products 目录换成一个普通文件 -> Files.createDirectories 必然失败
        subprocess.run(["docker", "exec", inst.name, "rm", "-rf", f"{upload_dir}/products"],
                       check=False)
        subprocess.run(["docker", "exec", inst.name, "touch", f"{upload_dir}/products"],
                       check=False)
        probes["upload_dir_broken"] = upload_probe(inst.base)

    # --- 上传目录是相对路径 ---
    if "upload_relative" in only:
        with ProbeInstance("jiuyan-cfg-relative-upload-dir",
                           {"FILE_UPLOAD_DIR": "relative-uploads"}) as inst:
            probes["upload_relative"] = upload_probe(inst.base)

    star = probes.get("cors_star") or {}
    empty = probes.get("cors_empty") or {}
    if empty.get("allow_origin") is not None:
        findings.append({
            "id": "CFG-CORS-EMPTY-ALLOWS-ANY-ORIGIN",
            "severity": "HIGH",
            "endpoint": "GET /api/**",
            "title": "CORS 白名单配成空值时反而允许任意来源跨域（fail-open）",
            "detail": (
                "这是本轮最反直觉的一条：**把白名单清空，跨域不是被收紧，而是被放开。**\n"
                "机制：WebConfig 把 app.cors-allowed-origins 拆开、滤掉空项，得到一个"
                "**空数组**，传给 allowedOriginPatterns(...)；而 Spring 的 "
                "CorsRegistration 默认先 applyPermitDefaultValues()（allowedOrigins 变成 "
                "单个星号），"
                "空数组并没有把它清掉 → 最终任何来源都放行。\n"
                f"实测（Origin: http://evil.example.com）："
                f"Access-Control-Allow-Origin = {empty.get('allow_origin')!r}。\n"
                "运维的直觉是「清空白名单 = 关掉跨域」，实际拿到的是「对全世界开放跨域」。"
                "叠加零鉴权：任何网页都能读全部业务数据。\n"
                "对照：白名单配成 * 时 Spring 回显请求的 Origin"
                f"（实测 {star.get('allow_origin')!r}），同样是任意来源放行 —— "
                "但那是显式配置，属于配置者自己的选择；空值这条是**踩空即中招**。\n"
                "实测全集：\n" + json.dumps(probes, ensure_ascii=False, indent=2)),
            "repro": "python3 config_probe.py",
            "evidence": {"probes": probes},
        })
    if star.get("allow_origin") is not None:
        findings.append({
            "id": "CFG-CORS-STAR-ALLOWS-ANY-ORIGIN",
            "severity": "HIGH",
            "endpoint": "GET /api/**",
            "title": "CORS 白名单配成 * 时，任何网页都能读全部业务数据",
            "detail": (
                "WebConfig 把 app.cors-allowed-origins 原样拆开交给 "
                "allowedOriginPatterns(...)。配成 * 时 Spring 按模式语义回显请求的 Origin，"
                f"实测 Access-Control-Allow-Origin: {star.get('allow_origin')!r}。\n"
                "配合 allowCredentials(false)：本系统本来就没有凭证要带（零鉴权），"
                "所以「不带凭证也能读」= 任何网页都能读全部业务数据。\n"
                "这条与文件层已开的 FILE-CORS-INTRANET-WIDE 是同一根因"
                "（CORS 允许源范围过大）的更严重形态，不重复开单。"),
            "repro": "python3 config_probe.py",
            "evidence": {"probes": probes},
        })
    bad = probes.get("upload_dir_bad") or {}
    if bad.get("started") is False:
        findings.append({
            "id": "CFG-UPLOAD-DIR-BAD-KILLS-APP",
            "severity": "LOW",
            "endpoint": "（启动期）",
            "title": "上传目录配错会让整个后端起不来，而不是降级运行",
            "detail": (
                "app.file-upload-dir 指向一个不可用的路径时，SeedAssetInitializer "
                "（ApplicationRunner）在启动期抛异常，Spring 上下文起不来 → 容器退出。\n"
                "失败方向本身是对的（fail-fast 比带着坏配置跑着强），但错误完全来自底层 IO，"
                "运维看到的是「应用启动失败」而不是「上传目录配错了」。\n"
                f"实测：{json.dumps(bad, ensure_ascii=False)}"),
            "repro": "python3 config_probe.py",
            "evidence": {"probe": bad},
        })
    up = probes.get("upload_dir_broken") or {}
    if up.get("status") and not up.get("path_leaks"):
        findings.append({
            "id": "CFG-UPLOAD-ERROR-NO-PATH-LEAK",
            "severity": "INFO",
            "endpoint": "POST /api/uploads/product-image",
            "title": "上传失败时错误响应不泄露服务器文件系统路径",
            "detail": (
                "把上传目录破坏掉（products 目录换成一个普通文件，Files.createDirectories "
                "必然失败）后，接口返回的是固定的业务错误，响应体里没有任何路径片段：\n"
                + json.dumps(up.get("body"), ensure_ascii=False) + "\n"
                "扫描的片段：" + json.dumps(up.get("path_leaks"), ensure_ascii=False)
                + "（空 = 没泄露）。\n"
                "对照：文件层已开的 INTERNAL_LEAK（25 处）是**其它**接口把框架异常原文透传，"
                "上传这条是干净的。"),
            "repro": "python3 config_probe.py",
            "evidence": {"probe": up},
        })
    if up.get("path_leaks"):
        findings.append({
            "id": "CFG-UPLOAD-ERROR-LEAKS-FILESYSTEM-PATH",
            "severity": "MEDIUM",
            "endpoint": "POST /api/uploads/product-image",
            "title": "上传目录不可用时，错误响应泄露了服务器文件系统路径",
            "detail": (
                f"把上传目录指到不可写/不存在的位置后，错误响应里出现了这些片段："
                f"{up.get('path_leaks')}\n"
                "完整响应：" + json.dumps(up.get("body"), ensure_ascii=False)),
            "repro": "python3 config_probe.py",
            "evidence": {"probe": up},
        })

    payload = {"service": findings, "probes": probes}
    # 单变体运行时写到独立文件，由调度器做并集合并（并集不缩集）
    target = OUT if not args.only else OUT.with_name(
        OUT.stem + "." + "_".join(sorted(only)) + ".json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"配置失败模式探针 -> {OUT}  {len(findings)} 条发现")
    for k, v in probes.items():
        print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:150]}")
    for f in findings:
        print(f"  {f['severity']:<9}{f['id']:<44}{f['title'][:40]}")


if __name__ == "__main__":
    main()
