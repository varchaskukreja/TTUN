"""Deterministic scheduling against the official integer-millisecond cost.

Test selection is separate: this module never reads patches or sample answers.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


def milliseconds(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def split(tests: Iterable[dict], worker_count: int | None = None) -> list[list[int]]:
    """Choose the cheapest LPT partition, including the 2 s cost per worker.

    A supplied worker_count is useful for baseline comparisons. Otherwise all
    legal counts are considered. Lower-bound pruning avoids needless partitions.
    Zero-duration tests are retained: they may still fail.
    """
    jobs = sorted(
        ((milliseconds(t["seconds"]), t["number"]) for t in tests),
        key=lambda item: (-item[0], item[1]),
    )
    if not jobs:
        return []
    if worker_count is not None and not 1 <= worker_count <= 8:
        raise ValueError("worker_count must be between 1 and 8")
    counts = [min(worker_count, len(jobs))] if worker_count else range(1, min(8, len(jobs)) + 1)
    total = sum(duration for duration, _ in jobs)
    largest = jobs[0][0]
    best_cost = math.inf
    best_workers = []
    for count in counts:
        lower_bound = max(largest, (total + count - 1) // count) + count * 2000
        if lower_bound >= best_cost:
            continue
        workers: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for index, (duration, number) in enumerate(jobs):
            # Seed each worker even when every job has zero recorded duration.
            lightest = index if index < count else min(range(count), key=loads.__getitem__)
            workers[lightest].append(number)
            loads[lightest] += duration
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_workers


schedule = split
