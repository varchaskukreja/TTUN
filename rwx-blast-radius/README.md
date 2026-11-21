# RWX — Blast Radius

TTUN's baseline-dependency test selector for the Builders Buildathon.

The root Dockerfile is the Blast Radius entrypoint. The selector applies each incoming patch to the pinned sqlglot baseline in memory, finds affected functions and fixture readers in a baseline coverage map, and schedules selected tests across the cheapest of one through eight workers. Unknown dependencies, unsupported import-time edits, malformed patches and syntax errors select all tests conservatively. Direct class-literal changes and literal arguments to verified closure factories use baseline class consumers only after a bounded fresh-process import and full-catalog collection check succeeds.

Dependency maps and syntactic factory metadata are generated exclusively from the **unmodified** sqlglot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. No sample answers, patch IDs, patch hashes, or patch-to-failure lookup tables are used in selection. Test numbers are resolved from test IDs in the supplied catalog. Sample answers are used only by the local evaluation tools and excluded from the image.

## Run

From this directory:

```sh
make sample
python3 evaluate.py output.jsonl
python3 -m unittest discover -s . -p 'test_*.py'
```

`make sample` builds the root Dockerfile and runs with the official network, memory, CPU, process, filesystem and nobody-user restrictions. Local scoring excludes container CPU time; leaderboard scoring includes it.

For local development without Docker:

```sh
python3 selector.py --data sample --source ../.work/sqlglot --map maps/coverage.json.gz > output.jsonl
python3 evaluate.py output.jsonl
```

Coverage generation is implemented in `tools/build_map.py` and `tools/coverage_plugin.py`; run `build_map.py --help` for explicit paths. Instrumentation records line execution, test outcomes, and file reads. `tools/class_attribute_exports.py` and `tools/propagate_class_consumers.py` then expand class consumers using only the unmodified baseline AST, retaining all existing links and flagging attributes exported to global registries. Export flags propagate back through static class aliases, derived values, and inherited attribute providers. Class-valued bindings also connect separately declared parser/generator implementations to their owning dialect consumers. Use the official Linux Python image and pinned test dependency versions in `requirements.lock` to avoid platform skew.

## Upstream code and data

- The starter kit, sample data, official scorer, scoring specification and dependency lock are supplied by Builders / RWX for this competition.
- sqlglot is MIT-licensed upstream software by Toby Mao and contributors, fetched at the exact challenge revision.
- Coverage.py provides offline baseline instrumentation. Pytest and pinned upstream test dependencies support the runtime import and collection guard.
- New TTUN implementation code is released under the MIT license in `LICENSE`.

## Current verification

The production candidate scores **256.055 simulated seconds, zero missed commits** on all 30 official samples. Its measured local process CPU was 11.167 seconds, including four successful collection guards. A fresh root Dockerfile build and execution passed under the official Linux/amd64 restrictions. These local results are not predictions of the hidden leaderboard score. The function-only mode scores 350.611 seconds; selecting all tests scores 1104.510 seconds.

The baseline map unions ordinary suite execution with isolated per-test execution from a collection-only parent, covering all 1620 catalog tests, subprocesses, and shared fixture scopes. All 2017 Linux coverage shards and 26 native repair shards passed integrity and test-context checks. Every dependency from the earlier map is retained. Three tests that reported DuckDB errors under emulation passed all 723 subtests in a native pinned-dependency coverage run; the merged map retains every Linux dependency and records the repair provenance. Fresh ClickHouse and DuckDB class-constant mutations each had zero missed failures. Two additional factory-argument mutations in DuckDB and PostgreSQL produced four failing catalog tests, all selected by the factory policy. Three further full-catalog mutations exposed missing consumers of class aliases, global function registries, and nested tokenizer metadata. Baseline AST consumer propagation and conservative fallback for exported registry attributes now select all 63 failing-test occurrences, repairing nine previously missed occurrences without removing any prior selections. See `validation/final-class-bindings-holdouts.json`. A further shared dispatch-cache mutation caused 146 catalog tests to fail; all were selected. A separate shared TPC-H fixture mutation produced three failing tests: the original submitted map missed two, while the repaired map selected all three.

All 121 primary regression tests, 22 factory policy tests, and 13 class export/propagation tests pass, covering the selector, scheduler, patch application, runtime overlay, bounded collection guard and factory provenance checks. Experimental granularity tests include reproducible counterexamples involving cached state and code-object metadata; statement-level selection remains excluded from production. Comparison experiments remain excluded. Factory narrowing requires matching Python version, whole-source AST hashes, stable import bindings, unchanged factory provenance and a successful collection guard; unsupported cases retain all tests.

First submitted commit `0d7ca6cd9e2b2b4ab86cd1a405b940f56c95ed42` received receipt **BB-C2E99545** and a public score of **938.3 seconds**. The user subsequently confirmed the requested `a242541` submission, followed by a public team score of **585.6 seconds**; its receipt, exact submitted commit, and private miss/CPU breakdown remain unverified. See `submissions/a242541-user-confirmed.json`. The prepared replacement is `1877195dffbfa5a23f923785b9bb7fb6a21205cf`. Submissions are limited to one per hour; the conservative next slot is November 20 at **9:06 PM Eastern** (`2026-11-21T02:06:00Z`), measured from the user confirmation with a buffer. Slot availability alone is not a reason to submit.

The entrypoint enables `--class-guard --factory-proofs /app/maps/factory_proofs.json --metrics`. The guard has an eight-second per-probe timeout and a 120-second cumulative budget; failed or unavailable checks select all tests. Diagnostics go to stderr. `tools/public_standings.py` reads public scores without browser access, cookies, or authentication.

For local rebuilding during a network outage, with a previously verified `blast-radius-entry` image available:

```sh
# From the repository root:
docker build --platform linux/amd64 -f rwx-blast-radius/tools/Dockerfile.offline -t ttun-blast-local .
make -C rwx-blast-radius run-sample IMAGE=ttun-blast-local
```

This development path does not replace the root Dockerfile or verify a fresh upstream download.
