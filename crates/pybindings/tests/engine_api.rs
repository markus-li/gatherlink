use std::net::UdpSocket;
use std::time::Duration;

use gatherlink_pybindings::dto::{PyPathConfig, PySchedulerConfig, PyUdpServiceConfig};
use gatherlink_pybindings::engine_api::PyCoreDataplane;

#[test]
fn python_facing_dataplane_forwards_one_udp_payload() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"python-bridge-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"python-bridge-core");
    assert_eq!(outcome.service(), "udp-main");
    assert_eq!(outcome.payload_len(), b"python-bridge-core".len());
    assert_eq!(outcome.sequence(), 1);
}

#[test]
fn python_facing_dataplane_forwards_ipv6_udp_payload() {
    let target = UdpSocket::bind("[::1]:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-v6".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("[::1]:0".to_owned()),
        100,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-v6").unwrap();
    let sender = UdpSocket::bind("[::1]:0").unwrap();

    sender.send_to(b"python-ipv6-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-v6").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"python-ipv6-core");
    assert_eq!(outcome.service(), "udp-v6");
    assert_eq!(outcome.target(), target.local_addr().unwrap().to_string());
}

#[test]
fn python_facing_dataplane_reapplies_target_update() {
    let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        first_target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
    )
    .unwrap();
    let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
    let listen = dataplane.service_local_addr("udp-main").unwrap();
    let updated = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        second_target.local_addr().unwrap().to_string(),
        Some(listen.clone()),
        100,
    )
    .unwrap();

    let outcome = dataplane.reapply_config(vec![updated]).unwrap();

    assert_eq!(dataplane.service_local_addr("udp-main").unwrap(), listen);
    assert_eq!(outcome.unchanged(), 0);
    assert_eq!(outcome.updated(), 1);
    assert_eq!(outcome.rebound(), 0);
    assert_eq!(outcome.added(), 0);
    assert_eq!(outcome.removed(), 0);
}

#[test]
fn python_facing_dataplane_accepts_explicit_paths() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        200,
    )
    .unwrap();
    assert_eq!(service.priority(), 200);
    let path = PyPathConfig::new(3, 1200, 0, false, true, "active", 1).unwrap();
    let mut dataplane = PyCoreDataplane::bind_with_paths(vec![service], vec![path]).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"python-path-core", service_addr).unwrap();
    let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

    let mut buffer = [0_u8; 64];
    let (length, _source) = target.recv_from(&mut buffer).unwrap();
    assert_eq!(&buffer[..length], b"python-path-core");
    assert_eq!(outcome.path_id(), 3);
    assert_eq!(outcome.frame_count(), 1);
}

#[test]
fn python_facing_dataplane_accepts_scheduler_config() {
    let target = UdpSocket::bind("127.0.0.1:0").unwrap();
    target.set_read_timeout(Some(Duration::from_millis(500))).unwrap();
    let service = PyUdpServiceConfig::new(
        "udp-main".to_owned(),
        target.local_addr().unwrap().to_string(),
        Some("127.0.0.1:0".to_owned()),
        100,
    )
    .unwrap();
    let paths = vec![
        PyPathConfig::new(5, 1200, 0, false, true, "active", 1).unwrap(),
        PyPathConfig::new(6, 1200, 0, false, true, "active", 1).unwrap(),
    ];
    let scheduler = PySchedulerConfig::new("round_robin").unwrap();
    let mut dataplane = PyCoreDataplane::bind_with_scheduler(vec![service], paths, scheduler).unwrap();
    let service_addr = dataplane.service_local_addr("udp-main").unwrap();
    let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

    sender.send_to(b"one", &service_addr).unwrap();
    sender.send_to(b"two", &service_addr).unwrap();
    let outcomes = dataplane.forward_available_for_service("udp-main", 2).unwrap();

    let mut buffer = [0_u8; 8];
    target.recv_from(&mut buffer).unwrap();
    target.recv_from(&mut buffer).unwrap();
    assert_eq!(outcomes[0].path_id(), 5);
    assert_eq!(outcomes[1].path_id(), 6);
}
