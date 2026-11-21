//! Exact 128-bit keys for byte strings.
//!
//! Strings of up to 15 bytes are packed big-endian into the high 15 bytes with the
//! length in the low byte, so comparing keys as integers compares the strings byte-wise.
//! Longer strings get a 128-bit hash tagged with 0xFF in the low byte and are kept in a
//! side table so they can be compared and printed.

use std::cmp::Ordering;
use std::collections::HashMap as StdMap;
use std::hash::Hasher;
use std::sync::{Mutex, OnceLock};

const LONG: u8 = 0xFF;

fn long_table() -> &'static Mutex<StdMap<u128, Box<[u8]>>> {
    static T: OnceLock<Mutex<StdMap<u128, Box<[u8]>>>> = OnceLock::new();
    T.get_or_init(|| Mutex::new(StdMap::new()))
}

#[inline]
pub fn pack(s: &[u8]) -> u128 {
    if s.len() <= 15 {
        let mut buf = [0u8; 16];
        buf[..s.len()].copy_from_slice(s);
        buf[15] = s.len() as u8;
        u128::from_be_bytes(buf)
    } else {
        pack_long(s)
    }
}

/// `pack`, reading 16 bytes at once when `s` has 16 readable bytes before `end`.
#[inline(always)]
pub fn pack_in(s: &[u8], end: usize) -> u128 {
    let n = s.len();
    if n <= 15 && s.as_ptr() as usize + 16 <= end {
        let v = unsafe { std::ptr::read_unaligned(s.as_ptr() as *const [u8; 16]) };
        let be = u128::from_be_bytes(v);
        let mask = if n == 0 { 0 } else { !0u128 << (8 * (16 - n)) };
        (be & mask) | n as u128
    } else {
        pack(s)
    }
}

#[cold]
fn pack_long(s: &[u8]) -> u128 {
    let h1 = hash64(s, 0x243F_6A88_85A3_08D3);
    let h2 = hash64(s, 0x1319_8A2E_0370_7344);
    let k = (((h1 as u128) << 64) | h2 as u128) & !0xFFu128 | LONG as u128;
    let mut t = long_table().lock().unwrap();
    t.entry(k).or_insert_with(|| s.to_vec().into_boxed_slice());
    k
}

fn hash64(s: &[u8], seed: u64) -> u64 {
    let mut h = seed ^ (s.len() as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    for c in s.chunks(8) {
        let mut b = [0u8; 8];
        b[..c.len()].copy_from_slice(c);
        h = (h ^ u64::from_le_bytes(b)).wrapping_mul(0xFF51_AFD7_ED55_8CCD);
        h ^= h >> 29;
    }
    h = h.wrapping_mul(0xC4CE_B9FE_1A85_EC53);
    h ^ (h >> 32)
}

#[inline]
pub fn is_long(k: u128) -> bool {
    (k as u8) == LONG
}

pub fn unpack(k: u128) -> Vec<u8> {
    if is_long(k) {
        long_table().lock().unwrap().get(&k).unwrap().to_vec()
    } else {
        let b = k.to_be_bytes();
        b[..b[15] as usize].to_vec()
    }
}

#[inline]
pub fn cmp_keys(a: u128, b: u128) -> Ordering {
    if !is_long(a) && !is_long(b) {
        a.cmp(&b)
    } else {
        unpack(a).cmp(&unpack(b))
    }
}

#[inline]
pub fn mix(k: u128) -> u64 {
    let lo = k as u64;
    let hi = (k >> 64) as u64;
    let mut x = lo ^ hi.rotate_left(29).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    x ^= x >> 32;
    x = x.wrapping_mul(0xD6E8_FEB8_6659_FD93);
    x ^= x >> 32;
    x = x.wrapping_mul(0xD6E8_FEB8_6659_FD93);
    x ^ (x >> 32)
}

/// An independent hash for partitioning, so bucket bits don't correlate with hash-table bits.
#[inline]
pub fn mix2(k: u128) -> u64 {
    let lo = k as u64;
    let hi = (k >> 64) as u64;
    let mut x = hi ^ lo.rotate_left(17).wrapping_mul(0xC2B2_AE3D_27D4_EB4F);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x ^= x >> 29;
    x = x.wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 32)
}

// ---- 64-bit keys.
//
// A string of 1..=10 bytes from [0-9A-Za-z] packs as 6-bit codes (in byte order, 0 = past
// the end) left-aligned in the low 60 bits, so integer order is byte-wise string order.
// Any other string is interned and gets INTERN | index. 0 is the empty string.

pub const INTERN: u64 = 1 << 63;

const fn code_table() -> [u8; 256] {
    let mut t = [0u8; 256];
    let mut c = 0;
    while c < 256 {
        let b = c as u8;
        t[c] = if b.is_ascii_digit() {
            b - b'0' + 1
        } else if b.is_ascii_uppercase() {
            b - b'A' + 11
        } else if b.is_ascii_lowercase() {
            b - b'a' + 37
        } else {
            0
        };
        c += 1;
    }
    t
}

static CODE: [u8; 256] = code_table();
const SYM: &[u8; 63] = b"\x000123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

struct Interner {
    map: StdMap<Box<[u8]>, u64>,
    strs: Vec<Box<[u8]>>,
}

fn interner() -> &'static Mutex<Interner> {
    static T: OnceLock<Mutex<Interner>> = OnceLock::new();
    T.get_or_init(|| Mutex::new(Interner { map: StdMap::new(), strs: Vec::new() }))
}

#[cold]
fn intern(s: &[u8]) -> u64 {
    let mut t = interner().lock().unwrap();
    if let Some(&k) = t.map.get(s) {
        return k;
    }
    let k = INTERN | t.strs.len() as u64;
    t.strs.push(s.into());
    t.map.insert(s.into(), k);
    k
}

/// Packs up to 10 bytes from `b` (only the first `n` count); None if a byte is outside the alphabet.
#[inline(always)]
fn compact(b: &[u8; 10], n: usize) -> Option<u64> {
    let mut hi = 0u64;
    let mut lo = 0u64;
    let mut bad = false;
    for i in 0..5 {
        let c = if i < n { CODE[b[i] as usize] } else { 0 };
        bad |= (i < n) & (c == 0);
        hi = (hi << 6) | c as u64;
    }
    for i in 5..10 {
        let c = if i < n { CODE[b[i] as usize] } else { 0 };
        bad |= (i < n) & (c == 0);
        lo = (lo << 6) | c as u64;
    }
    if bad {
        None
    } else {
        Some((hi << 30) | lo)
    }
}

pub fn k64(s: &[u8]) -> u64 {
    let n = s.len();
    if n == 0 {
        return 0;
    }
    if n <= 10 {
        let mut b = [0u8; 10];
        b[..n].copy_from_slice(s);
        if let Some(k) = compact(&b, n) {
            return k;
        }
    }
    intern(s)
}

/// `k64`, reading 10 bytes at once when they are readable before `end`.
#[inline(always)]
pub fn k64_in(s: &[u8], end: usize) -> u64 {
    let n = s.len();
    if n.wrapping_sub(1) < 10 && s.as_ptr() as usize + 10 <= end {
        let b = unsafe { &*(s.as_ptr() as *const [u8; 10]) };
        if let Some(k) = compact(b, n) {
            return k;
        }
    }
    k64(s)
}

pub fn unpack64(k: u64) -> Vec<u8> {
    let mut v = Vec::with_capacity(10);
    put64(&mut v, k);
    v
}

/// Appends the string of `k` to `out`.
#[inline]
pub fn put64(out: &mut Vec<u8>, k: u64) {
    if k & INTERN != 0 {
        out.extend_from_slice(&interner().lock().unwrap().strs[(k & !INTERN) as usize]);
        return;
    }
    for i in 0..10 {
        let c = ((k >> (54 - 6 * i)) & 63) as usize;
        if c == 0 {
            break;
        }
        out.push(SYM[c]);
    }
}

/// Byte length of the string of `k`.
#[inline]
pub fn len64(k: u64) -> usize {
    if k & INTERN != 0 {
        return interner().lock().unwrap().strs[(k & !INTERN) as usize].len();
    }
    // Codes are left-aligned from bit 59; trailing zero 6-bit groups are padding.
    if k == 0 {
        0
    } else {
        10 - (k.trailing_zeros() / 6) as usize
    }
}

#[inline]
pub fn cmp64(a: u64, b: u64) -> Ordering {
    if (a | b) & INTERN == 0 {
        a.cmp(&b)
    } else {
        unpack64(a).cmp(&unpack64(b))
    }
}

#[inline]
pub fn mix64(k: u64) -> u64 {
    let mut x = k ^ (k >> 31);
    x = x.wrapping_mul(0x7FB5_D329_728E_A185);
    x ^= x >> 27;
    x = x.wrapping_mul(0x81DA_DEF4_BC2D_D44D);
    x ^ (x >> 33)
}

/// An independent hash for partitioning.
#[inline]
pub fn mix64b(k: u64) -> u64 {
    let mut x = k.wrapping_add(0x9E37_79B9_7F4A_7C15);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

#[derive(Default)]
pub struct KeyHasher(u64);

impl Hasher for KeyHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0 ^ b as u64).wrapping_mul(0x100_0000_01B3);
        }
    }
    #[inline]
    fn write_u128(&mut self, k: u128) {
        self.0 = mix(k);
    }
    #[inline]
    fn write_u64(&mut self, k: u64) {
        self.0 = mix64(k);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn k64_exact_and_ordered() {
        let mut x: u64 = 0x2545_F491_4F6C_DD1D;
        let mut rnd = move || {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x
        };
        let alpha = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-\r\"~ \xff\x00";
        let mut strs: Vec<Vec<u8>> = vec![b"".to_vec(), b"u".to_vec(), b"u0".to_vec(), b"zzzzzzzzzz".to_vec(), b"zzzzzzzzzzz".to_vec()];
        for _ in 0..200_000 {
            let n = (rnd() % 13) as usize;
            let odd = rnd() % 8 == 0;
            let s: Vec<u8> = (0..n).map(|_| alpha[(rnd() as usize) % if odd { alpha.len() } else { 62 }]).collect();
            strs.push(s);
        }
        let keys: Vec<u64> = strs.iter().map(|s| k64(s)).collect();
        for (s, &k) in strs.iter().zip(&keys) {
            assert_eq!(&unpack64(k), s);
            assert_eq!(len64(k), s.len());
            // the fast path, reading past the string
            let mut buf = s.clone();
            buf.extend_from_slice(b",xyz,0123456789");
            assert_eq!(k64_in(&buf[..s.len()], buf.as_ptr() as usize + buf.len()), k);
        }
        for _ in 0..400_000 {
            let i = (rnd() as usize) % strs.len();
            let j = (rnd() as usize) % strs.len();
            assert_eq!(cmp64(keys[i], keys[j]), strs[i].cmp(&strs[j]));
            assert_eq!(keys[i] == keys[j], strs[i] == strs[j]);
        }
    }
}
