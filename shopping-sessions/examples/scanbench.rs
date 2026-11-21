#[path = "../src/scan.rs"]
mod scan;
#[path = "../src/parse.rs"]
#[allow(dead_code)]
mod parse;
#[repr(C)]
struct Ts { s: i64, ns: i64 }
extern "C" { fn clock_gettime(c: i32, t: *mut Ts) -> i32; }
fn cpu() -> f64 {
    let mut t = Ts { s: 0, ns: 0 };
    // CLOCK_THREAD_CPUTIME_ID: 3 on Linux, 16 on macOS
    let id = if cfg!(target_os = "linux") { 3 } else { 16 };
    unsafe { clock_gettime(id, &mut t) };
    t.s as f64 + t.ns as f64 * 1e-9
}
fn main() {
    let dir = std::env::args().nth(1).unwrap();
    let mut files: Vec<Vec<u8>> = Vec::new();
    for e in std::fs::read_dir(&dir).unwrap() {
        files.push(std::fs::read(e.unwrap().path()).unwrap());
    }
    // mode: (use new scan, number of timestamps parsed)
    let modes = [(false, 0), (false, 2), (true, 2), (false, 0)];
    let mut best = [f64::MAX; 4];
    let mut lines = 0u64;
    for _ in 0..15 {
        for (mi, &(new, nts)) in modes.iter().enumerate() {
            let t = cpu();
            let mut acc = 0u64;
            lines = 0;
            for f in &files {
                let mut cb = |x: &[&[u8]; 7]| {
                    lines += 1;
                    acc = acc.wrapping_add(x[0].len() as u64 + x[6].len() as u64);
                    if nts > 0 && new {
                        acc ^= parse::parse_ts_ref(x[2]).unwrap_or(0) as u64 ^ parse::parse_ts_ref(x[3]).unwrap_or(0) as u64;
                    } else if nts > 0 {
                        acc ^= parse::parse_ts(x[2]).unwrap_or(0) as u64 ^ parse::parse_ts(x[3]).unwrap_or(0) as u64;
                    }
                };
                scan::scan::<7, _>(f, &mut cb)
            }
            std::hint::black_box(acc);
            best[mi] = best[mi].min(cpu() - t);
        }
    }
    for (mi, &(new, nts)) in modes.iter().enumerate() {
        println!("{} scan, {} ts: {:.1} ns/line", if new { "ref-ts" } else { "fast-ts" }, nts, best[mi] * 1e9 / lines as f64);
    }
}
