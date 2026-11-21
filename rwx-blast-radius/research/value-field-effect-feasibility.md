# Value-only class-field effects: feasibility, not authorization

Baseline: SQLGlot eb11aec4be8cb2122afe713a4816662c3f0fb2fa. No production links removed; no new capture or mutation run. The existing receiver probe is evidence about one baseline execution, not proof of unused state.

A generic restricted proof is feasible in principle. A local AST recognizer is insufficient to justify narrowing with the current artifacts. The useful theorem would be: for an omitted context, replacing one exact built-in dictionary's boolean values, while preserving its ordered keys, changes only a set of explicitly tracked class fields; neither that context nor any earlier shared-state execution observes those fields' changed components. This is a relational noninterference obligation, not an absence-of-text-search result.

## Baseline evidence

`sqlglot/expressions/core.py:105–112` initializes `cls.required_args` with `{k for k, v in cls.arg_types.items() if v}`. The output set can change even though the dictionary keys cannot. The other writes in this method derive `key` from the class name and normalize `__doc__`; they are not syntactically value-dependent. `core.py:1322` consumes the derived set through `self` in `error_messages`, producing errors for absent required arguments.

Direct source reads found by search otherwise predominantly use dictionary iteration, membership, length, or copies. Examples include `core.py:1680/1686`, `generator.py:5039`, `optimizer/merge_subqueries.py:123`, and `expressions/functions.py:74`. The last is `**Cast.arg_types`: it propagates values and MUST NOT be classified as a keys-only use. Copies create additional affected classes/derived fields.

`helper.py:17–28` conditionally imports `trait` from mypy_extensions or defines an identity fallback. The local native venv has no mypy_extensions, so that execution takes the identity fallback. This does not establish the dependency state of another interpreter or the oracle. `super().__init_subclass__` must be resolved against each affected class's actual MRO. A syntactic same-class assignment is not automatically local if its metaclass intercepts assignment.

There are generic reflection sinks: `expressions/core.py:2511–2517` iterates an arbitrary value's `__dict__`, and `helper.py:283–287` uses `vars(obj)`. These are concrete reasons that a search for literal field names cannot prove completeness. Their presence alone does not show an affected class reaches them; a sound analysis must prove disjoint points-to sets or reject unresolved reachability. `serde.py:120` dynamically resolves a class and constructs it, so identity aliases must also be connected to receiver-use evidence.

## Required generic certificate

1. Bind the complete patch, baseline source hashes, Python/runtime/dependency identities, and analyzed test catalog. Accept exactly an AST dictionary literal with unique exact-string keys and exact-bool values; preserve ordered keys and all other AST/source semantics. Reject spreads in the edited literal, duplicate keys, custom objects, extra changes, decorators or annotations altered by the patch. Supporting multiple fields requires the union of their effects, never independent approval of only one hunk.
2. Use a product domain for mapping effects: ordered keys/cardinality; values; identity/representation; unknown. Built-in `iter`, `keys`, `len`, and membership are insensitive to value-only edits only after proving exact built-in types and unshadowed operations. `.values`, `.items`, indexing/get, equality, repr/serialization, copying, and `**` are value-sensitive or propagate value taint. Track aliases, function parameters/returns, container stores, imports, inherited attribute providers and class-valued bindings to a fixed point. Reject any unresolved class alias rather than silently dropping it.
3. Recognize initializer summaries from syntax and resolved operations, not field names. The set comprehension above transfers a mapping's value effect into a derived set's membership effect. Prove execution terminates without changed exceptions for exact bool/string inputs, and all changed writes target proven ordinary class storage. Resolve super calls, full MRO, decorators, metaclass setters/getters, descriptors, and nested helper calls. Any unresolved hook is a rejection. Independent writes can be excluded only after control dependence is checked too.
4. Propagate the changed component through all aliases, copies, subclass inheritance and derived fields. Global assignments/registries are not inherently harmless: retain their consumers or reject whenever their stored value/control flow changes. A global registry storing a class reference must retain its alias relation, even if the registry's keys are unchanged; later retrieval can observe the changed class fields. An exported key-only projection may be proven unchanged. An exported value-filtered key set is changed.
5. Identify every observing operation, including direct class reads and free functions that never create a `self`/`cls` frame. Attribute names computed through getattr/vars/__dict__, eval, native code, third-party hooks, serialization, or unbounded alias escape reject unless a separate complete model proves them irrelevant. Baseline executed-path absence cannot discharge an unknown edge. Public mutable dictionaries/classes and test monkeypatches need explicit treatment; importing a Python module exposes its classes but does not by itself prove which fields consumers inspect.
6. Only after that proof, separate receiver events into proven class initialization and runtime use. Keep every context observing the affected original/derived components, plus unknown lifecycle events and transitive global/cache consumers. Existing deduplicated class maps cannot reconstruct this partition. New lifecycle capture would need child start/end receipts, direct-read coverage, collection/global context treatment, and cold/shared-state order analysis. Missing contexts, unknown processes, changed imports, or proof version/hash mismatch mean retain the original conservative selection.

Import validation is necessary for malformed patches but insufficient for noninterference: successful collection does not rule out a changed global registry or a deferred observation. Likewise a full baseline trace does not cover a mutant-introduced path unless the proof establishes that all earlier control decisions are unaffected.

## Counterexamples the checker must reject or propagate

- `alias = C.flags; GLOBAL = tuple(alias.values())`: no literal `C.flags` read at the actual observing call.
- `D.flags = {**C.flags}` followed by D's initializer: same keys, different derived state on another class.
- `C.needed = {k for k,v in C.flags.items() if v}; REGISTRY = C.needed`: same-class derivation escapes globally.
- `REGISTRY['c'] = C; REGISTRY['c'].needed`: class registry aliases expose derived state without a C receiver method.
- Metaclass `__setattr__` publishes any changed `needed` into a global; custom `__getattribute__` or decorator consumes the mapping.
- `if any(C.flags.values()): register(C)`: unchanged mapping shape, changed global control flow.
- `free_function(C.needed)` or `getattr(C, computed_name)`: observing use absent from receiver maps.
- A subclass's hook reads an inherited mapping before calling super, or a runtime mutation changes the mapping's type/provider.
- Exact keys replaced with equal custom keys, bool replaced with integer/custom truthy value, or duplicate/reordered keys: textually similar edits violate the input theorem.
- A previous test caches a value-derived projection that a later test consumes: per-test warm event absence cannot establish independence.
- Source/code introspection observes the literal edit itself. Existing source-reader protections must remain in force.

## Decision and bounded next step

No map drop is currently justified. The measured single-test receiver probe showed only initialization events for TriggerProperties in TPCDS and its workers; it did not observe attribute reads or establish closed-world alias/reflection completeness. The hypothetical approximately 17.7-second scheduler saving is an upper-bound experiment target, not a validated improvement.

A useful next prototype would emit candidate certificates plus explicit unresolved obligations, never selected tests. First implement the literal-change gate and a small, typed AST transfer engine with adversarial tests for every counterexample above. Test that the pinned initializer yields a mapping-values → same-receiver-set-membership summary and that copies/global escapes stay tainted. Then assess whether the real baseline can discharge all hooks, aliases and reflection obligations. If it cannot, stop at candidate discovery; do not replace proof failures with a field-name whitelist or assume grep found every consumer. Only a successful certificate and an agreed bounded lifecycle/read-validation design would justify proposing another capture.
