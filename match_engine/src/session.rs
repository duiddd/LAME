//! Trading-session clock: pure functions from a Unix-millisecond timestamp to
//! Dublin local time (Europe/Dublin, with daylight saving) and the 09:30 to
//! 16:00 session (D22). Nothing here reads a clock.
//!
//! Weekends and holidays are not handled at this layer.

use crate::types::Timestamp;

const MS_PER_MIN: i64 = 60_000;
const MS_PER_HOUR: i64 = 3_600_000;
const MS_PER_DAY: i64 = 86_400_000;
/// Session opens at 09:30 local time.
pub const OPEN_MS: i64 = 9 * MS_PER_HOUR + 30 * MS_PER_MIN;
/// Session closes at 16:00 local time.
pub const CLOSE_MS: i64 = 16 * MS_PER_HOUR;

/// Days since 1970-01-01 for a civil date (proleptic Gregorian).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Civil date `(year, month, day)` for days since 1970-01-01.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Day of month of the last Sunday of `month` (31-day months only).
fn last_sunday(year: i64, month: i64) -> i64 {
    let last = days_from_civil(year, month, 31);
    // 1970-01-01 was a Thursday; 0 = Sunday.
    let weekday = (last + 4).rem_euclid(7);
    31 - weekday
}

/// Offset of Dublin local time from UTC in minutes at `ts`: +60 during Irish
/// Summer Time (01:00 UTC on the last Sunday of March until 01:00 UTC on the
/// last Sunday of October), otherwise 0.
pub fn utc_offset_minutes(ts: Timestamp) -> i64 {
    let (year, _, _) = civil_from_days(ts.div_euclid(MS_PER_DAY));
    let start = days_from_civil(year, 3, last_sunday(year, 3)) * MS_PER_DAY + MS_PER_HOUR;
    let end = days_from_civil(year, 10, last_sunday(year, 10)) * MS_PER_DAY + MS_PER_HOUR;
    if (start..end).contains(&ts) {
        60
    } else {
        0
    }
}

fn local_ms(ts: Timestamp) -> i64 {
    ts.saturating_add(utc_offset_minutes(ts) * MS_PER_MIN)
}

/// Milliseconds since local midnight, in `0..86_400_000`.
pub fn time_of_day_ms(ts: Timestamp) -> i64 {
    local_ms(ts).rem_euclid(MS_PER_DAY)
}

/// The Dublin local date at `ts` as `YYYY-MM-DD`. It identifies the trading
/// day and names the day's event store.
pub fn day_id(ts: Timestamp) -> String {
    let (y, m, d) = civil_from_days(local_ms(ts).div_euclid(MS_PER_DAY));
    format!("{y:04}-{m:02}-{d:02}")
}

/// True from 09:30 (inclusive) to 16:00 (exclusive) local time.
pub fn in_session(ts: Timestamp) -> bool {
    (OPEN_MS..CLOSE_MS).contains(&time_of_day_ms(ts))
}

/// True from 16:00 local time to midnight.
pub fn after_close(ts: Timestamp) -> bool {
    time_of_day_ms(ts) >= CLOSE_MS
}

/// The UTC instant for a local wall-clock time given as local milliseconds.
fn local_to_utc(local: i64) -> Timestamp {
    let guess = local - utc_offset_minutes(local) * MS_PER_MIN;
    local - utc_offset_minutes(guess) * MS_PER_MIN
}

/// The first 16:00 local instant strictly after `ts`.
pub fn next_close(ts: Timestamp) -> Timestamp {
    let midnight = local_ms(ts).div_euclid(MS_PER_DAY) * MS_PER_DAY;
    let today = local_to_utc(midnight + CLOSE_MS);
    if today > ts {
        today
    } else {
        local_to_utc(midnight + MS_PER_DAY + CLOSE_MS)
    }
}
