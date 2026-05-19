"""
helpers.wireguard.keys module for Gatherlink.

This module is part of the Gatherlink Python control plane. Python owns policy,
configuration, orchestration, diagnostics, and helper services. The Rust dataplane
should receive already-validated runtime state and should not contain business logic.
"""

from __future__ import annotations

import subprocess

from gatherlink.shared.logging import get_logger

logger = get_logger(__name__)


def generate_private_key(*, wg_binary: str = "wg") -> str:
    """Generate a WireGuard private key by delegating to the official `wg` tool."""
    return _run_wg([wg_binary, "genkey"])


def derive_public_key(private_key: str, *, wg_binary: str = "wg") -> str:
    """Derive a WireGuard public key by delegating to the official `wg` tool."""
    return _run_wg([wg_binary, "pubkey"], input_text=private_key + "\n")


def _run_wg(command: list[str], *, input_text: str | None = None) -> str:
    """Run a narrow WireGuard key command and return its single-line output."""
    try:
        result = subprocess.run(
            command,
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("WireGuard `wg` tool was not found; install wireguard-tools") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"WireGuard tool failed: {exc.stderr.strip()}") from exc
    return result.stdout.strip()
