"""GF(2) 环一致性的随机交叉验证：并查集奇偶 vs 暴力枚举染色。

此前「400 张随机图零分歧」只是一次性实验，没有持久守卫 —— 改坏奇偶逻辑只有
特定夹具能抓。这里把那一次性测量变成回归守卫（带种子，确定性可复现）。
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

_PILOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PILOT))
sys.path.insert(0, str(_PILOT / "engine"))

from engine.frame_check import _parity_check  # noqa: E402


def _brute(cells: list[str], edges: dict[tuple[str, str], str]) -> str:
    """参考实现：枚举全部 2^n 染色，看已知边能否同时满足。

    与引擎口径一致：有 unknown 边或无已知边 → unknown（保守，不许当成一致）。
    """
    known = {k: v for k, v in edges.items() if v != "unknown"}
    unknown = len(edges) - len(known)
    for bits in itertools.product((0, 1), repeat=len(cells)):
        color = dict(zip(cells, bits))
        if all(
            (color[a] ^ color[b]) == (0 if label == "consistent" else 1)
            for (a, b), label in known.items()
        ):
            satisfiable = True
            break
    else:
        satisfiable = False
    if not satisfiable:
        return "inconsistent"
    if not known or unknown > 0:
        return "unknown"
    return "consistent"


def test_parity_matches_brute_force_on_random_graphs():
    rng = random.Random(20261115)
    labels = ("consistent", "inconsistent", "unknown")
    mismatches = []
    count_mismatches = []
    seen = {"consistent": 0, "inconsistent": 0, "unknown": 0}
    for _ in range(400):
        n = rng.randint(2, 6)
        cells = [f"c{i}" for i in range(n)]
        edges: dict[tuple[str, str], str] = {}
        for a, b in itertools.combinations(cells, 2):
            if rng.random() < 0.45:
                edges[(a, b)] = rng.choice(labels)
        got, _conflict, known, unknown = _parity_check(cells, edges)
        want = _brute(cells, edges)
        seen[want] += 1
        if got != want:
            mismatches.append((cells, edges, got, want))
        # counts 契约：引擎在矛盾处 break，所以 known/unknown 都只统计 break 之前遇到的边。
        # 参考值按同一迭代顺序逐前缀重放求得。只断言 status 抓不到 break→continue。
        seq = list(edges.items())
        stop = len(seq)
        kl: list = []
        for i, (k, v) in enumerate(seq):
            if v == "unknown":
                continue
            kl.append((k, v))
            if _brute(cells, dict(kl)) == "inconsistent":
                stop = i + 1
                break
        head = seq[:stop]
        exp_known = sum(1 for _k, v in head if v != "unknown")
        exp_unknown = sum(1 for _k, v in head if v == "unknown")
        if known != exp_known:
            count_mismatches.append((edges, "known", known, exp_known))
        if unknown != exp_unknown:
            count_mismatches.append((edges, "unknown", unknown, exp_unknown))
    assert not mismatches, f"并查集奇偶与暴力法分歧 {len(mismatches)} 例: {mismatches[:3]}"
    assert not count_mismatches, f"counts 契约分歧 {len(count_mismatches)} 例: {count_mismatches[:3]}"
    # 三种结论都必须真被覆盖，否则这条守卫可能是空的
    assert all(v > 0 for v in seen.values()), f"随机语料没覆盖三态: {seen}"


def test_parity_detects_odd_cycle_contradiction():
    # 三角形上三条边要求两两异色 —— GF(2) 上不可满足，必须判 inconsistent
    cells = ["a", "b", "c"]
    edges = {("a", "b"): "inconsistent", ("b", "c"): "inconsistent", ("a", "c"): "inconsistent"}
    status, conflict, known, unknown = _parity_check(cells, edges)
    assert status == "inconsistent", status
    assert conflict is not None
    assert (known, unknown) == (3, 0)


def test_unknown_edge_never_becomes_consistent():
    cells = ["a", "b", "c"]
    edges = {("a", "b"): "consistent", ("b", "c"): "unknown"}
    status, _c, known, unknown = _parity_check(cells, edges)
    assert status == "unknown", "有 unknown 边却判成一致 —— 三态被压平"
    assert (known, unknown) == (1, 1)


def test_no_known_edges_is_unknown_not_consistent():
    status, _c, known, unknown = _parity_check(["a", "b"], {("a", "b"): "unknown"})
    assert status == "unknown"
    assert (known, unknown) == (0, 1)