"""涌现裁决器的三态判定 —— 执行失败的观测永远不许变成「没违反」。

硬约束 3 的推论在涌现引擎里的落点。引擎原先只检查**基线**的 status，
`combined` / 单注 / leave-one-out / 成对组合全都只看 `.violated`：
一次执行失败（status=error、violated=False）会被当成「这个条件不触发」，
于是判出**假的 EMERGENT_CONJUNCTIVE** —— 与「字段名拼错得到一张通过的成绩单」同族。

这组测试锁住四条执行路径上的同一个洞，外加不许误伤客观裁决的回归。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import emergence  # noqa: E402


A, B = "INJ-A", "INJ-B"


def _inj(iid: str) -> emergence.Injection:
    return emergence.Injection(
        id=iid, defect=f"rc.{iid}", layer="service", target_step="POST /api/orders",
        mutation={"kind": "data", "set": {"flag": 1}}, expected_symptom="BI-PROBE",
        alive_check={"kind": "scenario", "scenario": {"probe_invariant": "BI-PROBE"}},
        active=True, category="service.data_consistency")


def _chain() -> emergence.Chain:
    return emergence.Chain(
        id="CHAIN-UNKNOWN", title="三态自证链", hypothesis="h", severity="HIGH",
        target_invariant="BI-PROBE", root_cause_key="rc.chain",
        location={"file": "x"}, injections=(_inj(A), _inj(B)),
        claim="c", entity="order")


def _obs(subset: tuple[str, ...], violated: bool, *,
         status: str = "ok", magnitude: float = 0.0) -> emergence.Observation:
    return emergence.Observation(
        subset=subset, violated=violated, magnitude=magnitude,
        violated_invariants=("BI-PROBE",) if violated else (),
        status=status, detail="" if status == "ok" else "容器 502")


class Scripted:
    """按子集回放观测。值是单个观测或观测序列（同一子集被问多次时依次给出，
    最后一个重复）—— 用来区分「单注那次」与「留一法/成对那次」。"""

    def __init__(self, script: dict) -> None:
        self.script: dict[frozenset, list] = {}
        for key, val in script.items():
            self.script[frozenset(key)] = list(val) if isinstance(val, list) else [val]
        self.calls: list[tuple[str, ...]] = []

    def run(self, chain: emergence.Chain, subset: tuple[str, ...]) -> emergence.Observation:
        self.calls.append(subset)
        queue = self.script.get(frozenset(subset))
        if not queue:
            return _obs(subset, False)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def alive(self, chain: emergence.Chain,
              injection: emergence.Injection) -> emergence.AliveResult:
        return emergence.AliveResult(injection_id=injection.id, alive=True)


def _conjunctive_script() -> dict:
    """F(空)=0、F({A})=0、F({B})=0、F({A,B})=1 —— 合取型。"""
    return {
        (): _obs((), False),
        (A,): _obs((A,), False),
        (B,): _obs((B,), False),
        (A, B): _obs((A, B), True, magnitude=90000.0),
    }


# ------------------------------------------------------------------ 单注失败

def test_failed_single_is_not_evidence_of_not_triggering() -> None:
    """B 执行失败 -> 不许当成「B 单独不触发」而判出合取型涌现。"""
    script = _conjunctive_script()
    script[(B,)] = _obs((B,), False, status="error")

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.filings == ()
    assert j.ground_truth == emergence.GT_UNVERIFIED


def test_failed_single_is_named_in_warnings() -> None:
    """失败必须留痕：说清是哪个子集失败，不许静默降级。"""
    script = _conjunctive_script()
    script[(B,)] = _obs((B,), False, status="error")

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert any(B in w for w in j.warnings)
    assert any("容器 502" in w for w in j.warnings)


# ------------------------------------------------------------------ 合起来失败

def test_failed_combined_is_undecidable_not_falsified() -> None:
    """F(D) 执行失败 -> 不是「链的前提不成立」，是「不知道」。"""
    script = _conjunctive_script()
    script[(A, B)] = _obs((A, B), False, status="error")

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.verdict != emergence.VERDICT_FALSIFIED
    assert j.filings == ()


def test_failed_combined_with_violated_flag_is_still_undecidable() -> None:
    """执行失败时 violated 标志不可信 —— 哪怕它是 True 也不许开单。"""
    script = _conjunctive_script()
    script[(A, B)] = _obs((A, B), True, status="error", magnitude=90000.0)

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.filings == ()


# ------------------------------------------------------------------ 基线失败

def test_failed_baseline_is_undecidable_not_falsified() -> None:
    """基线执行失败原先判 FALSIFIED_CHAIN —— 那是对「不知道」下了正面结论。"""
    script = _conjunctive_script()
    script[()] = _obs((), False, status="error")

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.filings == ()


# ------------------------------------------------------------------ 留一法失败

def test_failed_leave_one_out_cannot_yield_a_core_set() -> None:
    """核心集算不出来就不许报 CORE / CONJUNCTIVE。

    留一法按 id 排序先剔除 A，问 F({B}) —— 那是 {B} 的第二次执行。
    """
    script = _conjunctive_script()
    script[(B,)] = [_obs((B,), False), _obs((B,), False, status="error")]

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.filings == ()


# ------------------------------------------------------------------ 成对组合失败

def test_failed_pairwise_run_is_undecidable_not_masking() -> None:
    """成对组合执行失败 -> 不许当成「症状被掩盖」。

    A 单注能触发，成对探测是 {A,B} 的第二次执行（第一次是 combined）。
    """
    script = {
        (): _obs((), False),
        (A,): _obs((A,), True, magnitude=10.0),
        (B,): _obs((B,), False),
        (A, B): [_obs((A, B), True, magnitude=10.0),
                 _obs((A, B), False, status="error")],
    }

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_UNDECIDABLE
    assert j.filings == ()


# ------------------------------------------------------------------ 不许误伤

def test_all_ok_conjunctive_verdict_is_unchanged() -> None:
    """全部 ok 时裁决与开单一个字都不许变 —— 三态闸门不能误伤客观结论。"""
    j = emergence.adjudicate(_chain(), Scripted(_conjunctive_script()))

    assert j.verdict == emergence.VERDICT_CONJUNCTIVE
    assert len(j.filings) == 1
    assert j.filings[0].layer == "integration"
    assert j.ground_truth == emergence.GT_VERIFIED


def test_all_ok_falsified_verdict_is_unchanged() -> None:
    """合起来确实不违反（执行成功）仍然判 FALSIFIED_CHAIN。"""
    script = _conjunctive_script()
    script[(A, B)] = _obs((A, B), False)

    j = emergence.adjudicate(_chain(), Scripted(script))

    assert j.verdict == emergence.VERDICT_FALSIFIED


def test_undecidable_is_a_declared_verdict() -> None:
    """第八种裁决必须进枚举，否则下游按未知字符串处理。"""
    assert emergence.VERDICT_UNDECIDABLE in emergence.ALL_VERDICTS
    assert len(emergence.ALL_VERDICTS) == 8


def test_undecidable_never_opens_a_filing() -> None:
    """UNDECIDABLE 不在开单映射里 —— 结构上就不可能开单。"""
    assert emergence.VERDICT_UNDECIDABLE not in emergence.VERDICT_FILING
