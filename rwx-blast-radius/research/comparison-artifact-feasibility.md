# Comparison evidence packaging: experimental proposal only

No production runtime, frozen capture helper, or production map was edited. No optimization was activated. No mutation answer labels were read by this experiment.

## Recommended representation

Use the entire merged baseline mapping as deterministic plain gzip JSON. This is lossless, preserves every site, type descriptor, observed/executing context, source/code reader, preexisting-module flag, capture receipt, uncertainty flag and provenance record, and avoids maintaining a second policy implementation. The preferred artifact is `.work/comparison-full-v3/compact-plain.json.gz` (3,107,324 bytes). Its decompressed bytes are exactly the original 44,881,432-byte mapping. The tracked repository measured 7,461,283 bytes before these new experimental files; adding the artifact leaves ample room below 20MB.

A generic interned JSON DAG compressed further to 614,353 bytes but took 1.268 CPU seconds median to expand and verify, versus 0.285 for plain gzip (three measured trials each). Prefer plain gzip: saving approximately 2.5MB does not justify about one CPU second of extra loading. These are native Python3.13 measurements, not a container CPU claim. The serialization helper preserves independent container identity when expanding the DAG; no accidental mutable aliases are introduced.

`load_plain(path, expected_expanded_sha256)` decompresses, verifies the exact pinned SHA256, and parses JSON. A proposed runtime loader should also cap compressed and expanded sizes, catch corrupt gzip/JSON, and fail closed to original selection. The expected digest belongs in a reviewed manifest, not in untrusted artifact contents. Existing capture/interpreter/catalog/hash gates remain mandatory after loading. Loading once per submitted process amortizes this cost; no process-wide mutable certificate cache should weaken source/hash validation.

## Equivalence validation

The validator proves exact expanded object equality, then evaluates all 1,671 sites against both representations. All site certificates and retention-policy decisions match: 267 conditional research proposals and 1,404 retained selections. It normalizes each site's source to one comparison to exercise its actual operand descriptors, source/code readers and observed/executing contexts; original path/span/source hash preservation is separately guaranteed by full equality. This normalization is not a claim that every operator pair qualifies as an actual patch: equal-width Is/Eq whole-patch checks still apply, and IsNot/NotEq edits are rejected by the current text gate.

Additionally, source-bound whole-patch qualification was exercised for one actual equal-width edit per source file with a suitable site: 98 files, including 11 eligible decisions. Full and compressed forms matched exactly. The first run measured 0.0337 CPU seconds median, 1.3913 maximum and 9.014 total for one 98-file qualification pass; parsing/source size dominates. Qualifying a patch currently reparses through both the exact-edit gate and the observation certificate. Any parse-cache or single-pass refactor needs separate equivalence validation and is outside this packaging experiment. Six unit tests cover serialization types, unknown preservation, corruption, no introduced aliases, source-bound eligible qualification and plain digest validation.

## Proposed integration API (not implemented or authorized)

`load_evidence(path, manifest, catalog, runtime_identity) -> Evidence | Unavailable`

Validate artifact hashes/size/schema, capture receipt audit, exact catalog, Python version and qualifier identity. Keep all false or unknown completeness flags. A successful capture gate is not a semantic authorization gate; the existing nonexhaustive reader/activity and warm-order limitations survive byte-for-byte.

`propose(patch, production_selection, production_map, baseline_unstable_tests) -> Proposal`

1. On missing authorization, missing/invalid artifact, stale source hash, unsupported whole patch, unobserved site, unknown contexts or collection escape, return the original selection plus known mandatory unstable tests (or all catalog tests when unstable metadata itself is malformed/unresolvable).
2. Use the existing exact whole-patch qualifier and observed-type certificate, with unchanged enum/type-slot assumptions. A certificate is conditional evidence, never a proof that an unexecuted path is safe.
3. Candidate removable tests are only the intersection of production selection, exact observed operand contexts and executing-code contexts. Leave every unobserved context selected.
4. Protect and add all source/code-object readers, production source-reader indices, missing/unmapped tests, nonpassing baseline outcomes, and explicit `baseline_unstable_tests`. Collection/global readers or unresolved indices reject narrowing. Unknown contexts reject globally rather than silently discarding them.
5. Always union known unstable tests into the result, even if the original selector omitted them. The current research analyzer does not inspect `baseline_unstable_tests`; preserving its historical decisions is therefore insufficient for this future API. This requirement is an explicit additional conservative safeguard, not an equivalence claim about the current analyzer.
6. Keep cross-test cached downstream effects and instrumentation transparency as unresolved authorization obligations. Retaining the direct reader alone need not retain downstream state consumers. Do not turn capture_complete into reader/activity completeness or infer cold/reordered safety from the warm trace.

The prototype exports no selected-test submission artifact. No descriptor, context, unknown or audit record was removed to make this representation small. The packaging result is ready for review; it does not settle the semantic conditions for activating comparison narrowing.
