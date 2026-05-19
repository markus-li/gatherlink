"""
Prepare reproducible local release artifacts for Debian-oriented Gatherlink users.

This module is release tooling, not runtime behavior. It packages tracked source
files, builds installable artifacts, writes checksums, and prepares a GitHub
Wiki payload from `docs/user/`. It deliberately refuses obviously host-local or
secret-looking paths.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

SENSITIVE_PATH_PARTS = {
    ".gatherlink",
    ".venv",
    "__pycache__",
}
SENSITIVE_SUFFIXES = (
    ".identity.json",
    ".sealed.json",
    ".pending.json",
    ".env",
)
SENSITIVE_NAMES = {
    "inventory.env",
    "known_hosts",
    "id_rsa",
    "id_ed25519",
}


@dataclass(frozen=True)
class ReleaseArtifactPlan:
    """Inspectable plan for a local release artifact build."""

    version: str
    output_dir: Path
    archive_name: str
    checksum_name: str
    wiki_payload_dir: Path
    wheel_dir: Path
    rust_binary_dir: Path


def plan_release_artifacts(version: str, output_dir: Path) -> ReleaseArtifactPlan:
    """Return the release artifact paths without writing files."""
    normalized = version.removeprefix("v")
    return ReleaseArtifactPlan(
        version=normalized,
        output_dir=output_dir,
        archive_name=f"gatherlink-{normalized}-source.tar.gz",
        checksum_name="SHA256SUMS",
        wiki_payload_dir=output_dir / "wiki-user-docs",
        wheel_dir=output_dir / "python-wheel",
        rust_binary_dir=output_dir / "rust-binaries",
    )


def build_release_artifacts(repo_root: Path, output_dir: Path, *, version: str) -> ReleaseArtifactPlan:
    """Build source archive, checksums, and a docs/user Wiki payload."""
    plan = plan_release_artifacts(version, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tracked_files = _tracked_files(repo_root)
    _reject_sensitive_paths(tracked_files)
    archive_path = output_dir / plan.archive_name
    _write_source_archive(repo_root, tracked_files, archive_path, root_name=f"gatherlink-{plan.version}")
    wheels = _build_python_wheel(repo_root, plan.wheel_dir)
    _reject_version_mismatched_wheels(wheels, version=plan.version)
    rust_binaries = _build_rust_binaries(repo_root, plan.rust_binary_dir)
    _write_checksums(output_dir / plan.checksum_name, [archive_path, *wheels, *rust_binaries])
    _copy_user_docs(repo_root / "docs" / "user", plan.wiki_payload_dir)
    return plan


def _tracked_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def _reject_sensitive_paths(paths: list[Path]) -> None:
    for path in paths:
        parts = set(path.parts)
        if path.name.endswith(".example.env"):
            continue
        if parts & SENSITIVE_PATH_PARTS or path.name in SENSITIVE_NAMES or path.name.endswith(SENSITIVE_SUFFIXES):
            raise ValueError(f"refusing to package sensitive or host-local path: {path}")


def _write_source_archive(repo_root: Path, files: list[Path], archive_path: Path, *, root_name: str) -> None:
    with tarfile.open(archive_path, "w:gz") as archive:
        for relative in files:
            archive.add(repo_root / relative, arcname=str(Path(root_name) / relative), recursive=False)


def _write_checksums(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_python_wheel(repo_root: Path, wheel_dir: Path) -> list[Path]:
    """Build a no-dependency Python wheel into the release output directory."""
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True)
    build_dir = repo_root / "build"
    had_build_dir = build_dir.exists()
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(repo_root)],
            cwd=repo_root,
            check=True,
        )
    finally:
        if build_dir.exists() and not had_build_dir:
            shutil.rmtree(build_dir)
    return sorted(wheel_dir.glob("*.whl"))


def _reject_version_mismatched_wheels(wheels: list[Path], *, version: str) -> None:
    """Fail closed if package metadata did not build the requested release version."""
    if not wheels:
        raise ValueError("Python wheel build did not produce a wheel")
    expected = f"-{version}-"
    mismatched = [wheel.name for wheel in wheels if expected not in wheel.name]
    if mismatched:
        raise ValueError(f"wheel version does not match release {version}: {', '.join(mismatched)}")


def _build_rust_binaries(repo_root: Path, binary_dir: Path) -> list[Path]:
    """Build and copy release-mode Rust binaries that are part of the Debian operator package."""
    if binary_dir.exists():
        shutil.rmtree(binary_dir)
    binary_dir.mkdir(parents=True)

    binary_names = ["gatherlink-time-helper"]
    for binary_name in binary_names:
        subprocess.run(
            ["cargo", "build", "--release", "--package", binary_name, "--bin", binary_name],
            cwd=repo_root,
            check=True,
        )
        source = repo_root / "target" / "release" / binary_name
        if not source.exists():
            raise ValueError(f"Rust binary build did not produce expected binary: {source}")
        shutil.copy2(source, binary_dir / binary_name)

    return sorted(binary_dir.iterdir())


def _copy_user_docs(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def main(argv: list[str] | None = None) -> None:
    """Run release artifact preparation from the command line."""
    parser = argparse.ArgumentParser(description="Prepare local Gatherlink release artifacts.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        plan = plan_release_artifacts(args.version, args.out)
    else:
        plan = build_release_artifacts(args.repo_root, args.out, version=args.version)
    print(f"release artifact plan: version={plan.version}")
    print(f"archive={plan.output_dir / plan.archive_name}")
    print(f"checksums={plan.output_dir / plan.checksum_name}")
    print(f"wheel_dir={plan.wheel_dir}")
    print(f"rust_binary_dir={plan.rust_binary_dir}")
    print(f"wiki_payload={plan.wiki_payload_dir}")


if __name__ == "__main__":
    main()
