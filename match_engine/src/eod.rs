//! The end-of-day timer (D22). It only wakes the journal at 16:00 Dublin
//! time; the journal decides from its own timestamps whether the day has
//! ended and writes `DayClose`. The timer is not on the matching path.

use std::time::Duration;

use tokio::sync::watch;

use crate::runtime::{stopped, Clock, JournalMsg, JournalTx};
use crate::session;
use crate::types::Timestamp;

/// How long to wait from `now` until the next 16:00 local time. At least one
/// millisecond, so a timer that wakes marginally early cannot spin.
pub fn delay_until_close(now: Timestamp) -> Duration {
    let ms = session::next_close(now).saturating_sub(now).max(1);
    Duration::from_millis(ms as u64)
}

/// Sleeps until each close in turn and tells the journal to check. Ends when a
/// stop begins.
pub async fn run(journal: JournalTx, clock: Clock, mut stop: watch::Receiver<bool>) {
    loop {
        let wait = delay_until_close(clock());
        tokio::select! {
            _ = tokio::time::sleep(wait) => {
                if !journal.send(JournalMsg::CloseCheck) {
                    break;
                }
            }
            _ = stopped(&mut stop) => break,
        }
    }
}
