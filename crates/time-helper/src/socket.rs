//! Unix-socket server for the privileged time helper.

use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;

use crate::request::{TimeCorrectionRequest, TimeCorrectionResponse, TimeCorrectionStatus};
use crate::system_time::{handle_request, SystemClock};

pub fn serve(socket_path: &Path, clock: &dyn SystemClock) -> io::Result<()> {
    if socket_path.exists() {
        fs::remove_file(socket_path)?;
    }
    if let Some(parent) = socket_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let listener = UnixListener::bind(socket_path)?;
    eprintln!(
        "gatherlink-time-helper listening on {}. This helper can set system time only when requests use apply=true.",
        socket_path.display()
    );
    for stream in listener.incoming() {
        match stream {
            Ok(mut stream) => {
                if let Err(error) = handle_stream(&mut stream, clock) {
                    let response = TimeCorrectionResponse {
                        status: TimeCorrectionStatus::Error,
                        applied: false,
                        offset_us: 0,
                        target_unix_us: 0,
                        system_unix_us: 0,
                        warning: Some(error.to_string()),
                    };
                    let _ = stream.write_all(response.render().as_bytes());
                }
            }
            Err(error) => eprintln!("failed to accept time-helper connection: {error}"),
        }
    }
    Ok(())
}

fn handle_stream(stream: &mut UnixStream, clock: &dyn SystemClock) -> io::Result<()> {
    let mut request_body = String::new();
    stream.read_to_string(&mut request_body)?;
    let response = match TimeCorrectionRequest::parse(&request_body) {
        Ok(request) => handle_request(clock, &request),
        Err(error) => TimeCorrectionResponse {
            status: TimeCorrectionStatus::Error,
            applied: false,
            offset_us: 0,
            target_unix_us: 0,
            system_unix_us: 0,
            warning: Some(error.to_string()),
        },
    };
    stream.write_all(response.render().as_bytes())
}
