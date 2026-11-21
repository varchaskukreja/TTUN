//! CSV dialect, timestamps and prices exactly as rules.md defines them.

/// Lines split on `\n` only. A final line without `\n` still counts.
pub fn lines(data: &[u8]) -> impl Iterator<Item = &[u8]> {
    let mut pos = 0usize;
    std::iter::from_fn(move || {
        if pos >= data.len() {
            return None;
        }
        let rest = &data[pos..];
        match memchr::memchr(b'\n', rest) {
            Some(i) => {
                pos += i + 1;
                Some(&rest[..i])
            }
            None => {
                pos = data.len();
                Some(rest)
            }
        }
    })
}

/// Splits a line into exactly N fields, unquoting whole-field quotes.
/// Returns false for a malformed line.
#[inline]
pub fn split<'a, const N: usize>(line: &'a [u8], out: &mut [&'a [u8]; N]) -> bool {
    let mut n = 0usize;
    let mut start = 0usize;
    let len = line.len();
    let mut i = 0usize;
    loop {
        // Find the end of this field.
        let end = match memchr::memchr(b',', &line[i..]) {
            Some(j) => i + j,
            None => len,
        };
        if n >= N {
            return false;
        }
        let f = &line[start..end];
        let v = if !f.is_empty() && f[0] == b'"' {
            if f.len() < 2 || f[f.len() - 1] != b'"' {
                return false;
            }
            let inner = &f[1..f.len() - 1];
            if memchr::memchr(b'"', inner).is_some() {
                return false;
            }
            inner
        } else {
            if memchr::memchr(b'"', f).is_some() {
                return false;
            }
            f
        };
        out[n] = v;
        n += 1;
        if end == len {
            break;
        }
        start = end + 1;
        i = start;
    }
    n == N
}

#[inline]
fn d2(b: &[u8], i: usize) -> Option<i64> {
    let a = b[i].wrapping_sub(b'0');
    let c = b[i + 1].wrapping_sub(b'0');
    if a < 10 && c < 10 {
        Some(a as i64 * 10 + c as i64)
    } else {
        None
    }
}

#[inline]
fn is_leap(y: i64) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

#[inline]
const fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

#[inline(always)]
fn dig(b: u8, bad: &mut u8) -> i64 {
    let d = b.wrapping_sub(b'0');
    *bad |= (d > 9) as u8;
    d as i64
}

const DIM: [u8; 13] = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

const YM_Y0: i64 = 1900;
const YM_YEARS: usize = 256;

/// For year `YM_Y0 + i` and month m (1..=12): `days_from_civil(y, m, 1) << 5 | days_in_month`.
static YM: [i32; YM_YEARS * 16] = build_ym();

const fn build_ym() -> [i32; YM_YEARS * 16] {
    let mut t = [0i32; YM_YEARS * 16];
    let mut i = 0;
    while i < YM_YEARS {
        let y = YM_Y0 + i as i64;
        let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
        let mut m = 1;
        while m <= 12 {
            let dim = DIM[m] as i64 + (m == 2 && leap) as i64;
            t[i * 16 + m] = ((days_from_civil(y, m as i64, 1) << 5) | dim) as i32;
            m += 1;
        }
        i += 1;
    }
    t
}

/// Same result as `parse_ts_ref`, with the checks folded into flags instead of branches.
#[inline(always)]
pub fn parse_ts(b: &[u8]) -> Option<i64> {
    let n = b.len();
    if (n == 20 || n == 25) && b[4] == b'-' {
        let b: &[u8; 20] = b[..20].try_into().unwrap();
        let mut bad = (b[7] != b'-') as u8 | (b[10] != b'T') as u8 | (b[13] != b':') as u8 | (b[16] != b':') as u8;
        let y = dig(b[0], &mut bad) * 1000 + dig(b[1], &mut bad) * 100 + dig(b[2], &mut bad) * 10 + dig(b[3], &mut bad);
        let mo = dig(b[5], &mut bad) * 10 + dig(b[6], &mut bad);
        let d = dig(b[8], &mut bad) * 10 + dig(b[9], &mut bad);
        let h = dig(b[11], &mut bad) * 10 + dig(b[12], &mut bad);
        let mi = dig(b[14], &mut bad) * 10 + dig(b[15], &mut bad);
        let s = dig(b[17], &mut bad) * 10 + dig(b[18], &mut bad);
        bad |= (mo.wrapping_sub(1) as u64 >= 12) as u8 | (h > 23) as u8 | (mi > 59) as u8 | (s > 59) as u8;
        if bad != 0 {
            return None;
        }
        let yi = (y - YM_Y0) as usize;
        let month_days = if yi < YM_YEARS {
            let e = YM[yi * 16 + mo as usize];
            if d < 1 || d > (e & 31) as i64 {
                return None;
            }
            (e >> 5) as i64
        } else {
            let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
            let dim = DIM[mo as usize] as i64 + (mo == 2 && leap) as i64;
            if d < 1 || d > dim {
                return None;
            }
            days_from_civil(y, mo, 1)
        };
        let off = if n == 20 {
            if b[19] != b'Z' {
                return None;
            }
            0
        } else {
            parse_off(unsafe { &*(b.as_ptr().add(19) as *const [u8; 6]) })?
        };
        let secs = (month_days + d - 1) * 86400 + h * 3600 + mi * 60 + s - off;
        return Some(secs * 1000);
    }
    if n != 0 && n <= 18 {
        return parse_digits(b);
    }
    parse_ts_ref(b)
}

#[inline(always)]
fn parse_off(o: &[u8; 6]) -> Option<i64> {
    let mut bad = (o[3] != b':') as u8;
    let oh = dig(o[1], &mut bad) * 10 + dig(o[2], &mut bad);
    let om = dig(o[4], &mut bad) * 10 + dig(o[5], &mut bad);
    bad |= (oh > 23) as u8 | (om > 59) as u8;
    let o2 = oh * 3600 + om * 60;
    match o[0] {
        b'+' if bad == 0 => Some(o2),
        b'-' if bad == 0 => Some(-o2),
        _ => None,
    }
}

/// 1 to 18 digits, so no overflow is possible.
#[inline(always)]
fn parse_digits(b: &[u8]) -> Option<i64> {
    let mut bad = 0u8;
    let mut v = 0i64;
    for &c in b {
        v = v * 10 + dig(c, &mut bad);
    }
    if bad == 0 {
        Some(v)
    } else {
        None
    }
}

/// F1 `YYYY-MM-DDTHH:MM:SSZ`, F2 `YYYY-MM-DDTHH:MM:SS±HH:MM`, F3 digits = epoch ms.
#[inline(never)]
pub fn parse_ts_ref(b: &[u8]) -> Option<i64> {
    let n = b.len();
    if n == 0 {
        return None;
    }
    if n >= 20 && b[4] == b'-' {
        if b[7] != b'-' || b[10] != b'T' || b[13] != b':' || b[16] != b':' {
            return None;
        }
        let y = d2(b, 0)? * 100 + d2(b, 2)?;
        let mo = d2(b, 5)?;
        let d = d2(b, 8)?;
        let h = d2(b, 11)?;
        let mi = d2(b, 14)?;
        let s = d2(b, 17)?;
        if !(1..=12).contains(&mo) || h > 23 || mi > 59 || s > 59 || d < 1 {
            return None;
        }
        let dim = match mo {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            _ => {
                if is_leap(y) {
                    29
                } else {
                    28
                }
            }
        };
        if d > dim {
            return None;
        }
        let off = if n == 20 && b[19] == b'Z' {
            0
        } else if n == 25 && (b[19] == b'+' || b[19] == b'-') && b[22] == b':' {
            let oh = d2(b, 20)?;
            let om = d2(b, 23)?;
            if oh > 23 || om > 59 {
                return None;
            }
            let o = oh * 3600 + om * 60;
            if b[19] == b'+' {
                o
            } else {
                -o
            }
        } else {
            return None;
        };
        let secs = days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s - off;
        return Some(secs * 1000);
    }
    let mut v: i64 = 0;
    for &c in b {
        let d = c.wrapping_sub(b'0');
        if d >= 10 {
            return None;
        }
        v = v.checked_mul(10)?.checked_add(d as i64)?;
    }
    Some(v)
}

/// Digits only, 1 to 10,000,000.
#[inline]
pub fn parse_price(b: &[u8]) -> Option<u32> {
    if b.is_empty() {
        return None;
    }
    let mut v: u64 = 0;
    for &c in b {
        let d = c.wrapping_sub(b'0');
        if d >= 10 {
            return None;
        }
        v = v * 10 + d as u64;
        if v > 10_000_000 {
            return None;
        }
    }
    if v == 0 {
        None
    } else {
        Some(v as u32)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ts() {
        assert_eq!(parse_ts(b"2026-10-03T23:58:10Z"), parse_ts(b"2026-10-03T19:58:10-04:00"));
        assert_eq!(parse_ts(b"2026-10-03T23:58:10Z"), Some(1791071890000));
        assert_eq!(parse_ts(b"0001791071890000"), Some(1791071890000));
        assert_eq!(parse_ts(b"2026-02-29T00:00:00Z"), None);
        assert!(parse_ts(b"2028-02-29T00:00:00Z").is_some());
        assert_eq!(parse_ts(b"2026-10-03T23:58:10+0200"), None);
        assert_eq!(parse_ts(b"2026-10-03 23:58:10Z"), None);
        assert_eq!(parse_ts(b"99999999999999999999999"), None);
    }

    #[test]
    fn fast_matches_ref() {
        let mut x: u64 = 0x1234_5678_9abc_def1;
        let mut rnd = || {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x
        };
        let seeds: &[&[u8]] = &[
            b"2027-12-18T22:22:54Z",
            b"2028-02-29T00:00:00Z",
            b"2100-02-29T00:00:00Z",
            b"2000-02-29T23:59:59Z",
            b"2027-12-21T10:03:10-02:00",
            b"2027-12-21T10:03:10+14:30",
            b"1829168499000",
            b"000000000000000000",
            b"999999999999999999",
            b"9223372036854775807",
            b"9223372036854775808",
            b"",
            b"1",
        ];
        let alphabet = b"0123456789-T:Z+ x9";
        for s in seeds {
            assert_eq!(parse_ts(s), parse_ts_ref(s), "{:?}", std::str::from_utf8(s));
            for _ in 0..200_000 {
                let mut v = s.to_vec();
                for _ in 0..(rnd() % 3) {
                    if v.is_empty() {
                        break;
                    }
                    let i = (rnd() as usize) % v.len();
                    v[i] = alphabet[(rnd() as usize) % alphabet.len()];
                }
                if rnd() % 8 == 0 {
                    v.truncate((rnd() as usize) % (v.len() + 1));
                }
                assert_eq!(parse_ts(&v), parse_ts_ref(&v), "{:?}", std::str::from_utf8(&v));
            }
        }
        for _ in 0..2_000_000 {
            let r = |m: u64, v: u64| v % m;
            let mut s = format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
                1890 + r(240, rnd()),
                r(14, rnd()),
                r(33, rnd()),
                r(26, rnd()),
                r(62, rnd()),
                r(62, rnd())
            );
            if rnd() % 2 == 0 {
                s.push('Z');
            } else {
                s += &format!("{}{:02}:{:02}", if rnd() % 2 == 0 { '+' } else { '-' }, r(26, rnd()), r(62, rnd()));
            }
            assert_eq!(parse_ts(s.as_bytes()), parse_ts_ref(s.as_bytes()), "{s}");
        }
        for _ in 0..2_000_000 {
            let n = (rnd() % 27) as usize;
            let v: Vec<u8> = (0..n).map(|_| alphabet[(rnd() as usize) % alphabet.len()]).collect();
            assert_eq!(parse_ts(&v), parse_ts_ref(&v));
        }
    }
}
