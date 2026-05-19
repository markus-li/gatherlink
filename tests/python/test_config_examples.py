from __future__ import annotations

from pathlib import Path

from gatherlink.config.validation import validate_config_file

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_windows_two_node_configs_use_authenticated_session_source_mode() -> None:
    """Keep the managed two-node examples aligned with the authenticated v1 session shape."""
    node_a = validate_config_file(REPO_ROOT / "configs/examples/windows-two-node-a.json")
    node_b = validate_config_file(REPO_ROOT / "configs/examples/windows-two-node-b.json")

    assert node_a.security.mode == "authenticated"
    assert node_b.security.mode == "authenticated"
    assert node_a.security.receiver_index == node_b.security.receiver_index
    assert node_a.security.send_key == node_b.security.receive_key
    assert node_a.security.receive_key == node_b.security.send_key


def test_windows_two_node_configs_and_acceptance_script_cover_three_paths() -> None:
    """Keep the WSL acceptance gate aligned with the documented three-path MVP smoke."""
    node_a = validate_config_file(REPO_ROOT / "configs/examples/windows-two-node-a.json")
    node_b = validate_config_file(REPO_ROOT / "configs/examples/windows-two-node-b.json")
    script = (REPO_ROOT / "tools/run_wsl_mvp_acceptance.ps1").read_text(encoding="utf-8")

    assert [path.name for path in node_a.paths] == ["wsl-path-a", "wsl-path-b", "wsl-path-c"]
    assert [path.name for path in node_b.paths] == ["wsl-path-a", "wsl-path-b", "wsl-path-c"]
    for path_name in ["wsl-path-a", "wsl-path-b", "wsl-path-c"]:
        assert path_name in script
    assert "diagnostics.jsonl" in script
    assert "services close" in script


def test_shared_sink_example_documents_multi_session_peer_scoped_return() -> None:
    config = validate_config_file(REPO_ROOT / "configs/examples/shared-sink-server.json")

    assert config.security.sessions
    assert len({session.local_receiver_index for session in config.security.sessions}) == len(config.security.sessions)
    assert all(session.services == ["udp-main"] for session in config.security.sessions)
    assert config.services[0].return_mode == "peer-scoped-source"


def test_windows_shared_sink_examples_cover_two_sources_one_sink_port() -> None:
    source_a = validate_config_file(REPO_ROOT / "configs/examples/windows-shared-sink-source-a.json")
    source_b = validate_config_file(REPO_ROOT / "configs/examples/windows-shared-sink-source-b.json")
    sink = validate_config_file(REPO_ROOT / "configs/examples/windows-shared-sink-server.json")
    setup_script = (REPO_ROOT / "tools/setup_wsl_private_lan.ps1").read_text(encoding="utf-8")

    assert source_a.services[0].listen == "10.88.0.11:55180"
    assert source_b.services[0].listen == "10.88.0.13:55180"
    assert source_a.services[0].return_mode == "learned-single-source"
    assert source_b.services[0].return_mode == "learned-single-source"
    assert sink.services[0].return_mode == "peer-scoped-source"
    assert [path.transport_bind for path in sink.paths] == [
        "10.88.1.12:57001",
        "10.88.2.12:57002",
        "10.88.3.12:57003",
    ]
    assert [path.transport_remote for path in sink.paths] == [None, None, None]
    assert source_a.paths[0].transport_remote == sink.paths[0].transport_bind
    assert source_b.paths[0].transport_remote == sink.paths[0].transport_bind
    assert {session.local_receiver_index for session in sink.security.sessions} == {201, 202}
    assert all(session.services == ["udp-main"] for session in sink.security.sessions)
    for address in ["10.88.0.13", "10.88.1.13", "10.88.2.13", "10.88.3.13"]:
        assert address in setup_script
