use std::env;
use std::fs;
use std::io;
use std::net::UdpSocket;
use std::process::ExitCode;
use std::time::{Duration, Instant};

const UDP_SOCKET_BUFFER_BYTES: usize = 1024 * 1024 * 1024;

#[derive(Debug)]
struct RunStats {
    packets: u64,
    bytes: u64,
    elapsed: Duration,
}

impl RunStats {
    fn bits_per_second(&self) -> f64 {
        let elapsed = self.elapsed.as_secs_f64().max(0.000_001);
        (self.bytes as f64 * 8.0) / elapsed
    }

    fn to_json(&self, complete: bool) -> String {
        format!(
            "{{\"bits_per_second\":{:.3},\"bytes\":{},\"complete\":{},\"elapsed_seconds\":{:.6},\"packets\":{}}}\n",
            self.bits_per_second(),
            self.bytes,
            if complete { "true" } else { "false" },
            self.elapsed.as_secs_f64(),
            self.packets
        )
    }
}

fn usage() -> &'static str {
    "usage:\n\
     udp_pressure send --target ADDR --duration SECONDS --payload-size BYTES [--target-mbit MBIT]\n\
     udp_pressure sink --bind ADDR --duration SECONDS [--idle-after-first SECONDS] [--out PATH]\n"
}

fn arg_value(args: &[String], name: &str) -> Result<String, String> {
    args.windows(2)
        .find(|window| window[0] == name)
        .map(|window| window[1].clone())
        .ok_or_else(|| format!("missing required argument {name}"))
}

fn optional_arg_value(args: &[String], name: &str) -> Option<String> {
    args.windows(2)
        .find(|window| window[0] == name)
        .map(|window| window[1].clone())
}

fn parse_duration_seconds(value: &str, name: &str) -> Result<Duration, String> {
    let seconds = value
        .parse::<f64>()
        .map_err(|error| format!("invalid {name}: {error}"))?;
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(format!("{name} must be a positive finite number"));
    }
    Ok(Duration::from_secs_f64(seconds))
}

fn parse_u64(value: &str, name: &str) -> Result<u64, String> {
    let parsed = value
        .parse::<u64>()
        .map_err(|error| format!("invalid {name}: {error}"))?;
    if parsed == 0 {
        return Err(format!("{name} must be greater than zero"));
    }
    Ok(parsed)
}

fn run_send(args: &[String]) -> Result<RunStats, String> {
    let target = arg_value(args, "--target")?;
    let duration = parse_duration_seconds(&arg_value(args, "--duration")?, "--duration")?;
    let payload_size = parse_u64(&arg_value(args, "--payload-size")?, "--payload-size")?;
    let target_bps = optional_arg_value(args, "--target-mbit")
        .map(|value| {
            value
                .parse::<f64>()
                .map(|mbit| mbit * 1_000_000.0)
                .map_err(|error| format!("invalid --target-mbit: {error}"))
        })
        .transpose()?;
    let socket = UdpSocket::bind("0.0.0.0:0").map_err(|error| format!("bind failed: {error}"))?;
    request_udp_socket_buffers(&socket);
    let payload = vec![b'u'; payload_size as usize];
    let started = Instant::now();
    let mut next_send = started;
    let mut packets = 0_u64;
    let mut bytes = 0_u64;

    while started.elapsed() < duration {
        bytes += socket
            .send_to(&payload, &target)
            .map_err(|error| format!("send_to failed: {error}"))? as u64;
        packets += 1;
        if let Some(target_bps) = target_bps {
            next_send += Duration::from_secs_f64((payload_size as f64 * 8.0) / target_bps);
            let now = Instant::now();
            if next_send > now {
                std::thread::sleep(next_send - now);
            }
        }
    }

    Ok(RunStats {
        packets,
        bytes,
        elapsed: started.elapsed(),
    })
}

fn write_snapshot(path: Option<&str>, stats: &RunStats, complete: bool) -> io::Result<()> {
    if let Some(path) = path {
        fs::write(path, stats.to_json(complete))?;
    }
    Ok(())
}

fn run_sink(args: &[String]) -> Result<RunStats, String> {
    let bind = arg_value(args, "--bind")?;
    let duration = parse_duration_seconds(&arg_value(args, "--duration")?, "--duration")?;
    let idle_after_first = optional_arg_value(args, "--idle-after-first")
        .map(|value| parse_duration_seconds(&value, "--idle-after-first"))
        .transpose()?
        .unwrap_or_else(|| Duration::from_secs_f64(2.0));
    let out = optional_arg_value(args, "--out");
    let socket = UdpSocket::bind(&bind).map_err(|error| format!("bind failed: {error}"))?;
    request_udp_socket_buffers(&socket);
    socket
        .set_read_timeout(Some(Duration::from_millis(200)))
        .map_err(|error| format!("set_read_timeout failed: {error}"))?;

    let started = Instant::now();
    let mut first_packet: Option<Instant> = None;
    let mut last_packet: Option<Instant> = None;
    let mut last_snapshot = started;
    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut buffer = vec![0_u8; 65_535];

    while started.elapsed() < duration {
        match socket.recv_from(&mut buffer) {
            Ok((length, _source)) => {
                let now = Instant::now();
                first_packet.get_or_insert(now);
                last_packet = Some(now);
                packets += 1;
                bytes += length as u64;
                if now.duration_since(last_snapshot) >= Duration::from_secs(1) {
                    let elapsed = first_packet.map_or(started.elapsed(), |first| now.duration_since(first));
                    let stats = RunStats { packets, bytes, elapsed };
                    write_snapshot(out.as_deref(), &stats, false)
                        .map_err(|error| format!("write snapshot failed: {error}"))?;
                    last_snapshot = now;
                }
            }
            Err(error)
                if error.kind() == io::ErrorKind::WouldBlock || error.kind() == io::ErrorKind::TimedOut =>
            {
                if let Some(last) = last_packet {
                    if last.elapsed() >= idle_after_first {
                        break;
                    }
                }
            }
            Err(error) => return Err(format!("recv_from failed: {error}")),
        }
    }

    let now = Instant::now();
    let elapsed = first_packet.map_or(started.elapsed(), |first| {
        last_packet.unwrap_or(now).duration_since(first)
    });
    Ok(RunStats {
        packets,
        bytes,
        elapsed,
    })
}

#[cfg(unix)]
fn request_udp_socket_buffers(socket: &UdpSocket) {
    use std::os::fd::AsRawFd;
    use std::os::raw::{c_int, c_void};

    type SockLen = u32;
    const SOL_SOCKET: c_int = 1;
    const SO_RCVBUF: c_int = 8;
    const SO_SNDBUF: c_int = 7;

    unsafe extern "C" {
        fn setsockopt(
            socket: c_int,
            level: c_int,
            option_name: c_int,
            option_value: *const c_void,
            option_len: SockLen,
        ) -> c_int;
    }

    let value = UDP_SOCKET_BUFFER_BYTES as c_int;
    let value_ptr = (&value as *const c_int).cast::<c_void>();
    let value_len = std::mem::size_of_val(&value) as SockLen;
    unsafe {
        let _ = setsockopt(
            socket.as_raw_fd(),
            SOL_SOCKET,
            SO_RCVBUF,
            value_ptr,
            value_len,
        );
        let _ = setsockopt(
            socket.as_raw_fd(),
            SOL_SOCKET,
            SO_SNDBUF,
            value_ptr,
            value_len,
        );
    }
}

#[cfg(not(unix))]
fn request_udp_socket_buffers(_socket: &UdpSocket) {}

fn main() -> ExitCode {
    let args = env::args().collect::<Vec<_>>();
    let result = match args.get(1).map(String::as_str) {
        Some("send") => run_send(&args[2..]),
        Some("sink") => run_sink(&args[2..]),
        _ => Err(usage().to_string()),
    };
    match result {
        Ok(stats) => {
            print!("{}", stats.to_json(true));
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(2)
        }
    }
}
