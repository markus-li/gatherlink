//! Runtime config accepted by the Rust dataplane.
//!
//! Python owns user config parsing, validation, expansion, and policy. This
//! module only models the already-compiled core data Rust needs to execute the
//! first userland UDP transport target.

use std::collections::HashSet;
use std::net::SocketAddr;

use crate::udp_service::{UdpServiceConfig, UdpServiceError};

/// Core runtime config for the userland UDP dataplane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreRuntimeConfig {
    services: Vec<UdpServiceConfig>,
}

impl CoreRuntimeConfig {
    /// Build a core runtime config from already-validated UDP services.
    pub fn new(services: Vec<UdpServiceConfig>) -> Result<Self, UdpServiceError> {
        let mut names = HashSet::new();
        let mut listens = HashSet::new();
        for service in &services {
            if !names.insert(service.name()) {
                return Err(UdpServiceError::DuplicateServiceName(
                    service.name().to_owned(),
                ));
            }

            if let Some(listen) = service.listen() {
                if !listens.insert(listen) {
                    return Err(UdpServiceError::DuplicateListenAddress(listen));
                }
            }
        }

        Ok(Self { services })
    }

    /// Return the service configs in deterministic order.
    pub fn services(&self) -> &[UdpServiceConfig] {
        &self.services
    }

    /// Convenience constructor for the first pure userland UDP test target.
    pub fn single_udp_service(
        name: impl Into<String>,
        listen: SocketAddr,
        target: SocketAddr,
    ) -> Result<Self, UdpServiceError> {
        Self::new(vec![UdpServiceConfig::new(name, Some(listen), target)?])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_duplicate_service_names() {
        let listen: SocketAddr = "127.0.0.1:55180".parse().unwrap();
        let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
        let first = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();
        let second = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();

        let err = CoreRuntimeConfig::new(vec![first, second]).unwrap_err();

        assert!(matches!(err, UdpServiceError::DuplicateServiceName(name) if name == "udp-main"));
    }

    #[test]
    fn rejects_duplicate_listen_addresses() {
        let listen: SocketAddr = "127.0.0.1:55180".parse().unwrap();
        let target: SocketAddr = "127.0.0.1:51820".parse().unwrap();
        let first = UdpServiceConfig::new("udp-main", Some(listen), target).unwrap();
        let second = UdpServiceConfig::new("udp-secondary", Some(listen), target).unwrap();

        let err = CoreRuntimeConfig::new(vec![first, second]).unwrap_err();

        assert!(matches!(err, UdpServiceError::DuplicateListenAddress(addr) if addr == listen));
    }
}

#[cfg(test)]
mod ipv6_tests {
    use super::*;

    #[test]
    fn accepts_ipv6_udp_service_addresses() {
        let listen: SocketAddr = "[::1]:55180".parse().unwrap();
        let target: SocketAddr = "[::1]:51820".parse().unwrap();
        let service = UdpServiceConfig::new("udp-v6", Some(listen), target).unwrap();

        let config = CoreRuntimeConfig::new(vec![service]).unwrap();

        assert_eq!(config.services()[0].listen(), Some(listen));
        assert_eq!(config.services()[0].target(), target);
    }
}
