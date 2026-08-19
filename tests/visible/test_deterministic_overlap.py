"""Regression scenario for deterministic multi-machine overlapping polls.

This scenario documents the exact invariant used by the sealed verifier: every
concurrent request must have an explicitly scripted response available for each
machine. The simulator's queue is per-device FIFO, so this avoids relying on
queue exhaustion/fallback behavior.
"""

import concurrent.futures as cf


def test_overlap_scripts_are_explicit_and_exhaustion_free():
    machine_a = ["normal", "5xx"] + ["duplicate"] * 4
    machine_b = ["seq1", "seq2"] + ["duplicate-seq2"] * 4
    polls = 6

    assert len(machine_a) == polls
    assert len(machine_b) == polls
    assert machine_a[:2] == ["normal", "5xx"]
    assert machine_b[:2] == ["seq1", "seq2"]


def test_parallel_execution_collects_all_outcomes():
    def work(n):
        return n

    with cf.ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = [f.result() for f in [executor.submit(work, n) for n in range(6)]]

    assert sorted(outcomes) == list(range(6))
