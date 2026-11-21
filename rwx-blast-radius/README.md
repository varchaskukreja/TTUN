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

The reviewed default comparison candidate scores **225.976 simulated seconds, zero missed commits** on all 30 official samples. Its measured local process CPU was 11.194804 seconds, including four successful collection guards. The pre-comparison scheduled cost was 262.603 seconds. The final default-entrypoint output exactly matches the previously measured explicit-enable output; see `validation/comparison-default-docker.json`. A fresh root Dockerfile build and execution passed under the official Linux/amd64 restrictions. These local results are not predictions of the hidden leaderboard score. The function-only mode scores 350.611 seconds; selecting all tests scores 1104.510 seconds.

The baseline map unions ordinary suite execution with isolated per-test execution from a collection-only parent, covering all 1620 catalog tests, subprocesses, and shared fixture scopes. All 2017 Linux coverage shards and 26 native repair shards passed integrity and test-context checks. Every dependency from the earlier map is retained. Three tests that reported DuckDB errors under emulation passed all 723 subtests in a native pinned-dependency coverage run; the merged map retains every Linux dependency and records the repair provenance. Fresh ClickHouse and DuckDB class-constant mutations each had zero missed failures. Two additional factory-argument mutations in DuckDB and PostgreSQL produced four failing catalog tests, all selected by the factory policy. Three further full-catalog mutations exposed missing consumers of class aliases, global function registries, and nested tokenizer metadata. Baseline AST consumer propagation and conservative fallback for exported registry attributes now select all 63 failing-test occurrences, repairing nine previously missed occurrences without removing any prior selections. See `validation/final-class-bindings-holdouts.json`. A further shared dispatch-cache mutation caused 146 catalog tests to fail; all were selected. A separate shared TPC-H fixture mutation produced three failing tests: the original submitted map missed two, while the repaired map selected all three.

All 145 primary regression tests, 22 factory policy tests, and 13 class export/propagation tests pass, covering the selector, scheduler, patch application, runtime overlay, bounded collection guard and factory provenance checks. Experimental granularity tests include reproducible counterexamples involving cached state and code-object metadata; statement-level selection remains excluded from production. Conditional observed-type comparison refinement is enabled after reviewed warm, order-stress, and isolated-cold validation; its remaining assumptions are described below. Factory narrowing requires matching Python version, whole-source AST hashes, stable import bindings, unchanged factory provenance and a successful collection guard; unsupported cases retain all tests.

Separate research controls completed all 1620 tests in normal, reversed, and fixed-seed shuffled order. Normal order passed; reverse and shuffle each exposed the same two unmodified-suite failures, and both affected tests passed individually in fresh processes. The evidence points to shared dialect-registry clearing by earlier tests. See `validation/order-baseline-audit.json`. A warm baseline trace alone does not establish order independence. A fresh isolated-cold baseline passed all 1620 tests, and two comparison mutants each passed all 1620 isolated tests with complete audited receipts. These empirical checks do not establish universal order independence; known unstable tests remain selected. See `validation/comparison-cold-baseline-v3.json` and `validation/comparison-cold-mutants-v3.json`.

First submitted commit `0d7ca6cd9e2b2b4ab86cd1a405b940f56c95ed42` received receipt **BB-C2E99545** and a public score of **938.3 seconds**. The user subsequently confirmed the requested `a242541` submission, followed by a public team score of **585.6 seconds**; its receipt, exact submitted commit, and private miss/CPU breakdown remain unverified. See `submissions/a242541-user-confirmed.json`. The earlier prepared replacement `1877195dffbfa5a23f923785b9bb7fb6a21205cf` scored 256.055 locally; its submission has not been confirmed. This candidate adds captured-definition dependencies and retention of observed baseline-unstable tests. Submissions are limited to one per hour; the conservative next slot is November 20 at **9:06 PM Eastern** (`2026-11-21T02:06:00Z`), measured from the user confirmation with a buffer. Slot availability alone is not a reason to submit.

The entrypoint enables `--class-guard --factory-proofs /app/maps/factory_proofs.json --metrics --experimental-comparisons` (the final flag retains its compatibility name). The guard has an eight-second per-probe timeout and a 120-second cumulative budget; failed or unavailable checks select all tests. Diagnostics go to stderr. `tools/public_standings.py` reads public scores without browser access, cookies, or authentication.

For local rebuilding during a network outage, with a previously verified `blast-radius-entry` image available:

```sh
# From the repository root:
docker build --platform linux/amd64 -f rwx-blast-radius/tools/Dockerfile.offline -t ttun-blast-local .
make -C rwx-blast-radius run-sample IMAGE=ttun-blast-local
```

This development path does not replace the root Dockerfile or verify a fresh upstream download.

The function/lambda definition-expression supplement (`tools/definition_expression_exports.py`) adds 13 recognized captures to class export evidence without changing sample selections. It covers defaults, decorators and evaluated annotations conservatively; it is not a complete Python binding analysis. The map also records `baseline_unstable_tests` from unmodified reversed/shuffled controls, with report hashes. These tests remain selected independently of changed-code coverage, costing 6.548 additional simulated sample seconds. Normal-order baseline outcomes remain intact. All 30 sample selections retain every previously selected test. A fresh CopyBudget defaults mutation produced five failing tests, all already selected; a shared dialect-registry mutation timed out after 180 seconds without audited outcomes and remains unresolved.

Changed explicit classmethods that write through their shared receiver now conservatively select all tests. The check covers both original and patched ASTs, local aliases, assignment/deletion/augmented writes and recognized container mutators. A three-node registry-reset sequence exposed two previously omitted failures; both are now selected. The downstream consumer passed alone before and after mutation, confirming order-dependent shared state. The earlier full-catalog timeout remains inconclusive. This additive guard is not a complete effect analysis: opaque helpers, decorator aliases and unrelated global aliases remain limitations. See `validation/shared-class-state-guard.json`. Adjacent container CPU observations were 23.288s before and 23.854s after; concurrent cold validation means these are local observations, not a leaderboard prediction.

Conditional comparison refinement accepts only whole-patch, equal-width `is`/`==` edits whose observed operand types have the reviewed equivalence certificate. It retains source/code readers, unobserved or unknown dependencies, baseline-unstable tests and persistent-class-state fallback selections. An explicit deployment manifest authorizes this policy; it does not claim exhaustive reader/native coverage, immutable future type slots, instrumentation transparency or universal shared-state/order safety. The lossless capture retains its historical provenance and limitations.
