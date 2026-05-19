from gatherlink.control import (
    BASELINE_CONTROL_CADENCE,
    MONITOR_CONTROL_CADENCE,
    MONITOR_CONTROL_REQUEST_REFRESH_SECONDS,
    MONITOR_CONTROL_REQUEST_TTL_SECONDS,
    ControlCadenceState,
    next_control_interval,
)


def test_baseline_control_cadence_slows_down_when_traffic_is_idle() -> None:
    assert next_control_interval(0, None) == BASELINE_CONTROL_CADENCE.active_interval_seconds
    assert next_control_interval(1, 0) == BASELINE_CONTROL_CADENCE.active_interval_seconds
    assert next_control_interval(1, 1) == BASELINE_CONTROL_CADENCE.idle_interval_seconds


def test_monitor_control_cadence_is_an_explicit_diagnostic_escalation() -> None:
    assert next_control_interval(9, 9, profile=MONITOR_CONTROL_CADENCE) == MONITOR_CONTROL_CADENCE.idle_interval_seconds
    assert MONITOR_CONTROL_CADENCE.idle_interval_seconds < BASELINE_CONTROL_CADENCE.idle_interval_seconds


def test_control_cadence_state_remembers_last_observed_traffic_total() -> None:
    state = ControlCadenceState()

    assert state.next_interval(0) == BASELINE_CONTROL_CADENCE.active_interval_seconds
    assert state.next_interval(0) == BASELINE_CONTROL_CADENCE.idle_interval_seconds
    assert state.next_interval(2) == BASELINE_CONTROL_CADENCE.active_interval_seconds


def test_monitor_control_request_expires_back_to_baseline(monkeypatch) -> None:
    import gatherlink.control.cadence as cadence

    now = 1000.0
    monkeypatch.setattr(cadence.time, "monotonic", lambda: now)
    state = ControlCadenceState()

    status = state.request_monitor_profile(ttl_seconds=MONITOR_CONTROL_REQUEST_TTL_SECONDS)

    assert status["profile"] == "monitor"
    assert state.effective_profile() == MONITOR_CONTROL_CADENCE

    now = 1121.0

    assert state.effective_profile() == BASELINE_CONTROL_CADENCE
    assert state.status()["profile"] == "baseline"


def test_monitor_control_refresh_happens_before_request_timeout() -> None:
    assert MONITOR_CONTROL_REQUEST_REFRESH_SECONDS < MONITOR_CONTROL_REQUEST_TTL_SECONDS
