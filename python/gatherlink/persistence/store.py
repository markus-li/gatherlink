"""
Durable JSON persistence helpers for Gatherlink control-plane state.

Persistent state is useful only when it remains subordinate to explicit config
and authenticated documents. These helpers provide the boring mechanics:
Debian path layout, atomic JSON writes, corruption-tolerant reads, and redaction
for operator-facing summaries. They do not decide policy.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from gatherlink.platform.debian import DebianCompatibilityBackend, default_debian_backend

SECRET_FIELD_MARKERS = (
    "private",
    "secret",
    "token",
    "password",
    "send_key",
    "receive_key",
    "session_key",
    "bootstrap_secret",
)
PRIVATE_FILE_ALLOWED_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass(frozen=True)
class GatherlinkStatePaths:
    """Concrete Debian paths for config, state, runtime sockets, and logs."""

    config_dir: Path
    state_dir: Path
    runtime_dir: Path
    log_dir: Path

    @classmethod
    def debian(cls, backend: DebianCompatibilityBackend | None = None) -> GatherlinkStatePaths:
        """Return the v1 Debian path layout."""
        backend = backend or default_debian_backend()
        return cls(
            config_dir=backend.config_dir,
            state_dir=backend.state_dir,
            runtime_dir=backend.runtime_dir,
            log_dir=backend.log_dir,
        )

    def identity_path(self, node_name: str = "local") -> Path:
        """Return the default private identity path for one local node identity."""
        return self.state_dir / "identities" / f"{node_name}.identity.json"

    def public_identity_path(self, node_name: str = "local") -> Path:
        """Return the default public identity export path for one local node identity."""
        return self.state_dir / "identities" / f"{node_name}.public.json"

    def bootstrap_cache_path(self) -> Path:
        """Return the default endpoint cache path."""
        return self.state_dir / "bootstrap" / "endpoints.json"

    def signed_bundle_path(self, name: str) -> Path:
        """Return the default path for one signed control-plane bundle."""
        return self.state_dir / "bundles" / f"{name}.signed.json"

    def trust_root_path(self, name: str) -> Path:
        """Return the default path for one trusted public identity/root."""
        return self.state_dir / "trust-roots" / f"{name}.public.json"

    def hint_path(self, name: str) -> Path:
        """Return the default path for non-authoritative runtime hints."""
        return self.state_dir / "hints" / f"{name}.json"

    def sealed_secret_path(self, name: str) -> Path:
        """Return the default path for one passphrase-sealed local secret."""
        return self.state_dir / "secrets" / f"{name}.sealed.json"

    def diagnostics_jsonl_path(self, service_name: str = "gatherlink") -> Path:
        """Return the default durable diagnostics JSONL path for one service."""
        return self.log_dir / f"{service_name}.jsonl"


@dataclass(frozen=True)
class PersistentStateStore:
    """
    Typed local state access for Python-owned v1 control-plane artifacts.

    The store deliberately distinguishes authority from hints. Identities,
    trust roots, and signed bundles have explicit paths and permissions; hints
    are loaded corruption-tolerantly and remain non-authoritative runtime input.
    """

    paths: GatherlinkStatePaths

    @classmethod
    def debian(cls, backend: DebianCompatibilityBackend | None = None) -> PersistentStateStore:
        """Return a store rooted at the Debian v1 state layout."""
        return cls(GatherlinkStatePaths.debian(backend))

    def write_private_identity(self, node_name: str, payload: dict[str, Any], *, force: bool = False) -> Path:
        """Persist one private node identity with owner-only permissions."""
        return self._write_json(self.paths.identity_path(node_name), payload, force=force, mode=0o600)

    def write_sealed_secret(self, name: str, payload: dict[str, Any], *, force: bool = False) -> Path:
        """Persist one sealed secret envelope with owner-only permissions."""
        return self._write_json(self.paths.sealed_secret_path(name), payload, force=force, mode=0o600)

    def read_private_identity(self, node_name: str) -> Any:
        """Read one owner-only private identity record."""
        return load_secret_json(self.paths.identity_path(node_name))

    def write_public_identity(self, node_name: str, payload: dict[str, Any], *, force: bool = False) -> Path:
        """Persist one public identity export."""
        return self._write_json(self.paths.public_identity_path(node_name), payload, force=force, mode=0o644)

    def write_trust_root(self, name: str, payload: dict[str, Any], *, force: bool = False) -> Path:
        """Persist one trusted public root without private material."""
        return self._write_json(self.paths.trust_root_path(name), payload, force=force, mode=0o644)

    def list_trust_roots(self) -> list[Path]:
        """List persisted public trust-root records."""
        root_dir = self.paths.state_dir / "trust-roots"
        return sorted(root_dir.glob("*.public.json"))

    def write_signed_bundle(self, name: str, payload: dict[str, Any], *, force: bool = False) -> Path:
        """Persist one signed control-plane bundle."""
        return self._write_json(self.paths.signed_bundle_path(name), payload, force=force, mode=0o644)

    def write_endpoint_cache(self, payload: dict[str, Any]) -> Path:
        """Persist non-authoritative peer endpoint cache data."""
        return self._write_json(self.paths.bootstrap_cache_path(), payload, force=True, mode=0o644)

    def read_endpoint_cache(self) -> Any:
        """Load endpoint cache data, ignoring absent or corrupt cache files."""
        return load_json_or_default(self.paths.bootstrap_cache_path(), {})

    def write_hint(self, name: str, payload: dict[str, Any]) -> Path:
        """Persist one non-authoritative runtime hint."""
        return self._write_json(self.paths.hint_path(name), payload, force=True, mode=0o644)

    def read_hint(self, name: str, default: Any | None = None) -> Any:
        """Load one non-authoritative hint, returning ``default`` when unusable."""
        return load_json_or_default(self.paths.hint_path(name), {} if default is None else default)

    def public_summary(self) -> dict[str, Any]:
        """Return operator-safe store facts without secret values."""
        state_dir = self.paths.state_dir
        return {
            "config_dir": str(self.paths.config_dir),
            "state_dir": str(state_dir),
            "runtime_dir": str(self.paths.runtime_dir),
            "log_dir": str(self.paths.log_dir),
            "identities": sorted(path.name for path in (state_dir / "identities").glob("*.identity.json")),
            "trust_roots": sorted(path.name for path in (state_dir / "trust-roots").glob("*.public.json")),
            "bundles": sorted(path.name for path in (state_dir / "bundles").glob("*.signed.json")),
            "hints": sorted(path.name for path in (state_dir / "hints").glob("*.json")),
            "sealed_secrets": sorted(path.name for path in (state_dir / "secrets").glob("*.sealed.json")),
        }

    def _write_json(self, path: Path, payload: dict[str, Any], *, force: bool, mode: int) -> Path:
        """Write one JSON artifact without accidentally overwriting authority."""
        if path.exists() and not force:
            raise FileExistsError(f"{path} already exists")
        atomic_write_json(path, payload, mode=mode)
        return path


def atomic_write_json(path: Path, payload: Any, *, mode: int | None = None) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    if mode is not None:
        os.chmod(temporary_path, mode)
    temporary_path.replace(path)
    if mode is not None:
        path.chmod(mode)


def load_json_or_default(path: Path, default: Any) -> Any:
    """Load JSON state, returning ``default`` when absent or corrupt."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def load_secret_json(path: Path) -> Any:
    """
    Load owner-only secret JSON and fail closed on broad POSIX permissions.

    Private identity and pending-session files contain material that must never
    become casual operator output or group-readable state. Public exports use
    their own loaders and are intentionally not checked here.
    """
    _assert_owner_only_file(path)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _assert_owner_only_file(path: Path) -> None:
    """Raise when a secret file grants group/other permissions on POSIX."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise FileNotFoundError(path) from exc
    if os.name != "posix":
        return
    if stat.S_IMODE(mode) & ~PRIVATE_FILE_ALLOWED_MODE:
        raise PermissionError(f"{path} must be owner-only (0600)")


def redact_secrets(value: Any) -> Any:
    """Return a copy of a JSON-like value with secret-looking fields redacted."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in SECRET_FIELD_MARKERS):
                redacted[key] = _redacted_marker(item)
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _redacted_marker(value: Any) -> str | None:
    """Return a stable marker proving a secret existed without exposing it."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.startswith("[redacted:") and value.endswith("]"):
            return value
        return f"[redacted:{len(value)} chars]"
    if isinstance(value, bytes):
        return f"[redacted:{len(value)} bytes]"
    return "[redacted]"
