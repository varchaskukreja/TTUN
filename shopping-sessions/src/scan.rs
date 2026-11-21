//! SIMD structural scan: bitmasks of `,` `\n` `"` per 64-byte block, then a walk over
//! the separator bits that yields each well-formed line's unquoted fields.

#[inline(always)]
fn masks(p: &[u8; 64]) -> (u64, u64, u64) {
    #[cfg(target_arch = "x86_64")]
    unsafe {
        x86::masks(p)
    }
    #[cfg(target_arch = "aarch64")]
    unsafe {
        arm::masks(p)
    }
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    {
        let (mut c, mut n, mut q) = (0u64, 0u64, 0u64);
        for i in 0..64 {
            c |= ((p[i] == b',') as u64) << i;
            n |= ((p[i] == b'\n') as u64) << i;
            q |= ((p[i] == b'"') as u64) << i;
        }
        (c, n, q)
    }
}

#[cfg(target_arch = "x86_64")]
mod x86 {
    #[cfg(target_feature = "avx2")]
    #[inline(always)]
    pub unsafe fn masks(p: &[u8; 64]) -> (u64, u64, u64) {
        use std::arch::x86_64::*;
        let a = _mm256_loadu_si256(p.as_ptr() as *const __m256i);
        let b = _mm256_loadu_si256(p.as_ptr().add(32) as *const __m256i);
        let m = |v: u8| -> u64 {
            let s = _mm256_set1_epi8(v as i8);
            let lo = _mm256_movemask_epi8(_mm256_cmpeq_epi8(a, s)) as u32 as u64;
            let hi = _mm256_movemask_epi8(_mm256_cmpeq_epi8(b, s)) as u32 as u64;
            lo | (hi << 32)
        };
        (m(b','), m(b'\n'), m(b'"'))
    }

    #[cfg(not(target_feature = "avx2"))]
    #[inline(always)]
    pub unsafe fn masks(p: &[u8; 64]) -> (u64, u64, u64) {
        use std::arch::x86_64::*;
        let m = |v: u8| -> u64 {
            let s = _mm_set1_epi8(v as i8);
            let mut r = 0u64;
            for k in 0..4 {
                let x = _mm_loadu_si128(p.as_ptr().add(16 * k) as *const __m128i);
                r |= (_mm_movemask_epi8(_mm_cmpeq_epi8(x, s)) as u32 as u64) << (16 * k);
            }
            r
        };
        (m(b','), m(b'\n'), m(b'"'))
    }
}

#[cfg(target_arch = "aarch64")]
mod arm {
    use std::arch::aarch64::*;
    #[inline(always)]
    pub unsafe fn masks(p: &[u8; 64]) -> (u64, u64, u64) {
        let v0 = vld1q_u8(p.as_ptr());
        let v1 = vld1q_u8(p.as_ptr().add(16));
        let v2 = vld1q_u8(p.as_ptr().add(32));
        let v3 = vld1q_u8(p.as_ptr().add(48));
        let bits: [u8; 16] = [1, 2, 4, 8, 16, 32, 64, 128, 1, 2, 4, 8, 16, 32, 64, 128];
        let bm = vld1q_u8(bits.as_ptr());
        let m = |c: u8| -> u64 {
            let s = vdupq_n_u8(c);
            let t0 = vandq_u8(vceqq_u8(v0, s), bm);
            let t1 = vandq_u8(vceqq_u8(v1, s), bm);
            let t2 = vandq_u8(vceqq_u8(v2, s), bm);
            let t3 = vandq_u8(vceqq_u8(v3, s), bm);
            let s0 = vpaddq_u8(t0, t1);
            let s1 = vpaddq_u8(t2, t3);
            let s0 = vpaddq_u8(s0, s1);
            let s0 = vpaddq_u8(s0, s0);
            vgetq_lane_u64(vreinterpretq_u64_u8(s0), 0)
        };
        (m(b','), m(b'\n'), m(b'"'))
    }
}

/// Calls `f` with the N unquoted fields of every well-formed line in `data`.
/// `data` holds whole lines; a last line without `\n` counts.
#[inline(always)]
pub fn scan<'d, const N: usize, F: FnMut(&[&'d [u8]; N])>(data: &'d [u8], mut f: F) {
    let len = data.len();
    let mut ends = [0usize; N];
    let mut nf = 0usize;
    let mut line_start = 0usize;
    let mut q_cum = 0u32; // quotes before the current block
    let mut q_line_start = 0u32;
    let mut base = 0usize;
    let mut tail = [0u8; 64];
    while base < len {
        let blk: &[u8; 64] = if base + 64 <= len {
            data[base..base + 64].try_into().unwrap()
        } else {
            tail = [0u8; 64];
            tail[..len - base].copy_from_slice(&data[base..]);
            &tail
        };
        let (mc, mn, mq) = masks(blk);
        let mut sep = mc | mn;
        while sep != 0 {
            let i = sep.trailing_zeros() as usize;
            let pos = base + i;
            if (mn >> i) & 1 == 0 {
                if nf < N {
                    ends[nf] = pos;
                }
                nf += 1;
            } else {
                let q_here = q_cum + (mq & ((1u64 << i) - 1)).count_ones();
                if nf == N - 1 {
                    ends[N - 1] = pos;
                    emit::<N, F>(data, line_start, &ends, q_here - q_line_start, &mut f);
                }
                nf = 0;
                line_start = pos + 1;
                q_line_start = q_here + 0;
            }
            sep &= sep - 1;
        }
        q_cum += mq.count_ones();
        base += 64;
    }
    if line_start < len && nf == N - 1 {
        ends[N - 1] = len;
        emit::<N, F>(data, line_start, &ends, q_cum - q_line_start, &mut f);
    }
}

#[inline(always)]
fn emit<'d, const N: usize, F: FnMut(&[&'d [u8]; N])>(data: &'d [u8], line_start: usize, ends: &[usize; N], quotes: u32, f: &mut F) {
    // Branch-free unquoting: quoting is common and random, so branches here mispredict.
    let mut fields: [&'d [u8]; N] = [&[]; N];
    let mut s = line_start;
    let last = data.len() - 1;
    let mut proper = 0u32;
    for k in 0..N {
        let e = ends[k];
        let (b0, b1) = unsafe { (*data.get_unchecked(s.min(last)), *data.get_unchecked(e.max(1) - 1)) };
        let q = ((e - s >= 2) & (b0 == b'"') & (b1 == b'"')) as usize;
        proper += q as u32;
        fields[k] = unsafe { data.get_unchecked(s + q..e - q) };
        s = e + 1;
    }
    if quotes != 2 * proper {
        return;
    }
    f(&fields);
}

#[cfg(test)]
mod tests {
    use super::*;
    fn collect<const N: usize>(d: &[u8]) -> Vec<Vec<Vec<u8>>> {
        let mut v = Vec::new();
        scan::<N, _>(d, |f| v.push(f.iter().map(|x| x.to_vec()).collect()));
        v
    }
    #[test]
    fn basic() {
        let d = b"a,b\n\"x\",\"\"\n\"\n,\na\"b,c\n\"a\"\"\",b\nq,r,s\nlast,one";
        let r = collect::<2>(d);
        let exp: Vec<Vec<Vec<u8>>> = vec![
            vec![b"a".to_vec(), b"b".to_vec()],
            vec![b"x".to_vec(), b"".to_vec()],
            vec![b"".to_vec(), b"".to_vec()],
            vec![b"last".to_vec(), b"one".to_vec()],
        ];
        assert_eq!(r, exp);
        // long lines crossing blocks
        let mut big = Vec::new();
        for i in 0..1000 {
            big.extend_from_slice(format!("\"{}{}\",{}\n", "x".repeat(i % 97), i, "y".repeat(i % 131)).as_bytes());
        }
        let r = collect::<2>(&big);
        assert_eq!(r.len(), 1000);
        assert_eq!(r[999][1], "y".repeat(999 % 131).into_bytes());
    }
}
