from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from gatherlink.release.artifacts import (
    _build_debian_package,
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
    assert plan.debian_build_dir == tmp_path / "dist" / "debian-package-root"
    assert plan.debian_package_name == "gatherlink_0.9.1_amd64.deb"


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


def test_debian_package_builder_stages_cli_docs_examples_and_binaries(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    (repo / "docs" / "user").mkdir(parents=True)
    (repo / "docs" / "user" / "quickstart.md").write_text("quickstart\n", encoding="utf-8")
    (repo / "configs" / "examples").mkdir(parents=True)
    (repo / "configs" / "examples" / "minimal.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    rust_binary = tmp_path / "gatherlink-time-helper"
    rust_binary.write_text("binary\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        if command[:4] == ["python", "-m", "pip", "install"]:
            target = Path(command[command.index("--target") + 1])
            (target / "gatherlink" / "cli").mkdir(parents=True)
            (target / "gatherlink" / "__init__.py").write_text("", encoding="utf-8")
            (target / "__pycache__").mkdir()
            (target / "__pycache__" / "x.pyc").write_bytes(b"cache")
        if command[:3] == ["dpkg-deb", "--root-owner-group", "--build"]:
            Path(command[3]).exists()
            Path(command[4]).write_text("deb\n", encoding="utf-8")

    monkeypatch.setattr("gatherlink.release.artifacts.sys.executable", "python")
    monkeypatch.setattr("gatherlink.release.artifacts.subprocess.run", fake_run)

    package = _build_debian_package(
        repo,
        tmp_path / "pkgroot",
        tmp_path / "gatherlink_0.9.1_amd64.deb",
        version="0.9.1",
        rust_binaries=[rust_binary],
    )

    assert package.read_text(encoding="utf-8") == "deb\n"
    assert (tmp_path / "pkgroot" / "DEBIAN" / "control").read_text(encoding="utf-8").startswith(
        "Package: gatherlink"
    )
    assert (tmp_path / "pkgroot" / "usr" / "bin" / "gatherlink").stat().st_mode & 0o111
    wrapper = (tmp_path / "pkgroot" / "usr" / "bin" / "gatherlink").read_text(encoding="utf-8")
    assert "/usr/lib/gatherlink/python" in wrapper
    assert (tmp_path / "pkgroot" / "usr" / "lib" / "gatherlink" / "python" / "gatherlink").exists()
    assert (tmp_path / "pkgroot" / "usr" / "lib" / "gatherlink" / "bin" / "gatherlink-time-helper").exists()
    assert (tmp_path / "pkgroot" / "usr" / "share" / "doc" / "gatherlink" / "user" / "quickstart.md").exists()
    assert not list((tmp_path / "pkgroot").rglob("__pycache__"))
    install_call = next(command for command, _, _ in calls if command[:4] == ["python", "-m", "pip", "install"])
    assert "--no-deps" not in install_call
    assert calls[-1][0][:3] == ["dpkg-deb", "--root-owner-group", "--build"]
