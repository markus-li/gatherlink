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

#[test]
fn accepts_large_multipath_reorder_inside_window() {
    let mut window = ReplayWindow::new(1_048_576);
    assert!(window.accept(1));
    assert!(window.accept(700_000));
    assert!(window.accept(350_000));
    assert!(!window.accept(350_000));
    assert!(window.accept(2));
}

#[test]
fn rejects_large_reorder_outside_window() {
    let mut window = ReplayWindow::new(1_024);
    assert!(window.accept(1));
    assert!(window.accept(2_000));
    assert!(!window.accept(900));
    assert!(window.accept(1_100));
}
