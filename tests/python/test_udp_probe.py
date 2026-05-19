from __future__ import annotations

import importlib.util
import socket
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_udp_probe():
    spec = importlib.util.spec_from_file_location("udp_probe", REPO_ROOT / "tools/udp_probe.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_udp_probe_receive_allows_timeout_after_minimum_count(monkeypatch, capsys) -> None:
    """Support degraded-path acceptance checks where UDP loss is the expected signal."""
    udp_probe = _load_udp_probe()

    class FakeSocket:
        def __init__(self) -> None:
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def bind(self, bind) -> None:
            self.bind_address = bind

        def recvfrom(self, size: int):
            self.calls += 1
            if self.calls == 1:
                return b"hello", ("127.0.0.1", 5555)
            raise TimeoutError

    monkeypatch.setattr(udp_probe.socket, "socket", lambda *_args: FakeSocket())

    udp_probe._receive(("127.0.0.1", 9999), timeout=0.01, count=3, min_count=1)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "127.0.0.1:5555" in output


def test_udp_probe_send_supports_duration_and_payload_size(monkeypatch, capsys) -> None:
    """Duration sends make VM acceptance useful beyond one tiny packet."""
    udp_probe = _load_udp_probe()
    sent_payloads: list[bytes] = []
    ticks = iter([0.0, 0.0, 0.01, 0.02, 0.03])

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def sendto(self, data: bytes, target) -> int:
            sent_payloads.append(data)
            return len(data)

    monkeypatch.setattr(udp_probe.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(udp_probe.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(udp_probe.time, "sleep", lambda _seconds: None)

    sent = udp_probe._send(
        ("127.0.0.1", 9999),
        "hello",
        0,
        duration_seconds=0.02,
        interval_seconds=0.001,
        payload_size=16,
    )

    assert sent == 2
    assert len(sent_payloads) == 2
    assert all(len(payload) == 16 for payload in sent_payloads)
    assert "sent 16 bytes" in capsys.readouterr().out
