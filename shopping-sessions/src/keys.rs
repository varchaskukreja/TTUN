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
}
