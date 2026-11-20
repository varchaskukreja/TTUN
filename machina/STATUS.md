# Machina work status

No scored submissions made. Goal remains active; victory is unverified.

## Evidence

- Public manifest: 188,722 files, 139,707,781,499 bytes.
- 42 private representative files downloaded (about 19 MB).
- 57 unit/integration tests passing; 5 additional parameter subtests passing.
- Real sample identity transformation: five reviewed synthetic pseudonyms replaced
  13 occurrences in extracted metadata candidates; zero of those five originals
  remain in the final extracted candidates. This is not a full leakage audit.
- Six sample archives fail closed because children lack supported handlers.
- Modern SolidWorks no-op rebuilds and metadata edits independently parsed;
  unedited streams preserved. No vendor application reopen test available.
- Raw samples, registry, key, and reports are ignored and never committed.

## External blockers

- Local disk had only 2.5 GiB free at the last capacity check. Full processing needs
  a case-sensitive storage volume or remote machine with adequate capacity.
- Submission requires Builders sign-in, team identity and upload access. Browser
  automation host was unavailable; no authenticated submission panel inspected.

## Next engineering work

1. Inventory a broad stratified sample on adequate storage; derive reviewed
   corpus-wide identity candidates and aliases, preserving engineering vocabulary.
2. Add geometry-aware archive member handling and native reference rewriting.
3. Implement OCR/logo replacement with visual validation, not text-only checks.
4. Validate files in native CAD software or reliable independent readers.
5. Process the full corpus, audit paths/references/leakage and package immutable
   attempt corpus/code/README before using the 20-attempt scoring budget.

Private development artifacts are under `.machina-private/`. `mapped-v2.jsonl`
records the most recent real sample run, including all six archive exceptions.
