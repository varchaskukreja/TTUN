#!/usr/bin/env python3
"""Detailed offline diagnostics; sample answers never enter the submission image.

Usage: python evaluate.py output.jsonl [--data sample] [--json]
The official scorer validates the complete output before detailed reporting.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from score import evaluate_run, _duration_milliseconds


def evaluate(data: Path, output: Path, answers_path: Path | None = None) -> dict:
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((answers_path or data / "answers.json").read_text())["commits"]
    patches = {p.stem for p in (data / "commits").glob("*.patch")}
    if patches != set(answers):
        raise ValueError("Answers and patch files must cover exactly the same commits")
    stdout = output.read_bytes()
    if len(stdout) > 2 * 1024 * 1024:
        return {"ok": False, "code": "stdout_too_large", "bytes": len(stdout)}
    result = evaluate_run(stdout, tests, answers)
    if not result["ok"]:
        return result
    catalog = {test["number"]: test for test in tests}
    rows = []
    for line in stdout.decode().splitlines():
        entry = json.loads(line)
        workers = [w for w in entry["workers"] if w]
        selected = {int(n) for w in workers for n in w}
        failing = set(answers[entry["commit"]]["failing"])
        missing = sorted(failing - selected)
        load = max((sum(_duration_milliseconds(catalog[n]["seconds"]) for n in w) for w in workers), default=0)
        cost = load + 2000 * len(workers)
        rows.append({
            "commit": entry["commit"],
            "selected": len(selected),
            "failing": len(failing),
            "missing": missing,
            "missing_ids": [catalog[n]["id"] for n in missing],
            "workers": len(workers),
            "selection_milliseconds": cost,
            "total_milliseconds": cost + (300000 if missing else 0),
        })
    result["commits"] = sorted(rows, key=lambda row: row["commit"])
    result["missed_test_count"] = sum(len(row["missing"]) for row in rows)
    result["selection_milliseconds"] = sum(row["selection_milliseconds"] for row in rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "sample")
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = evaluate(args.data, args.output, args.answers)
    if args.json or not result["ok"]:
        print(json.dumps(result, indent=2))
    else:
        print(f"Simulated: {result['simulated_milliseconds'] / 1000:.3f} s; "
              f"missed commits: {len(result['missed_commits'])}; "
              f"missing tests: {result['missed_test_count']}")
        print("commit  selected  failing  missing  workers  CI seconds")
        for row in result["commits"]:
            print(f"{row['commit']:6}  {row['selected']:8}  {row['failing']:7}  "
                  f"{len(row['missing']):7}  {row['workers']:7}  "
                  f"{row['selection_milliseconds'] / 1000:10.3f}")
            for test_id in row["missing_ids"]:
                print(f"    MISSED {test_id}")
        print("CPU is not included; use the constrained container run for timing.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
