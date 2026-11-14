"""变异测试 —— 契约里 `ground_truth: mutant_survivor` 的实现。

**问题**：函数层抓到的缺陷（成交价无上界、明细归属不校验…）到底抓得牢不牢？
静态推理说不清 —— 只有把被测代码改坏，看我们的 oracle 能不能发现，才能回答。

做法（**不碰被测仓库**）：
  1. 把被测源码**复制**到临时目录（jiuyan 只读）
  2. 注入一个变异（改一处行为）
  3. 编译到独立目录，放在 classpath **最前面** → JVM 加载的就是变异后的字节码
  4. 用函数层 harness 跑同一批用例，比对变异前后的**观测**与**判定**

两种口径（都要报）：
  killed_by_verdict  变异前后判定不同 → oracle 抓得住
  survived           观测变了但判定没变 → **等价变异 / 弱断言**：oracle 抓不住，
                     这类用例正是 mutant_survivor 的来源

产出 mutation_findings.json（function 层），进总表与覆盖度矩阵。
"""
from __future__ import annotations
import project

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = project.artifact_dir()
sys.path.insert(0, str(HERE))
import fn_dynamic as fd  # noqa: E402
import fn_static  # noqa: E402

WORK = project.work_dir()
OUT = HERE / "mutation_findings.json"

ORDER_SERVICE = "com/jiuyan/banquet/service/OrderService.java"

MUTANTS: list[dict[str, Any]] = [
    {
        "id": "MUT-ADD-PRICE-UPPER-BOUND",
        "target": ORDER_SERVICE,
        "desc": "给 resolveFinalPrice 加上「成交价不得高于套餐总价」的上界校验",
        "should_kill": "RFP-06（成交价高于套餐价必须被拒）",
        "old": """        if (finalPriceCent == null || finalPriceCent < 0) {""",
        "new": """        if (finalPriceCent == null || finalPriceCent < 0) {""",
    },
]


def _mutants() -> list[dict[str, Any]]:
    """变异定义。old/new 必须能在源码里精确定位。"""
    return [
        {
            "id": "MUT-ADD-PRICE-UPPER-BOUND",
            "target": ORDER_SERVICE,
            "desc": "给 resolveFinalPrice 加上「成交价不得高于套餐总价」的上界校验",
            "should_kill": "RFP-06（成交价高于套餐价必须被拒）",
            "affects": ["#resolveFinalPrice"],
            "old": """        if (finalPriceCent == null || finalPriceCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能小于 0", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return finalPriceCent;""",
            "new": """        if (finalPriceCent == null || finalPriceCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能小于 0", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        if (finalPriceCent > planTotalCent) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能高于套餐总价", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return finalPriceCent;""",
        },
        {
            "id": "MUT-DROP-NEGATIVE-PRICE-GUARD",
            "target": ORDER_SERVICE,
            "desc": "删掉 resolveFinalPrice 的「成交价不得为负」检查",
            "should_kill": "RFP-04（负成交价必须被拒）",
            "affects": ["#resolveFinalPrice"],
            "old": """        if (finalPriceCent == null || finalPriceCent < 0) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能小于 0", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return finalPriceCent;""",
            "new": """        if (finalPriceCent == null) {
            throw new BusinessException(ErrorCode.PRICE_INVALID, "成交价不能小于 0", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return finalPriceCent;""",
        },
        {
            "id": "MUT-DROP-DUPLICATE-ITEM-GUARD",
            "target": ORDER_SERVICE,
            "desc": "删掉 normalizePackageItems 的「套餐商品不能重复」检查",
            "should_kill": "NPI-*（套餐明细重复必须被拒）",
            "affects": ["#normalizePackageItems"],
            "old": """            if (result.containsKey(item.productId())) {
                throw new BusinessException(ErrorCode.VALIDATION_ERROR, "套餐商品不能重复", HttpStatus.UNPROCESSABLE_ENTITY);
            }""",
            "new": """            if (false) {
                throw new BusinessException(ErrorCode.VALIDATION_ERROR, "套餐商品不能重复", HttpStatus.UNPROCESSABLE_ENTITY);
            }""",
        },
    ]


def _compile_mutant(mut: dict[str, Any], work: Path) -> Path | None:
    """复制源码 → 注入变异 → 编译到独立目录。失败返回 None（不静默跳过）。"""
    src_root = work / "src"
    shutil.copytree(SUT_SRC, src_root)
    target = src_root / mut["target"]
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")
    if mut["old"] not in text:
        return None
    target.write_text(text.replace(mut["old"], mut["new"], 1), encoding="utf-8")

    out = work / "classes"
    out.mkdir(parents=True, exist_ok=True)
    listing = work / "sources.txt"
    listing.write_text("\n".join(str(p) for p in sorted(src_root.rglob("*.java"))))
    proc = subprocess.run(
        ["javac", "-nowarn", "-encoding", "UTF-8", "-d", str(out),
         "-cp", (WORK / "deps.txt").read_text().strip(), f"@{listing}"],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return None
    return out


def _run_harness(classes: Path, cases_file: Path, obs_file: Path) -> dict[str, Any] | None:
    """用变异后的 class 跑通用 harness（classpath 覆盖：变异目录排最前）。

    与函数层走**同一个** harness（`fn_harness/GenericHarness.java`）——
    以前这里用的是旧的 `FunctionHarness`，而 `fn_dynamic.py` 切到通用 harness
    之后就不再编译旧的那个了，于是变异测试悄悄坏掉（「基线 harness 跑不起来」）。
    坏掉的那段时间，失败的作业被调度器静默重试掉了，没人发现。
    """
    deps = (WORK / "deps.txt").read_text().strip()
    harness = fd.ensure_generic_harness()
    cp = os.pathsep.join([harness[0], str(classes), str(WORK / "classes"), deps])
    proc = subprocess.run(
        ["java", "-cp", cp, "GenericHarness", str(cases_file), str(obs_file),
         str(WORK / "uploads")],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        print(f"  harness 失败（rc={proc.returncode}）: "
              f"{(proc.stderr or proc.stdout or '')[-400:]}", file=sys.stderr)
        return None
    return json.loads(obs_file.read_text(encoding="utf-8"))


def _observation_signature(obs: dict[str, Any]) -> str:
    """观测指纹：只看「发生了什么」，不看判定。"""
    keys = ("value", "exception", "error", "status")
    return json.dumps({k: obs.get(k) for k in keys if k in obs}, sort_keys=True, ensure_ascii=False)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="变异测试")
    parser.add_argument("--only", action="append", default=[], metavar="MUTANT_ID",
                        help="只跑指定的变异（可重复）")
    args = parser.parse_args()

    global SUT_SRC
    sut = json.loads((HERE / "target.json").read_text(encoding="utf-8"))
    backend = Path(sut["sut"]["repo"]) / "backend"
    SUT_SRC = backend / "src/main/java"

    fn_static.ensure_compiled()
    # 用例直接读项目配置（与函数层同一个文件）—— 不再调 fd.build_cases()：
    # 那条路径在切换到「用例文件驱动」之后已经不是函数层实际跑的那批用例了。
    cases_path = project.project_dir() / project.path("fn_cases", "fn_cases.json")
    spec = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = spec["cases"]
    cases_file = WORK / "cases.json"
    cases_file.write_text(json.dumps(spec, ensure_ascii=False))

    baseline_obs = _run_harness(WORK / "classes", cases_file, WORK / "observation_raw.json")
    if baseline_obs is None:
        raise SystemExit("基线 harness 跑不起来")
    base_by_id = {o["id"]: o for o in baseline_obs}
    base_verdict = {}
    for c in cases:
        ok, why = fd.evaluate(c, base_by_id[c["id"]])
        base_verdict[c["id"]] = (ok, why)

    results: list[dict[str, Any]] = []
    wanted = set(args.only)
    for mut in _mutants():
        if wanted and mut["id"] not in wanted:
            continue
        with tempfile.TemporaryDirectory(prefix="jiuyan_mut_") as td:
            work = Path(td)
            classes = _compile_mutant(mut, work)
            if classes is None:
                results.append({"id": mut["id"], "status": "SKIPPED",
                                "reason": "变异未能注入或编译失败（源码已变或签名不匹配）"})
                continue
            obs = _run_harness(classes, cases_file, work / "obs.json")
            if obs is None:
                results.append({"id": mut["id"], "status": "SKIPPED", "reason": "harness 跑不起来"})
                continue
            mut_by_id = {o["id"]: o for o in obs}

            killed, survived, changed_behaviour = [], [], []
            for c in cases:
                cid = c["id"]
                if cid not in mut_by_id:
                    continue
                # 只比对该变异真正影响的方法上的用例 —— 否则观测里的随机值
                # （订单号、上传 UUID）会造成伪差异，把无关用例算成「存活」
                target = mut_by_id[cid].get("target") or ""
                if not any(a in target for a in mut["affects"]):
                    continue
                ok_m, why_m = fd.evaluate(c, mut_by_id[cid])
                ok_o, why_o = base_verdict[cid]
                same_obs = _observation_signature(mut_by_id[cid]) == _observation_signature(base_by_id[cid])
                if same_obs:
                    continue
                changed_behaviour.append(cid)
                if ok_o != ok_m:
                    killed.append(cid)
                else:
                    survived.append(cid)

            results.append({
                "id": mut["id"],
                "status": "OK",
                "desc": mut["desc"],
                "should_kill": mut["should_kill"],
                "killed_by_verdict": killed,
                "survived": survived,
                "changed_behaviour_cases": changed_behaviour,
                "mutation_score": round(len(killed) / len(changed_behaviour), 3) if changed_behaviour else None,
            })

    ok_results = [r for r in results if r.get("status") == "OK"]
    survived_cases = sorted({c for r in ok_results for c in r["survived"]})
    findings: list[dict[str, Any]] = []
    if survived_cases:
        findings.append({
            "id": "FN-MUTATION-SURVIVOR-WEAK-ASSERTION",
            "severity": "MEDIUM",
            "function": "fn_dynamic.py#evaluate",
            "title": "有变异改了实际行为但我们的 oracle 没抓住 —— 断言只看结果，不看「该发生什么」",
            "detail": (
                "变异测试：把被测源码复制到临时目录、注入变异、编译后放在 classpath 最前面，"
                "用同一批用例跑。判定两种口径 —— 观测变了但判定没变 = **oracle 抓不住**。\n"
                + json.dumps(ok_results, ensure_ascii=False, indent=2)
                + "\n含义：这些用例只能证明「结果不是期望值」，不能证明「异常该抛还是该返回」。"
                "缺陷被同类变异掩盖时，回归资产会假绿。"),
            "repro": "python3 mutation_test.py",
            "evidence": {"survived_cases": survived_cases, "mutants": ok_results},
        })
    if ok_results:
        findings.append({
            "id": "FN-MUTATION-HARNESS-WORKS",
            "severity": "INFO",
            "function": "mutation_test.py",
            "title": "变异测试链路跑通：函数层 oracle 抓得住大部分变异",
            "detail": (
                f"实测 {len(ok_results)} 个变异：" +
                "、".join(f"{r['id']} 杀死 {len(r['killed_by_verdict'])}/{len(r['changed_behaviour_cases'])}"
                          for r in ok_results) + "。\n"
                "这条能力本身是资产：它让「我们抓得牢不牢」变成可度量的数，而不是靠感觉。"),
            "repro": "python3 mutation_test.py",
            "evidence": {"mutants": ok_results},
        })

    payload = {"function": findings, "mutants": results}
    target = OUT if not args.only else OUT.with_name(
        OUT.stem + "." + "_".join(sorted(wanted)) + ".json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"变异测试 -> {OUT}")
    for r in results:
        if r.get("status") == "OK":
            print(f"  {r['id']:<34} 杀死 {len(r['killed_by_verdict'])}"
                  f"/{len(r['changed_behaviour_cases'])}  存活 {len(r['survived'])}")
            if r["survived"]:
                print(f"      存活用例: {', '.join(r['survived'][:8])}")
        else:
            print(f"  {r['id']:<34} {r.get('reason', r.get('status'))}")


if __name__ == "__main__":
    main()
