use std::thread;
use std::time::Duration;

use gatherlink_dataplane::metrics::DataplaneMetrics;
use gatherlink_protocol::control::SequenceObservation;
use gatherlink_protocol::ids::USER_SERVICE_ID_START;

#[test]
fn counters_record_cheap_tx_and_rx_timing_facts() {
    let mut metrics = DataplaneMetrics::default();

    metrics.record_forward_parts("udp-main", 7, 100);
    thread::sleep(Duration::from_millis(1));
    metrics.record_forward_parts("udp-main", 7, 100);

    metrics.record_receive(USER_SERVICE_ID_START, 7, 1, 80, Some(1));
    thread::sleep(Duration::from_millis(1));
    metrics.record_receive(USER_SERVICE_ID_START, 7, 2, 80, Some(1));

    let snapshot = metrics.snapshot();
    let path = snapshot.paths.get(&7).expect("path timing counters");
    let service = snapshot.services.get("udp-main").expect("service timing counters");

    assert!(path.last_tx_at_us > 0);
    assert!(path.last_tx_gap_us > 0);
    assert!(path.last_rx_at_us > 0);
    assert!(path.last_rx_gap_us > 0);
    assert!(service.last_tx_at_us > 0);
    assert!(service.last_tx_gap_us > 0);
    assert_eq!(service.last_rx_at_us, 0);
    assert_eq!(service.last_rx_gap_us, 0);
}

#[test]
fn metrics_drain_sparse_real_data_timing_samples() {
    let mut metrics = DataplaneMetrics::default();

    thread::sleep(Duration::from_millis(1));
    metrics.record_transmit_timing_sample(7, 2048, 3, Some(42));
    metrics.record_receive(USER_SERVICE_ID_START, 7, 2048, 80, Some(42));

    let (tx_samples, rx_samples) = metrics.drain_data_timing_samples();

    assert_eq!(tx_samples.len(), 1);
    assert_eq!(tx_samples[0].path_id, 7);
    assert_eq!(tx_samples[0].sequence, 2048);
    assert_eq!(tx_samples[0].packet_count, 1);
    assert_eq!(tx_samples[0].peer_scope, Some(42));
    assert!(tx_samples[0].observed_at_us > 0);
    assert_eq!(rx_samples.len(), 1);
    assert_eq!(rx_samples[0].sequence, 2048);
    assert!(rx_samples[0].observed_at_us > 0);

    let (tx_after_drain, rx_after_drain) = metrics.drain_data_timing_samples();
    assert!(tx_after_drain.is_empty());
    assert!(rx_after_drain.is_empty());
}

#[test]
fn tx_timing_sampler_matches_sequences_inside_coalesced_batches() {
    let mut metrics = DataplaneMetrics::default();

    thread::sleep(Duration::from_millis(1));
    metrics.record_transmit_timing_sample(7, 1020, 8, None);
    metrics.record_receive(USER_SERVICE_ID_START, 7, 1024, 80, None);

    let (tx_samples, rx_samples) = metrics.drain_data_timing_samples();

    assert_eq!(tx_samples.len(), 1);
    assert_eq!(tx_samples[0].sequence, 1024);
    assert_eq!(tx_samples[0].packet_count, 1);
    assert_eq!(rx_samples.len(), 1);
    assert_eq!(rx_samples[0].sequence, 1024);
}

#[test]
fn service_path_counters_expose_executed_service_path_intersections() {
    let mut metrics = DataplaneMetrics::default();

    metrics.record_forward_batch("wireguard-fast", 2, 3, 3600);
    metrics.record_forward_batch("wireguard-fast", 3, 2, 2400);
    metrics.record_forward_batch("wireguard-stable", 1, 4, 4800);
    metrics.record_receive_for_service(
        "wireguard-fast",
        USER_SERVICE_ID_START,
        3,
        77,
        1200,
        SequenceObservation {
            missing_packets: 0,
            out_of_order: false,
            duplicate_or_late: false,
        },
    );

    let snapshot = metrics.snapshot();
    let fast_path_two = snapshot
        .service_paths
        .get("wireguard-fast")
        .and_then(|paths| paths.get(&2))
        .expect("fast service path two counters");
    let fast_path_three = snapshot
        .service_paths
        .get("wireguard-fast")
        .and_then(|paths| paths.get(&3))
        .expect("fast service path three counters");
    let stable_path_one = snapshot
        .service_paths
        .get("wireguard-stable")
        .and_then(|paths| paths.get(&1))
        .expect("stable service path one counters");

    assert_eq!(fast_path_two.tx_packets, 3);
    assert_eq!(fast_path_two.tx_bytes, 3600);
    assert_eq!(fast_path_three.tx_packets, 2);
    assert_eq!(fast_path_three.rx_packets, 1);
    assert_eq!(fast_path_three.rx_bytes, 1200);
    assert_eq!(stable_path_one.tx_packets, 4);
    assert!(snapshot.service_paths["wireguard-fast"].get(&1).is_none());
}
