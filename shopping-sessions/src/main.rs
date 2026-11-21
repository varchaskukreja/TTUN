//! Shopping Sessions pipeline: CSV click-stream -> user_daily / campaign_daily Parquet.

mod keys;
mod out;
mod parse;
mod ptable;
mod scan;

use hashbrown::HashMap;
use keys::{cmp64, k64, k64_in, mix64b, pack, pack_in, KeyHasher};
use memmap2::Mmap;
use parse::{parse_price, parse_ts};
use ptable::PTable;
use std::fs::File;
use std::hash::BuildHasherDefault;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

pub type KMap<V> = HashMap<u128, V, BuildHasherDefault<KeyHasher>>;
pub type KMap64<V> = HashMap<u64, V, BuildHasherDefault<KeyHasher>>;

const DAY_MS: i64 = 86_400_000;
const GAP_MS: i64 = 1_800_000;
const WEEK_MS: i64 = 604_800_000;
const LATE_MS: i64 = 86_400_000;

const CHUNK: usize = 2 << 20;
const DEDUP_BITS: u32 = 10;
const PERSON_BITS: u32 = 10;
const NO_TS: u32 = u32::MAX;

macro_rules! log {
    ($t0:expr, $($arg:tt)*) => {
        if std::env::var_os("SS_QUIET").is_none() {
            eprintln!("[{:>8.3}s] {}", $t0.elapsed().as_secs_f64(), format!($($arg)*));
        }
    };
}

/// Runs `ntasks` tasks over one thread per state, handing out task indices dynamically.
fn run_pool<S: Send, F: Fn(&mut S, usize) + Sync>(ntasks: usize, mut states: Vec<S>, f: F) -> Vec<S> {
    let next = AtomicUsize::new(0);
    std::thread::scope(|s| {
        for st in states.iter_mut() {
            let next = &next;
            let f = &f;
            s.spawn(move || loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= ntasks {
                    break;
                }
                f(st, i);
            });
        }
    });
    states
}

struct Input {
    maps: Vec<Mmap>,
    /// (file index, start, end) byte ranges, each a run of whole lines with headers skipped.
    chunks: Vec<(usize, usize, usize)>,
    bytes: usize,
}

impl Input {
    fn chunk(&self, i: usize) -> &[u8] {
        let (fi, s, e) = self.chunks[i];
        &self.maps[fi][s..e]
    }
}

fn list_csv(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(rd) => rd
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().map(|x| x == "csv").unwrap_or(false) && p.is_file())
            .collect(),
        Err(_) => Vec::new(),
    };
    v.sort();
    v
}

fn open_input(dir: &Path) -> Input {
    let mut maps = Vec::new();
    let mut chunks = Vec::new();
    let mut bytes = 0;
    for p in list_csv(dir) {
        let f = File::open(&p).expect("open");
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        if len == 0 {
            continue;
        }
        let m = unsafe { Mmap::map(&f).expect("mmap") };
        let data: &[u8] = &m;
        bytes += data.len();
        let fi = maps.len();
        // Skip the header line.
        let mut pos = match memchr::memchr(b'\n', data) {
            Some(i) => i + 1,
            None => data.len(),
        };
        while pos < data.len() {
            let mut end = (pos + CHUNK).min(data.len());
            if end < data.len() {
                end = match memchr::memchr(b'\n', &data[end..]) {
                    Some(i) => end + i + 1,
                    None => data.len(),
                };
            }
            chunks.push((fi, pos, end));
            pos = end;
        }
        maps.push(m);
    }
    // Biggest chunks first so stragglers are small.
    chunks.sort_by_key(|c| std::cmp::Reverse(c.2 - c.1));
    Input { maps, chunks, bytes }
}

fn read_small(path: &Path) -> Vec<u8> {
    std::fs::read(path).unwrap_or_default()
}

fn body(raw: &[u8]) -> &[u8] {
    match memchr::memchr(b'\n', raw) {
        Some(i) => &raw[i + 1..],
        None => &[][..],
    }
}

fn manifest_ts(text: &str, key: &str) -> i64 {
    let i = text.find(key).expect("manifest key");
    let rest = &text[i + key.len()..];
    let q1 = rest.find('"').unwrap();
    let rest = &rest[q1 + 1..];
    let q2 = rest.find('"').unwrap();
    parse_ts(rest[..q2].as_bytes()).expect("manifest ts")
}

/// One parsed copy of an event. `ts` is ms after window_start, or NO_TS if the copy is invalid.
/// `pc` packs category << 24 | price (price 0 unless a purchase).
#[derive(Clone, Copy)]
#[repr(C, packed(8))]
struct EvRaw {
    key: u64,
    uid: u64,
    ingest: i64,
    ts: u32,
    pc: u32,
}

/// A surviving event: `who` is the user id after dedup, the person after routing.
#[derive(Clone, Copy)]
#[repr(C, packed(8))]
struct Ev {
    who: u64,
    ts: u32,
    pc: u32,
}

/// A valid click. `ts` is ms after window_start - 7 days.
#[derive(Clone, Copy)]
#[repr(C, packed(8))]
struct Click {
    who: u64,
    click: u64,
    ts: u32,
    camp: u32,
}

pub struct URow {
    pub person: u64,
    pub day: i32,
    pub sessions: u32,
    pub events: u32,
    pub revenue: i64,
    pub top: i32,
}

struct Ctx {
    ws: i64,
    we: i64,
    products: KMap<u16>,
    unknown_cat: u16,
    campaigns: KMap<u16>,
}

const PF: usize = 16;

#[inline]
fn pbucket(p: u64) -> usize {
    (mix64b(p) >> (64 - PERSON_BITS)) as usize
}

fn main() {
    let t0 = Instant::now();
    let data_dir = PathBuf::from(std::env::var("DATA_DIR").unwrap_or_else(|_| "/data".into()));
    let out_dir = PathBuf::from(std::env::var("OUTPUT_DIR").unwrap_or_else(|_| "/output".into()));
    let nthreads: usize = std::env::var("NCPU")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| std::thread::available_parallelism().map(|n| n.get()).unwrap_or(8))
        .max(1);

    let manifest = String::from_utf8_lossy(&read_small(&data_dir.join("manifest.json"))).into_owned();
    let ws = manifest_ts(&manifest, "\"window_start\"");
    let we = manifest_ts(&manifest, "\"window_end\"");

    // Categories in byte order so ties go to the smallest index.
    let prod_raw = read_small(&data_dir.join("products.csv"));
    let mut prod_rows: Vec<(&[u8], &[u8])> = Vec::new();
    scan::scan::<2, _>(body(&prod_raw), |f| {
        if !f[0].is_empty() {
            prod_rows.push((f[0], f[1]));
        }
    });
    let mut cats: Vec<Vec<u8>> = prod_rows.iter().map(|r| r.1.to_vec()).collect();
    cats.push(b"unknown".to_vec());
    cats.sort();
    cats.dedup();
    assert!(cats.len() < 255);
    let cat_idx = |c: &[u8]| cats.binary_search_by(|x| x.as_slice().cmp(c)).unwrap() as u16;
    let unknown_cat = cat_idx(b"unknown");
    let mut products: KMap<u16> = KMap::default();
    for (p, c) in &prod_rows {
        products.insert(pack(p), cat_idx(c));
    }

    let camp_raw = read_small(&data_dir.join("campaigns.csv"));
    let mut camp_names: Vec<Vec<u8>> = Vec::new();
    let mut camp_channels: Vec<Vec<u8>> = Vec::new();
    let mut campaigns: KMap<u16> = KMap::default();
    scan::scan::<2, _>(body(&camp_raw), |f| {
        if !f[0].is_empty() {
            let k = pack(f[0]);
            if !campaigns.contains_key(&k) {
                campaigns.insert(k, camp_names.len() as u16);
                camp_names.push(f[0].to_vec());
                camp_channels.push(f[1].to_vec());
            }
        }
    });
    let ncamp = camp_names.len();
    let ctx = Ctx { ws, we, products, unknown_cat, campaigns };

    // ---- Parse links, events and clicks in one pool.
    let li = open_input(&data_dir.join("links"));
    let ei = open_input(&data_dir.join("events"));
    let ci = open_input(&data_dir.join("clicks"));
    log!(t0, "opened: links {} MB, events {} MB, clicks {} MB", li.bytes >> 20, ei.bytes >> 20, ci.bytes >> 20);
    let nd = 1usize << DEDUP_BITS;
    let np = 1usize << PERSON_BITS;
    let (nl, ne, nc) = (li.chunks.len(), ei.chunks.len(), ci.chunks.len());
    let ev_est = ei.bytes / 70 + 1024;
    let per_bucket = ev_est * 9 / 8 / (nd * nthreads) + 16;
    // Links are small: one thread parses them and runs the union-find while the rest parse events.
    let (persons, ev_parts) = std::thread::scope(|s| {
        let h = s.spawn(|| {
            let mut links: Vec<(u64, u64)> = Vec::with_capacity(li.bytes / 20);
            for i in 0..nl {
                scan::scan::<2, _>(li.chunk(i), |f| {
                    if !f[0].is_empty() && !f[1].is_empty() {
                        links.push((k64(f[0]), k64(f[1])));
                    }
                });
            }
            build_persons(links)
        });
        let states: Vec<Vec<Vec<EvRaw>>> =
            (0..nthreads).map(|_| (0..nd).map(|_| Vec::with_capacity(per_bucket)).collect()).collect();
        let ev_parts = run_pool(ne, states, |st, i| parse_events(ei.chunk(i), &ctx, st));
        log!(t0, "parsed {} event chunks", ne);
        (h.join().unwrap(), ev_parts)
    });
    log!(t0, "persons ready: {} linked ids", persons.len());

    // ---- Dedup events by event_id and parse clicks; route both to person buckets.
    let ngroups = nthreads * 8;
    let per_group = nd.div_ceil(ngroups);
    struct RState {
        ev: Vec<Vec<Ev>>,
        cl: Vec<Vec<Click>>,
    }
    let rstates: Vec<RState> = (0..nthreads)
        .map(|_| RState {
            ev: (0..np).map(|_| Vec::with_capacity(per_bucket * nd / np)).collect(),
            cl: (0..np).map(|_| Vec::new()).collect(),
        })
        .collect();
    let routed = run_pool(nc + ngroups, rstates, |st, i| {
        if i < nc {
            parse_clicks(ci.chunk(i), &ctx, &persons, &mut st.cl);
            return;
        }
        let g = i - nc;
        let mut map: KMap64<u32> = KMap64::default();
        let mut buf: Vec<EvRaw> = Vec::new();
        for b in g * per_group..((g + 1) * per_group).min(nd) {
            buf.clear();
            map.clear();
            for t in 0..ev_parts.len() {
                for r in &ev_parts[t][b] {
                    let key = r.key;
                    match map.get_mut(&key) {
                        Some(i) => {
                            let cur = &mut buf[*i as usize];
                            if r.ingest < cur.ingest {
                                *cur = *r;
                            }
                        }
                        None => {
                            map.insert(key, buf.len() as u32);
                            buf.push(*r);
                        }
                    }
                }
            }
            let n = buf.len();
            for i in 0..n {
                if i + PF < n {
                    let r2 = &buf[i + PF];
                    if r2.ts != NO_TS {
                        persons.prefetch(r2.uid);
                    }
                }
                let r = &buf[i];
                if r.ts != NO_TS {
                    let p = persons.person(r.uid);
                    unsafe { st.ev.get_unchecked_mut(pbucket(p)) }.push(Ev { who: p, ts: r.ts, pc: r.pc });
                }
            }
        }
    });
    drop(ev_parts);
    log!(t0, "dedup + routed");

    // ---- Per person: sessions, attribution, assists.
    let day0 = ws.div_euclid(DAY_MS);
    let ndays = ((we - 1).div_euclid(DAY_MS) - day0 + 1) as usize;
    let ncat = cats.len();
    let cl_off = ws - WEEK_MS;
    struct SState {
        urows: Vec<URow>,
        camp: Vec<[i64; 4]>,
    }
    let sres = run_pool(np, (0..nthreads).map(|_| SState { urows: Vec::new(), camp: vec![[0i64; 4]; (ncamp + 1) * ndays] }).collect(), |st, b| {
        let mut evs: Vec<Ev> = Vec::with_capacity(routed.iter().map(|r| r.ev[b].len()).sum());
        for r in &routed {
            evs.extend_from_slice(&r.ev[b]);
        }
        let mut cls: Vec<Click> = Vec::with_capacity(routed.iter().map(|r| r.cl[b].len()).sum());
        for r in &routed {
            cls.extend_from_slice(&r.cl[b]);
        }
        evs.sort_unstable_by(|a, b| { a.who }.cmp(&{ b.who }).then({ a.ts }.cmp(&{ b.ts })));
        cls.sort_unstable_by(|a, b| { a.who }.cmp(&{ b.who }).then({ a.ts }.cmp(&{ b.ts })).then_with(|| cmp64(a.click, b.click)));
        let mut sc = Scratch::new(ncat);
        let mut j = 0usize;
        let mut i = 0usize;
        while i < evs.len() {
            let p = evs[i].who;
            let mut k = i + 1;
            while k < evs.len() && { evs[k].who } == p {
                k += 1;
            }
            while j < cls.len() && { cls[j].who } < p {
                j += 1;
            }
            let mut jk = j;
            while jk < cls.len() && { cls[jk].who } == p {
                jk += 1;
            }
            let pctx = PCtx { ws, cl_off, day0, ndays, ncamp };
            process_person(p, &evs[i..k], &cls[j..jk], &pctx, &mut sc, &mut st.urows, &mut st.camp);
            i = k;
            j = jk;
        }
    });
    drop(routed);
    log!(t0, "sessions done");

    // ---- Merge campaign counters and write outputs.
    let ud = out_dir.join("user_daily");
    let cd = out_dir.join("campaign_daily");
    std::fs::create_dir_all(&ud).unwrap();
    std::fs::create_dir_all(&cd).unwrap();
    let mut camp_tot = vec![[0i64; 4]; (ncamp + 1) * ndays];
    for st in &sres {
        for (a, b) in camp_tot.iter_mut().zip(st.camp.iter()) {
            for x in 0..4 {
                a[x] += b[x];
            }
        }
    }
    let organic = b"organic".to_vec();
    let mut crow = out::CampRows::default();
    for c in 0..=ncamp {
        for d in 0..ndays {
            let v = camp_tot[c * ndays + d];
            if v[0] > 0 || v[3] > 0 {
                let (name, chan) = if c == ncamp { (&organic, &organic) } else { (&camp_names[c], &camp_channels[c]) };
                crow.push(name, chan, (day0 + d as i64) as i32, v);
            }
        }
    }
    std::thread::scope(|s| {
        for (i, st) in sres.iter().enumerate() {
            let path = ud.join(format!("part-{i:05}.parquet"));
            let cats = &cats;
            s.spawn(move || out::write_user(&path, &st.urows, cats));
        }
        out::write_camp(&cd.join("part-00000.parquet"), crow);
    });
    log!(t0, "done");
    // Skip freeing the big structures: the process is about to exit.
    std::process::exit(0);
}

struct PCtx {
    ws: i64,
    cl_off: i64,
    day0: i64,
    ndays: usize,
    ncamp: usize,
}

struct Scratch {
    sess: Vec<Sess>,
    catsum: Vec<i64>,
    touched: Vec<u16>,
    cc: Vec<(u32, i64)>,
}

impl Scratch {
    fn new(ncat: usize) -> Self {
        Scratch { sess: Vec::new(), catsum: vec![0; ncat], touched: Vec::new(), cc: Vec::new() }
    }
}

#[derive(Clone, Copy)]
struct Sess {
    start: i64,
    day: u32,
    /// index one past the last session of the same day
    day_end: u32,
    revenue: i64,
    purchase: bool,
}

fn flush_row(sc: &mut Scratch, p: u64, day: i64, s: u32, e: u32, r: i64, urows: &mut Vec<URow>) {
    let mut top: i32 = -1;
    let mut best = 0i64;
    for &c in &sc.touched {
        let v = sc.catsum[c as usize];
        if v > best || (v == best && top >= 0 && (c as i32) < top) {
            best = v;
            top = c as i32;
        }
        sc.catsum[c as usize] = 0;
    }
    sc.touched.clear();
    urows.push(URow { person: p, day: day as i32, sessions: s, events: e, revenue: r, top });
    let n = sc.sess.len() as u32;
    for s in sc.sess.iter_mut().rev() {
        if s.day_end != u32::MAX {
            break;
        }
        s.day_end = n;
    }
}

fn process_person(p: u64, evs: &[Ev], cls: &[Click], c: &PCtx, sc: &mut Scratch, urows: &mut Vec<URow>, camp: &mut [[i64; 4]]) {
    sc.sess.clear();
    let ndays = c.ndays;
    let mut cur_day: i64 = i64::MIN;
    let mut row_sessions = 0u32;
    let mut row_events = 0u32;
    let mut row_rev = 0i64;
    let mut prev_ts = i64::MIN;
    for ev in evs {
        let ts = c.ws + ev.ts as i64;
        if prev_ts == i64::MIN || ts - prev_ts > GAP_MS {
            let day = ts.div_euclid(DAY_MS);
            if day != cur_day {
                if cur_day != i64::MIN {
                    flush_row(sc, p, cur_day, row_sessions, row_events, row_rev, urows);
                }
                cur_day = day;
                row_sessions = 0;
                row_events = 0;
                row_rev = 0;
            }
            row_sessions += 1;
            sc.sess.push(Sess { start: ts, day: (day - c.day0) as u32, day_end: u32::MAX, revenue: 0, purchase: false });
        }
        prev_ts = ts;
        row_events += 1;
        let price = (ev.pc & 0xFF_FFFF) as i64;
        if price > 0 {
            let s = sc.sess.last_mut().unwrap();
            s.revenue += price;
            s.purchase = true;
            row_rev += price;
            let cat = (ev.pc >> 24) as usize;
            if sc.catsum[cat] == 0 {
                sc.touched.push(cat as u16);
            }
            sc.catsum[cat] += price;
        }
    }
    if cur_day != i64::MIN {
        flush_row(sc, p, cur_day, row_sessions, row_events, row_rev, urows);
    }

    // Credit: latest click at or before the start within 7 days, ties to smallest click_id.
    let mut j = 0usize;
    let mut grp = usize::MAX;
    let mut grp_ts = 0i64;
    for s in &sc.sess {
        while j < cls.len() && c.cl_off + cls[j].ts as i64 <= s.start {
            if j == 0 || { cls[j].ts } != { cls[j - 1].ts } {
                grp = j;
                grp_ts = c.cl_off + cls[j].ts as i64;
            }
            j += 1;
        }
        let k = if grp != usize::MAX && s.start - grp_ts <= WEEK_MS { cls[grp].camp as usize } else { c.ncamp };
        let a = &mut camp[k * ndays + s.day as usize];
        a[0] += 1;
        a[1] += s.purchase as i64;
        a[2] += s.revenue;
    }

    // Assists: each campaign's clicks cover [ts, ts + 7d]; count sessions starting inside the union.
    if cls.is_empty() {
        return;
    }
    sc.cc.clear();
    sc.cc.extend(cls.iter().map(|k| (k.camp, c.cl_off + k.ts as i64)));
    sc.cc.sort_unstable();
    let sess = &sc.sess;
    let cc = &sc.cc;
    let mut i = 0;
    while i < cc.len() {
        let k = cc[i].0;
        let mut lo = cc[i].1;
        let mut hi = lo + WEEK_MS;
        i += 1;
        loop {
            let more = i < cc.len() && cc[i].0 == k;
            if more && cc[i].1 <= hi + 1 {
                hi = cc[i].1 + WEEK_MS;
                i += 1;
                continue;
            }
            let a = sess.partition_point(|s| s.start < lo);
            let b = sess.partition_point(|s| s.start <= hi);
            let mut x = a;
            while x < b {
                let e = (sess[x].day_end as usize).min(b);
                camp[k as usize * ndays + sess[x].day as usize][3] += (e - x) as i64;
                x = e;
            }
            if !more {
                break;
            }
            lo = cc[i].1;
            hi = lo + WEEK_MS;
            i += 1;
        }
    }
}

fn parse_events(data: &[u8], ctx: &Ctx, out: &mut [Vec<EvRaw>]) {
    let end = data.as_ptr() as usize + data.len();
    scan::scan::<7, _>(data, |f| {
        if f[0].is_empty() {
            return;
        }
        let key = k64_in(f[0], end);
        let ingest = parse_ts(f[3]).unwrap_or(i64::MAX);
        let mut ts_rel = NO_TS;
        let mut pc = 0u32;
        let mut uid = 0u64;
        'v: {
            if f[1].is_empty() || f[5].is_empty() {
                break 'v;
            }
            let ts = match parse_ts(f[2]) {
                Some(t) if t >= ctx.ws && t < ctx.we => t,
                _ => break 'v,
            };
            let purchase = match f[4] {
                b"view" | b"cart" => false,
                b"purchase" => true,
                _ => break 'v,
            };
            if purchase {
                let price = match parse_price(f[6]) {
                    Some(p) => p,
                    None => break 'v,
                };
                let cat = *ctx.products.get(&pack_in(f[5], end)).unwrap_or(&ctx.unknown_cat);
                pc = ((cat as u32) << 24) | price;
            }
            if ingest == i64::MAX || ingest - ts > LATE_MS {
                break 'v;
            }
            uid = k64_in(f[1], end);
            ts_rel = (ts - ctx.ws) as u32;
        }
        let b = (mix64b(key) >> (64 - DEDUP_BITS)) as usize;
        unsafe { out.get_unchecked_mut(b) }.push(EvRaw { key, uid, ingest, ts: ts_rel, pc });
    });
}

fn parse_clicks(data: &[u8], ctx: &Ctx, persons: &PTable, out: &mut [Vec<Click>]) {
    let lo = ctx.ws - WEEK_MS;
    let mut local: Vec<Click> = Vec::with_capacity(data.len() / 48);
    let end = data.as_ptr() as usize + data.len();
    scan::scan::<4, _>(data, |f| {
        if f[0].is_empty() || f[1].is_empty() {
            return;
        }
        let ts = match parse_ts(f[3]) {
            Some(t) if t >= lo && t < ctx.we => t,
            _ => return,
        };
        let camp = match ctx.campaigns.get(&pack_in(f[2], end)) {
            Some(&c) => c,
            None => return,
        };
        local.push(Click { who: k64_in(f[1], end), click: k64_in(f[0], end), ts: (ts - lo) as u32, camp: camp as u32 });
    });
    let n = local.len();
    for i in 0..n {
        if i + PF < n {
            persons.prefetch(local[i + PF].who);
        }
        let c = local[i];
        let p = persons.person(c.who);
        unsafe { out.get_unchecked_mut(pbucket(p)) }.push(Click { who: p, ..c });
    }
}

/// Union-find over linked IDs; returns id -> person name (smallest id) for every linked id.
fn build_persons(links: Vec<(u64, u64)>) -> PTable {
    let total = links.len();
    let mut idx: KMap64<u32> = KMap64::with_capacity_and_hasher(total, Default::default());
    let mut ids: Vec<u64> = Vec::with_capacity(total);
    let mut parent: Vec<u32> = Vec::with_capacity(total);
    fn find(parent: &mut [u32], mut x: u32) -> u32 {
        while parent[x as usize] != x {
            let g = parent[parent[x as usize] as usize];
            parent[x as usize] = g;
            x = g;
        }
        x
    }
    {
        for &(a, b) in &links {
            let mut get = |k: u64| -> u32 {
                *idx.entry(k).or_insert_with(|| {
                    ids.push(k);
                    parent.push(parent.len() as u32);
                    (ids.len() - 1) as u32
                })
            };
            let ia = get(a);
            let ib = get(b);
            let ra = find(&mut parent, ia);
            let rb = find(&mut parent, ib);
            if ra != rb {
                // Keep the root at the smaller id so the root is the person's name.
                if cmp64(ids[ra as usize], ids[rb as usize]).is_lt() {
                    parent[rb as usize] = ra;
                } else {
                    parent[ra as usize] = rb;
                }
            }
        }
    }
    drop(idx);
    let mut persons = PTable::with_capacity(ids.len());
    for i in 0..ids.len() {
        let r = find(&mut parent, i as u32);
        persons.insert(ids[i], ids[r as usize]);
    }
    persons
}
