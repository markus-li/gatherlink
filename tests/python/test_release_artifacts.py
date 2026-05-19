from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from gatherlink.release.artifacts import (
    _build_python_wheel,
    _build_rust_binaries,
    _reject_sensitive_paths,
    _reject_version_mismatched_wheels,
    _write_checksums,
    _write_source_archive,
    plan_release_artifacts,
)


def test_release_artifact_plan_names_source_archive_and_wiki_payload(tmp_path: Path) -> None:
    plan = plan_release_artifacts("v0.9.1", tmp_path / "dist")

    assert plan.version == "0.9.1"
    assert plan.archive_name == "gatherlink-0.9.1-source.tar.gz"
    assert plan.checksum_name == "SHA256SUMS"
    assert plan.wiki_payload_dir == tmp_path / "dist" / "wiki-user-docs"
    assert plan.wheel_dir == tmp_path / "dist" / "python-wheel"
    assert plan.rust_binary_dir == tmp_path / "dist" / "rust-binaries"


def test_release_artifacts_refuse_secret_or_host_local_paths() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        _reject_sensitive_paths([Path(".gatherlink/state/local.identity.json")])
    with pytest.raises(ValueError, match="sensitive"):
        _reject_sensitive_paths([Path("tools/hyperv/inventory.env")])
    _reject_sensitive_paths([Path("tools/hyperv/inventory.example.env")])


def test_release_archive_and_checksum_are_reproducible_shapes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    archive = tmp_path / "gatherlink-0.9.1-source.tar.gz"
    checksum = tmp_path / "SHA256SUMS"

    _write_source_archive(repo, [Path("README.md")], archive, root_name="gatherlink-0.9.1")
    _write_checksums(checksum, [archive])

    with tarfile.open(archive, "r:gz") as package:
        assert package.getnames() == ["gatherlink-0.9.1/README.md"]
    assert "gatherlink-0.9.1-source.tar.gz" in checksum.read_text(encoding="utf-8")


def test_release_wheel_builder_uses_local_no_dependency_pip_wheel(monkeypatch, tmp_path: Path) -> None:
    calls = []
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        wheel_dir = Path(command[command.index("--wheel-dir") + 1])
        (wheel_dir / "gatherlink-0.9.1-py3-none-any.whl").write_text("wheel\n", encoding="utf-8")

    monkeypatch.setattr("gatherlink.release.artifacts.subprocess.run", fake_run)

    wheels = _build_python_wheel(repo, tmp_path / "dist" / "python-wheel")

    assert wheels == [tmp_path / "dist" / "python-wheel" / "gatherlink-0.9.1-py3-none-any.whl"]
    assert calls[0][0][2:5] == ["pip", "wheel", "--no-deps"]
    assert calls[0][1] == repo
    assert calls[0][2] is True


def test_release_wheel_builder_removes_transient_build_directory(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(command, *, cwd, check):
        (cwd / "build" / "lib").mkdir(parents=True)
        wheel_dir = Path(command[command.index("--wheel-dir") + 1])
        (wheel_dir / "gatherlink-0.9.1-py3-none-any.whl").write_text("wheel\n", encoding="utf-8")

    monkeypatch.setattr("gatherlink.release.artifacts.subprocess.run", fake_run)

    _build_python_wheel(repo, tmp_path / "dist" / "python-wheel")

    assert not (repo / "build").exists()


def test_release_wheel_builder_preserves_preexisting_build_directory(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    build_dir = repo / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "keep.txt").write_text("operator data\n", encoding="utf-8")

    def fake_run(command, *, cwd, check):
        wheel_dir = Path(command[command.index("--wheel-dir") + 1])
        (wheel_dir / "gatherlink-0.9.1-py3-none-any.whl").write_text("wheel\n", encoding="utf-8")

    monkeypatch.setattr("gatherlink.release.artifacts.subprocess.run", fake_run)

    _build_python_wheel(repo, tmp_path / "dist" / "python-wheel")

    assert (build_dir / "keep.txt").read_text(encoding="utf-8") == "operator data\n"


def test_release_rust_binary_builder_copies_expected_debian_operator_binary(monkeypatch, tmp_path: Path) -> None:
    calls = []
    repo = tmp_path / "repo"
    binary = repo / "target" / "release" / "gatherlink-time-helper"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary\n", encoding="utf-8")

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))

    monkeypatch.setattr("gatherlink.release.artifacts.subprocess.run", fake_run)

    binaries = _build_rust_binaries(repo, tmp_path / "dist" / "rust-binaries")

    assert binaries == [tmp_path / "dist" / "rust-binaries" / "gatherlink-time-helper"]
    assert binaries[0].read_text(encoding="utf-8") == "binary\n"
    assert calls == [
        (
            [
                "cargo",
                "build",
                "--release",
                "--package",
                "gatherlink-time-helper",
                "--bin",
                "gatherlink-time-helper",
            ],
            repo,
            True,
        )
    ]


def test_release_artifacts_reject_version_mismatched_wheels(tmp_path: Path) -> None:
    matching = tmp_path / "gatherlink-0.9.1-py3-none-any.whl"
    mismatched = tmp_path / "gatherlink-0.9.0-py3-none-any.whl"

    _reject_version_mismatched_wheels([matching], version="0.9.1")
    with pytest.raises(ValueError, match="does not match"):
        _reject_version_mismatched_wheels([mismatched], version="0.9.1")
