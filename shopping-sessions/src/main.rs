//! Shopping Sessions pipeline: CSV click-stream -> user_daily / campaign_daily Parquet.

mod keys;
mod parse;
mod out;

use hashbrown::HashMap;
use keys::{cmp_keys, mix, mix2, pack, KeyHasher};
use memmap2::Mmap;
use parse::{lines, parse_price, parse_ts, split};
use std::fs::File;
use std::hash::BuildHasherDefault;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

pub type KMap<V> = HashMap<u128, V, BuildHasherDefault<KeyHasher>>;

const DAY_MS: i64 = 86_400_000;
const GAP_MS: i64 = 1_800_000;
const WEEK_MS: i64 = 604_800_000;
const LATE_MS: i64 = 86_400_000;

const CHUNK: usize = 4 << 20;
const DEDUP_BITS: u32 = 10;
const PERSON_BITS: u32 = 10;

macro_rules! log {
    ($t0:expr, $($arg:tt)*) => {
        eprintln!("[{:>8.3}s] {}", $t0.elapsed().as_secs_f64(), format!($($arg)*));
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
    for p in list_csv(dir) {
        let f = File::open(&p).expect("open");
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        if len == 0 {
            continue;
        }
        let m = unsafe { Mmap::map(&f).expect("mmap") };
        let data: &[u8] = &m;
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
    Input { maps, chunks }
}

fn read_small(path: &Path) -> Vec<u8> {
    std::fs::read(path).unwrap_or_default()
}

fn manifest_ts(text: &str, key: &str) -> i64 {
    let i = text.find(key).expect("manifest key");
    let rest = &text[i + key.len()..];
    let q1 = rest.find('"').unwrap();
    let rest = &rest[q1 + 1..];
    let q2 = rest.find('"').unwrap();
    parse_ts(rest[..q2].as_bytes()).expect("manifest ts")
}

#[derive(Clone, Copy)]
struct EvRaw {
    key: u128,
    person: u128,
    ingest: i64,
    ts: i64,
    price: u32,
    cat: u16,
    /// bit0: valid, bit1: purchase
    flags: u8,
}

#[derive(Clone, Copy)]
struct Ev {
    person: u128,
    ts: i64,
    price: u32,
    cat: u16,
    purchase: bool,
}

#[derive(Clone, Copy)]
struct Click {
    person: u128,
    click: u128,
    ts: i64,
    camp: u16,
}

pub struct URow {
    pub person: u128,
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
    persons: KMap<u128>,
}

impl Ctx {
    #[inline]
    fn person(&self, uid: u128) -> u128 {
        match self.persons.get(&uid) {
            Some(&p) => p,
            None => uid,
        }
    }
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
    let body = match memchr::memchr(b'\n', &prod_raw) {
        Some(i) => &prod_raw[i + 1..],
        None => &[][..],
    };
    for line in lines(body) {
        let mut f = [&[][..]; 2];
        if split(line, &mut f) && !f[0].is_empty() {
            prod_rows.push((f[0], f[1]));
        }
    }
    let mut cats: Vec<Vec<u8>> = prod_rows.iter().map(|r| r.1.to_vec()).collect();
    cats.push(b"unknown".to_vec());
    cats.sort();
    cats.dedup();
    let cat_idx = |c: &[u8]| cats.binary_search_by(|x| x.as_slice().cmp(c)).unwrap() as u16;
    let unknown_cat = cat_idx(b"unknown");
    let mut products: KMap<u16> = KMap::default();
    for (p, c) in &prod_rows {
        products.insert(pack(p), cat_idx(c));
    }

    let camp_raw = read_small(&data_dir.join("campaigns.csv"));
    let body = match memchr::memchr(b'\n', &camp_raw) {
        Some(i) => &camp_raw[i + 1..],
        None => &[][..],
    };
    let mut camp_names: Vec<Vec<u8>> = Vec::new();
    let mut camp_channels: Vec<Vec<u8>> = Vec::new();
    let mut campaigns: KMap<u16> = KMap::default();
    for line in lines(body) {
        let mut f = [&[][..]; 2];
        if split(line, &mut f) && !f[0].is_empty() {
            let k = pack(f[0]);
            if !campaigns.contains_key(&k) {
                campaigns.insert(k, camp_names.len() as u16);
                camp_names.push(f[0].to_vec());
                camp_channels.push(f[1].to_vec());
            }
        }
    }
    let ncamp = camp_names.len();
    log!(t0, "window {}..{} products {} cats {} campaigns {}", ws, we, products.len(), cats.len(), ncamp);

    // ---- Links -> persons.
    let links_in = open_input(&data_dir.join("links"));
    let link_parts = run_pool(links_in.chunks.len(), (0..nthreads).map(|_| Vec::<(u128, u128)>::new()).collect(), |st, i| {
        let (fi, s, e) = links_in.chunks[i];
        for line in lines(&links_in.maps[fi][s..e]) {
            let mut f = [&[][..]; 2];
            if split(line, &mut f) && !f[0].is_empty() && !f[1].is_empty() {
                st.push((pack(f[0]), pack(f[1])));
            }
        }
    });
    let persons = build_persons(link_parts);
    log!(t0, "links -> {} linked ids", persons.len());

    let ctx = Ctx { ws, we, products, unknown_cat, campaigns, persons };

    // ---- Events and clicks: parse, validate, partition.
    let ev_in = open_input(&data_dir.join("events"));
    let cl_in = open_input(&data_dir.join("clicks"));
    let nd = 1usize << DEDUP_BITS;
    let np = 1usize << PERSON_BITS;
    let nev_chunks = ev_in.chunks.len();
    let ntask = nev_chunks + cl_in.chunks.len();
    let states: Vec<(Vec<Vec<EvRaw>>, Vec<Vec<Click>>)> =
        (0..nthreads).map(|_| ((0..nd).map(|_| Vec::new()).collect(), (0..np).map(|_| Vec::new()).collect())).collect();
    let parsed = run_pool(ntask, states, |st, i| {
        if i < nev_chunks {
            let (fi, s, e) = ev_in.chunks[i];
            parse_events(&ev_in.maps[fi][s..e], &ctx, &mut st.0);
        } else {
            let (fi, s, e) = cl_in.chunks[i - nev_chunks];
            parse_clicks(&cl_in.maps[fi][s..e], &ctx, &mut st.1);
        }
    });
    log!(t0, "parsed {} event chunks, {} click chunks", nev_chunks, cl_in.chunks.len());
    let mut ev_parts: Vec<Vec<Vec<EvRaw>>> = Vec::with_capacity(nthreads);
    let mut cl_parts: Vec<Vec<Vec<Click>>> = Vec::with_capacity(nthreads);
    for (e, c) in parsed {
        ev_parts.push(e);
        cl_parts.push(c);
    }

    // ---- Dedup events by event_id, re-partition survivors by person.
    let ngroups = nthreads * 4;
    let per_group = nd.div_ceil(ngroups);
    let dd_states: Vec<Vec<Vec<Vec<Ev>>>> = (0..nthreads).map(|_| Vec::new()).collect();
    let dd = run_pool(ngroups, dd_states, |st, g| {
        let mut outb: Vec<Vec<Ev>> = (0..np).map(|_| Vec::new()).collect();
        let mut map: KMap<u32> = KMap::default();
        let mut buf: Vec<EvRaw> = Vec::new();
        for b in g * per_group..((g + 1) * per_group).min(nd) {
            buf.clear();
            map.clear();
            for t in 0..ev_parts.len() {
                for r in &ev_parts[t][b] {
                    match map.get_mut(&r.key) {
                        Some(i) => {
                            if r.ingest < buf[*i as usize].ingest {
                                buf[*i as usize] = *r;
                            }
                        }
                        None => {
                            map.insert(r.key, buf.len() as u32);
                            buf.push(*r);
                        }
                    }
                }
            }
            for r in &buf {
                if r.flags & 1 != 0 {
                    let pb = (mix2(r.person) >> (64 - PERSON_BITS)) as usize;
                    outb[pb].push(Ev { person: r.person, ts: r.ts, price: r.price, cat: r.cat, purchase: r.flags & 2 != 0 });
                }
            }
        }
        st.push(outb);
    });
    drop(ev_parts);
    let ev_groups: Vec<Vec<Vec<Ev>>> = dd.into_iter().flatten().collect();
    log!(t0, "dedup done");

    // ---- Per person: sessions, attribution, assists.
    let ndays = ((we - 1).div_euclid(DAY_MS) - ws.div_euclid(DAY_MS) + 1) as usize;
    let day0 = ws.div_euclid(DAY_MS);
    let ncat = cats.len();
    struct PState {
        urows: Vec<URow>,
        camp: Vec<[i64; 4]>,
    }
    let pstates: Vec<PState> =
        (0..nthreads).map(|_| PState { urows: Vec::new(), camp: vec![[0i64; 4]; (ncamp + 1) * ndays] }).collect();
    let pres = run_pool(np, pstates, |st, b| {
        let mut evs: Vec<Ev> = Vec::new();
        for g in &ev_groups {
            evs.extend_from_slice(&g[b]);
        }
        let mut cls: Vec<Click> = Vec::new();
        for t in &cl_parts {
            cls.extend_from_slice(&t[b]);
        }
        evs.sort_unstable_by(|a, b| a.person.cmp(&b.person).then(a.ts.cmp(&b.ts)));
        cls.sort_unstable_by(|a, b| a.person.cmp(&b.person).then(a.ts.cmp(&b.ts)).then_with(|| cmp_keys(a.click, b.click)));
        let mut sc = Scratch::new(ncat);
        let mut j = 0usize;
        let mut i = 0usize;
        while i < evs.len() {
            let p = evs[i].person;
            let mut k = i + 1;
            while k < evs.len() && evs[k].person == p {
                k += 1;
            }
            while j < cls.len() && cls[j].person < p {
                j += 1;
            }
            let mut jk = j;
            while jk < cls.len() && cls[jk].person == p {
                jk += 1;
            }
            process_person(p, &evs[i..k], &cls[j..jk], day0, ndays, ncamp, &mut sc, &mut st.urows, &mut st.camp);
            i = k;
            j = jk;
        }
    });
    drop(ev_groups);
    drop(cl_parts);
    log!(t0, "sessions done");

    // ---- Merge campaign counters and write outputs.
    let mut camp_tot = vec![[0i64; 4]; (ncamp + 1) * ndays];
    let mut urows_parts: Vec<Vec<URow>> = Vec::new();
    for st in pres {
        for (a, b) in camp_tot.iter_mut().zip(st.camp.iter()) {
            for x in 0..4 {
                a[x] += b[x];
            }
        }
        urows_parts.push(st.urows);
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
    out::write_all(&out_dir, urows_parts, crow, &cats);
    log!(t0, "done");
}

struct Scratch {
    sess: Vec<Sess>,
    catsum: Vec<i64>,
    touched: Vec<u16>,
    cc: Vec<(u16, i64)>,
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

#[allow(clippy::too_many_arguments)]
fn process_person(
    p: u128,
    evs: &[Ev],
    cls: &[Click],
    day0: i64,
    ndays: usize,
    ncamp: usize,
    sc: &mut Scratch,
    urows: &mut Vec<URow>,
    camp: &mut [[i64; 4]],
) {
    sc.sess.clear();
    // Current user-day row.
    let mut cur_day: i64 = i64::MIN;
    let mut row_sessions = 0u32;
    let mut row_events = 0u32;
    let mut row_rev = 0i64;
    let mut prev_ts = i64::MIN;
    let flush = |sc: &mut Scratch, day: i64, s: u32, e: u32, r: i64, urows: &mut Vec<URow>| {
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
    };
    for ev in evs {
        if prev_ts == i64::MIN || ev.ts - prev_ts > GAP_MS {
            let day = ev.ts.div_euclid(DAY_MS);
            if day != cur_day {
                if cur_day != i64::MIN {
                    flush(sc, cur_day, row_sessions, row_events, row_rev, urows);
                    let n = sc.sess.len() as u32;
                    for s in sc.sess.iter_mut().rev() {
                        if s.day_end != u32::MAX {
                            break;
                        }
                        s.day_end = n;
                    }
                }
                cur_day = day;
                row_sessions = 0;
                row_events = 0;
                row_rev = 0;
            }
            row_sessions += 1;
            sc.sess.push(Sess { start: ev.ts, day: (day - day0) as u32, day_end: u32::MAX, revenue: 0, purchase: false });
        }
        prev_ts = ev.ts;
        row_events += 1;
        if ev.purchase {
            let s = sc.sess.last_mut().unwrap();
            s.revenue += ev.price as i64;
            s.purchase = true;
            row_rev += ev.price as i64;
            let c = ev.cat as usize;
            if sc.catsum[c] == 0 {
                sc.touched.push(ev.cat);
            }
            sc.catsum[c] += ev.price as i64;
        }
    }
    if cur_day != i64::MIN {
        flush(sc, cur_day, row_sessions, row_events, row_rev, urows);
        let n = sc.sess.len() as u32;
        for s in sc.sess.iter_mut().rev() {
            if s.day_end != u32::MAX {
                break;
            }
            s.day_end = n;
        }
    }

    // Credit: latest click at or before the start within 7 days, ties to smallest click_id.
    let mut j = 0usize;
    let mut grp = usize::MAX;
    for s in &sc.sess {
        while j < cls.len() && cls[j].ts <= s.start {
            if j == 0 || cls[j].ts != cls[j - 1].ts {
                grp = j;
            }
            j += 1;
        }
        let c = if grp != usize::MAX && s.start - cls[grp].ts <= WEEK_MS { cls[grp].camp as usize } else { ncamp };
        let a = &mut camp[c * ndays + s.day as usize];
        a[0] += 1;
        a[1] += s.purchase as i64;
        a[2] += s.revenue;
    }

    // Assists: each campaign's clicks cover [ts, ts + 7d]; count sessions starting inside the union.
    if cls.is_empty() {
        return;
    }
    sc.cc.clear();
    sc.cc.extend(cls.iter().map(|c| (c.camp, c.ts)));
    sc.cc.sort_unstable();
    let sess = &sc.sess;
    let mut i = 0;
    while i < sc.cc.len() {
        let c = sc.cc[i].0;
        let mut lo = sc.cc[i].1;
        let mut hi = lo + WEEK_MS;
        i += 1;
        loop {
            let more = i < sc.cc.len() && sc.cc[i].0 == c;
            if more && sc.cc[i].1 <= hi + 1 {
                hi = sc.cc[i].1 + WEEK_MS;
                i += 1;
                continue;
            }
            // Sessions with lo <= start <= hi.
            let a = sess.partition_point(|s| s.start < lo);
            let b = sess.partition_point(|s| s.start <= hi);
            let mut x = a;
            while x < b {
                let e = (sess[x].day_end as usize).min(b);
                camp[c as usize * ndays + sess[x].day as usize][3] += (e - x) as i64;
                x = e;
            }
            if !more {
                break;
            }
            lo = sc.cc[i].1;
            hi = lo + WEEK_MS;
            i += 1;
        }
    }
}

fn parse_events(data: &[u8], ctx: &Ctx, out: &mut [Vec<EvRaw>]) {
    for line in lines(data) {
        let mut f = [&[][..]; 7];
        if !split(line, &mut f) || f[0].is_empty() {
            continue;
        }
        let key = pack(f[0]);
        let ingest = parse_ts(f[3]).unwrap_or(i64::MAX);
        let mut flags = 0u8;
        let mut ts = 0i64;
        let mut price = 0u32;
        let mut cat = 0u16;
        let mut person = 0u128;
        'v: {
            if f[1].is_empty() || f[5].is_empty() {
                break 'v;
            }
            ts = match parse_ts(f[2]) {
                Some(t) if t >= ctx.ws && t < ctx.we => t,
                _ => break 'v,
            };
            let purchase = match f[4] {
                b"view" | b"cart" => false,
                b"purchase" => true,
                _ => break 'v,
            };
            if purchase {
                price = match parse_price(f[6]) {
                    Some(p) => p,
                    None => break 'v,
                };
                cat = *ctx.products.get(&pack(f[5])).unwrap_or(&ctx.unknown_cat);
            }
            if ingest == i64::MAX || ingest - ts > LATE_MS {
                break 'v;
            }
            person = ctx.person(pack(f[1]));
            flags = 1 | ((purchase as u8) << 1);
        }
        let b = (mix2(key) >> (64 - DEDUP_BITS)) as usize;
        out[b].push(EvRaw { key, person, ingest, ts, price, cat, flags });
    }
}

fn parse_clicks(data: &[u8], ctx: &Ctx, out: &mut [Vec<Click>]) {
    let lo = ctx.ws - WEEK_MS;
    for line in lines(data) {
        let mut f = [&[][..]; 4];
        if !split(line, &mut f) || f[0].is_empty() || f[1].is_empty() {
            continue;
        }
        let ts = match parse_ts(f[3]) {
            Some(t) if t >= lo && t < ctx.we => t,
            _ => continue,
        };
        let camp = match ctx.campaigns.get(&pack(f[2])) {
            Some(&c) => c,
            None => continue,
        };
        let person = ctx.person(pack(f[1]));
        let b = (mix2(person) >> (64 - PERSON_BITS)) as usize;
        out[b].push(Click { person, click: pack(f[0]), ts, camp });
    }
}

/// Union-find over linked IDs; returns id -> person name (smallest id) for every linked id.
fn build_persons(parts: Vec<Vec<(u128, u128)>>) -> KMap<u128> {
    let total: usize = parts.iter().map(|p| p.len()).sum();
    let mut idx: KMap<u32> = KMap::with_capacity_and_hasher(total, Default::default());
    let mut ids: Vec<u128> = Vec::new();
    let mut parent: Vec<u32> = Vec::new();
    fn find(parent: &mut [u32], mut x: u32) -> u32 {
        while parent[x as usize] != x {
            let g = parent[parent[x as usize] as usize];
            parent[x as usize] = g;
            x = g;
        }
        x
    }
    for part in &parts {
        for &(a, b) in part {
            let mut get = |k: u128| -> u32 {
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
                if cmp_keys(ids[ra as usize], ids[rb as usize]).is_lt() {
                    parent[rb as usize] = ra;
                } else {
                    parent[ra as usize] = rb;
                }
            }
        }
    }
    let mut persons: KMap<u128> = KMap::with_capacity_and_hasher(ids.len(), Default::default());
    for i in 0..ids.len() {
        let r = find(&mut parent, i as u32);
        persons.insert(ids[i], ids[r as usize]);
    }
    let _ = mix;
    persons
}
