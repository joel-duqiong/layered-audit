"""函数层 · numeric 类别的 property_based_test 探针。

类别 desc：溢出 / 截断 / 单位换算错误。oracle：property_based_test。

跑的是前端**线上同款**模块的钱相关函数（Node 直接 import src/api/banquetApi.js），
以及 App.jsx 里那个金额输入规范化函数（从 shipped 源码抽出来直接执行，不是复刻）。
随机数用固定种子 → 每次同一批输入 → 结果可比对、可复现。

产出 numeric_findings.json（function 层），直接进总表与覆盖度矩阵。
"""
from __future__ import annotations
import project

import json
import os
import subprocess
from pathlib import Path
from typing import Any

HERE = project.artifact_dir()
PROBE = HERE / "numeric_probe.mjs"
OUT = HERE / "numeric_findings.json"
REGISTER = HERE / "fe_register.mjs"


def run_probe() -> dict[str, Any]:
    proc = subprocess.run(
        ["node", "--import", "./fe_register.mjs", "numeric_probe.mjs"],
        capture_output=True, text=True, timeout=120, cwd=str(HERE))
    if proc.returncode != 0:
        raise RuntimeError(f"numeric probe 失败: {proc.stderr.strip()[-500:]}")
    return json.loads(proc.stdout)


def build_findings(res: dict[str, Any]) -> dict[str, Any]:
    np = res["NP_money_input_normalization"]
    src = res.get("sources", {})
    fp4 = res["FP4_large_value_precision"]
    positives = [k for k in ("FP1_roundtrip", "FP2_display_precision", "FP3_yuan_to_cent")
                 if res[k]["ok"]]
    failed = [k for k in ("FP1_roundtrip", "FP2_display_precision", "FP3_yuan_to_cent")
              if not res[k]["ok"]]

    findings: list[dict[str, Any]] = []

    if np["sign_failures"] or np["truncation_failures"]:
        findings.append({
            "id": "FN-NUMERIC-MONEY-INPUT-SILENT-MUTATION",
            "severity": "LOW",
            "function": "App.jsx#normalizeMoneyInput",
            "title": "金额输入框静默改变用户输入的数值：负号被吞、超过 2 位小数被截断",
            "detail": (
                "normalizeMoneyInput 是所有金额输入框的唯一入口（成交价 / 定金 / 套餐价 / "
                "商品价共 6 处调用）。它把非数字字符全删掉、把小数截到 2 位，两处都**静默**："
                "用户看到输入框里显示的就是规范化后的值，没有任何提示说原来的输入被改过。\n"
                f"负号被吞：{json.dumps(np['sign_failures'], ensure_ascii=False)}\n"
                "  → 输入 -19.99，框里变成 19.99，存进库也是 +19.99。金额方向被反转。\n"
                f"截断：{json.dumps(np['truncation_failures'], ensure_ascii=False)}\n"
                "  → 输入 19.995，四舍五入应是 2000 分，系统存 1999 分；输入 0.005 变成 0.00，"
                "半分钱直接归零。\n"
                "性质：不是「显示与存储不一致」（框里显示多少就存多少），而是"
                "「存储值与用户输入不一致，且无任何反馈」。"),
            "repro": "python3 numeric_props.py",
            "location": {"file": np["source_file"], "line": np.get("source_line", 0),
                         "symbol": np.get("symbol", "normalizeMoneyInput"), "endpoint": ""},
            "evidence": {
                "source_file": np["source_file"],
                "source_line": np.get("source_line"),
                "sign_failures": np["sign_failures"],
                "truncation_failures": np["truncation_failures"],
                "shape_rows": np["shape_rows"],
                "call_sites": ["App.jsx:1840 dealPrice", "App.jsx:1895 orderForm.deposit",
                                "App.jsx:2262 packageForm.price", "App.jsx:2487 form.amount",
                                "App.jsx:2493 form.deposit", "App.jsx:2662 productForm.price"],
            },
        })

    if not failed:
        findings.append({
            "id": "FN-NUMERIC-FE-MONEY-ROUNDTRIP-EXACT",
            "severity": "INFO",
            "function": "banquetApi.js#yuanToCent / centToYuan",
            "title": "前端金额换算在合理量级内精确闭合（分→元→分、元→分、显示精度三条性质全过）",
            "detail": (
                f"确定性随机性质测试，种子固定，每次同一批输入："
                f"{json.dumps(positives, ensure_ascii=False)} 各 2000 例全过。\n"
                "· FP1 分→元→分往返闭合：yuanToCent(centToYuan(x)) === x\n"
                "· FP2 显示精度：centToYuan(x).toFixed(2) 等于 x 分的精确十进制展开\n"
                "· FP3 元→分：yuanToCent(y) === Math.round(y * 100)\n"
                "这条是正面结论：金额换算本身没有精度 bug，问题不在换算，在输入规范化（见 "
                "FN-NUMERIC-MONEY-INPUT-SILENT-MUTATION）与金额上界缺失。"),
            "repro": "python3 numeric_props.py",
            "location": {"file": (src.get("centToYuan") or {}).get("file", ""),
                         "line": (src.get("centToYuan") or {}).get("line", 0),
                         "symbol": "yuanToCent / centToYuan", "endpoint": ""},
            "evidence": {
                "properties_passed": positives,
                "sources": src,
                "cases_per_property": 2000,
                "seed": 20261107,
                "upper_bound_cases": "0..10^12 分",
            },
        })

    if fp4.get("samples"):
        lost = [s for s in fp4["samples"] if s["lost_cent"] != "0"]
        if lost:
            findings.append({
                "id": "FN-NUMERIC-BIGINT-PRECISION-LOSS",
                "severity": "LOW",
                "function": "banquetApi.js#centToYuan",
                "title": "金额超过 2^53 分时，前端显示的金额与库里存的不一致（后端金额无上界是前提）",
                "detail": (
                    "后端对金额只查下界（@Min(0) + CHECK >= 0），没有上界，BIGINT 能存到 "
                    "9.22e18 分。前端用 JS Number 装分值，超过 2^53（约 9007 万亿元）后就装不下每一个整数。\n"
                    + "\n".join(json.dumps(s, ensure_ascii=False) for s in lost)
                    + "\n后端能存进去，前端显示的是另一个数 —— 库里和界面上的金额对不上。\n"
                    "这是平台级事实（IEEE754 双精度装不下每一个 int64），不依赖任何业务预期；"
                    "「金额无上界」只是可达性前提。归属与根因由 finding_meta.py 单一声明，"
                    "本文案不再复述，避免两处口径漂移。"),
                "repro": "python3 numeric_props.py",
                "location": {"file": (src.get("centToYuan") or {}).get("file", ""),
                             "line": (src.get("centToYuan") or {}).get("line", 0),
                             "symbol": "centToYuan", "endpoint": ""},
                "evidence": {
                    "samples": fp4["samples"],
                    "prerequisite": "后端金额无上界（@Min(0) + CHECK >= 0，无上界）",
                    "realistic_impact": "需要荒谬金额才触发，属于边界缺失而非业务风险",
                },
            })

    return {"function": findings}


def main() -> None:
    res = run_probe()
    payload = build_findings(res)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"numeric 探针 -> {OUT}  {len(payload['function'])} 条发现")
    for f in payload["function"]:
        print(f"  {f['severity']:<9}{f['id']:<46}{f['title'][:50]}")


if __name__ == "__main__":
    main()
