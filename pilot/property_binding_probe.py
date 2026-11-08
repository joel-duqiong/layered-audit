"""属性绑定探针（项目侧）。

**同一个属性可以有多种绑定形式**（环境变量、宽松绑定的变体、SPRING_APPLICATION_JSON、
系统属性）。它们优先级不同，而**部署的人通常只知道其中一种** —— 于是「我明明设了
FILE_UPLOAD_DIR，怎么没生效」这类事故就是这么来的。

本探针的做法：对每个属性，把各种形式**同时**设成不同的可区分值，然后**观察实际生效的是
哪个**（行为观测，不是读配置）。观察不出来的属性记为「不可判定」，不猜。

零残留：探针容器用完即删（ProbeInstance）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import project

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_instance import ProbeInstance  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "property_binding_findings.json"
SCENARIOS_FILE = HERE / "property_binding_scenarios.json"

# 观察方式：怎么看出「实际生效的值」是哪一个
OBSERVERS: dict[str, Any] = {}


def observe_upload_dir_error_path(base: str, candidates: list[str]) -> str:
    """上传到不可写的目录 -> 错误响应里会泄漏真实路径 -> 路径里带着生效的值。"""
    import requests
    try:
        resp = requests.post(f"{base}/api/uploads/product-image",
                             files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                             timeout=20)
        text = resp.text
    except requests.RequestException as exc:
        return f"请求失败: {exc}"
    for value in candidates:
        if value and value in text:
            return value
    return ""


def observe_cors_allow_origin(base: str, candidates: list[str]) -> str:
    """CORS 的允许来源直接出现在响应头里。

    做法：把候选值**轮流当作请求的 Origin**发出去 —— 哪个被回显，哪个就是生效的值。
    """
    import requests
    for value in candidates:
        if not value:
            continue
        try:
            resp = requests.get(f"{base}/api/products",
                                headers={"Origin": value}, timeout=20)
        except requests.RequestException:
            continue
        got = resp.headers.get("Access-Control-Allow-Origin", "")
        if got and value in got:
            return value
    return ""


OBSERVERS["upload_dir_error_path"] = observe_upload_dir_error_path
OBSERVERS["cors_allow_origin"] = observe_cors_allow_origin


def _forms(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """这个属性有哪些绑定形式（形式本身是 Spring Boot 的语义，通用）。"""
    canonical = spec.get("canonical_env")
    relaxed = spec.get("relaxed_env")
    out: list[dict[str, Any]] = []
    if canonical:
        out.append({"kind": "canonical_env", "env": {canonical: "{value}"},
                    "label": f"环境变量 {canonical}"})
    if relaxed and relaxed != canonical:
        out.append({"kind": "relaxed_env", "env": {relaxed: "{value}"},
                    "label": f"环境变量 {relaxed}（宽松绑定变体）"})
        if canonical:
            out.append({"kind": "both_env",
                        "env": {canonical: "{value_canonical}", relaxed: "{value_relaxed}"},
                        "label": f"两个环境变量同时设（{canonical} vs {relaxed}）"})
    prop = spec.get("property")
    if prop:
        out.append({"kind": "spring_json", "json": {prop: "{value}"},
                    "label": "SPRING_APPLICATION_JSON"})
        out.append({"kind": "sysprop", "sysprop": prop, "label": f"系统属性 -{prop}"})
    return out


def run_property(spec: dict[str, Any]) -> dict[str, Any]:
    """跑一个属性的各种绑定形式，看实际生效的是哪个。"""
    observer = OBSERVERS.get(spec.get("observer") or "")
    if observer is None:
        return {"property": spec.get("property"), "skipped": f"未知观察方式 {spec.get('observer')}"}
    base_value = spec.get("probe_value") or "/proc/self/nonexistent-dir"
    forms = _forms(spec)
    results: list[dict[str, Any]] = []
    for i, form in enumerate(forms):
        env = {k: v.replace("{value}", f"{base_value}-canon")
                  .replace("{value_canonical}", f"{base_value}-canon")
                  .replace("{value_relaxed}", f"{base_value}-relaxed")
               for k, v in (form.get("env") or {}).items()}
        json_env: dict[str, Any] | None = None
        if form.get("json"):
            json_env = {k: str(v).replace("{value}", f"{base_value}-canon")
                        .replace("{value_canonical}", f"{base_value}-canon")
                        .replace("{value_relaxed}", f"{base_value}-relaxed")
                        for k, v in form["json"].items()}
        jto = None
        if form.get("sysprop"):
            jto = f"-D{form['sysprop']}={base_value}-canon"
        name = f"jiuyan-bind-{i}-{spec.get('property', 'x').replace('.', '-')}"[:60]
        # 每种形式**单独**观测：把源容器继承来的同名环境变量剔掉，
        # 否则继承的那个永远赢，其它形式永远轮不到 —— 那样测的不是优先级，是继承。
        drop = tuple(spec.get("drop_env") or [])
        # SPRING_APPLICATION_JSON 必须**在容器启动前**进 env ——
        # 容器起来之后再设 os.environ 是没用的（那是本进程的环境，不是容器的）。
        if json_env:
            env = dict(env)
            env["SPRING_APPLICATION_JSON"] = json.dumps(json_env, ensure_ascii=False)
        # 候选值**独立于注入方式**：JSON 与系统属性那两档的值根本不在环境变量里，
        # 拿 env 的值当候选会永远观测不到。
        candidates = sorted({v for v in env.values() if v and not v.startswith("{")})
        if json_env:
            candidates += [str(v) for v in json_env.values()]
        candidates.append(f"{base_value}-canon")
        candidates.append(f"{base_value}-relaxed")
        candidates = sorted(set(candidates))
        try:
            with ProbeInstance(name, env_overrides=env,
                               java_tool_options=jto,
                               default_env={},
                               drop_env=drop) as inst:
                observed = observer(inst.base, candidates)
                results.append({"kind": form["kind"], "label": form["label"],
                                "env": env, "observed": observed})
        except Exception as exc:                       # 探针起不来 = 该形式无法判定
            results.append({"kind": form["kind"], "label": form["label"],
                            "env": env, "observed": "", "error": str(exc)[:200]})
        finally:
            pass
    return {"property": spec.get("property"), "results": results}


def main() -> None:
    cfg = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    specs = cfg.get("properties", [])
    findings: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for spec in specs:
        res = run_property(spec)
        observations.append(res)
        winners = [r for r in res.get("results", []) if r.get("observed")]
        if not winners:
            continue
        winner = winners[0]
        # 「两个环境变量同时设」这一档才有意义：谁赢了是不是直觉里的那个
        both = next((r for r in res.get("results", []) if r["kind"] == "both_env"), None)
        if both and both.get("observed"):
            canonical = spec.get("canonical_env")
            relaxed = spec.get("relaxed_env")
            relaxed_won = "relax" in str(both["observed"])
            if relaxed_won:
                findings.append({
                    "id": "CFG-BINDING-PRIORITY-SURPRISE",
                    "severity": "MEDIUM",
                    "endpoint": "",
                    "title": (f"{spec.get('property')} 的绑定优先级与直觉相反："
                              f"{relaxed} 赢了 {canonical}"),
                    "detail": (
                        f"同一个属性 `{spec.get('property')}` 有两种环境变量写法："
                        f"`{canonical}`（配置里占位符引用的那个）与 `{relaxed}`"
                        f"（Spring 宽松绑定的变体）。**两个同时设上，后者赢**。\n"
                        f"实测生效的值：{both['observed']}\n"
                        "部署的人按 `{canonical}` 想事情，那个名字**根本不生效** —— "
                        "而且没有任何报错，配置静默取到了另一个值。"
                        "这类问题静态看代码看不出来（配置里只写了占位符），"
                        "只有真的把两种形式都设上、跑一次才知道。"),
                    "repro": "python3 property_binding_probe.py",
                    "evidence": {"property": spec.get("property"),
                                 "canonical_env": canonical,
                                 "relaxed_env": relaxed,
                                 "both_forms": both,
                                 "all_forms": res.get("results", [])},
                })

    payload = {"service": findings,
               "results": observations,
               "properties": len(specs)}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"属性绑定探针 -> {OUT}")
    for obs in observations:
        for r in obs.get("results", []):
            print(f"  {obs.get('property'):28s} {r.get('label'):34s} 生效={r.get('observed') or '—'}")
    print(f"  发现 {len(findings)} 条")


if __name__ == "__main__":
    main()
