use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, CoreRuntimeConfig, PathSchedulerPrimitives, PathSchedulerState, SchedulerConfig, SchedulerMode,
};
use gatherlink_dataplane::udp_service::UdpServiceConfig;

fn path(path_id: u16, tx_capacity_bps: u64, latency_us: u32, loss_ppm: u32) -> CorePathConfig {
    path_with_limits(path_id, tx_capacity_bps, latency_us, loss_ppm, 0, 0, 0)
}

fn path_with_limits(
    path_id: u16,
    tx_capacity_bps: u64,
    latency_us: u32,
    loss_ppm: u32,
    reorder_hold_us: u32,
    max_in_flight_packets: u16,
    max_in_flight_bytes: u32,
) -> CorePathConfig {
    path_with_limits_and_mtu(
        path_id,
        1200,
        tx_capacity_bps,
        latency_us,
        loss_ppm,
        reorder_hold_us,
        max_in_flight_packets,
        max_in_flight_bytes,
    )
}

#[allow(clippy::too_many_arguments)]
fn path_with_limits_and_mtu(
    path_id: u16,
    mtu: usize,
    tx_capacity_bps: u64,
    latency_us: u32,
    loss_ppm: u32,
    reorder_hold_us: u32,
    max_in_flight_packets: u16,
    max_in_flight_bytes: u32,
) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        mtu,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(
            Some(tx_capacity_bps),
            None,
            Some(latency_us),
            loss_ppm,
            reorder_hold_us,
            max_in_flight_packets,
            max_in_flight_bytes,
            0,
            0,
            0,
            0,
        ),
    )
    .unwrap()
}

#[allow(clippy::too_many_arguments)]
fn path_with_weight_limits_and_mtu(
    path_id: u16,
    mtu: usize,
    weight: u16,
    tx_capacity_bps: u64,
    latency_us: u32,
    loss_ppm: u32,
    reorder_hold_us: u32,
    max_in_flight_packets: u16,
    max_in_flight_bytes: u32,
) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        mtu,
        true,
        PathSchedulerState::Active,
        weight,
        PathSchedulerPrimitives::new(
            Some(tx_capacity_bps),
            None,
            Some(latency_us),
            loss_ppm,
            reorder_hold_us,
            max_in_flight_packets,
            max_in_flight_bytes,
            0,
            0,
            0,
            0,
        ),
    )
    .unwrap()
}

fn path_with_pacing_budget(path_id: u16, tx_capacity_bps: u64, pacing_budget_bps: u64) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        1200,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(
            Some(tx_capacity_bps),
            None,
            Some(1_000),
            0,
            50_000,
            0,
            0,
            pacing_budget_bps,
            0,
            0,
            0,
        ),
    )
    .unwrap()
}

fn path_with_queue_and_limits(
    path_id: u16,
    tx_capacity_bps: u64,
    latency_us: u32,
    max_in_flight_packets: u16,
    max_in_flight_bytes: u32,
    queue_depth_packets: u32,
    queue_depth_bytes: u32,
) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        1200,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(
            Some(tx_capacity_bps),
            None,
            Some(latency_us),
            0,
            100_000,
            max_in_flight_packets,
            max_in_flight_bytes,
            0,
            queue_depth_packets,
            queue_depth_bytes,
            0,
        ),
    )
    .unwrap()
}

fn queued_path(path_id: u16, queue_depth_packets: u32, queue_depth_bytes: u32, oldest_age_us: u64) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        1200,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(
            Some(1_000_000),
            None,
            Some(10_000),
            0,
            0,
            0,
            0,
            0,
            queue_depth_packets,
            queue_depth_bytes,
            oldest_age_us,
        ),
    )
    .unwrap()
}

fn forward_with_mode(mode: SchedulerMode, paths: Vec<CorePathConfig>) -> u16 {
    forward_payload_with_mode(mode, paths, b"select-me".to_vec())
}

fn forward_payload_with_mode(mode: SchedulerMode, paths: Vec<CorePathConfig>, payload: Vec<u8>) -> u16 {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        paths,
        SchedulerConfig::new(mode),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    sender.send_to(&payload, service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 16];
    target.recv_from(&mut buffer).unwrap();
    outcome.path_id
}

fn forward_payloads_with_mode(mode: SchedulerMode, paths: Vec<CorePathConfig>, payloads: usize) -> Vec<u16> {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        paths,
        SchedulerConfig::new(mode),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let mut selected = Vec::with_capacity(payloads);

    for index in 0..payloads {
        sender.send_to(&vec![index as u8; 1200], service_addr).unwrap();
        selected.push(dataplane.forward_one_for_service("udp-main").unwrap().path_id);
        let mut buffer = [0_u8; 1400];
        target.recv_from(&mut buffer).unwrap();
    }
    selected
}

fn forward_burst_payloads_with_mode(mode: SchedulerMode, paths: Vec<CorePathConfig>, payloads: usize) -> Vec<u16> {
    forward_burst_payloads_with_size(mode, paths, payloads, 1200)
}

fn forward_burst_payloads_with_size(
    mode: SchedulerMode,
    paths: Vec<CorePathConfig>,
    payloads: usize,
    payload_len: usize,
) -> Vec<u16> {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        paths,
        SchedulerConfig::new(mode),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    for index in 0..payloads {
        sender.send_to(&vec![index as u8; payload_len], service_addr).unwrap();
    }

    dataplane
        .forward_available_for_service_nonblocking("udp-main", payloads)
        .unwrap()
        .into_iter()
        .map(|outcome| outcome.path_id)
        .collect()
}

#[test]
fn weighted_round_robin_interleaves_capacity_weights_for_large_payload_bursts() {
    let selected = forward_burst_payloads_with_size(
        SchedulerMode::WeightedRoundRobin,
        vec![
            path_with_weight_limits_and_mtu(10, 9000, 3, 300_000_000, 1_000, 0, 50_000, 128, 2_000_000),
            path_with_weight_limits_and_mtu(20, 9000, 5, 500_000_000, 1_000, 0, 50_000, 128, 2_000_000),
            path_with_weight_limits_and_mtu(30, 9000, 7, 700_000_000, 1_000, 0, 50_000, 128, 2_000_000),
        ],
        15,
        8192,
    );

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        (2..=4).contains(&path_a),
        "300 Mbit path should receive a smooth 20% share: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        (4..=6).contains(&path_b),
        "500 Mbit path should receive a smooth 33% share: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        (6..=8).contains(&path_c),
        "700 Mbit path should receive a smooth 47% share: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn lowest_latency_scheduler_uses_latency_primitive() {
    let selected = forward_with_mode(
        SchedulerMode::LowestLatency,
        vec![path(10, 10_000_000, 50_000, 0), path(20, 1_000_000, 5_000, 0)],
    );

    assert_eq!(selected, 20);
}

#[test]
fn ordered_multipath_observes_python_compiled_pacing_budget() {
    let selected = forward_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_pacing_budget(10, 1_000_000_000, 1_000_000),
            path_with_pacing_budget(20, 1_000_000_000, 0),
        ],
    );

    assert_eq!(selected, 20);
}

#[test]
fn loss_aware_scheduler_uses_loss_primitive_before_latency() {
    let selected = forward_with_mode(
        SchedulerMode::LossAware,
        vec![path(10, 10_000_000, 5_000, 100_000), path(20, 1_000_000, 50_000, 0)],
    );

    assert_eq!(selected, 20);
}

#[test]
fn capacity_aware_scheduler_uses_tx_capacity_primitive() {
    let selected = forward_with_mode(
        SchedulerMode::CapacityAware,
        vec![path(10, 1_000_000, 5_000, 0), path(20, 10_000_000, 50_000, 0)],
    );

    assert_eq!(selected, 20);
}

#[test]
fn least_queue_scheduler_uses_live_queue_primitives() {
    let selected = forward_with_mode(
        SchedulerMode::LeastQueue,
        vec![queued_path(10, 32, 4_096, 10_000), queued_path(20, 1, 256, 50_000)],
    );

    assert_eq!(selected, 20);
}

#[test]
fn earliest_completion_first_considers_payload_size_and_capacity() {
    let selected = forward_payload_with_mode(
        SchedulerMode::EarliestCompletionFirst,
        vec![path(10, 100_000, 5_000, 0), path(20, 10_000_000, 20_000, 0)],
        vec![0; 1200],
    );

    assert_eq!(selected, 20);
}

#[test]
fn ordered_multipath_uses_more_than_one_path_when_arrival_order_stays_safe() {
    let selected = forward_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![path(10, 100_000, 5_000, 0), path(20, 100_000, 6_000, 0)],
        4,
    );

    assert!(selected.contains(&10));
    assert!(selected.contains(&20));
}

#[test]
fn ordered_multipath_avoids_paths_that_would_arrive_too_early() {
    let selected = forward_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![path(10, 100_000, 80_000, 0), path(20, 10_000_000, 5_000, 0)],
        3,
    );

    assert_eq!(selected, vec![20, 20, 20]);
}

#[test]
fn ordered_multipath_uses_compiled_in_flight_credit() {
    let selected = forward_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_limits(10, 1_000_000_000, 50_000, 0, 100_000, 1, 1200),
            path_with_limits(20, 1_000_000_000, 60_000, 0, 100_000, 64, 64 * 1200),
        ],
        3,
    );

    assert_eq!(selected[0], 10);
    assert_eq!(selected[1], 20);
}

#[test]
fn ordered_multipath_uses_available_credit_before_overfilled_paths() {
    let selected = forward_burst_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_queue_and_limits(10, 1_000_000_000, 1_000_000, 1, 1200, 0, 0),
            path_with_queue_and_limits(20, 1_000_000_000, 1_000_000, 64, 64 * 1200, 0, 0),
        ],
        3,
    );

    assert_eq!(selected[0], 10);
    assert_eq!(selected[1], 20);
    assert_eq!(
        selected[2], 20,
        "path 10 is already at its Python-compiled in-flight limit, so Rust should use the path with credit"
    );
}

#[test]
fn ordered_multipath_reports_in_flight_and_delivery_facts() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            path_with_queue_and_limits(10, 1_000_000_000, 1_000_000, 64, 64 * 1200, 0, 0),
            path_with_queue_and_limits(20, 1_000_000_000, 1_100_000, 64, 64 * 1200, 0, 0),
        ],
        SchedulerConfig::new(SchedulerMode::OrderedMultipath),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

    for index in 0..4 {
        sender.send_to(&vec![index as u8; 1200], service_addr).unwrap();
    }
    dataplane
        .forward_available_for_service_nonblocking("udp-main", 4)
        .unwrap();

    let snapshot = dataplane.metrics_snapshot();
    let total_in_flight = snapshot
        .paths
        .values()
        .map(|path| path.scheduler_in_flight_packets)
        .sum::<u64>();

    assert_eq!(total_in_flight, 4);
    assert!(
        snapshot
            .paths
            .values()
            .any(|path| path.scheduler_in_flight_bytes > 0 && path.scheduler_predicted_delivery_us > 0),
        "ordered scheduler should expose cheap facts for Python-owned pressure decisions"
    );
}

#[test]
fn ordered_multipath_falls_back_when_all_paths_exceed_compiled_credit() {
    let selected = forward_burst_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_queue_and_limits(10, 1_000_000_000, 1_000, 1, 1200, 0, 0),
            path_with_queue_and_limits(20, 1_000_000_000, 1_000, 1, 1200, 0, 0),
        ],
        3,
    );

    assert_eq!(selected.len(), 3);
    assert!(
        selected.contains(&10) || selected.contains(&20),
        "bounded credit is an execution preference, not a deadlock when every path is full"
    );
}

#[test]
fn ordered_multipath_stripes_unequal_capacity_inside_reorder_hold() {
    let selected = forward_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_limits_and_mtu(10, 1443, 300_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(20, 1443, 500_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(30, 1443, 700_000_000, 1_000, 0, 50_000, 128, 256_000),
        ],
        90,
    );

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        path_a > 0,
        "slowest path should still carry some ordered traffic: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_b > path_a,
        "middle path should carry more traffic than the slowest path: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_c > path_b,
        "fastest path should carry the largest share: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn ordered_multipath_keeps_capacity_split_during_burst_forwarding() {
    let selected = forward_burst_payloads_with_mode(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_limits_and_mtu(10, 1443, 300_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(20, 1443, 500_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(30, 1443, 700_000_000, 1_000, 0, 50_000, 128, 256_000),
        ],
        90,
    );

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        path_a > 0,
        "slowest path should still carry some ordered traffic: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_b > path_a,
        "middle path should carry more burst traffic than the slowest path: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_c > path_b,
        "fastest path should carry the largest burst share: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn ordered_multipath_keeps_capacity_split_for_jumbo_bursts() {
    let selected = forward_burst_payloads_with_size(
        SchedulerMode::OrderedMultipath,
        vec![
            path_with_limits_and_mtu(10, 9000, 300_000_000, 1_000, 0, 50_000, 8192, 8_000_000),
            path_with_limits_and_mtu(20, 9000, 500_000_000, 1_000, 0, 50_000, 8192, 8_000_000),
            path_with_limits_and_mtu(30, 9000, 700_000_000, 1_000, 0, 50_000, 8192, 8_000_000),
        ],
        150,
        8192,
    );

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        path_b > path_a,
        "middle path should carry more jumbo traffic than the slowest path: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_c > path_b,
        "fastest path should carry the largest jumbo share: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn ordered_multipath_does_not_starve_idle_fast_path_after_wall_clock_progress() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            path_with_limits_and_mtu(10, 1443, 300_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(20, 1443, 500_000_000, 1_000, 0, 50_000, 128, 256_000),
            path_with_limits_and_mtu(30, 1443, 700_000_000, 1_000, 0, 50_000, 128, 256_000),
        ],
        SchedulerConfig::new(SchedulerMode::OrderedMultipath),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let mut selected = Vec::new();

    for index in 0..12 {
        sender.send_to(&vec![index as u8; 1200], service_addr).unwrap();
        selected.push(dataplane.forward_one_for_service("udp-main").unwrap().path_id);
        let mut buffer = [0_u8; 1400];
        target.recv_from(&mut buffer).unwrap();
    }
    std::thread::sleep(Duration::from_millis(20));
    for index in 12..60 {
        sender.send_to(&vec![index as u8; 1200], service_addr).unwrap();
        selected.push(dataplane.forward_one_for_service("udp-main").unwrap().path_id);
        let mut buffer = [0_u8; 1400];
        target.recv_from(&mut buffer).unwrap();
    }

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        path_c > 0,
        "idle fast path should not look permanently too early: {path_a}/{path_b}/{path_c}"
    );
    assert!(
        path_c >= path_a,
        "fast path should remain competitive after wall clock progress: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn ordered_multipath_rotates_equal_paths_after_idle_gaps() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let config = CoreRuntimeConfig::new_with_paths_and_scheduler(
        vec![UdpServiceConfig::new(
            "udp-main",
            Some("127.0.0.1:0".parse().unwrap()),
            target.local_addr().unwrap(),
        )
        .unwrap()],
        vec![
            path_with_limits_and_mtu(10, 1443, 5_000_000_000, 1_000, 0, 2_000, 128, 256_000),
            path_with_limits_and_mtu(20, 1443, 5_000_000_000, 1_000, 0, 2_000, 128, 256_000),
            path_with_limits_and_mtu(30, 1443, 5_000_000_000, 1_000, 0, 2_000, 128, 256_000),
        ],
        SchedulerConfig::new(SchedulerMode::OrderedMultipath),
    )
    .unwrap();
    let mut dataplane = CoreDataplane::bind(config).unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();
    let mut selected = Vec::new();

    for index in 0..12 {
        sender.send_to(&vec![index as u8; 1200], service_addr).unwrap();
        selected.push(dataplane.forward_one_for_service("udp-main").unwrap().path_id);
        let mut buffer = [0_u8; 1400];
        target.recv_from(&mut buffer).unwrap();
        std::thread::sleep(Duration::from_millis(3));
    }

    let path_a = selected.iter().filter(|path_id| **path_id == 10).count();
    let path_b = selected.iter().filter(|path_id| **path_id == 20).count();
    let path_c = selected.iter().filter(|path_id| **path_id == 30).count();

    assert!(
        path_a > 0 && path_b > 0 && path_c > 0,
        "equal clean paths should all get a chance after idle gaps: {path_a}/{path_b}/{path_c}"
    );
}

#[test]
fn balanced_scheduler_uses_capacity_latency_and_loss() {
    let selected = forward_with_mode(
        SchedulerMode::Balanced,
        vec![path(10, 10_000_000, 5_000, 100_000), path(20, 5_000_000, 20_000, 0)],
    );

    assert_eq!(selected, 20);
}
