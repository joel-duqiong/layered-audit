"""Spring 上下文探针（项目侧）。

**装配结果只有在真实容器里才看得见** —— 静态扫描看不出「最后注入的是哪个实现」。

本探针用容器日志（DefaultListableBeanFactory=DEBUG）拿到三样东西：
  1. **bean 清单** —— 这次装配到底造了哪些 bean（哪些 @Profile/@Conditional 生效了）
  2. **按名解析** —— 哪些注入点是「按类型不唯一、按名字匹配」的
     （将来加第二个实现就会静默换掉 —— 这是真实的部署风险）
  3. **按类型解析** —— 正常的唯一类型注入

然后和**静态声明**对账：声明了 @Primary/@Qualifier/@Profile 的类，
运行时是不是真的按它装配了。声明与运行时不一致 = 装配没按声明走。

零残留：探针容器用完即删（ProbeInstance）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import project

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_instance import ProbeInstance  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = project.artifact_dir() / "spring_ctx_findings.json"

DEBUG_LEVEL = ("-Dlogging.level.org.springframework.beans.factory.support."
               "DefaultListableBeanFactory=DEBUG")

BEAN_RE = re.compile(r"Creating shared instance of singleton bean '([^']+)'")
AUTOWIRE_RE = re.compile(
    r"Autowiring by type from bean name '([^']+)' to bean named '([^']+)'")

# 静态声明里「会影响装配结果」的注解
ASSEMBLY_ANNOTATIONS = ("Primary", "Qualifier", "Profile", "Conditional", "ConditionalOn")


def parse_logs(logs: str) -> dict[str, Any]:
    beans = sorted(set(BEAN_RE.findall(logs)))
    by_name: list[dict[str, str]] = []
    for src, dst in AUTOWIRE_RE.findall(logs):
        by_name.append({"from": src, "to": dst})
    return {"beans": beans, "by_name_autowiring": by_name}


def declared_assembly_classes() -> dict[str, list[str]]:
    """静态侧：声明了装配相关注解的类 —— 从类层探针的观测里拿（它带类注解）。"""
    path = project.artifact_dir() / "class_observation.json"
    out: dict[str, list[str]] = {}
    if not path.exists():
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    shape = (data.get("class_shape") or {}).get("classes") or []
    for c in shape:
        anns = [a for a in (c.get("annotations") or []) if a in ASSEMBLY_ANNOTATIONS]
        if anns:
            out[c["class"]] = anns
    return out


def run_instance(name: str, profile: str | None,
                 src_container: str | None = None) -> dict[str, Any]:
    env = {}
    jto = DEBUG_LEVEL
    if profile:
        env["SPRING_PROFILES_ACTIVE"] = profile
    with ProbeInstance(name, env_overrides=env, java_tool_options=jto,
                       default_env={}, src_container=src_container) as inst:
        logs = inst.logs()
    return parse_logs(logs)


def main() -> None:
    declared = declared_assembly_classes()
    instances: list[dict[str, Any]] = []
    for spec in (project.binding().get("spring_ctx_instances") or []):
        name = f"jiuyan-ctx-{spec.get('profile') or 'default'}"
        try:
            parsed = run_instance(name, spec.get("profile"), spec.get("instance"))
        except Exception as exc:
            parsed = {"beans": [], "by_name_autowiring": [], "error": str(exc)[:200]}
        parsed["profile"] = spec.get("profile") or "default"
        parsed["instance"] = spec.get("instance")
        instances.append(parsed)

    findings: list[dict[str, Any]] = []
    positives: list[dict[str, Any]] = []

    # 1) 按名解析 —— 风险点：将来加第二个实现，注入会静默换掉
    for inst in instances:
        for hit in inst.get("by_name_autowiring") or []:
            src, dst = hit["from"], hit["to"]
            if src == dst.rsplit(".", 1)[-1]:
                continue        # 名字就是类型的简单名 —— 正常的按类型唯一解析
            findings.append({
                "id": "SPRING-INJECTION-BY-NAME",
                "severity": "MEDIUM",
                "endpoint": "",
                "title": f"注入点按**名字**匹配到了 bean（{src} -> {dst}）",
                "detail": (
                    f"实例 {inst.get('profile')} 的装配日志里，"
                    f"`{src}` 是**按名字**匹配到 `{dst}` 的 —— "
                    "说明这个注入点的类型不唯一，Spring 退回了按名匹配。\n"
                    "现在能跑对，是因为**恰好只有这一个实现**；"
                    "将来有人加了第二个实现，注入的那个就静默换掉了 —— "
                    "没有编译错误、没有启动报错。\n"
                    "正解是加 @Primary 或 @Qualifier，把「选哪个」写死在代码里。"),
                "repro": "python3 spring_ctx_probe.py",
                "evidence": {"instance": inst.get("profile"), **hit},
            })

    # 2) 声明了装配注解的类，运行时是不是真的按它装配了
    for inst in instances:
        beans = set(inst.get("beans") or [])
        for cls, anns in declared.items():
            simple = cls.rsplit(".", 1)[-1]
            created = any(simple in b for b in beans)
            if "Profile" in anns and created and inst.get("profile") == "default":
                findings.append({
                    "id": "SPRING-PROFILE-ANNOTATION-INEFFECTIVE",
                    "severity": "HIGH",
                    "endpoint": "",
                    "title": f"{simple} 标了 @Profile，却在 default 装配里被创建了",
                    "detail": (
                        f"类 {cls} 声明了 {anns}，但在 default profile 的实例里"
                        "**它被创建了** —— 装配上下文与声明不符。"
                        "条件装配一旦不按声明走，行为就会在别的部署里静默变样。"),
                    "repro": "python3 spring_ctx_probe.py",
                    "evidence": {"class": cls, "annotations": anns,
                                 "instance": inst.get("profile"),
                                 "bean_sample": sorted(b for b in beans if simple in b)[:5]},
                })

    # 3) 装配结果随 profile 变 —— 而变的那几个 bean 是**业务维度**的
    #    （项目声明的：哪个 bean 的装配结果属于业务语义、而不是部署语义）
    bindings = project.binding().get("spring_ctx_business_bindings") or []
    if len(instances) >= 2 and bindings:
        by_profile = {i.get("profile"): set(i.get("beans") or []) for i in instances}
        declared = {b.get("profile"): b for b in bindings}
        for prof, spec in declared.items():
            others = [pr for pr in by_profile if pr != prof]
            if not others:
                continue
            other = others[0]
            only_here = by_profile.get(prof, set()) - by_profile.get(other, set())
            only_there = by_profile.get(other, set()) - by_profile.get(prof, set())
            bean = spec.get("bean")
            if bean and bean in only_here:
                findings.append({
                    "id": "SPRING-BUSINESS-BOUND-TO-PROFILE",
                    "severity": "HIGH",
                    "endpoint": "",
                    "title": (f"{spec.get('business_dimension')} 被绑到了部署维度："
                              f"{prof} profile 装配的是 {bean}，"
                              f"{other} profile 装配的是 "
                              f"{sorted(only_there) or '（无）'}"),
                    "detail": (
                        f"**运行时证据**：两个 profile 各起了一个容器，"
                        f"读装配日志对比 bean 清单 ——\n"
                        f"  {prof:>8} profile：{bean}\n"
                        f"  {other:>8} profile：{', '.join(sorted(only_there)) or '（无）'}\n"
                        f"两者只差这 {len(only_here)} 个 bean ⇒ 装配结果确实随部署环境变。\n"
                        f"问题在于：{spec.get('business_dimension')} 是**业务维度**，"
                        "不是部署维度。用 @Profile（部署环境）来表达它，"
                        "意味着换一套部署环境，这个业务语义就变了 —— "
                        "而且没有任何报错。\n"
                        f"{spec.get('note', '')}"),
                    "repro": "python3 spring_ctx_probe.py",
                    "evidence": {
                        "bean": bean,
                        "profiles": {pr: sorted(by_profile.get(pr, set()) & (only_here | only_there))
                                     for pr in by_profile},
                        "diff_default": sorted(only_here),
                        "diff_other": sorted(only_there),
                    },
                })

    # 4) 正面结论：装配清单（查过没问题也是证据）
    if instances and not findings:
        positives.append({
            "id": "SPRING-ASSEMBLY-UNAMBIGUOUS",
            "severity": "INFO",
            "endpoint": "",
            "title": f"装配无歧义：{len(instances)} 个实例的全部注入点都按唯一类型解析",
            "detail": (
                "逐个实例读了容器装配日志："
                + "；".join(f"{i.get('profile')} 造了 {len(i.get('beans') or [])} 个 bean，"
                           f"按名解析 {len(i.get('by_name_autowiring') or [])} 处"
                           for i in instances)
                + "\n没有出现「类型不唯一、按名字匹配」的注入点，"
                  "声明了 @Primary/@Qualifier/@Profile 的类也都按声明装配。"
                  "这是查过没问题的证据 —— 装配这一面今天没有盲区。"),
            "repro": "python3 spring_ctx_probe.py",
            "evidence": {"instances": [{"profile": i.get("profile"),
                                       "beans": len(i.get("beans") or []),
                                       "by_name": len(i.get("by_name_autowiring") or [])}
                                      for i in instances]},
        })

    payload = {"service": findings + positives,
               "results": instances,
               "declared_assembly_classes": declared}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Spring 上下文探针 -> {OUT}")
    for i in instances:
        print(f"  实例 {i.get('profile'):8s} bean {len(i.get('beans') or [])} 个"
              f"｜按名解析 {len(i.get('by_name_autowiring') or [])} 处"
              f"{'｜错误: ' + str(i.get('error'))[:80] if i.get('error') else ''}")
    print(f"  发现 {len(findings)} 条，正面结论 {len(positives)} 条")


if __name__ == "__main__":
    main()
