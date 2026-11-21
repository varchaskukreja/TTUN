//! Open-addressing map from linked user id to person name, built for batched lookups with
//! software prefetch. Key 0 is the empty slot: it would be the empty string, which is never
//! inserted.

use crate::keys::mix64;

#[derive(Clone, Copy)]
#[repr(C, align(16))]
struct Slot {
    key: u64,
    val: u64,
}

pub struct PTable {
    slots: Vec<Slot>,
    mask: usize,
    len: usize,
}

impl PTable {
    pub fn with_capacity(n: usize) -> Self {
        let cap = (n * 2).next_power_of_two().max(16);
        PTable { slots: vec![Slot { key: 0, val: 0 }; cap], mask: cap - 1, len: 0 }
    }

    #[inline(always)]
    pub fn slot_of(&self, k: u64) -> usize {
        (mix64(k) as usize) & self.mask
    }

    pub fn insert(&mut self, k: u64, v: u64) {
        let mut i = self.slot_of(k);
        loop {
            let s = &mut self.slots[i];
            if s.key == 0 {
                *s = Slot { key: k, val: v };
                self.len += 1;
                return;
            }
            if s.key == k {
                s.val = v;
                return;
            }
            i = (i + 1) & self.mask;
        }
    }

    pub fn len(&self) -> usize {
        self.len
    }

    #[inline(always)]
    pub fn prefetch(&self, k: u64) {
        let p = unsafe { self.slots.as_ptr().add(self.slot_of(k)) } as *const i8;
        #[cfg(target_arch = "x86_64")]
        unsafe {
            std::arch::x86_64::_mm_prefetch(p, std::arch::x86_64::_MM_HINT_T0);
        }
        #[cfg(target_arch = "aarch64")]
        unsafe {
            std::arch::asm!("prfm pldl1keep, [{0}]", in(reg) p, options(nostack, readonly, preserves_flags));
        }
    }

    /// The person of `k`: its mapped name if linked, else itself.
    #[inline(always)]
    pub fn person(&self, k: u64) -> u64 {
        let mut i = self.slot_of(k);
        loop {
            let s = unsafe { self.slots.get_unchecked(i) };
            if s.key == k {
                return s.val;
            }
            if s.key == 0 {
                return k;
            }
            i = (i + 1) & self.mask;
        }
    }
}
