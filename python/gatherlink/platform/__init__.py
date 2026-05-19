"""Platform compatibility backends for OS-specific control-plane behavior."""

from gatherlink.platform.debian import DebianCompatibilityBackend, SubprocessCommandRunner, default_debian_backend

__all__ = ["DebianCompatibilityBackend", "SubprocessCommandRunner", "default_debian_backend"]
