use std::cell::{Cell, RefCell};
use std::io;

use gatherlink_time_helper::request::{TimeCorrectionRequest, TimeCorrectionStatus, DEFAULT_MAX_STEP_US};
use gatherlink_time_helper::system_time::{handle_request, SystemClock};

struct FakeClock {
    now: Cell<i64>,
    set_values: RefCell<Vec<i64>>,
}

impl FakeClock {
    fn new(now: i64) -> Self {
        Self {
            now: Cell::new(now),
            set_values: RefCell::new(Vec::new()),
        }
    }
}

impl SystemClock for FakeClock {
    fn now_unix_us(&self) -> io::Result<i64> {
        Ok(self.now.get())
    }

    fn set_unix_us(&self, value: i64) -> io::Result<()> {
        self.set_values.borrow_mut().push(value);
        self.now.set(value);
        Ok(())
    }
}

#[test]
fn parses_time_correction_request_with_safe_defaults() {
    let request = TimeCorrectionRequest::parse("target_unix_us=1000\nsource=ntp\nquality=synchronized\n")
        .expect("request should parse");

    assert_eq!(request.target_unix_us, 1000);
    assert_eq!(request.source, "ntp");
    assert_eq!(request.quality, "synchronized");
    assert_eq!(request.max_step_us, DEFAULT_MAX_STEP_US);
    assert!(!request.apply);
}

#[test]
fn preview_request_reports_offset_without_setting_time() {
    let clock = FakeClock::new(1_000_000);
    let request = TimeCorrectionRequest {
        target_unix_us: 1_001_000,
        source: "ntp".to_owned(),
        quality: "synchronized".to_owned(),
        max_step_us: 10_000,
        apply: false,
    };

    let response = handle_request(&clock, &request);

    assert_eq!(response.status, TimeCorrectionStatus::Preview);
    assert_eq!(response.offset_us, 1_000);
    assert!(!response.applied);
    assert!(clock.set_values.borrow().is_empty());
}

#[test]
fn applied_request_sets_time_when_within_bound() {
    let clock = FakeClock::new(1_000_000);
    let request = TimeCorrectionRequest {
        target_unix_us: 1_001_000,
        source: "ntp".to_owned(),
        quality: "synchronized".to_owned(),
        max_step_us: 10_000,
        apply: true,
    };

    let response = handle_request(&clock, &request);

    assert_eq!(response.status, TimeCorrectionStatus::Applied);
    assert!(response.applied);
    assert_eq!(clock.set_values.borrow().as_slice(), &[1_001_000]);
}

#[test]
fn helper_refuses_correction_outside_bound() {
    let clock = FakeClock::new(1_000_000);
    let request = TimeCorrectionRequest {
        target_unix_us: 2_000_000,
        source: "ntp".to_owned(),
        quality: "synchronized".to_owned(),
        max_step_us: 10_000,
        apply: true,
    };

    let response = handle_request(&clock, &request);

    assert_eq!(response.status, TimeCorrectionStatus::Refused);
    assert!(!response.applied);
    assert!(clock.set_values.borrow().is_empty());
}
