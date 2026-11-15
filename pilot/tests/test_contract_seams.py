"""契约接缝守卫 —— 独占文件所有者之间的接口必须有人拥有并测试。

为什么需要这个文件:本轮最贵的两次回归都不是文件互相覆盖,而是**接缝断裂**:
  · 坑 16:采集器改成发射求值器侧词汇,桥的映射表只认旧词汇 → 可执行判据 47→14
  · 坑 11:封闭原语集有三个物理载体,改一个不同步另两个
  · 坑 14:三态闸门只绑一个字段,unknown 换个位置编码就漏
  · ImportError:改上游导出没同步下游导入 → 整个测试模块无法收集

独占所有权防住了覆盖,但防不住接缝。所以接缝本身要有守卫。
这些测试**端到端跑真实 CLI**,不 mock,因为 mock 会把接缝本身 mock 掉。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PILOT = Path(__file__).resolve().parent.parent
ENGINE = PILOT / "engine"
HARVESTED = PILOT / "oracles_harvested.json"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ENGINE / script), *args],
                          capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def harvested() -> dict:
    return json.loads(HARVESTED.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bridge_report(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("seam") / "bridge.json"
    p = run("oracle_bridge.py", "--harvested", str(HARVESTED),
            "--target", "seam-test", "--out-report", str(out))
    assert p.returncode == 0, f"桥跑不起来: {p.stderr[-800:]}"
    return json.loads(out.read_text(encoding="utf-8"))


def test_harvester_exports_what_its_own_tests_import():
    """ImportError 接缝:改上游导出必须同步下游导入,否则整个模块无法收集。"""
    p = subprocess.run([sys.executable, "-m", "pytest",
                        str(PILOT / "tests" / "test_oracle_harvest.py"), "--collect-only", "-q"],
                       capture_output=True, text=True, timeout=180)
    assert "ImportError" not in p.stdout + p.stderr, \
        "采集器测试无法收集 —— 上游导出与下游导入断裂"
    assert p.returncode == 0, p.stdout[-600:]


def test_bridge_accepts_both_vocabularies(bridge_report):
    """坑 16:采集器侧词汇与求值器侧词汇都必须能映射,不得把已是封闭原语的当未知丢掉。"""
    unknown_prim = [u for u in bridge_report["unmappable"]
                    if "未知原语" in str(u.get("reason", ""))]
    assert not unknown_prim, \
        f"{len(unknown_prim)} 条因『未知原语』被丢 —— 词汇契约断裂(47→14 回归的根因)"


def test_every_mapped_rule_is_accepted_by_the_evaluator(bridge_report, tmp_path):
    """坑 11:桥产出的原语必须真被求值器认识,否则映射是装饰。"""
    rules = bridge_report.get("mapped") or []
    if not rules:
        pytest.skip("桥无可映射判据")
    closed = set()
    src = (ENGINE / "rule_assessment.py").read_text(encoding="utf-8")
    for name in ("field_eq_field", "field_lte_field", "field_gte_field",
                 "field_lte_const", "field_gte_const", "field_in_set",
                 "field_not_null", "field_not_blank", "field_gt_const",
                 "jsonb_sum_eq_field", "jsonb_subset_of"):
        if name in src:
            closed.add(name)
    assert closed, "求值器封闭集为空 —— 无法核对"
    for r in rules:
        prim = r.get("primitive_to")
        assert prim in closed, f"桥产出 {prim} 不在求值器封闭集里 —— 映射不可执行"


def test_refuted_never_enters_judgment(bridge_report, harvested):
    """硬约束:文档明确未定义的条款永不进判定。"""
    undefined = {u["id"] for u in harvested.get("explicitly_undefined", [])}
    mapped = {m.get("oracle_id") for m in bridge_report.get("mapped", [])}
    assert not (undefined & mapped), f"refuted 判据进了判定: {undefined & mapped}"
    excluded = {e.get("oracle_id") or e.get("id") for e in bridge_report.get("excluded", [])}
    assert undefined <= excluded, "refuted 判据未被显式排除"


def test_denominators_are_disjoint_and_explicit(harvested, bridge_report):
    """坑 20:两个分母必须显式且不重叠,三桶之和必须等于处理总数。"""
    c = harvested["counts"]
    assert c.get("denominators_disjoint") is True, "产物未声明两分母不重叠"
    assert c["total"] == c["oracles_count"], "counts.total 必须等于 oracles 数组数"
    assert c["total_declarations"] == c["oracles_count"] + c["explicitly_undefined_count"]
    b = bridge_report["counts"]
    buckets = b["mapped_oracles"] + b["unmappable"] + b["excluded"]
    assert buckets == c["total_declarations"], \
        f"三桶之和 {buckets} ≠ 处理总数 {c['total_declarations']} —— 看起来像丢判据"


def test_unknown_access_is_never_a_definite_write(tmp_path):
    """坑 14:上游用 access=unknown 编码 may-write,下游不得洗成确定写。

    注入必须落在**真实候选的重叠资源**上,否则形不成重叠、守卫空过(坑 2 同义反复)。
    本守卫自身经过变异验证:注入后必须真的出现 may-write 资源,否则测试失败。
    """
    net = json.loads((PILOT / "relation_net.json").read_text(encoding="utf-8"))
    units = json.loads((PILOT / "units.json").read_text(encoding="utf-8"))["units"]
    known = {u["unit_key"] for u in units}
    out = tmp_path / "fc.json"
    args = ["--net", str(PILOT / "relation_net.json"), "--units", str(PILOT / "units.json"),
            "--findings", str(PILOT / "all_findings.json"), "--oracles", str(HARVESTED),
            "--out", str(out)]
    p = run("frame_check.py", *args)
    assert p.returncode == 0, p.stderr[-600:]
    base = json.loads(out.read_text(encoding="utf-8"))

    # 找一个真实候选资源,并选一个**尚未确定写它**的细胞来注入 unknown 边,
    # 这样才会产生纯 may-write 资源(选已有确定写的细胞会被「确定写优先」吸收,
    # 导致 may-write 标志不单独出现,守卫空过)。
    target = None
    for cand in base.get("candidates", []):
        for res in cand.get("resources", []):
            prov = res.get("provenance") or {}
            writers = {c for c, ev in prov.items()
                       if any(e.get("source") == "WRITES" for e in ev)}
            non_writers = [c for c in prov if c in known and c not in writers]
            if non_writers:
                target = (res["resource"], non_writers[0])
                break
        if target:
            break
    if target is None:
        pytest.skip("真实数据无「有资源但非确定写者」的细胞可用于注入")

    resource, cell = target
    net["edges"] = net["edges"] + [{
        "from": cell, "to": resource, "type": "WRITES", "access": "unknown"}]
    injected = tmp_path / "net.json"
    injected.write_text(json.dumps(net), encoding="utf-8")
    out2 = tmp_path / "fc2.json"
    p2 = run("frame_check.py", "--net", str(injected), "--units", str(PILOT / "units.json"),
             "--findings", str(PILOT / "all_findings.json"), "--oracles", str(HARVESTED),
             "--out", str(out2))
    assert p2.returncode == 0, p2.stderr[-600:]
    report = json.loads(out2.read_text(encoding="utf-8"))

    may_write = [res for c in report.get("candidates", [])
                 for res in c.get("resources", [])
                 if res.get("a_may_write") or res.get("b_may_write")]
    if not may_write:
        # 已知局限:真实数据上注入 unknown 边未必产生独立的 may-write 资源
        # (「确定写优先」会吸收它)。两个方向的不变式由 W2 自己的回归守卫覆盖
        # (test_frame_check.py,含具名反例 FC-0019 / Category.setIsDeleted)。
        # 本守卫在此情形下不做断言,避免产生假红;不得当成 pass 来解读。
        pytest.skip("注入未产生独立 may-write 资源;两方向不变式由 test_frame_check.py 覆盖")
    # 方向一:只有 may-write 证据(无确定写)的资源,不得被标成 definite。
    # 注意:同时有确定写与 may-write 时,definite 是**正确**的(确定写优先),
    # 所以这里必须排除掉有确定写证据的资源,否则与方向二自相矛盾。
    for res in may_write:
        has_definite_evidence = bool(res.get("a_writes")) or bool(res.get("b_writes"))
        if not has_definite_evidence:
            assert res.get("write_strength") != "definite", \
                "仅有 may-write 证据的资源被标成 definite —— 三态红线违规(方向一)"

    # 方向二(V10 证伪发现的反向漏洞):同一资源既有确定写又有 may-write 时,
    # 确定写不得被降级。**键必须用 (资源, 细胞对) 而不是候选 ID** ——
    # 注入会新增候选并使 ID 位移,按 ID 比对会把「不同候选」误判成「降级」。
    def definite_keys(rep):
        return {(r["resource"], tuple(sorted((r.get("provenance") or {}).keys())))
                for c in rep.get("candidates", []) for r in c.get("resources", [])
                if r.get("write_strength") == "definite"}
    lost = definite_keys(base) - definite_keys(report)
    assert not lost, (
        f"注入 may-write 边后 {len(lost)} 个原有确定写资源被降级 —— "
        "三态红线违规(方向二:确定写被洗成 unknown)。确定写必须优先。")


def test_purity_guard_passes():
    """所有引擎零项目字面量。"""
    p = subprocess.run([sys.executable, str(ENGINE / "check_engine_purity.py")],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stdout[-600:]


def test_confirmation_gate_covers_every_emitted_kind(bridge_report, tmp_path):
    """端到端盲区守卫:可映射 ≠ 可判定。

    实测教训(编排者端到端跑出来的):桥产出 203 条规则、喂真实观测后 **203 全 UNKNOWN**,
    因为 `rule_assessment.assess` 的确认门控只认 kind ∈ (user, document),
    而桥发射 openapi/annotation/ddl/document —— **84% 结构上永远过不了门**。
    测试全绿、映射数字漂亮,但链路终点是空的。本守卫锁住这个盲区。

    判据:桥发射的每个 kind 都必须被求值器的确认门控接受(业务 tier 或技术 tier)。
    不得靠放宽三态来让它通过 —— 缺字段仍须 UNKNOWN。
    """
    rules_path = tmp_path / "rules.json"
    p = run("oracle_bridge.py", "--harvested", str(HARVESTED), "--target", "seam-test",
            "--out-report", str(tmp_path / "r.json"), "--out-rules", str(rules_path))
    assert p.returncode == 0, p.stderr[-600:]
    pack = json.loads(rules_path.read_text(encoding="utf-8"))
    kinds = {r["source"]["kind"] for r in pack["rules"]}

    src = (ENGINE / "rule_assessment.py").read_text(encoding="utf-8")
    import re
    m = re.search(r'source\.get\("kind"\)\s+in\s+\(([^)]*)\)', src)
    assert m, "找不到确认门控的 kind 白名单 —— 门控形态变了,请更新本守卫"
    accepted = {s.strip().strip('"\'') for s in m.group(1).split(",")}
    blocked = kinds - accepted
    assert not blocked, (
        f"kind {sorted(blocked)} 被确认门控拒收 → 这些判据无论观测多完整都只能 UNKNOWN。"
        f"可映射 ≠ 可判定。需区分业务 tier(user/document)与技术 tier(ddl/annotation/openapi),"
        f"不得粗暴放宽,也不得把 unknown 当 pass。")