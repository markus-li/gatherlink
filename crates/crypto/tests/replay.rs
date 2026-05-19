use gatherlink_crypto::replay::ReplayWindow;

#[test]
fn accepts_new_counters_and_rejects_replays() {
    let mut window = ReplayWindow::new(8);
    assert!(window.accept(10));
    assert!(!window.accept(10));
    assert!(window.accept(11));
    assert!(window.accept(9));
    assert!(!window.accept(9));
}

#[test]
fn rejects_packets_older_than_window() {
    let mut window = ReplayWindow::new(4);
    assert!(window.accept(10));
    assert!(window.accept(14));
    assert!(!window.accept(10));
    assert!(window.accept(11));
}
