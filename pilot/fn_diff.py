"""函数层 · 差分侧：把「实测行为」和「业务契约」对齐，分组成缺陷单。

归属规则（全局一致）：owner = 能给出可复现证据的最低层。
本层出的单必须回答三件事：
  1. 根因在哪个函数（精确到方法）
  2. 这个缺陷从 HTTP 入口是否可达（可达 = 真缺陷；不可达 = 防御纵深缺失）
  3. 高层是否已经开过单（开过就只补根因证据，不重复计数）

产出 fn_findings.json / fn_findings.md。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent

# 每条缺陷的元数据：根因函数、边界是否已挡、HTTP 可达性、关联的高层单
GROUPS: dict[str, dict] = {
    "PRICE_CLIENT_CONTROLLED": {
        "id": "FN-PRICE-CLIENT-CONTROLLED",
        "severity": "CRITICAL",
        "title": "成交价由客户端说了算：resolveFinalPrice 只做下界检查，完全不与套餐价比对",
        "function": "OrderService.resolveFinalPrice",
        "root_cause": ("resolveFinalPrice(boolean, Long, Long) 只校验成交价 >= 0，"
                       "既不要求成交价 <= 套餐总价，也不要求有折扣依据，"
                       "于是客户端可以把任意数字直接变成订单最终价。"),
        "api_reachable": True,
        "boundary_protected": False,
        "boundary_note": "CreateOrderRequest.finalPriceCent 只有 @Min(0)，上界没有任何约束",
        "cwe": None,
        "related": [("integration", "PRICE_CLIENT_CONTROLLED_XL", "集成症状：本层是根因"),
                    ("service", "test_create_order_use_final_price", "既有测试把「保留客户端成交价」写成了断言——缺陷被固化成契约")],
    },
    "SNAPSHOT_SEMANTICS": {
        "id": "FN-SNAPSHOT-SEMANTICS",
        "severity": "HIGH",
        "title": "订单快照：明细归属不校验，空明细静默回退成套餐默认明细",
        "function": "OrderService.buildPackageSnapshot / normalizePackageItems",
        "root_cause": ("buildPackageSnapshot 用「requestedItems 是否为空」来决定用请求明细还是回退套餐默认明细，"
                       "「用户明确不要明细」与「用户没传明细」在这里不可区分；"
                       "同时它只校验商品存在，完全不校验商品是否属于该套餐。"),
        "api_reachable": True,
        "boundary_protected": False,
        "boundary_note": "DTO 只约束数量 >= 1 与 productId 非空，不约束归属，也不区分空 list 与未传",
        "cwe": None,
        "related": [("integration", "PACKAGE_ITEM_NOT_VALIDATED", "集成症状：本层是根因"),
                    ("integration", "PACKAGE_QUANTITY_NOT_VALIDATED", "集成症状：本层是根因"),
                    ("integration", "UNKNOWN_FIELD_SILENT_DROP", "集成症状：字段拼错 → 明细为空 → 落到本函数的回退分支")],
    },
    "ITEM_QUANTITY_UNVALIDATED": {
        "id": "FN-ITEM-QUANTITY-NOVALIDATION",
        "severity": "MEDIUM",
        "title": "明细归一化只查重复，不查 productId/quantity：数量可为空、为 0、为负",
        "function": "OrderService.normalizePackageItems / normalizeAddedItems",
        "root_cause": ("两个归一化函数只做「同一商品重复」检查，"
                       "productId 为 null 会变成 map 的 null 键、quantity 为 null/0/负数原样入表；"
                       "quantity 也没有上限。"),
        "api_reachable": True,
        "boundary_protected": True,
        "boundary_note": ("PackageItemRequest/AddedItemRequest 的 @NotNull @Min(1) 在 HTTP 边界已挡住 null/0/负数，"
                          "但数量上限无任何约束——集成层实测 quantity=99 被接受"),
        "cwe": None,
        "related": [("integration", "PACKAGE_QUANTITY_NOT_VALIDATED", "集成症状：本层是根因")],
    },
    "ORDER_NO_COLLISION": {
        "id": "FN-ORDER-NO-COLLISION",
        "severity": "HIGH",
        "title": "订单号每秒只有 1000 个槽位，并发下单必然撞号（唯一键冲突 → 500）",
        "function": "OrderService.generateOrderNo",
        "root_cause": ("订单号格式 DD + yyyyMMddHHmmss + 3 位随机，随机空间只有 1000/秒；"
                       "existsByOrderNo 检查与 insert 非原子（TOCTOU），"
                       "并发事务互相看不见，重复号直接撞 uk_orders_order_no 唯一约束。"),
        "api_reachable": True,
        "boundary_protected": False,
        "boundary_note": "无任何边界能挡住并发写；客户端请求速率就是下单速率",
        "cwe": "CWE-362",
        "related": [],
    },
    "UPLOAD_CONTENT_NOT_VERIFIED": {
        "id": "FN-UPLOAD-NO-CONTENT-SNIFF",
        "severity": "CRITICAL",
        "title": "上传只信客户端自报的扩展名与 contentType：任意字节都以 .png 落盘",
        "function": "UploadService.uploadImage / extension",
        "root_cause": ("白名单校验的对象是客户端自己提供的 originalFilename 与 Content-Type，"
                       "既不做内容嗅探，也不校验文件头；"
                       "实测一个 PE 可执行文件（MZ 头）被原样写成 products/<uuid>.png。"),
        "api_reachable": True,
        "boundary_protected": False,
        "boundary_note": "HTTP 契约层看不出内容问题——必须真的写一次文件才暴露",
        "cwe": "CWE-434",
        "related": [],
    },
    "UPLOAD_NO_SIZE_LIMIT": {
        "id": "FN-UPLOAD-NO-SIZE-LIMIT",
        "severity": "MEDIUM",
        "title": "上传无体积上限：单请求 20MB 被完整写盘（上传放大 DoS）",
        "function": "UploadService.uploadImage",
        "root_cause": "uploadImage 从不检查 file.getSize()，也没有服务端配额，文件按客户端给的字节数完整落盘。",
        "api_reachable": True,
        "boundary_protected": False,
        "boundary_note": "需要看容器/反向代理是否另有兜底限制——应用层没有",
        "cwe": "CWE-770",
        "related": [],
    },
    "APPLY_NULL_CONTRACT": {
        "id": "FN-APPLY-NULL-CONTRACT",
        "severity": "MEDIUM",
        "title": "apply 直接对入参 .trim()：空值触发 NPE（500）而不是业务异常（422）",
        "function": "OrderService.apply / CategoryService.create",
        "root_cause": ("apply 对 customerName/customerPhone/banquetName/banquetType 直接调用 .trim()，"
                       "空值抛 NullPointerException；CategoryService.create 对 name 同样。"
                       "函数自身不做空值防御，把全部校验押在 DTO 注解上。"),
        "api_reachable": False,
        "boundary_protected": True,
        "boundary_note": ("HTTP 路径由 @NotBlank 挡住；但任何内部调用或新增入口都会直接 500，"
                          "属于防御纵深缺失而非可利用缺陷"),
        "cwe": None,
        "related": [],
    },
    "APPLY_AMOUNT_INVARIANTS": {
        "id": "FN-APPLY-AMOUNT-INVARIANTS",
        "severity": "MEDIUM",
        "title": "apply 的金额/状态不变量只查了 >= 0：定金可以大于套餐总价，状态与预订时间可为空",
        "function": "OrderService.apply",
        "root_cause": ("apply 只校验 planTotalCent >= 0 与 depositCent >= 0，"
                       "不校验 depositCent <= planTotalCent、tableCount >= 1、"
                       "reservationTime/status 非空——DTO 上声明的约束没有在函数里落地。"),
        "api_reachable": True,
        "boundary_protected": True,
        "boundary_note": ("tableCount 与 status、reservationTime 由 DTO 注解挡住；"
                          "但「定金大于总价」没有任何约束，HTTP 可达"),
        "cwe": None,
        "related": [],
    },
}

HYPOTHESES = [
    {"source": "jiuyan-layer-manifest.yaml / fn-order",
     "hypothesis": "parseStatus 抛 IllegalArgumentException 而非 BusinessException，非法状态返回 500",
     "verdict": "FALSIFIED",
     "evidence": "字节码异常表里有 IllegalArgumentException 捕获；实测非法状态抛 ORDER_STATUS_INVALID / HTTP 422"},
    {"source": "jiuyan-layer-manifest.yaml / fn-order",
     "hypothesis": "generateOrderNo 每秒仅 1000 槽位，existsByOrderNo 与 insert 非原子 -> 并发重复单号",
     "verdict": "CONFIRMED",
     "evidence": "并发生成 3000 个订单号（16ms）实测重复 2052 个；DB 上存在 uk_orders_order_no 唯一约束"},
    {"source": "jiuyan-layer-manifest.yaml / svc-catalog",
     "hypothesis": "四个 service 各自复制 normalizePage/normalizePageSize，上限行为不一致",
     "verdict": "FALSIFIED",
     "evidence": "只有 ProductService 与 PackageService 有分页；两个实现逐用例行为完全一致（16 条用例全通过）"},
    {"source": "函数层实测新发现",
     "hypothesis": "（新增）空明细与套餐外明细是集成层现象，函数层应无问题",
     "verdict": "CONFIRMED",
     "evidence": "根因落在 buildPackageSnapshot / normalizePackageItems，函数层单独可见"},
]


def load(name: str):
    path = HERE / name
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return data.get("findings", [])
    return data


def main() -> None:
    observation = json.loads((HERE / "fn_observation.json").read_text())
    declaration = json.loads((HERE / "fn_declaration.json").read_text())
    cases = observation["cases"]

    higher = {
        "service": load("findings.json"),
        "class": load("class_findings.json"),
        "integration": load("integration_findings.json"),
    }

    by_group: dict[str, list[dict]] = {}
    for c in cases:
        by_group.setdefault(c["group"], []).append(c)

    findings = []
    for group, meta in GROUPS.items():
        failed = [c for c in by_group.get(group, []) if c["verdict"] == "FAIL"]
        if not failed:
            continue
        evidence = []
        for c in failed:
            obs = c["observation"]
            snippet = c["reason"]
            if obs.get("upload_dir_state"):
                f = obs["upload_dir_state"][0]
                snippet += f" | 落盘 {f['name']} size={f['size']} head={f.get('magic_ascii')}"
            if obs.get("duplicates") is not None:
                snippet += (f" | generated={obs['generated']} distinct={obs['distinct']}"
                            f" elapsed={obs['elapsed_ms']}ms")
            evidence.append({
                "case_id": c["id"], "function": c["target"],
                "invariant": c["invariant"], "actual": snippet,
                "exception": obs.get("exception_class"),
            })
        related = []
        for layer, ident, relation in meta["related"]:
            if layer == "service" and ident.startswith("test_"):
                related.append({"layer": "service", "id": ident, "relation": relation})
                continue
            hit = any(f.get("id") == ident for f in higher.get(layer, []))
            related.append({"layer": layer, "id": ident, "relation": relation,
                            "already_filed_in_that_layer": hit})
        findings.append({
            "id": meta["id"], "layer": "function", "severity": meta["severity"],
            "title": meta["title"], "function": meta["function"],
            "root_cause": meta["root_cause"],
            "api_reachable": meta["api_reachable"],
            "boundary_protected": meta["boundary_protected"],
            "boundary_note": meta["boundary_note"],
            "cwe": meta["cwe"],
            "owner": f"函数层（{meta['function']}）",
            "case_count": len(failed),
            "evidence": evidence,
            "related_findings": related,
        })

    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 9), f["id"]))

    passed = [c for c in cases if c["verdict"] == "PASS"]
    passed_highlights = []
    for c in passed:
        if c["group"] in ("STATUS_PARSE", "DATE_SCOPE", "PAGINATION", "SNAPSHOT_SEMANTICS",
                          "UPLOAD_CONTENT_NOT_VERIFIED", "ORDER_NO_COLLISION", "PRICE_CLIENT_CONTROLLED"):
            passed_highlights.append({"case_id": c["id"], "function": c["target"],
                                      "invariant": c["invariant"], "actual": c["reason"]})

    payload = {
        "meta": {
            "layer": "function",
            "carrier": "JVM 内反射直调（静态字节码 + 动态属性/边界用例）",
            "functions_declared": declaration["meta"]["functions"],
            "cases": len(cases),
            "failed_cases": len(cases) - len(passed),
            "passed_cases": len(passed),
            "total": len(findings),
            "by_severity": {
                s: sum(1 for f in findings if f["severity"] == s)
                for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            },
            "source_vs_deployed_drift": declaration["meta"]["source_vs_deployed_drift"],
        },
        "findings": findings,
        "passed_checks": passed_highlights,
        "hypotheses_verdict": HYPOTHESES,
    }
    out_json = HERE / "fn_findings.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    md = ["# 函数层缺陷报告（JVM 直调 · 字节码声明 vs 实测行为）", "",
          f"函数总数 {payload['meta']['functions_declared']}，用例 {payload['meta']['cases']} 条，"
          f"失败 {payload['meta']['failed_cases']} 条 / 通过 {payload['meta']['passed_cases']} 条，"
          f"归并为 {len(findings)} 条缺陷。", ""]
    for f in findings:
        md.append(f"## [{f['severity']}] {f['id']} — {f['title']}")
        md.append("")
        md.append(f"- 根因函数：`{f['function']}`")
        md.append(f"- 根因：{f['root_cause']}")
        md.append(f"- HTTP 可达：{'是' if f['api_reachable'] else '否'}"
                  f"（边界情况：{f['boundary_note']}）")
        if f.get("cwe"):
            md.append(f"- CWE: {f['cwe']}")
        md.append(f"- 证据（{f['case_count']} 条实测）：")
        for e in f["evidence"]:
            md.append(f"  - `{e['case_id']}` {e['function']} — 不变量「{e['invariant']}」→ {e['actual']}")
        if f["related_findings"]:
            md.append("- 跨层关联：")
            for r in f["related_findings"]:
                mark = "（高层已开单）" if r.get("already_filed_in_that_layer") else ""
                md.append(f"  - {r['layer']} 层 `{r['id']}`：{r['relation']}{mark}")
        md.append("")
    md.append("## 假设裁决")
    md.append("")
    for h in HYPOTHESES:
        md.append(f"- **{h['verdict']}**（{h['source']}）：{h['hypothesis']} → {h['evidence']}")
    md.append("")
    out_md = HERE / "fn_findings.md"
    out_md.write_text("\n".join(md))

    print(f"函数层差异 -> {out_json} / {out_md}")
    print(f"  缺陷 {len(findings)} 条 " +
          " ".join(f"{k} {v}" for k, v in payload["meta"]["by_severity"].items() if v))
    print(f"  通过用例 {len(passed)} 条，断言亮点 {len(passed_highlights)} 条")
    for f in findings:
        print(f"    {f['severity']:<8} {f['id']:<30} {f['function']}")


if __name__ == "__main__":
    main()
