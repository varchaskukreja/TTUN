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
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// F1 `YYYY-MM-DDTHH:MM:SSZ`, F2 `YYYY-MM-DDTHH:MM:SS±HH:MM`, F3 digits = epoch ms.
#[inline]
pub fn parse_ts(b: &[u8]) -> Option<i64> {
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
}
