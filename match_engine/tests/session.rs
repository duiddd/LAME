//! Dublin session clock: DST rules, 09:30 to 16:00 window, day ids, next close.

use match_engine::session::{
    after_close, day_id, in_session, next_close, time_of_day_ms, utc_offset_minutes,
};

/// Unix ms for a UTC date and time.
fn utc(y: i64, mo: i64, d: i64, h: i64, mi: i64) -> i64 {
    // Same civil-days formula as the module, written out independently.
    let (yy, mm) = if mo <= 2 {
        (y - 1, mo + 9)
    } else {
        (y, mo - 3)
    };
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let doy = (153 * mm + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    days * 86_400_000 + h * 3_600_000 + mi * 60_000
}

#[test]
fn known_instants() {
    assert_eq!(utc(1970, 1, 1, 0, 0), 0);
    assert_eq!(utc(2026, 11, 4, 12, 0), 1_793_793_600_000);
}

#[test]
fn offset_follows_irish_summer_time() {
    // 2026: DST starts Sunday 29 March 01:00 UTC and ends Sunday 25 October 01:00 UTC.
    assert_eq!(utc_offset_minutes(utc(2026, 1, 15, 12, 0)), 0);
    assert_eq!(utc_offset_minutes(utc(2026, 3, 29, 0, 59)), 0);
    assert_eq!(utc_offset_minutes(utc(2026, 3, 29, 1, 0)), 60);
    assert_eq!(utc_offset_minutes(utc(2026, 7, 1, 12, 0)), 60);
    assert_eq!(utc_offset_minutes(utc(2026, 10, 25, 0, 59)), 60);
    assert_eq!(utc_offset_minutes(utc(2026, 10, 25, 1, 0)), 0);
    assert_eq!(utc_offset_minutes(utc(2026, 11, 4, 12, 0)), 0);
}

#[test]
fn offset_for_other_years() {
    // 2025: 30 March to 26 October. 2024: 31 March to 27 October.
    assert_eq!(utc_offset_minutes(utc(2025, 3, 30, 1, 0)), 60);
    assert_eq!(utc_offset_minutes(utc(2025, 3, 30, 0, 59)), 0);
    assert_eq!(utc_offset_minutes(utc(2025, 10, 26, 1, 0)), 0);
    assert_eq!(utc_offset_minutes(utc(2024, 3, 31, 1, 0)), 60);
    assert_eq!(utc_offset_minutes(utc(2024, 10, 27, 0, 59)), 60);
}

#[test]
fn session_window_in_winter() {
    // Winter: Dublin is UTC+0.
    assert!(!in_session(utc(2026, 11, 4, 9, 29)));
    assert!(in_session(utc(2026, 11, 4, 9, 30)));
    assert!(in_session(utc(2026, 11, 4, 15, 59)));
    assert!(!in_session(utc(2026, 11, 4, 16, 0)));
    assert!(after_close(utc(2026, 11, 4, 16, 0)));
    assert!(!after_close(utc(2026, 11, 4, 15, 59)));
}

#[test]
fn session_window_in_summer_is_one_hour_earlier_in_utc() {
    // Summer: Dublin is UTC+1, so 09:30 local is 08:30 UTC.
    assert!(!in_session(utc(2026, 7, 1, 8, 29)));
    assert!(in_session(utc(2026, 7, 1, 8, 30)));
    assert!(in_session(utc(2026, 7, 1, 14, 59)));
    assert!(!in_session(utc(2026, 7, 1, 15, 0)));
    assert!(after_close(utc(2026, 7, 1, 15, 0)));
}

#[test]
fn time_of_day_wraps_at_local_midnight() {
    // 23:30 UTC in summer is 00:30 local the next day.
    assert_eq!(time_of_day_ms(utc(2026, 7, 1, 23, 30)), 30 * 60_000);
    assert_eq!(day_id(utc(2026, 7, 1, 23, 30)), "2026-07-02");
    assert_eq!(day_id(utc(2026, 7, 1, 22, 59)), "2026-07-01");
}

#[test]
fn day_id_is_the_dublin_date() {
    assert_eq!(day_id(0), "1970-01-01");
    assert_eq!(day_id(utc(2026, 11, 4, 12, 0)), "2026-11-04");
    assert_eq!(day_id(utc(2024, 2, 29, 12, 0)), "2024-02-29");
    assert_eq!(day_id(utc(2025, 12, 31, 23, 59)), "2025-12-31");
}

#[test]
fn next_close_is_the_next_sixteen_hundred_local() {
    // Winter: 16:00 local is 16:00 UTC.
    assert_eq!(next_close(utc(2026, 11, 4, 9, 0)), utc(2026, 11, 4, 16, 0));
    assert_eq!(
        next_close(utc(2026, 11, 4, 15, 59)),
        utc(2026, 11, 4, 16, 0)
    );
    // Exactly at the close the next one is tomorrow's.
    assert_eq!(next_close(utc(2026, 11, 4, 16, 0)), utc(2026, 11, 5, 16, 0));
    assert_eq!(next_close(utc(2026, 11, 4, 20, 0)), utc(2026, 11, 5, 16, 0));
    // Summer: 16:00 local is 15:00 UTC.
    assert_eq!(next_close(utc(2026, 7, 1, 9, 0)), utc(2026, 7, 1, 15, 0));
}

#[test]
fn next_close_across_daylight_saving_changes() {
    // The Saturday before the March change is still winter time.
    assert_eq!(next_close(utc(2026, 3, 28, 17, 0)), utc(2026, 3, 29, 15, 0));
    // Sunday 25 October 2026: the clocks go back at 01:00 UTC; 16:00 is winter.
    assert_eq!(
        next_close(utc(2026, 10, 25, 2, 0)),
        utc(2026, 10, 25, 16, 0)
    );
    assert_eq!(
        next_close(utc(2026, 10, 24, 17, 0)),
        utc(2026, 10, 25, 16, 0)
    );
}

#[test]
fn next_close_is_always_a_close_and_strictly_later() {
    let mut ts = utc(2026, 1, 1, 0, 0);
    for _ in 0..2000 {
        let c = next_close(ts);
        assert!(c > ts);
        assert_eq!(time_of_day_ms(c), 16 * 3_600_000);
        assert!(after_close(c) && !in_session(c));
        ts += 7 * 3_600_000 + 123_456;
    }
}
