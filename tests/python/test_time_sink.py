from gatherlink.control.metadata import (
    empty_control_metadata,
    estimated_sink_one_way_us,
    record_sink_time,
    refresh_gatherlink_time,
    sink_time_messages,
)
from gatherlink.time.offset import SinkTimeMessage, internal_monotonic_us
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
