use std::collections::HashMap;
use std::env;
use std::fs;
use std::io;
use std::net::{SocketAddr, UdpSocket};
use std::process::ExitCode;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

const UDP_BATCH_SIZE: usize = 128;
const UDP_SOCKET_BUFFER_BYTES: usize = 1024 * 1024 * 1024;
const DEFAULT_SEND_BATCH_SIZE: usize = 64;
const DEFAULT_RECV_BUFFER_BYTES: usize = 65_535;
const MAX_UDP_PAYLOAD_BYTES: usize = 65_507;
const DEFAULT_FEEDBACK_PROBE_STEP_MBIT: f64 = 250.0;
const DEFAULT_FEEDBACK_GOOD_RATIO: f64 = 0.985;
const DEFAULT_FEEDBACK_LOW_RATIO: f64 = 0.75;
const DEFAULT_FEEDBACK_BACKOFF_RATIO: f64 = 0.95;

#[derive(Debug)]
struct RunStats {
    packets: u64,
    bytes: u64,
    elapsed: Duration,
    send_calls: u64,
    recv_calls: u64,
    max_send_batch: u64,
    max_recv_batch: u64,
}

impl RunStats {
    fn bits_per_second(&self) -> f64 {
        let elapsed = self.elapsed.as_secs_f64().max(0.000_001);
        (self.bytes as f64 * 8.0) / elapsed
    }

    fn to_json(&self, complete: bool) -> String {
        format!(
            concat!(
                "{{\"bits_per_second\":{:.3},\"bytes\":{},\"complete\":{},\"elapsed_seconds\":{:.6},",
                "\"max_recv_batch\":{},\"max_send_batch\":{},\"packets\":{},\"recv_calls\":{},\"send_calls\":{}}}\n",
            ),
            self.bits_per_second(),
            self.bytes,
            if complete { "true" } else { "false" },
            self.elapsed.as_secs_f64(),
            self.max_recv_batch,
            self.max_send_batch,
            self.packets,
            self.recv_calls,
            self.send_calls,
        )
    }
}

fn usage() -> &'static str {
    "usage:\n\
     udp_pressure send --target ADDR --duration SECONDS --payload-size BYTES [--target-mbit MBIT] [--flows N] [--target-port-stride N]\n\
                       [--send-batch N] [--udp-gso-segments N]\n\
                       [--feedback-bind ADDR] [--feedback-headroom RATIO] [--feedback-initial-mbit MBIT]\n\
                       [--feedback-max-mbit MBIT] [--feedback-probe-step-mbit MBIT]\n\
                       [--feedback-good-ratio RATIO] [--feedback-low-ratio RATIO] [--feedback-backoff-ratio RATIO]\n\
     udp_pressure sink --bind ADDR --duration SECONDS [--idle-after-first SECONDS] [--out PATH] [--workers N] [--bind-port-stride N]\n\
                       [--recv-batch N] [--recv-buffer-size BYTES] [--recv-truncate]\n\
                       [--feedback-target ADDR] [--feedback-interval-ms MS]\n"
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

fn has_arg(args: &[String], name: &str) -> bool {
    args.iter().any(|arg| arg == name)
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

fn parse_usize(value: &str, name: &str) -> Result<usize, String> {
    let parsed = value
        .parse::<usize>()
        .map_err(|error| format!("invalid {name}: {error}"))?;
    if parsed == 0 {
        return Err(format!("{name} must be greater than zero"));
    }
    Ok(parsed)
}

fn parse_f64(value: &str, name: &str) -> Result<f64, String> {
    let parsed = value
        .parse::<f64>()
        .map_err(|error| format!("invalid {name}: {error}"))?;
    if !parsed.is_finite() || parsed <= 0.0 {
        return Err(format!("{name} must be a positive finite number"));
    }
    Ok(parsed)
}

fn parse_ratio(value: &str, name: &str) -> Result<f64, String> {
    let parsed = parse_f64(value, name)?;
    if parsed > 1.0 {
        return Err(format!("{name} must be greater than zero and at most one"));
    }
    Ok(parsed)
}

fn run_send(args: &[String]) -> Result<RunStats, String> {
    let target = arg_value(args, "--target")?;
    let duration = parse_duration_seconds(&arg_value(args, "--duration")?, "--duration")?;
    let payload_size = parse_u64(&arg_value(args, "--payload-size")?, "--payload-size")?;
    let flows = optional_arg_value(args, "--flows")
        .map(|value| parse_usize(&value, "--flows"))
        .transpose()?
        .unwrap_or(1);
    let target_port_stride = optional_arg_value(args, "--target-port-stride")
        .map(|value| parse_usize(&value, "--target-port-stride"))
        .transpose()?
        .unwrap_or(0);
    let send_batch = optional_arg_value(args, "--send-batch")
        .map(|value| parse_usize(&value, "--send-batch"))
        .transpose()?
        .unwrap_or(DEFAULT_SEND_BATCH_SIZE);
    let udp_gso_segments = optional_arg_value(args, "--udp-gso-segments")
        .map(|value| parse_usize(&value, "--udp-gso-segments"))
        .transpose()?
        .unwrap_or(1);
    let super_payload_size = (payload_size as usize)
        .checked_mul(udp_gso_segments)
        .ok_or_else(|| "--payload-size * --udp-gso-segments overflowed".to_string())?;
    if super_payload_size > MAX_UDP_PAYLOAD_BYTES {
        return Err(format!(
            "--payload-size * --udp-gso-segments must fit in one UDP datagram payload ({MAX_UDP_PAYLOAD_BYTES} bytes)"
        ));
    }
    let target_bps = optional_arg_value(args, "--target-mbit")
        .map(|value| parse_f64(&value, "--target-mbit").map(|mbit| mbit * 1_000_000.0))
        .transpose()?;
    let feedback_bind = optional_arg_value(args, "--feedback-bind");
    let feedback_headroom = optional_arg_value(args, "--feedback-headroom")
        .map(|value| parse_f64(&value, "--feedback-headroom"))
        .transpose()?
        .unwrap_or(1.02);
    let feedback_initial_bps = optional_arg_value(args, "--feedback-initial-mbit")
        .map(|value| parse_f64(&value, "--feedback-initial-mbit").map(|mbit| mbit * 1_000_000.0))
        .transpose()?
        .unwrap_or(0.0);
    let feedback_max_bps = optional_arg_value(args, "--feedback-max-mbit")
        .map(|value| parse_f64(&value, "--feedback-max-mbit").map(|mbit| mbit * 1_000_000.0))
        .transpose()?
        .unwrap_or(0.0);
    let feedback_probe_step_bps = optional_arg_value(args, "--feedback-probe-step-mbit")
        .map(|value| parse_f64(&value, "--feedback-probe-step-mbit").map(|mbit| mbit * 1_000_000.0))
        .transpose()?
        .unwrap_or(DEFAULT_FEEDBACK_PROBE_STEP_MBIT * 1_000_000.0);
    let feedback_good_ratio = optional_arg_value(args, "--feedback-good-ratio")
        .map(|value| parse_ratio(&value, "--feedback-good-ratio"))
        .transpose()?
        .unwrap_or(DEFAULT_FEEDBACK_GOOD_RATIO);
    let feedback_low_ratio = optional_arg_value(args, "--feedback-low-ratio")
        .map(|value| parse_ratio(&value, "--feedback-low-ratio"))
        .transpose()?
        .unwrap_or(DEFAULT_FEEDBACK_LOW_RATIO);
    let feedback_backoff_ratio = optional_arg_value(args, "--feedback-backoff-ratio")
        .map(|value| parse_ratio(&value, "--feedback-backoff-ratio"))
        .transpose()?
        .unwrap_or(DEFAULT_FEEDBACK_BACKOFF_RATIO);
    if feedback_low_ratio > feedback_good_ratio {
        return Err("--feedback-low-ratio must be less than or equal to --feedback-good-ratio".to_string());
    }
    let initial_bps = target_bps.unwrap_or(feedback_initial_bps).max(0.0) as u64;
    let target_bps = Arc::new(AtomicU64::new(initial_bps));
    let feedback_handle = feedback_bind.map(|bind| {
        let feedback_target_bps = Arc::clone(&target_bps);
        let feedback_config = FeedbackControllerConfig {
            headroom: feedback_headroom,
            max_bps: feedback_max_bps,
            probe_step_bps: feedback_probe_step_bps,
            good_ratio: feedback_good_ratio,
            low_ratio: feedback_low_ratio,
            backoff_ratio: feedback_backoff_ratio,
        };
        thread::spawn(move || run_feedback_listener(bind, duration, feedback_config, feedback_target_bps))
    });
    if flows == 1 {
        let stats = run_send_flow(
            target,
            duration,
            payload_size,
            send_batch,
            udp_gso_segments,
            target_bps,
            1,
        )?;
        join_feedback_listener(feedback_handle)?;
        return Ok(stats);
    }

    let started = Instant::now();
    let mut handles = Vec::with_capacity(flows);
    for index in 0..flows {
        let flow_target = offset_socket_addr(&target, index, target_port_stride, "--target-port-stride")?;
        let flow_target_bps = Arc::clone(&target_bps);
        handles.push(thread::spawn(move || {
            run_send_flow(
                flow_target,
                duration,
                payload_size,
                send_batch,
                udp_gso_segments,
                flow_target_bps,
                flows,
            )
        }));
    }

    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut send_calls = 0_u64;
    let mut max_send_batch = 0_u64;
    for handle in handles {
        let stats = handle.join().map_err(|_| "send worker panicked".to_string())??;
        packets += stats.packets;
        bytes += stats.bytes;
        send_calls += stats.send_calls;
        max_send_batch = max_send_batch.max(stats.max_send_batch);
    }
    join_feedback_listener(feedback_handle)?;
    Ok(RunStats {
        packets,
        bytes,
        elapsed: started.elapsed(),
        send_calls,
        recv_calls: 0,
        max_send_batch,
        max_recv_batch: 0,
    })
}

fn run_send_flow(
    target: String,
    duration: Duration,
    payload_size: u64,
    send_batch_size: usize,
    udp_gso_segments: usize,
    target_bps: Arc<AtomicU64>,
    flow_count: usize,
) -> Result<RunStats, String> {
    let socket = UdpSocket::bind("0.0.0.0:0").map_err(|error| format!("bind failed: {error}"))?;
    request_udp_socket_buffers(&socket);
    configure_udp_gso(&socket, payload_size as usize, udp_gso_segments)?;
    socket
        .connect(&target)
        .map_err(|error| format!("connect failed: {error}"))?;
    let super_payload_size = (payload_size as usize)
        .checked_mul(udp_gso_segments)
        .ok_or_else(|| "--payload-size * --udp-gso-segments overflowed".to_string())?;
    let payload = vec![b'u'; super_payload_size];
    let mut send_batch = SendBatch::new(&payload, send_batch_size);
    let started = Instant::now();
    let mut next_send = started;
    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut send_calls = 0_u64;
    let mut max_send_batch = 0_u64;

    while started.elapsed() < duration {
        let sent = send_batch
            .send(&socket)
            .map_err(|error| format!("send batch failed: {error}"))?;
        send_calls += 1;
        max_send_batch = max_send_batch.max(sent as u64);
        bytes += sent as u64 * payload.len() as u64;
        packets += sent as u64 * udp_gso_segments as u64;
        let target_bps = target_bps.load(Ordering::Relaxed);
        if target_bps > 0 {
            let flow_target_bps = (target_bps as f64 / flow_count as f64).max(1.0);
            next_send += Duration::from_secs_f64((sent as f64 * payload.len() as f64 * 8.0) / flow_target_bps);
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
        send_calls,
        recv_calls: 0,
        max_send_batch,
        max_recv_batch: 0,
    })
}

#[derive(Clone, Copy, Debug)]
struct FeedbackControllerConfig {
    headroom: f64,
    max_bps: f64,
    probe_step_bps: f64,
    good_ratio: f64,
    low_ratio: f64,
    backoff_ratio: f64,
}

fn run_feedback_listener(
    bind: String,
    duration: Duration,
    config: FeedbackControllerConfig,
    target_bps: Arc<AtomicU64>,
) -> Result<(), String> {
    let socket = UdpSocket::bind(&bind).map_err(|error| format!("feedback bind failed: {error}"))?;
    socket
        .set_read_timeout(Some(Duration::from_millis(100)))
        .map_err(|error| format!("feedback set_read_timeout failed: {error}"))?;
    let started = Instant::now();
    let mut buffer = [0_u8; 256];
    let mut sources: HashMap<SocketAddr, (f64, Instant)> = HashMap::new();
    while started.elapsed() < duration {
        match socket.recv_from(&mut buffer) {
            Ok((length, source)) => {
                if let Ok(text) = std::str::from_utf8(&buffer[..length]) {
                    if let Some(sample) = FeedbackSample::parse(text.trim()) {
                        let observed_bps = sample.bps;
                        if observed_bps.is_finite() && observed_bps > 0.0 {
                            let now = Instant::now();
                            sources.insert(source, (observed_bps, now));
                            sources
                                .retain(|_, (_, last_seen)| now.duration_since(*last_seen) <= Duration::from_secs(2));
                            let observed_total_bps = sources.values().map(|(bps, _)| *bps).sum::<f64>();
                            let current_bps = target_bps.load(Ordering::Relaxed) as f64;
                            let desired_bps = observed_total_bps * config.headroom;
                            let next_bps =
                                next_feedback_target_bps(current_bps, observed_total_bps, desired_bps, config);
                            let capped_bps = if config.max_bps > 0.0 {
                                next_bps.min(config.max_bps)
                            } else {
                                next_bps
                            };
                            target_bps.store(capped_bps.max(1.0) as u64, Ordering::Relaxed);
                        }
                    }
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock || error.kind() == io::ErrorKind::TimedOut => {}
            Err(error) => return Err(format!("feedback recv failed: {error}")),
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct FeedbackSample {
    bps: f64,
}

impl FeedbackSample {
    fn parse(text: &str) -> Option<Self> {
        if let Ok(bps) = text.parse::<f64>() {
            return Some(Self { bps });
        }
        let mut bps = None;
        for part in text.split_whitespace() {
            let Some((name, value)) = part.split_once('=') else {
                continue;
            };
            if name == "bps" {
                bps = value.parse::<f64>().ok();
            }
        }
        bps.map(|bps| Self { bps })
    }
}

fn next_feedback_target_bps(
    current_bps: f64,
    observed_bps: f64,
    desired_bps: f64,
    config: FeedbackControllerConfig,
) -> f64 {
    if current_bps <= 0.0 {
        return desired_bps;
    }
    let fill_ratio = observed_bps / current_bps.max(1.0);
    if fill_ratio >= config.good_ratio {
        return current_bps.max(desired_bps).max(current_bps + config.probe_step_bps);
    }
    if fill_ratio < config.low_ratio {
        return desired_bps.min(current_bps * config.backoff_ratio).max(observed_bps);
    }
    current_bps.max(desired_bps)
}

#[cfg(target_os = "linux")]
fn configure_udp_gso(socket: &UdpSocket, segment_size: usize, segments: usize) -> Result<(), String> {
    if segments <= 1 {
        return Ok(());
    }
    set_udp_segment_size(socket, segment_size).map_err(|error| format!("UDP GSO setup failed: {error}"))
}

#[cfg(not(target_os = "linux"))]
fn configure_udp_gso(_socket: &UdpSocket, _segment_size: usize, segments: usize) -> Result<(), String> {
    if segments <= 1 {
        return Ok(());
    }
    Err("UDP GSO is only supported by this benchmark tool on Linux".to_string())
}

fn join_feedback_listener(handle: Option<thread::JoinHandle<Result<(), String>>>) -> Result<(), String> {
    if let Some(handle) = handle {
        handle.join().map_err(|_| "feedback listener panicked".to_string())??;
    }
    Ok(())
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
    let workers = optional_arg_value(args, "--workers")
        .map(|value| parse_usize(&value, "--workers"))
        .transpose()?
        .unwrap_or(1);
    let bind_port_stride = optional_arg_value(args, "--bind-port-stride")
        .map(|value| parse_usize(&value, "--bind-port-stride"))
        .transpose()?
        .unwrap_or(0);
    let feedback_target = optional_arg_value(args, "--feedback-target");
    let feedback_interval = optional_arg_value(args, "--feedback-interval-ms")
        .map(|value| parse_duration_millis(&value, "--feedback-interval-ms"))
        .transpose()?
        .unwrap_or_else(|| Duration::from_millis(500));
    let recv_batch = optional_arg_value(args, "--recv-batch")
        .map(|value| parse_usize(&value, "--recv-batch"))
        .transpose()?
        .unwrap_or(UDP_BATCH_SIZE);
    let recv_buffer_size = optional_arg_value(args, "--recv-buffer-size")
        .map(|value| parse_usize(&value, "--recv-buffer-size"))
        .transpose()?
        .unwrap_or(DEFAULT_RECV_BUFFER_BYTES);
    let recv_truncate = has_arg(args, "--recv-truncate");
    let out = optional_arg_value(args, "--out");
    if workers == 1 {
        let socket = UdpSocket::bind(&bind).map_err(|error| format!("bind failed: {error}"))?;
        request_udp_socket_buffers(&socket);
        let feedback = make_feedback_sink(feedback_target.as_deref(), feedback_interval)?;
        return run_sink_socket(
            socket,
            duration,
            idle_after_first,
            out.as_deref(),
            feedback,
            recv_batch,
            recv_buffer_size,
            recv_truncate,
        );
    }
    let base_socket = if bind_port_stride == 0 {
        let socket = UdpSocket::bind(&bind).map_err(|error| format!("bind failed: {error}"))?;
        request_udp_socket_buffers(&socket);
        Some(socket)
    } else {
        None
    };
    let mut handles = Vec::with_capacity(workers);
    for index in 0..workers {
        let worker_socket = if bind_port_stride == 0 {
            base_socket
                .as_ref()
                .ok_or_else(|| "base socket missing".to_string())?
                .try_clone()
                .map_err(|error| format!("clone socket failed: {error}"))?
        } else {
            let worker_bind = offset_socket_addr(&bind, index, bind_port_stride, "--bind-port-stride")?;
            let worker_socket = UdpSocket::bind(&worker_bind).map_err(|error| format!("bind failed: {error}"))?;
            request_udp_socket_buffers(&worker_socket);
            worker_socket
        };
        let worker_feedback = make_feedback_sink(feedback_target.as_deref(), feedback_interval)?;
        handles.push(thread::spawn(move || {
            run_sink_socket(
                worker_socket,
                duration,
                idle_after_first,
                None,
                worker_feedback,
                recv_batch,
                recv_buffer_size,
                recv_truncate,
            )
        }));
    }

    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut active_elapsed = Duration::ZERO;
    let mut fallback_elapsed = Duration::ZERO;
    let mut recv_calls = 0_u64;
    let mut max_recv_batch = 0_u64;
    for handle in handles {
        let stats = handle.join().map_err(|_| "sink worker panicked".to_string())??;
        packets += stats.packets;
        bytes += stats.bytes;
        fallback_elapsed = fallback_elapsed.max(stats.elapsed);
        if stats.packets > 0 {
            active_elapsed = active_elapsed.max(stats.elapsed);
        }
        recv_calls += stats.recv_calls;
        max_recv_batch = max_recv_batch.max(stats.max_recv_batch);
    }
    let elapsed = if packets > 0 {
        active_elapsed
    } else {
        fallback_elapsed
    };
    let stats = RunStats {
        packets,
        bytes,
        elapsed,
        send_calls: 0,
        recv_calls,
        max_send_batch: 0,
        max_recv_batch,
    };
    write_snapshot(out.as_deref(), &stats, true).map_err(|error| format!("write snapshot failed: {error}"))?;
    Ok(stats)
}

fn parse_duration_millis(value: &str, name: &str) -> Result<Duration, String> {
    let millis = parse_u64(value, name)?;
    Ok(Duration::from_millis(millis))
}

struct FeedbackSink {
    socket: UdpSocket,
    target: SocketAddr,
    interval: Duration,
    last_send: Instant,
    last_bytes: u64,
}

impl FeedbackSink {
    fn maybe_send(&mut self, stats: &RunStats, now: Instant) -> Result<(), String> {
        let elapsed = now.duration_since(self.last_send);
        if elapsed < self.interval {
            return Ok(());
        }
        let byte_delta = stats.bytes.saturating_sub(self.last_bytes);
        let interval_bps = (byte_delta as f64 * 8.0) / elapsed.as_secs_f64().max(0.000_001);
        let cumulative_bps = stats.bits_per_second();
        let bps = interval_bps.max(cumulative_bps);
        self.last_send = now;
        self.last_bytes = stats.bytes;
        self.send_sample(bps, interval_bps, cumulative_bps, stats)
    }

    fn send_sample(&self, bps: f64, interval_bps: f64, cumulative_bps: f64, stats: &RunStats) -> Result<(), String> {
        let message = format!(
            "bps={:.3} interval_bps={:.3} cumulative_bps={:.3} bytes={} packets={}",
            bps, interval_bps, cumulative_bps, stats.bytes, stats.packets
        );
        self.socket
            .send_to(message.as_bytes(), self.target)
            .map_err(|error| format!("feedback send failed: {error}"))?;
        Ok(())
    }
}

fn make_feedback_sink(target: Option<&str>, interval: Duration) -> Result<Option<FeedbackSink>, String> {
    let Some(target) = target else {
        return Ok(None);
    };
    let target = target
        .parse::<SocketAddr>()
        .map_err(|error| format!("invalid --feedback-target: {error}"))?;
    let socket = UdpSocket::bind("0.0.0.0:0").map_err(|error| format!("feedback bind failed: {error}"))?;
    Ok(Some(FeedbackSink {
        socket,
        target,
        interval,
        last_send: Instant::now(),
        last_bytes: 0,
    }))
}

fn offset_socket_addr(address: &str, index: usize, stride: usize, name: &str) -> Result<String, String> {
    if index == 0 || stride == 0 {
        return Ok(address.to_string());
    }
    let mut parsed = address
        .parse::<SocketAddr>()
        .map_err(|error| format!("{name} requires a numeric socket address: {error}"))?;
    let offset = index
        .checked_mul(stride)
        .ok_or_else(|| format!("{name} port offset overflow"))?;
    let offset = u16::try_from(offset).map_err(|_| format!("{name} port offset overflow"))?;
    let port = parsed
        .port()
        .checked_add(offset)
        .ok_or_else(|| format!("{name} port overflow"))?;
    parsed.set_port(port);
    Ok(parsed.to_string())
}

#[cfg(target_os = "linux")]
fn run_sink_socket(
    socket: UdpSocket,
    duration: Duration,
    idle_after_first: Duration,
    out: Option<&str>,
    mut feedback: Option<FeedbackSink>,
    recv_batch_size: usize,
    recv_buffer_size: usize,
    recv_truncate: bool,
) -> Result<RunStats, String> {
    socket
        .set_nonblocking(true)
        .map_err(|error| format!("set_nonblocking failed: {error}"))?;

    let started = Instant::now();
    let mut first_packet: Option<Instant> = None;
    let mut last_packet: Option<Instant> = None;
    let mut last_snapshot = started;
    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut recv_calls = 0_u64;
    let mut max_recv_batch = 0_u64;
    let mut batch = RecvBatch::new(recv_batch_size, recv_buffer_size, recv_truncate);

    while started.elapsed() < duration {
        match batch.recv(&socket) {
            Ok(received) if received > 0 => {
                recv_calls += 1;
                max_recv_batch = max_recv_batch.max(received as u64);
                let now = Instant::now();
                first_packet.get_or_insert(now);
                last_packet = Some(now);
                for length in batch.lengths(received) {
                    packets += 1;
                    bytes += length as u64;
                }
                if now.duration_since(last_snapshot) >= Duration::from_secs(1) {
                    let elapsed = first_packet.map_or(started.elapsed(), |first| now.duration_since(first));
                    let stats = RunStats {
                        packets,
                        bytes,
                        elapsed,
                        send_calls: 0,
                        recv_calls,
                        max_send_batch: 0,
                        max_recv_batch,
                    };
                    write_snapshot(out, &stats, false).map_err(|error| format!("write snapshot failed: {error}"))?;
                    last_snapshot = now;
                }
                if let Some(feedback) = feedback.as_mut() {
                    let elapsed = first_packet.map_or(started.elapsed(), |first| now.duration_since(first));
                    let stats = RunStats {
                        packets,
                        bytes,
                        elapsed,
                        send_calls: 0,
                        recv_calls,
                        max_send_batch: 0,
                        max_recv_batch,
                    };
                    feedback.maybe_send(&stats, now)?;
                }
            }
            Ok(_) => thread::yield_now(),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                if let Some(last) = last_packet {
                    if last.elapsed() >= idle_after_first {
                        break;
                    }
                }
                thread::sleep(Duration::from_micros(250));
            }
            Err(error) => return Err(format!("recvmmsg failed: {error}")),
        }
    }

    let now = Instant::now();
    let elapsed = first_packet.map_or(started.elapsed(), |first| {
        last_packet.unwrap_or(now).duration_since(first)
    });
    let stats = RunStats {
        packets,
        bytes,
        elapsed,
        send_calls: 0,
        recv_calls,
        max_send_batch: 0,
        max_recv_batch,
    };
    if let Some(mut feedback) = feedback {
        feedback.maybe_send(&stats, now)?;
    }
    Ok(stats)
}

#[cfg(not(target_os = "linux"))]
fn run_sink_socket(
    socket: UdpSocket,
    duration: Duration,
    idle_after_first: Duration,
    out: Option<&str>,
    mut feedback: Option<FeedbackSink>,
    _recv_batch_size: usize,
    recv_buffer_size: usize,
    _recv_truncate: bool,
) -> Result<RunStats, String> {
    socket
        .set_read_timeout(Some(Duration::from_millis(200)))
        .map_err(|error| format!("set_read_timeout failed: {error}"))?;

    let started = Instant::now();
    let mut first_packet: Option<Instant> = None;
    let mut last_packet: Option<Instant> = None;
    let mut last_snapshot = started;
    let mut packets = 0_u64;
    let mut bytes = 0_u64;
    let mut recv_calls = 0_u64;
    let mut max_recv_batch = 0_u64;
    let mut buffer = vec![0_u8; recv_buffer_size];

    while started.elapsed() < duration {
        match socket.recv_from(&mut buffer) {
            Ok((length, _source)) => {
                recv_calls += 1;
                max_recv_batch = 1;
                let now = Instant::now();
                first_packet.get_or_insert(now);
                last_packet = Some(now);
                packets += 1;
                bytes += length as u64;
                if now.duration_since(last_snapshot) >= Duration::from_secs(1) {
                    let elapsed = first_packet.map_or(started.elapsed(), |first| now.duration_since(first));
                    let stats = RunStats {
                        packets,
                        bytes,
                        elapsed,
                        send_calls: 0,
                        recv_calls,
                        max_send_batch: 0,
                        max_recv_batch,
                    };
                    write_snapshot(out, &stats, false).map_err(|error| format!("write snapshot failed: {error}"))?;
                    last_snapshot = now;
                }
                if let Some(feedback) = feedback.as_mut() {
                    let elapsed = first_packet.map_or(started.elapsed(), |first| now.duration_since(first));
                    let stats = RunStats {
                        packets,
                        bytes,
                        elapsed,
                        send_calls: 0,
                        recv_calls,
                        max_send_batch: 0,
                        max_recv_batch,
                    };
                    feedback.maybe_send(&stats, now)?;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock || error.kind() == io::ErrorKind::TimedOut => {
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
    let stats = RunStats {
        packets,
        bytes,
        elapsed,
        send_calls: 0,
        recv_calls,
        max_send_batch: 0,
        max_recv_batch,
    };
    if let Some(mut feedback) = feedback {
        feedback.maybe_send(&stats, now)?;
    }
    Ok(stats)
}

#[cfg(target_os = "linux")]
struct RecvBatch {
    _buffers: Vec<Vec<u8>>,
    names: Vec<SockaddrStorage>,
    iovecs: Vec<IoVec>,
    messages: Vec<MmsgHdr>,
    truncate: bool,
}

#[cfg(target_os = "linux")]
impl RecvBatch {
    fn new(batch_size: usize, buffer_size: usize, truncate: bool) -> Self {
        let effective_buffer_size = if truncate { 1 } else { buffer_size };
        let mut buffers = (0..batch_size)
            .map(|_| vec![0_u8; effective_buffer_size])
            .collect::<Vec<_>>();
        let mut names = vec![SockaddrStorage::default(); batch_size];
        let mut iovecs = buffers
            .iter_mut()
            .map(|buffer| IoVec {
                iov_base: buffer.as_mut_ptr().cast(),
                iov_len: buffer.len(),
            })
            .collect::<Vec<_>>();
        let mut messages = Vec::with_capacity(batch_size);
        for index in 0..batch_size {
            messages.push(MmsgHdr {
                msg_hdr: MsgHdr {
                    msg_name: (&mut names[index] as *mut SockaddrStorage).cast(),
                    msg_namelen: std::mem::size_of::<SockaddrStorage>() as SockLen,
                    msg_iov: &mut iovecs[index] as *mut IoVec,
                    msg_iovlen: 1,
                    msg_control: std::ptr::null_mut(),
                    msg_controllen: 0,
                    msg_flags: 0,
                },
                msg_len: 0,
            });
        }
        Self {
            _buffers: buffers,
            names,
            iovecs,
            messages,
            truncate,
        }
    }

    fn recv(&mut self, socket: &UdpSocket) -> io::Result<usize> {
        use std::os::fd::AsRawFd;

        for (index, message) in self.messages.iter_mut().enumerate() {
            message.msg_hdr.msg_namelen = std::mem::size_of::<SockaddrStorage>() as SockLen;
            message.msg_hdr.msg_iov = &mut self.iovecs[index] as *mut IoVec;
            message.msg_hdr.msg_name = (&mut self.names[index] as *mut SockaddrStorage).cast();
            message.msg_len = 0;
        }

        let flags = if self.truncate { MSG_TRUNC as CUInt } else { 0 };
        let received = unsafe {
            recvmmsg(
                socket.as_raw_fd(),
                self.messages.as_mut_ptr(),
                self.messages.len() as CUInt,
                flags,
                std::ptr::null_mut(),
            )
        };
        if received < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::WouldBlock {
                return Err(error);
            }
            return Err(error);
        }
        Ok(received as usize)
    }

    fn lengths(&self, received: usize) -> impl Iterator<Item = usize> + '_ {
        self.messages
            .iter()
            .take(received)
            .map(|message| message.msg_len as usize)
    }
}

#[cfg(target_os = "linux")]
struct SendBatch {
    iovecs: Vec<IoVec>,
    messages: Vec<MmsgHdr>,
}

#[cfg(target_os = "linux")]
impl SendBatch {
    fn new(payload: &[u8], batch_size: usize) -> Self {
        let mut iovecs = (0..batch_size)
            .map(|_| IoVec {
                iov_base: payload.as_ptr().cast::<CVoid>().cast_mut(),
                iov_len: payload.len(),
            })
            .collect::<Vec<_>>();
        let mut messages = Vec::with_capacity(batch_size);
        for index in 0..batch_size {
            messages.push(MmsgHdr {
                msg_hdr: MsgHdr {
                    msg_name: std::ptr::null_mut(),
                    msg_namelen: 0,
                    msg_iov: &mut iovecs[index] as *mut IoVec,
                    msg_iovlen: 1,
                    msg_control: std::ptr::null_mut(),
                    msg_controllen: 0,
                    msg_flags: 0,
                },
                msg_len: 0,
            });
        }
        Self { iovecs, messages }
    }

    fn send(&mut self, socket: &UdpSocket) -> io::Result<usize> {
        use std::os::fd::AsRawFd;

        for (index, message) in self.messages.iter_mut().enumerate() {
            message.msg_hdr.msg_iov = &mut self.iovecs[index] as *mut IoVec;
            message.msg_len = 0;
        }
        let sent = unsafe {
            sendmmsg(
                socket.as_raw_fd(),
                self.messages.as_mut_ptr(),
                self.messages.len() as CUInt,
                0,
            )
        };
        if sent < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(sent as usize)
    }
}

#[cfg(not(target_os = "linux"))]
struct SendBatch<'a> {
    payload: &'a [u8],
    batch_size: usize,
}

#[cfg(not(target_os = "linux"))]
impl<'a> SendBatch<'a> {
    fn new(payload: &'a [u8], batch_size: usize) -> Self {
        Self { payload, batch_size }
    }

    fn send(&mut self, socket: &UdpSocket) -> io::Result<usize> {
        let mut sent = 0_usize;
        for _ in 0..self.batch_size {
            socket.send(self.payload)?;
            sent += 1;
        }
        Ok(sent)
    }
}

#[cfg(target_os = "linux")]
#[repr(C, align(8))]
#[derive(Clone, Copy)]
struct SockaddrStorage {
    storage: [u8; 128],
}

#[cfg(target_os = "linux")]
impl Default for SockaddrStorage {
    fn default() -> Self {
        Self { storage: [0; 128] }
    }
}

#[cfg(target_os = "linux")]
type SizeT = usize;
#[cfg(target_os = "linux")]
type SockLen = u32;
#[cfg(target_os = "linux")]
type CInt = std::os::raw::c_int;
#[cfg(target_os = "linux")]
type CUInt = std::os::raw::c_uint;
#[cfg(target_os = "linux")]
type CVoid = std::os::raw::c_void;
#[cfg(target_os = "linux")]
const MSG_TRUNC: CInt = 0x20;

#[cfg(target_os = "linux")]
#[repr(C)]
struct IoVec {
    iov_base: *mut CVoid,
    iov_len: SizeT,
}

#[cfg(target_os = "linux")]
#[repr(C)]
struct MsgHdr {
    msg_name: *mut CVoid,
    msg_namelen: SockLen,
    msg_iov: *mut IoVec,
    msg_iovlen: SizeT,
    msg_control: *mut CVoid,
    msg_controllen: SizeT,
    msg_flags: CInt,
}

#[cfg(target_os = "linux")]
#[repr(C)]
struct MmsgHdr {
    msg_hdr: MsgHdr,
    msg_len: CUInt,
}

#[cfg(target_os = "linux")]
#[repr(C)]
struct TimeSpec {
    tv_sec: i64,
    tv_nsec: i64,
}

#[cfg(target_os = "linux")]
unsafe extern "C" {
    fn recvmmsg(fd: CInt, msgvec: *mut MmsgHdr, vlen: CUInt, flags: CUInt, timeout: *mut TimeSpec) -> CInt;

    fn sendmmsg(fd: CInt, msgvec: *mut MmsgHdr, vlen: CUInt, flags: CUInt) -> CInt;
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
        let _ = setsockopt(socket.as_raw_fd(), SOL_SOCKET, SO_RCVBUF, value_ptr, value_len);
        let _ = setsockopt(socket.as_raw_fd(), SOL_SOCKET, SO_SNDBUF, value_ptr, value_len);
    }
}

#[cfg(not(unix))]
fn request_udp_socket_buffers(_socket: &UdpSocket) {}

#[cfg(target_os = "linux")]
fn set_udp_segment_size(socket: &UdpSocket, segment_size: usize) -> io::Result<()> {
    use std::os::fd::AsRawFd;

    const SOL_UDP: CInt = 17;
    const UDP_SEGMENT: CInt = 103;

    unsafe extern "C" {
        fn setsockopt(
            socket: CInt,
            level: CInt,
            option_name: CInt,
            option_value: *const CVoid,
            option_len: SockLen,
        ) -> CInt;
    }

    let value = segment_size as CInt;
    let result = unsafe {
        setsockopt(
            socket.as_raw_fd(),
            SOL_UDP,
            UDP_SEGMENT,
            (&value as *const CInt).cast::<CVoid>(),
            std::mem::size_of_val(&value) as SockLen,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

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
