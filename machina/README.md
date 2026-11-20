# Machina CAD De-ID

Work in progress for the Builders Machina track. No scored attempt has been submitted.

## Setup and private input

Run `uv sync --project machina` from the repository root. Dependencies are pinned
in `uv.lock`. Run tests with `uv run --project machina pytest machina`.

Raw input, discovered identities, pseudonym keys, mappings and run logs must remain
private and outside Git. The `.machina-private/` folder is ignored. Do not add raw
fixtures to this public repository. All committed test fixtures are synthetic.

Fetch the manifest privately:

```sh
mkdir -p .machina-private
curl -fsSL https://machina-input.joinbuilders.org/manifest.tsv -o .machina-private/manifest.tsv
uv run --project machina python machina/inventory.py .machina-private/manifest.tsv
```

To download a bounded, deterministic sample onto a **case-sensitive** volume:

```sh
uv run --project machina python machina/inventory.py .machina-private/manifest.tsv \
  --sample-dir /your/case-sensitive-volume/input --budget-mb 100
uv run --project machina python machina/scan.py /your/case-sensitive-volume/input \
  --private-report .machina-private/candidates.jsonl
```

`inventory.py` validates paths, checks sizes and available MD5s, and samples across
extensions. It refuses case-insensitive destination filesystems. The full manifest
contains 188,722 files totaling 139,707,781,499 bytes. Allow ample space for input,
expanded archives, output and validation; roughly 400+ GB is a starting estimate,
not an archive expansion guarantee.

## Current components

- `identity.py`: explicit identity/alias registry; deterministic keyed pseudonyms;
  conflict checks and coordinated filename replacement. Identity candidates need
  review: engineering labels and material names must not be treated as identities.
- `neutral.py`: format-aware editing of supported neutral CAD metadata/text spans.
- `native.py`: strict OLE property parsing and equal-byte-size string replacement.
  Other OLE streams are preserved; unsupported structures are reported.
- `pdf_handler.py`: known-identity text and metadata replacement with PDF drawings
  preserved; reports unhandled content.
- `scan.py`: private review inventory, not an automatic identity classifier.

This is not yet a full-corpus submission pipeline. Native CAD references, modern
SolidWorks containers, nested archives, image/logo sanitization and OCR require
further work. Passing unit tests does not establish complete de-identification or
native application compatibility. Never submit unchanged unsupported copies as
if they were sanitized.

## Submission constraints

Track rules: https://www.joinbuilders.org/buildathon/tracks/machina-deid

Twenty scored attempts total, two submissions per hour, one grading job at a time.
Each attempt needs `corpus/`, `code/` (complete source and pinned dependencies), and
`README.txt` (exact instructions, limitations, source defects and exceptions).
No private mappings, raw input, credentials or private run logs may be packaged.
Keep each submitted attempt immutable until results are published.

Prefer the private Builders upload folder offered by the signed-in submission
panel. A user sign-in and team upload token are needed. Do not consume a scored
attempt until a complete candidate and local validation report are ready.
