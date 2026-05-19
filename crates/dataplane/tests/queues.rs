use gatherlink_dataplane::queues::{BoundedPathQueue, PathQueueLimits, QueuedPacket};

#[test]
fn bounded_path_queue_reports_depth_and_oldest_age() {
    let mut queue = BoundedPathQueue::new(7, PathQueueLimits::new(4, 1024));

    queue.push(QueuedPacket::new(vec![1; 100], 1_000)).unwrap();
    queue.push(QueuedPacket::new(vec![2; 200], 2_000)).unwrap();

    let snapshot = queue.snapshot(3_500);
    assert_eq!(snapshot.path_id, 7);
    assert_eq!(snapshot.depth_packets, 2);
    assert_eq!(snapshot.depth_bytes, 300);
    assert_eq!(snapshot.oldest_age_us, 2_500);
    assert_eq!(snapshot.dropped_packets, 0);
}

#[test]
fn bounded_path_queue_drops_when_packet_limit_would_overflow() {
    let mut queue = BoundedPathQueue::new(7, PathQueueLimits::new(1, 0));

    queue.push(QueuedPacket::new(vec![1; 100], 1_000)).unwrap();
    let dropped = queue.push(QueuedPacket::new(vec![2; 100], 2_000)).unwrap_err();

    let snapshot = queue.snapshot(3_000);
    assert_eq!(dropped.path_id, 7);
    assert_eq!(dropped.payload_len, 100);
    assert_eq!(snapshot.depth_packets, 1);
    assert_eq!(snapshot.dropped_packets, 1);
    assert_eq!(snapshot.dropped_bytes, 100);
}

#[test]
fn bounded_path_queue_drops_when_byte_limit_would_overflow() {
    let mut queue = BoundedPathQueue::new(7, PathQueueLimits::new(0, 150));

    queue.push(QueuedPacket::new(vec![1; 100], 1_000)).unwrap();
    queue.push(QueuedPacket::new(vec![2; 100], 2_000)).unwrap_err();
    let packet = queue.pop().unwrap();

    assert_eq!(packet.payload(), &[1; 100]);
    assert_eq!(queue.snapshot(3_000).depth_packets, 0);
}
