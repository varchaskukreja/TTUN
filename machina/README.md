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
uv run --project machina python -m machina.inventory .machina-private/manifest.tsv
```

To download a bounded, deterministic sample onto a **case-sensitive** volume:

```sh
uv run --project machina python -m machina.inventory .machina-private/manifest.tsv \
  --sample-dir /your/case-sensitive-volume/input --budget-mb 100
uv run --project machina python -m machina.scan /your/case-sensitive-volume/input \
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
- `native.py`: strict OLE property parsing and bounded variable-length string replacement
  within existing stream capacity.
  Other OLE streams are preserved; unsupported structures are reported.
- `pdf_handler.py`: known-identity text and metadata replacement with PDF drawings
  preserved; reports unhandled content.
- `solidworks.py`: bounded modern SolidWorks stream parsing and XML metadata
  rewriting with fixed spans and CRC updates; native application reopen unverified.
- `archives.py`: bounded ZIP/TAR/GZIP recursion; rejects unsafe paths and unsupported
  children. Unsupported archives do not get copied unchanged into candidate output.
- `text_handler.py`: known-identity replacement in XML values, preserving keys.
- `freecad.py`: typed native XML, BREP, colors and material texture references.
- `ocr.py`: local Tesseract candidate extraction, with page/time/pixel limits.
- `raster.py`: opt-in known-identity replacement in scanned PDF regions and RGB
  PNGs. Untouched PNG pixels are preserved; edited regions require visual review.
- `discovery.py`: semantic author/company/contact/provenance candidate records.
- `probe.py`: bounded stratified remote samples processed in memory, persisting
  only private metadata and candidate evidence.
- `scan.py`: private review inventory, not an automatic identity classifier.
- `pipeline.py`: coordinated path/content transformation and a private coverage ledger.

To run a development candidate with a reviewed JSON registry and private key:

```sh
uv run --project machina python -m machina.pipeline /private/input /private/candidate \
  --registry /private/identities.json --key-file /private/pseudonym.key \
  --private-report /private/candidate-report.jsonl
```

The registry is a JSON array of objects with `kind`, `identity`, optional `aliases`,
and optional `pseudonym`. Supported kinds are person, company, project, part,
account, email, domain, address and phone. Generate a private random key of at
least 16 bytes once and reuse it across the corpus. Without explicit pseudonyms,
HMAC-derived pseudonyms are deterministic. OLE string growth is supported when existing stream padding or shrinking fields
provide capacity; unaffordable growth is explicitly rejected.

Every run currently reports `ready_to_submit: false`. This is intentional:
identity discovery, visual validation and native compatibility are incomplete.
Never treat a successful candidate run as a validated final corpus.

This is not yet a full-corpus submission pipeline. Native CAD references, non-XML SolidWorks metadata, archive member
coverage, image/logo sanitization and OCR-based replacement require further work. Passing unit tests does not establish complete de-identification or
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

## Broader discovery without retaining raw downloads

```sh
uv run --project machina python -m machina.probe .machina-private/manifest.tsv \
  --private-report .machina-private/probe.jsonl --per-format 5 --budget-mb 20
uv run --project machina python -m machina.discovery \
  --manifest .machina-private/manifest.tsv --scan-report .machina-private/candidates.jsonl \
  --input /your/case-sensitive-volume/input --private-output .machina-private/identities-review.jsonl
```

Candidate confidence describes evidence, not approval. Even an author/company
field can hold a software vendor or generic value. Review and reconcile aliases
before constructing the registry. `probe.py` is a storage-saving discovery tool;
it does not satisfy the requirement to process and upload the whole corpus.

Pass `--ocr` to `pipeline` to enable OCR-guided image edits, including RGB PNG
thumbnails in archives. The current tested external OCR runtime is Tesseract
5.5.4 with Leptonica 1.87.0. Image regions with uncertain recognition, unsupported
transparency, logos, and geometry overlap remain explicitly flagged. OCR does
not independently validate geometry or guarantee complete identification.
