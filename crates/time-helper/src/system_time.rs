//! Minimal system-time execution primitive for the privileged helper.

use std::io;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::request::{TimeCorrectionRequest, TimeCorrectionResponse, TimeCorrectionStatus};

pub trait SystemClock {
    fn now_unix_us(&self) -> io::Result<i64>;
    fn set_unix_us(&self, value: i64) -> io::Result<()>;
}

pub struct RealSystemClock;

impl SystemClock for RealSystemClock {
    fn now_unix_us(&self) -> io::Result<i64> {
        let duration = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| io::Error::new(io::ErrorKind::Other, error))?;
        Ok((duration.as_secs() as i64 * 1_000_000) + i64::from(duration.subsec_micros()))
    }

    fn set_unix_us(&self, value: i64) -> io::Result<()> {
        let seconds = value.div_euclid(1_000_000);
        let micros = value.rem_euclid(1_000_000);
        let timespec = libc::timespec {
            tv_sec: seconds as libc::time_t,
            tv_nsec: (micros * 1_000) as libc::c_long,
        };
        // SAFETY: clock_settime receives a valid pointer to a fully initialized
        // timespec. The OS enforces CAP_SYS_TIME/root permissions.
        let result = unsafe { libc::clock_settime(libc::CLOCK_REALTIME, &timespec) };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }
}

pub fn handle_request(clock: &dyn SystemClock, request: &TimeCorrectionRequest) -> TimeCorrectionResponse {
    let system_unix_us = match clock.now_unix_us() {
        Ok(value) => value,
        Err(error) => {
            return TimeCorrectionResponse {
                status: TimeCorrectionStatus::Error,
                applied: false,
                offset_us: 0,
                target_unix_us: request.target_unix_us,
                system_unix_us: 0,
                warning: Some(format!("failed to read system time: {error}")),
            };
        }
    };
    let offset_us = request.target_unix_us - system_unix_us;
    if offset_us.abs() > request.max_step_us {
        return TimeCorrectionResponse {
            status: TimeCorrectionStatus::Refused,
            applied: false,
            offset_us,
            target_unix_us: request.target_unix_us,
            system_unix_us,
            warning: Some(format!(
                "requested correction exceeds max_step_us: {} > {}",
                offset_us.abs(),
                request.max_step_us
            )),
        };
    }
    if !request.apply {
        return TimeCorrectionResponse {
            status: TimeCorrectionStatus::Preview,
            applied: false,
            offset_us,
            target_unix_us: request.target_unix_us,
            system_unix_us,
            warning: Some("preview only; resend with apply=true to set system time".to_owned()),
        };
    }
    match clock.set_unix_us(request.target_unix_us) {
        Ok(()) => TimeCorrectionResponse {
            status: TimeCorrectionStatus::Applied,
            applied: true,
            offset_us,
            target_unix_us: request.target_unix_us,
            system_unix_us,
            warning: None,
        },
        Err(error) => TimeCorrectionResponse {
            status: TimeCorrectionStatus::Error,
            applied: false,
            offset_us,
            target_unix_us: request.target_unix_us,
            system_unix_us,
            warning: Some(format!("failed to set system time: {error}")),
        },
    }
}
