use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, CoreRuntimeConfig, PathSchedulerPrimitives, PathSchedulerState, SchedulerConfig, SchedulerMode,
};
use gatherlink_dataplane::udp_service::UdpServiceConfig;

fn path(path_id: u16, tx_capacity_bps: u64, latency_us: u32, loss_ppm: u32) -> CorePathConfig {
    CorePathConfig::new_with_scheduler_primitives(
        path_id,
        0,
        1200,
        true,
        PathSchedulerState::Active,
        1,
        PathSchedulerPrimitives::new(Some(tx_capacity_bps), None, Some(latency_us), loss_ppm, 0, 0, 0),
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

#[test]
fn lowest_latency_scheduler_uses_latency_primitive() {
    let selected = forward_with_mode(
        SchedulerMode::LowestLatency,
        vec![path(10, 10_000_000, 50_000, 0), path(20, 1_000_000, 5_000, 0)],
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
fn earliest_completion_first_considers_payload_size_and_capacity() {
    let selected = forward_payload_with_mode(
        SchedulerMode::EarliestCompletionFirst,
        vec![path(10, 100_000, 5_000, 0), path(20, 10_000_000, 20_000, 0)],
        vec![0; 1200],
    );

    assert_eq!(selected, 20);
}

#[test]
fn balanced_scheduler_uses_capacity_latency_and_loss() {
    let selected = forward_with_mode(
        SchedulerMode::Balanced,
        vec![path(10, 10_000_000, 5_000, 100_000), path(20, 5_000_000, 20_000, 0)],
    );

    assert_eq!(selected, 20);
}
