//! gatherlink-time-helper
//!
//! Minimal privileged helper for system time discipline.
//!
//! This binary should do only one thing: receive authorized time correction
//! requests from the unprivileged Gatherlink main process over a Unix socket and
//! apply safe system clock corrections when policy allows.

use std::env;
use std::path::PathBuf;

use gatherlink_time_helper::socket;
use gatherlink_time_helper::system_time::RealSystemClock;

fn main() {
    let socket_path = parse_socket_path().unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    let clock = RealSystemClock;
    if let Err(error) = socket::serve(&socket_path, &clock) {
        eprintln!("gatherlink-time-helper failed: {error}");
        std::process::exit(1);
    }
}

fn parse_socket_path() -> Result<PathBuf, String> {
    let mut args = env::args().skip(1);
    let mut socket_path = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" => {
                let value = args
                    .next()
                    .ok_or_else(|| "--socket requires a path argument".to_owned())?;
                socket_path = Some(PathBuf::from(value));
            }
            "--help" | "-h" => {
                return Err("usage: gatherlink-time-helper --socket /run/gatherlink/time-helper.sock".to_owned());
            }
            other => {
                return Err(format!("unknown argument: {other}"));
            }
        }
    }
    socket_path.ok_or_else(|| "usage: gatherlink-time-helper --socket /run/gatherlink/time-helper.sock".to_owned())
}
