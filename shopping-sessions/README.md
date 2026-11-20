# Shopping Sessions (Databricks track)

A native Rust pipeline that turns the click-stream CSVs in `/data` into
`/output/user_daily/` and `/output/campaign_daily/` Parquet, following `rules.md` exactly.

The repository root `Dockerfile` builds this folder into a `linux/amd64` image.

## How it works

1. **Links**: parse every `links/*.csv` in parallel, union-find the IDs, and name
   each person by the byte-wise smallest ID.
2. **Events and clicks**: memory-map every CSV, split the files into ~4 MB line-aligned
   chunks, and parse them on all cores. Each copy of an event is validated as it is
   parsed, and copies are hash-partitioned by `event_id`. Valid clicks are
   hash-partitioned by person. Click duplicates are identical, so they can't change the
   credit or the assists and are not removed.
3. **Dedup**: for each `event_id` partition, keep the copy with the smallest `ingest_ts`,
   then re-partition the surviving valid events by person.
4. **Sessions**: for each person partition, sort events by `(person, ts)` and clicks by
   `(person, ts, click_id)`. The pipeline then sessionizes, credits each session with a
   two-pointer scan, and counts assists. For assists, each campaign's clicks are merged
   into `[ts, ts + 7d]` intervals and the sessions starting inside each interval are
   counted, so whale persons stay linear.
5. **Output**: the Parquet files are written in parallel.

IDs are packed exactly into 128-bit keys: strings of up to 15 bytes are stored
big-endian with their length, so comparing keys as integers compares the strings
byte-wise. Longer strings fall back to a hashed key plus a side table.

## Local checks

```sh
cargo build --release
python3 scripts/run_fixtures.py fixtures -- ./target/release/ss
DATA_DIR=dev OUTPUT_DIR=out ./target/release/ss && python3 scripts/compare.py out dev-answer
```

## Third-party code

These crates come from crates.io: `memchr`, `memmap2`, `hashbrown` and `parquet`
(Apache Arrow). The `fixtures/` and `scripts/` folders and `rules.md` come from the
track's starter kit.

## License

MIT
