from __future__ import annotations

import json
import socket
import threading

from gatherlink.cli.main import app
from gatherlink.time.helper_client import (
    TimeCorrectionRequest,
    parse_time_helper_response,
    request_time_correction,
)
from typer.testing import CliRunner


def test_time_correction_request_renders_helper_wire_format() -> None:
    request = TimeCorrectionRequest(
        target_unix_us=1_776_000_000_000_000,
        source="ntp",
        quality="synchronized",
        max_step_us=1000,
        apply=True,
    )

    assert request.render_wire() == (
        "target_unix_us=1776000000000000\n" "source=ntp\n" "quality=synchronized\n" "max_step_us=1000\n" "apply=true\n"
    )


def test_time_helper_response_parser_returns_structured_diagnostics() -> None:
    response = parse_time_helper_response(
        "status=preview\n"
        "applied=false\n"
        "offset_us=125\n"
        "target_unix_us=2000\n"
        "system_unix_us=1875\n"
        "warning=preview only\n"
    )

    assert response.status == "preview"
    assert response.applied is False
    assert response.offset_us == 125
    assert response.warning == "preview only"


def test_time_helper_client_talks_to_unix_socket(tmp_path) -> None:
    socket_path = tmp_path / "time-helper.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received = []

    def serve_once() -> None:
        connection, _address = listener.accept()
        with connection:
            received.append(connection.recv(4096).decode("utf-8"))
            connection.sendall(
                b"status=preview\n"
                b"applied=false\n"
                b"offset_us=10\n"
                b"target_unix_us=1010\n"
                b"system_unix_us=1000\n"
            )
        listener.close()

    thread = threading.Thread(target=serve_once)
    thread.start()

    response = request_time_correction(
        TimeCorrectionRequest(target_unix_us=1010, max_step_us=50),
        socket_path=socket_path,
    )

    thread.join(timeout=2)
    assert received[0].startswith("target_unix_us=1010\n")
    assert response.status == "preview"
    assert response.offset_us == 10


def test_time_cli_correct_defaults_to_preview(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_request(request, *, socket_path):
        captured["request"] = request
        captured["socket_path"] = socket_path
        return parse_time_helper_response(
            "status=preview\n" "applied=false\n" "offset_us=0\n" "target_unix_us=1000\n" "system_unix_us=1000\n"
        )

    monkeypatch.setattr("gatherlink.cli.time.request_time_correction", fake_request)

    result = CliRunner().invoke(
        app,
        ["time", "correct", "1000", "--socket", str(tmp_path / "helper.sock")],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "preview"
    assert captured["request"].apply is False
    assert captured["socket_path"] == tmp_path / "helper.sock"
