"""Gatherlink config package."""

from gatherlink.config.migration import (
    ConfigMigrationRegistry,
    ConfigMigrationResult,
    ConfigMigrationStep,
    migrate_config_dict,
)

__all__ = [
    "ConfigMigrationRegistry",
    "ConfigMigrationResult",
    "ConfigMigrationStep",
    "migrate_config_dict",
]

from gatherlink.config.expansion import expand_config
from gatherlink.config.loader import load_config_dict
from gatherlink.config.models import (
    DnsHelperConfig,
    DnsHelperUpstreamConfig,
    GatherlinkConfig,
    HelpersConfig,
    PathConfig,
    SchedulerConfig,
    SecurityConfig,
    ServiceConfig,
    Socks5HelperConfig,
    TcpForwardHelperConfig,
    WireGuardHelperConfig,
)
from gatherlink.config.runtime import (
    RuntimeConfig,
    RuntimeDnsHelperConfig,
    RuntimeDnsUpstreamConfig,
    RuntimePathConfig,
    RuntimeSecurityConfig,
    RuntimeServiceConfig,
    RuntimeSocks5HelperConfig,
    RuntimeTcpForwardHelperConfig,
    RuntimeWireGuardHelperConfig,
)
from gatherlink.config.validation import detect_config_format, validate_config_dict, validate_config_file
from gatherlink.config.versions import CURRENT_SCHEMA_VERSION, supported_schema_versions

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DnsHelperConfig",
    "DnsHelperUpstreamConfig",
    "GatherlinkConfig",
    "HelpersConfig",
    "PathConfig",
    "RuntimeConfig",
    "RuntimeDnsHelperConfig",
    "RuntimeDnsUpstreamConfig",
    "RuntimePathConfig",
    "RuntimeSecurityConfig",
    "RuntimeServiceConfig",
    "RuntimeSocks5HelperConfig",
    "RuntimeTcpForwardHelperConfig",
    "RuntimeWireGuardHelperConfig",
    "SchedulerConfig",
    "SecurityConfig",
    "ServiceConfig",
    "Socks5HelperConfig",
    "TcpForwardHelperConfig",
    "WireGuardHelperConfig",
    "detect_config_format",
    "expand_config",
    "load_config_dict",
    "supported_schema_versions",
    "validate_config_dict",
    "validate_config_file",
]
