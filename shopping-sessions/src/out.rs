//! A minimal Parquet writer for the two output tables: one row group, one PLAIN-encoded,
//! uncompressed data page per column, and a thrift-compact footer.

use crate::keys::{is_long, unpack};
use crate::URow;
use std::fs::File;
use std::io::Write;
use std::path::Path;

const T_INT32: i32 = 1;
const T_INT64: i32 = 2;
const T_BYTE_ARRAY: i32 = 6;
const CT_UTF8: i32 = 0;
const CT_DATE: i32 = 6;

/// Thrift compact protocol encoder.
struct Tc {
    b: Vec<u8>,
    last: Vec<i16>,
}

impl Tc {
    fn new() -> Self {
        Tc { b: Vec::new(), last: vec![0] }
    }
    fn varint(&mut self, mut v: u64) {
        while v >= 0x80 {
            self.b.push((v as u8) | 0x80);
            v >>= 7;
        }
        self.b.push(v as u8);
    }
    fn zz(&mut self, v: i64) {
        self.varint(((v << 1) ^ (v >> 63)) as u64);
    }
    fn field(&mut self, id: i16, ty: u8) {
        let d = id - *self.last.last().unwrap();
        if d > 0 && d <= 15 {
            self.b.push(((d as u8) << 4) | ty);
        } else {
            self.b.push(ty);
            self.zz(id as i64);
        }
        *self.last.last_mut().unwrap() = id;
    }
    fn i32f(&mut self, id: i16, v: i32) {
        self.field(id, 5);
        self.zz(v as i64);
    }
    fn i64f(&mut self, id: i16, v: i64) {
        self.field(id, 6);
        self.zz(v);
    }
    fn strf(&mut self, id: i16, s: &[u8]) {
        self.field(id, 8);
        self.varint(s.len() as u64);
        self.b.extend_from_slice(s);
    }
    fn list_header(&mut self, n: usize, ty: u8) {
        if n < 15 {
            self.b.push(((n as u8) << 4) | ty);
        } else {
            self.b.push(0xF0 | ty);
            self.varint(n as u64);
        }
    }
    fn listf(&mut self, id: i16, n: usize, ty: u8) {
        self.field(id, 9);
        self.list_header(n, ty);
    }
    fn begin(&mut self) {
        self.last.push(0);
    }
    fn structf(&mut self, id: i16) {
        self.field(id, 12);
        self.begin();
    }
    fn end(&mut self) {
        self.b.push(0);
        self.last.pop();
    }
}

#[derive(Clone, Copy)]
enum Kind {
    Str,
    OptStr,
    Date,
    I64,
}

struct Col {
    name: &'static str,
    kind: Kind,
    data: Vec<u8>,
    nvals: usize,
}

fn write_file(path: &Path, cols: Vec<Col>, nrows: usize) {
    let mut out: Vec<u8> = Vec::with_capacity(cols.iter().map(|c| c.data.len() + 64).sum::<usize>() + 4096);
    out.extend_from_slice(b"PAR1");
    let mut metas = Vec::new();
    for c in &cols {
        let offset = out.len() as i64;
        let mut h = Tc::new();
        h.i32f(1, 0); // DATA_PAGE
        h.i32f(2, c.data.len() as i32);
        h.i32f(3, c.data.len() as i32);
        h.structf(5);
        h.i32f(1, nrows as i32);
        h.i32f(2, 0); // PLAIN
        h.i32f(3, 3); // RLE
        h.i32f(4, 3); // RLE
        h.end();
        h.b.push(0);
        out.extend_from_slice(&h.b);
        out.extend_from_slice(&c.data);
        let size = out.len() as i64 - offset;
        metas.push((offset, size));
        let _ = c.nvals;
    }
    let mut m = Tc::new();
    m.i32f(1, 1);
    m.listf(2, cols.len() + 1, 12);
    // Root.
    m.begin();
    m.strf(4, b"schema");
    m.i32f(5, cols.len() as i32);
    m.end();
    for c in &cols {
        m.begin();
        let (ty, ct, logical) = match c.kind {
            Kind::Str | Kind::OptStr => (T_BYTE_ARRAY, Some(CT_UTF8), Some(1i16)),
            Kind::Date => (T_INT32, Some(CT_DATE), Some(6i16)),
            Kind::I64 => (T_INT64, None, None),
        };
        m.i32f(1, ty);
        m.i32f(3, if matches!(c.kind, Kind::OptStr) { 1 } else { 0 });
        m.strf(4, c.name.as_bytes());
        if let Some(ct) = ct {
            m.i32f(6, ct);
        }
        if let Some(l) = logical {
            m.structf(10);
            m.structf(l);
            m.end();
            m.end();
        }
        m.end();
    }
    m.i64f(3, nrows as i64);
    m.listf(4, 1, 12);
    m.begin();
    m.listf(1, cols.len(), 12);
    let mut total = 0i64;
    for (c, &(offset, size)) in cols.iter().zip(&metas) {
        total += size;
        m.begin();
        m.i64f(2, offset);
        m.structf(3);
        let ty = match c.kind {
            Kind::Str | Kind::OptStr => T_BYTE_ARRAY,
            Kind::Date => T_INT32,
            Kind::I64 => T_INT64,
        };
        m.i32f(1, ty);
        m.listf(2, 2, 5);
        m.zz(0); // PLAIN
        m.zz(3); // RLE
        m.listf(3, 1, 8);
        m.varint(c.name.len() as u64);
        m.b.extend_from_slice(c.name.as_bytes());
        m.i32f(4, 0); // UNCOMPRESSED
        m.i64f(5, nrows as i64);
        m.i64f(6, size);
        m.i64f(7, size);
        m.i64f(9, offset);
        m.end();
        m.end();
    }
    m.i64f(2, total);
    m.i64f(3, nrows as i64);
    m.end();
    m.strf(6, b"shopping-sessions");
    m.b.push(0);
    out.extend_from_slice(&m.b);
    out.extend_from_slice(&(m.b.len() as u32).to_le_bytes());
    out.extend_from_slice(b"PAR1");
    File::create(path).unwrap().write_all(&out).unwrap();
}

fn put_str(d: &mut Vec<u8>, s: &[u8]) {
    d.extend_from_slice(&(s.len() as u32).to_le_bytes());
    d.extend_from_slice(s);
}

fn put_key(d: &mut Vec<u8>, k: u128) {
    if is_long(k) {
        put_str(d, &unpack(k));
    } else {
        let b = k.to_be_bytes();
        put_str(d, &b[..b[15] as usize]);
    }
}

/// Definition levels for a max-level-1 column: one bit-packed run, length-prefixed.
fn def_levels(bits: impl Iterator<Item = bool>, n: usize) -> Vec<u8> {
    let groups = n.div_ceil(8);
    let mut v = Vec::with_capacity(groups + 16);
    v.extend_from_slice(&[0, 0, 0, 0]);
    let mut h = ((groups as u64) << 1) | 1;
    while h >= 0x80 {
        v.push((h as u8) | 0x80);
        h >>= 7;
    }
    v.push(h as u8);
    let start = v.len();
    v.resize(start + groups, 0);
    for (i, b) in bits.enumerate() {
        if b {
            v[start + i / 8] |= 1 << (i % 8);
        }
    }
    let len = (v.len() - 4) as u32;
    v[..4].copy_from_slice(&len.to_le_bytes());
    v
}

pub fn write_user(path: &Path, rows: &[URow], cats: &[Vec<u8>]) {
    let n = rows.len();
    let mut uid = Vec::with_capacity(n * 16);
    let mut day = Vec::with_capacity(n * 4);
    let mut ses = Vec::with_capacity(n * 8);
    let mut evs = Vec::with_capacity(n * 8);
    let mut rev = Vec::with_capacity(n * 8);
    for r in rows {
        put_key(&mut uid, r.person);
        day.extend_from_slice(&r.day.to_le_bytes());
        ses.extend_from_slice(&(r.sessions as i64).to_le_bytes());
        evs.extend_from_slice(&(r.events as i64).to_le_bytes());
        rev.extend_from_slice(&r.revenue.to_le_bytes());
    }
    let mut top = def_levels(rows.iter().map(|r| r.top >= 0), n);
    for r in rows {
        if r.top >= 0 {
            put_str(&mut top, &cats[r.top as usize]);
        }
    }
    let cols = vec![
        Col { name: "user_id", kind: Kind::Str, data: uid, nvals: n },
        Col { name: "day", kind: Kind::Date, data: day, nvals: n },
        Col { name: "sessions", kind: Kind::I64, data: ses, nvals: n },
        Col { name: "events", kind: Kind::I64, data: evs, nvals: n },
        Col { name: "revenue_cents", kind: Kind::I64, data: rev, nvals: n },
        Col { name: "top_category", kind: Kind::OptStr, data: top, nvals: n },
    ];
    write_file(path, cols, n);
}

#[derive(Default)]
pub struct CampRows {
    n: usize,
    names: Vec<u8>,
    channels: Vec<u8>,
    days: Vec<u8>,
    vals: [Vec<u8>; 4],
}

impl CampRows {
    pub fn push(&mut self, name: &[u8], channel: &[u8], day: i32, v: [i64; 4]) {
        self.n += 1;
        put_str(&mut self.names, name);
        put_str(&mut self.channels, channel);
        self.days.extend_from_slice(&day.to_le_bytes());
        for i in 0..4 {
            self.vals[i].extend_from_slice(&v[i].to_le_bytes());
        }
    }
}

pub fn write_camp(path: &Path, c: CampRows) {
    let n = c.n;
    let [v0, v1, v2, v3] = c.vals;
    let cols = vec![
        Col { name: "campaign_id", kind: Kind::Str, data: c.names, nvals: n },
        Col { name: "channel", kind: Kind::Str, data: c.channels, nvals: n },
        Col { name: "day", kind: Kind::Date, data: c.days, nvals: n },
        Col { name: "sessions", kind: Kind::I64, data: v0, nvals: n },
        Col { name: "purchasing_sessions", kind: Kind::I64, data: v1, nvals: n },
        Col { name: "revenue_cents", kind: Kind::I64, data: v2, nvals: n },
        Col { name: "assisted_sessions", kind: Kind::I64, data: v3, nvals: n },
    ];
    write_file(path, cols, n);
}
