//! Small helpers with no dependencies: time, ids, hashing, text clipping and
//! atomic file writes.

use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn unix_now() -> (i64, u32) {
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    (d.as_secs() as i64, d.subsec_nanos())
}

/// Days since 1970-01-01 to (year, month, day), proleptic Gregorian.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn parts(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    (
        y,
        m,
        d,
        (rem / 3600) as u32,
        ((rem % 3600) / 60) as u32,
        (rem % 60) as u32,
    )
}

/// Current UTC time as RFC 3339, e.g. `2026-11-06T18:50:00Z`.
pub fn now_iso() -> String {
    let (y, m, d, hh, mm, ss) = parts(unix_now().0);
    format!("{y:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

/// Today's UTC date, e.g. `2026-11-06`.
pub fn today() -> String {
    let (y, m, d, ..) = parts(unix_now().0);
    format!("{y:04}-{m:02}-{d:02}")
}

/// A sortable, unique-enough id: `20261106-185000-a1b2`.
pub fn new_id() -> String {
    let (secs, nanos) = unix_now();
    let (y, m, d, hh, mm, ss) = parts(secs);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let salt = fnv1a(format!("{nanos}-{n}-{}", std::process::id()).as_bytes());
    format!(
        "{y:04}{m:02}{d:02}-{hh:02}{mm:02}{ss:02}-{:04x}",
        salt & 0xffff
    )
}

/// 64-bit FNV-1a. Stable across platforms and Rust versions, unlike `DefaultHasher`.
pub fn fnv1a(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// A pseudo-random number in `0..max` (for retry jitter; not cryptographic).
pub fn jitter(max: u64) -> u64 {
    if max == 0 {
        return 0;
    }
    let (_, nanos) = unix_now();
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    fnv1a(format!("{nanos}{n}").as_bytes()) % max
}

/// At most `max` characters of `s`, with an ellipsis when it was cut.
pub fn clip(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        None => s.to_string(),
        Some((idx, _)) => format!("{}…", &s[..idx]),
    }
}

/// Byte index of the `n`th character (or the end of the string).
pub fn char_offset(s: &str, n: usize) -> usize {
    s.char_indices().nth(n).map(|(i, _)| i).unwrap_or(s.len())
}

/// Write a file atomically: write a sibling temp file, then rename over the target.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, path)
}

/// Lower-cased alphanumeric words of at least two characters.
pub fn words(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.chars().count() >= 2)
        .map(|w| w.to_lowercase())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_398), (2025, 11, 6));
        assert_eq!(civil_from_days(-1), (1969, 12, 31));
    }

    #[test]
    fn clip_is_char_safe() {
        assert_eq!(clip("héllo", 2), "hé…");
        assert_eq!(clip("hi", 5), "hi");
    }

    #[test]
    fn ids_are_unique() {
        assert_ne!(new_id(), new_id());
    }
}
