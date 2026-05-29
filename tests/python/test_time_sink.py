from gatherlink.control.metadata import (
    empty_control_metadata,
    estimated_sink_one_way_us,
    record_sink_time,
    refresh_gatherlink_time,
    sink_time_messages,
)
from gatherlink.time.offset import (
    InternalClockSyncClient,
    InternalClockSyncMessage,
    SinkTimeMessage,
    internal_monotonic_us,
)
from gatherlink.time.sources.direct_ntp import DirectNtpSample


def test_sink_time_uses_direct_ntp_sample_as_source_of_truth(monkeypatch) -> None:
    import gatherlink.control.metadata as metadata_module

    sample = DirectNtpSample(
        server="ntp.example",
        unix_us=1_776_000_000_000_000,
        received_monotonic_us=internal_monotonic_us(),
        offset_us=125,
        rtt_us=4_000,
        stratum=2,
    )
    monkeypatch.setattr(metadata_module, "read_system_ntp_status", lambda: "unsynchronized")

    messages = sink_time_messages(["path-a"], {"path-a": 7}, sample)
    metadata = empty_control_metadata()
    record_sink_time(
        metadata,
        list(messages.values()),
        {7: "path-a"},
        received_at_internal_us=internal_monotonic_us(),
        local_sink=True,
        ntp_sample=sample,
    )

    message = messages["path-a"]
    sink_time = metadata["sink_time"]
    assert message.ntp_state == 1
    assert abs(message.sink_unix_us - sample.current_unix_us()) < 100_000
    assert sink_time["ntp_state"] == "synchronized"
    assert sink_time["ntp_source"] == "ntp.example"
    assert sink_time["ntp_source_type"] == "ntp"
    assert sink_time["ntp_offset_us"] == 125
    assert sink_time["ntp_rtt_us"] == 4_000


def test_sink_ntp_sample_tries_configured_servers(monkeypatch) -> None:
    from gatherlink.time import sink

    calls = []
    sample = DirectNtpSample(
        server="second.example",
        unix_us=1_776_000_000_000_000,
        received_monotonic_us=internal_monotonic_us(),
        offset_us=0,
        rtt_us=1,
        stratum=1,
    )

    def fake_query(server: str) -> DirectNtpSample | None:
        calls.append(server)
        return sample if server == "second.example" else None

    monkeypatch.setattr(sink, "query_direct_ntp", fake_query)

    assert sink.read_sink_ntp_sample(("first.example", "second.example"), ()) == sample
    assert calls == ["first.example", "second.example"]


def test_sink_time_falls_back_to_https_date_when_direct_ntp_fails(monkeypatch) -> None:
    from gatherlink.time import sink

    sample = DirectNtpSample(
        server="https://time.example",
        unix_us=1_776_000_000_000_000,
        received_monotonic_us=internal_monotonic_us(),
        offset_us=20_000,
        rtt_us=50_000,
        stratum=None,
        source="https-date",
    )

    monkeypatch.setattr(sink, "query_direct_ntp", lambda server: None)
    monkeypatch.setattr(sink, "query_https_date_time", lambda url: sample if url == "https://time.example" else None)

    assert sink.read_sink_ntp_sample(("ntp.example",), ("https://time.example",)) == sample

    messages = sink_time_messages(["path-a"], {"path-a": 1}, sample)
    metadata = empty_control_metadata()
    record_sink_time(
        metadata,
        list(messages.values()),
        {1: "path-a"},
        received_at_internal_us=internal_monotonic_us(),
        local_sink=True,
        ntp_sample=sample,
    )

    sink_time = metadata["sink_time"]
    assert next(iter(messages.values())).ntp_state == 0
    assert sink_time["ntp_state"] == "unknown"
    assert sink_time["ntp_source"] == "https://time.example"
    assert sink_time["ntp_source_type"] == "https-date"


def test_sink_time_can_bootstrap_from_supervisor_environment(monkeypatch) -> None:
    from gatherlink.time import sink

    sample = DirectNtpSample(
        server="time.cloudflare.com",
        unix_us=1_776_000_000_000_000,
        received_monotonic_us=internal_monotonic_us(),
        offset_us=50,
        rtt_us=1_000,
        stratum=3,
        source="ntp",
    )
    monkeypatch.setenv(sink.SINK_TIME_BOOTSTRAP_ENV, sink.encode_sink_time_sample(sample))
    monkeypatch.setattr(sink, "query_direct_ntp", lambda server: None)
    monkeypatch.setattr(sink, "query_https_date_time", lambda url: None)

    restored = sink.read_sink_ntp_sample(("ntp.example",), ("https://time.example",))

    assert restored is not None
    assert restored.server == "time.cloudflare.com"
    assert restored.source == "ntp"
    assert restored.stratum == 3


def test_gatherlink_time_uses_half_average_rtt_until_directional_latency_is_confident() -> None:
    metadata = empty_control_metadata()
    metadata["internal_clock"].update({"rtt_us": 12_000, "mean_rtt_us": 8_000})
    assert estimated_sink_one_way_us(metadata) == 4_000

    record_sink_time(
        metadata,
        [
            SinkTimeMessage(
                path_id=1,
                sink_unix_us=1_776_000_000_000_000,
                sink_internal_us=10,
                ntp_state=1,
            )
        ],
        {1: "path-a"},
        received_at_internal_us=internal_monotonic_us(),
    )
    before = metadata["sink_time"]["gatherlink_unix_us"]
    refresh_gatherlink_time(metadata)

    assert before >= 1_776_000_000_004_000
    assert metadata["sink_time"]["gatherlink_unix_us"] >= before


def test_internal_clock_sync_rejects_impossible_exchange(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a"])
    client._pending[1] = ("path-a", 1_000)
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: 1_500)

    update = client.observe_control_frame(
        [
            InternalClockSyncMessage(
                exchange_id=1,
                path_id=1,
                mode=2,
                origin_us=1_000,
                receive_us=10_000,
                transmit_us=11_000,
            )
        ],
        path_names_by_id={1: "path-a"},
    )

    assert update["path_latency_rejections"] == [
        {
            "path": "path-a",
            "reason": "impossible-clock-exchange",
            "rtt_us": -500,
            "clock_error_us": None,
        }
    ]


def test_internal_clock_sync_uses_robust_sample_summary(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a"])
    destination_times = iter([1_011_000, 1_021_000, 1_031_000, 1_041_000])
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: next(destination_times))

    update = {}
    for exchange_id, origin_us in enumerate([1_000_000, 1_010_000, 1_020_000], start=1):
        client._pending[exchange_id] = ("path-a", origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=1,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=origin_us + 105_000,
                    transmit_us=origin_us + 115_000,
                )
            ],
            path_names_by_id={1: "path-a"},
        )

    assert update["confidence"] == "good"
    assert update["mean_offset_us"] == 104_500
    assert update["best_rtt_us"] == 1_000
    assert update["error_budget_us"] >= 1_000
    assert update["path_confidence"] == "good"

    client._pending[4] = ("path-a", 1_030_000)
    outlier_update = client.observe_control_frame(
        [
            InternalClockSyncMessage(
                exchange_id=4,
                path_id=1,
                mode=2,
                origin_us=1_030_000,
                receive_us=2_000_000,
                transmit_us=2_010_000,
            )
        ],
        path_names_by_id={1: "path-a"},
    )

    assert outlier_update["path_latency_rejections"] == [
        {
            "path": "path-a",
            "reason": "offset-outlier",
            "rtt_us": 1_000,
            "clock_error_us": None,
        }
    ]


def test_internal_clock_sync_reports_directional_latency_from_selected_offset(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a"])
    destination_times = iter([1_020_000, 1_030_000, 1_040_000, 1_051_000])
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: next(destination_times))

    update = {}
    for exchange_id, origin_us in enumerate([1_000_000, 1_010_000, 1_020_000], start=1):
        client._pending[exchange_id] = ("path-a", origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=1,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=origin_us + 105_000,
                    transmit_us=origin_us + 115_000,
                )
            ],
            path_names_by_id={1: "path-a"},
        )

    assert update["mean_offset_us"] == 100_000

    client._pending[4] = ("path-a", 1_030_000)
    update = client.observe_control_frame(
        [
            InternalClockSyncMessage(
                exchange_id=4,
                path_id=1,
                mode=2,
                origin_us=1_030_000,
                receive_us=1_135_000,
                transmit_us=1_145_000,
            )
        ],
        path_names_by_id={1: "path-a"},
    )

    observation = update["path_latency_observations"][-1]
    assert observation["source"] == "clock-synced-one-way"
    assert observation["offset_us"] == 100_000
    assert observation["rtt_us"] == 11_000
    assert observation["tx_one_way_us"] == 5_000
    assert observation["rx_one_way_us"] == 6_000


def test_internal_clock_sync_uses_path_offset_for_path_latency(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a", "path-b"])
    destination_times = iter(
        [
            1_020_000,
            1_030_000,
            1_040_000,
            1_050_000,
            1_060_000,
            1_070_000,
            1_081_000,
        ]
    )
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: next(destination_times))

    update = {}
    # Establish path-a around 100 ms offset.
    for exchange_id, origin_us in enumerate([1_000_000, 1_010_000, 1_020_000], start=1):
        client._pending[exchange_id] = ("path-a", origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=1,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=origin_us + 105_000,
                    transmit_us=origin_us + 115_000,
                )
            ],
            path_names_by_id={1: "path-a", 2: "path-b"},
        )

    # Add path-b around 200 ms offset. Its directional latency must use the
    # path-b offset, not the global median that is still biased by path-a.
    for exchange_id, origin_us in enumerate([1_030_000, 1_040_000, 1_050_000], start=4):
        client._pending[exchange_id] = ("path-b", origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=2,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=origin_us + 205_000,
                    transmit_us=origin_us + 215_000,
                )
            ],
            path_names_by_id={1: "path-a", 2: "path-b"},
        )

    assert update["mean_offset_us"] == 100_000
    assert update["path_mean_offset_us"] == 200_000

    client._pending[7] = ("path-b", 1_060_000)
    update = client.observe_control_frame(
        [
            InternalClockSyncMessage(
                exchange_id=7,
                path_id=2,
                mode=2,
                origin_us=1_060_000,
                receive_us=1_265_000,
                transmit_us=1_275_000,
            )
        ],
        path_names_by_id={1: "path-a", 2: "path-b"},
    )

    observation = update["path_latency_observations"][-1]
    assert observation["path"] == "path-b"
    assert observation["offset_us"] == 200_000
    assert observation["tx_one_way_us"] == 5_000
    assert observation["rx_one_way_us"] == 6_000


def test_internal_clock_sync_prefers_low_delay_multipath_consensus(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a", "path-b", "path-c"])
    destination_times = iter(
        [
            1_011_000,
            1_021_000,
            1_031_000,
            1_081_000,
            1_051_000,
            1_061_000,
            1_071_000,
        ]
    )
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: next(destination_times))

    update = {}
    samples = [
        (1, "path-a", 1, 1_000_000, 1_005_000, 1_015_000),
        (2, "path-b", 2, 1_010_000, 1_016_000, 1_026_000),
        (3, "path-c", 3, 1_020_000, 1_027_000, 1_037_000),
        # Path-a sees a later high-RTT queued sample with a distorted offset.
        # The consensus should keep using the minimum-delay path cluster.
        (4, "path-a", 1, 1_030_000, 1_070_000, 1_080_000),
        (5, "path-a", 1, 1_040_000, 1_045_000, 1_055_000),
        (6, "path-b", 2, 1_050_000, 1_056_000, 1_066_000),
        (7, "path-c", 3, 1_060_000, 1_067_000, 1_077_000),
    ]
    for exchange_id, path_name, path_id, origin_us, receive_us, transmit_us in samples:
        client._pending[exchange_id] = (path_name, origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=path_id,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=receive_us,
                    transmit_us=transmit_us,
                )
            ],
            path_names_by_id={1: "path-a", 2: "path-b", 3: "path-c"},
        )

    assert update["confidence"] == "good"
    assert 5_000 <= update["mean_offset_us"] <= 7_000
    assert update["base_rtt_us"] == 1_000
    assert update["uncertainty_us"] >= 1_000
    assert "drift_ppb" in update
    assert update["path_summaries"]["path-a"]["base_rtt_us"] == 1_000


def test_internal_clock_sync_drift_is_path_local() -> None:
    import gatherlink.time.offset as offset_module

    samples = [
        offset_module._ClockSyncSample(observed_at=1.0, path_name="path-a", offset_us=5_000, rtt_us=1_000),
        offset_module._ClockSyncSample(observed_at=2.0, path_name="path-a", offset_us=5_000, rtt_us=1_000),
        offset_module._ClockSyncSample(observed_at=3.0, path_name="path-b", offset_us=50_000, rtt_us=1_000),
        offset_module._ClockSyncSample(observed_at=4.0, path_name="path-b", offset_us=50_000, rtt_us=1_000),
    ]

    summary = offset_module._sample_summary(samples)

    assert summary["drift_ppb"] == 0


def test_internal_clock_sync_remains_usable_with_one_path(monkeypatch) -> None:
    import gatherlink.time.offset as offset_module

    client = InternalClockSyncClient(["path-a"])
    destination_times = iter([1_011_000, 1_021_000, 1_031_000])
    monkeypatch.setattr(offset_module, "internal_monotonic_us", lambda: next(destination_times))

    update = {}
    for exchange_id, origin_us in enumerate([1_000_000, 1_010_000, 1_020_000], start=1):
        client._pending[exchange_id] = ("path-a", origin_us)
        update = client.observe_control_frame(
            [
                InternalClockSyncMessage(
                    exchange_id=exchange_id,
                    path_id=1,
                    mode=2,
                    origin_us=origin_us,
                    receive_us=origin_us + 5_000,
                    transmit_us=origin_us + 15_000,
                )
            ],
            path_names_by_id={1: "path-a"},
    )

    assert update["confidence"] == "good"
    assert update["mean_offset_us"] == 4_500
    assert update["base_rtt_us"] == 1_000
    assert update["uncertainty_us"] == 1_000
    assert update["path_summaries"]["path-a"]["confidence"] == "good"
