from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_probe_module():
    path = Path(__file__).parents[2] / "tools" / "hyperv" / "live_tcp_outcome_probe.py"
    spec = importlib.util.spec_from_file_location("live_tcp_outcome_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retransmits_from_line_reads_current_and_total_counter() -> None:
    module = _load_probe_module()

    assert module._retransmits_from_line(" cubic wscale:7,7 retrans:3/12 rtt:10.2/1.1") == 12
    assert module._retransmits_from_line(" cubic wscale:7,7 rtt:10.2/1.1") == 0


def test_tcp_retransmits_for_target_parses_socket_context_window(monkeypatch) -> None:
    module = _load_probe_module()

    class _Completed:
        stdout = "\n".join(
            [
                "ESTAB 0 0 10.205.0.2:39114 10.205.0.1:7811",
                "\t cubic wscale:7,7 rto:204 retrans:2/13 rtt:88.1/2.1",
                "ESTAB 0 0 10.205.0.2:39116 10.205.0.1:7811",
                "\t cubic wscale:7,7 rto:204 retrans:1/4 rtt:86.1/2.1",
            ]
        )

    def _run(*args, **kwargs):
        return _Completed()

    monkeypatch.setattr(module.subprocess, "run", _run)

    assert module.tcp_retransmits_for_target("10.205.0.1") == 13


def test_tcp_global_retransmits_reads_snmp_counter(tmp_path, monkeypatch) -> None:
    module = _load_probe_module()
    snmp = tmp_path / "snmp"
    snmp.write_text(
        "\n".join(
            [
                "Tcp: RtoAlgorithm RtoMin RtoMax RetransSegs InErrs",
                "Tcp: 1 200 120000 42 0",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "TCP_SNMP", snmp)

    assert module.tcp_global_retransmits() == 42


def test_outcome_payload_is_empty_until_retransmit_threshold_is_crossed() -> None:
    module = _load_probe_module()

    assert module.outcome_payload(service="wireguard-stable", retransmits=3, max_retransmits=10) == {"outcomes": []}

    payload = module.outcome_payload(service="wireguard-stable", retransmits=11, max_retransmits=10)
    assert payload["outcomes"] == [
        {
            "service": "wireguard-stable",
            "degraded": True,
            "reason": "live tcp retransmits 11 above 10",
        }
    ]
