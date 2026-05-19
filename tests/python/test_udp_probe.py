from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("udp_probe", Path("tools/udp_probe.py"))
assert _SPEC is not None and _SPEC.loader is not None
_UDP_PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_UDP_PROBE)


def test_receive_can_count_without_printing_payloads(tmp_path: Path, capsys) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        bind = listener.getsockname()

    count_file = tmp_path / "count.txt"
    thread = threading.Thread(
        target=_UDP_PROBE._receive,
        args=(bind, 2.0, 2, 2),
        kwargs={"max_print_packets": 0, "count_file": count_file},
    )
    thread.start()
    time.sleep(0.1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"first", bind)
        sender.sendto(b"second", bind)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert count_file.read_text(encoding="utf-8") == "received_packets=2\n"
    assert capsys.readouterr().out == ""


def test_request_receives_expected_echo_reply(capsys) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        bind = listener.getsockname()

    thread = threading.Thread(
        target=_UDP_PROBE._echo,
        args=(bind, 2.0, 1),
    )
    thread.start()
    time.sleep(0.1)

    reply = _UDP_PROBE._request(bind, "hello-reply", 2.0)
    thread.join(timeout=2)

    assert reply == b"reply:hello-reply"
    assert not thread.is_alive()
    output = capsys.readouterr().out
    assert "hello-reply" in output
    assert "reply:hello-reply" in output
