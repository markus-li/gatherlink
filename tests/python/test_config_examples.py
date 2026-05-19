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
