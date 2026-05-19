//! Core dataplane engine.
//!
//! The engine owns userland UDP service sockets and applies the Gatherlink
//! frame boundary before emitting virtual UDP payloads. Business policy stays in
//! Python; this module executes already-compiled runtime state.

use std::fmt;

use gatherlink_protocol::errors::ProtocolError;
use gatherlink_protocol::frame::{Frame, FrameKind};

use crate::runtime_config::CoreRuntimeConfig;
use crate::udp_service::{UdpServiceError, UserlandUdpService};

/// Core userland UDP dataplane.
#[derive(Debug)]
pub struct CoreDataplane {
    services: Vec<UserlandUdpService>,
    next_sequence: u64,
}

impl CoreDataplane {
    /// Bind all services from compiled runtime config.
    pub fn bind(config: CoreRuntimeConfig) -> Result<Self, DataplaneError> {
        let services = config
            .services()
            .iter()
            .cloned()
            .map(UserlandUdpService::bind)
            .collect::<Result<Vec<_>, _>>()?;

        Ok(Self {
            services,
            next_sequence: 1,
        })
    }

    /// Atomically reapply already-compiled runtime config from the Python control plane.
    ///
    /// This is intentionally a runtime-state operation, not config interpretation.
    /// Rust preserves compatible sockets, binds new sockets before swapping the
    /// active service set, and reports what changed so Python can publish clean
    /// diagnostics or decide whether a more disruptive restart is needed.
    pub fn reapply_config(
        &mut self,
        config: CoreRuntimeConfig,
    ) -> Result<ReapplyOutcome, DataplaneError> {
        let mut outcome = ReapplyOutcome::default();
        let mut services = Vec::with_capacity(config.services().len());

        for desired in config.services().iter().cloned() {
            if let Some(current) = self.service(desired.name()) {
                if current.config() == &desired {
                    outcome.unchanged += 1;
                    services.push(current.clone_with_config(desired)?);
                } else if current.can_preserve_socket_for(&desired)? {
                    outcome.updated += 1;
                    services.push(current.clone_with_config(desired)?);
                } else {
                    // Listener changes are possible, but they are disruptive to
                    // that service. Bind first and swap only after every service
                    // in the requested config has been prepared.
                    outcome.rebound += 1;
                    services.push(UserlandUdpService::bind(desired)?);
                }
            } else {
                outcome.added += 1;
                services.push(UserlandUdpService::bind(desired)?);
            }
        }

        outcome.removed = self
            .services
            .iter()
            .filter(|service| {
                !config
                    .services()
                    .iter()
                    .any(|desired| desired.name() == service.config().name())
            })
            .count();

        self.services = services;
        Ok(outcome)
    }

    /// Return a bound service by name.
    pub fn service(&self, name: &str) -> Option<&UserlandUdpService> {
        self.services
            .iter()
            .find(|service| service.config().name() == name)
    }

    /// Receive one local UDP datagram, pass it through the v1 data-frame
    /// boundary, and emit the original virtual payload to the service target.
    pub fn forward_one_for_service(
        &mut self,
        name: &str,
    ) -> Result<ForwardOutcome, DataplaneError> {
        let service_index = self
            .services
            .iter()
            .position(|service| service.config().name() == name)
            .ok_or_else(|| DataplaneError::UnknownService(name.to_owned()))?;

        let mut buffer = vec![0_u8; u16::MAX as usize];
        let (length, source) = self.services[service_index].recv_from(&mut buffer)?;
        buffer.truncate(length);

        let sequence = self.next_sequence;
        self.next_sequence += 1;

        // In the full system this frame travels over selected logical paths.
        // The first core test target immediately decodes it in-process so we
        // can prove the UDP service boundary without inventing helper tunnels.
        let frame = Frame::data(0, service_index as u64 + 1, 0, 0, sequence, buffer)?;
        let encoded = frame.encode()?;
        let decoded = Frame::decode(&encoded)?;
        if decoded.header.kind != FrameKind::Data {
            return Err(DataplaneError::UnexpectedFrameKind);
        }

        let emitted = self.services[service_index].emit_to_target(&decoded.payload)?;
        Ok(ForwardOutcome {
            service: name.to_owned(),
            source,
            target: self.services[service_index].config().target(),
            payload_len: emitted,
            sequence,
        })
    }
}

/// Summary of one config reapply operation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ReapplyOutcome {
    pub unchanged: usize,
    pub updated: usize,
    pub rebound: usize,
    pub added: usize,
    pub removed: usize,
}

/// Observable result for one forwarded datagram.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForwardOutcome {
    pub service: String,
    pub source: std::net::SocketAddr,
    pub target: std::net::SocketAddr,
    pub payload_len: usize,
    pub sequence: u64,
}

/// Dataplane execution errors.
#[derive(Debug)]
pub enum DataplaneError {
    UdpService(UdpServiceError),
    Protocol(ProtocolError),
    UnknownService(String),
    UnexpectedFrameKind,
}

impl fmt::Display for DataplaneError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UdpService(error) => write!(formatter, "{error}"),
            Self::Protocol(error) => write!(formatter, "{error}"),
            Self::UnknownService(name) => write!(formatter, "unknown UDP service: {name}"),
            Self::UnexpectedFrameKind => write!(formatter, "decoded frame was not a data frame"),
        }
    }
}

impl std::error::Error for DataplaneError {}

impl From<UdpServiceError> for DataplaneError {
    fn from(error: UdpServiceError) -> Self {
        Self::UdpService(error)
    }
}

impl From<ProtocolError> for DataplaneError {
    fn from(error: ProtocolError) -> Self {
        Self::Protocol(error)
    }
}

#[cfg(test)]
mod tests {
    use std::net::UdpSocket;
    use std::time::Duration;

    use super::*;
    use crate::runtime_config::CoreRuntimeConfig;
    use crate::udp_service::UdpServiceConfig;

    #[test]
    fn forwards_one_udp_payload_through_core_frame_boundary() {
        let target = UdpSocket::bind("127.0.0.1:0").unwrap();
        target
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let config = CoreRuntimeConfig::single_udp_service(
            "udp-main",
            "127.0.0.1:0".parse().unwrap(),
            target.local_addr().unwrap(),
        )
        .unwrap();
        let mut dataplane = CoreDataplane::bind(config).unwrap();
        let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
        let service_addr = dataplane.service("udp-main").unwrap().local_addr().unwrap();

        sender.send_to(b"plain-user-udp", service_addr).unwrap();
        let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

        let mut buffer = [0_u8; 64];
        let (length, _source) = target.recv_from(&mut buffer).unwrap();
        assert_eq!(&buffer[..length], b"plain-user-udp");
        assert_eq!(outcome.service, "udp-main");
        assert_eq!(outcome.payload_len, b"plain-user-udp".len());
        assert_eq!(outcome.sequence, 1);
    }

    #[test]
    fn forwards_ipv6_udp_payload_through_core_frame_boundary() {
        let target = UdpSocket::bind("[::1]:0").unwrap();
        target
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let config = CoreRuntimeConfig::single_udp_service(
            "udp-v6",
            "[::1]:0".parse().unwrap(),
            target.local_addr().unwrap(),
        )
        .unwrap();
        let mut dataplane = CoreDataplane::bind(config).unwrap();
        let sender = UdpSocket::bind("[::1]:0").unwrap();
        let service_addr = dataplane.service("udp-v6").unwrap().local_addr().unwrap();

        sender.send_to(b"plain-ipv6-udp", service_addr).unwrap();
        let outcome = dataplane.forward_one_for_service("udp-v6").unwrap();

        let mut buffer = [0_u8; 64];
        let (length, _source) = target.recv_from(&mut buffer).unwrap();
        assert_eq!(&buffer[..length], b"plain-ipv6-udp");
        assert_eq!(outcome.service, "udp-v6");
        assert_eq!(outcome.source.ip(), sender.local_addr().unwrap().ip());
        assert_eq!(outcome.target, target.local_addr().unwrap());
    }

    #[test]
    fn reapply_updates_target_without_rebinding_listener() {
        let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        second_target
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let config = CoreRuntimeConfig::single_udp_service(
            "udp-main",
            "127.0.0.1:0".parse().unwrap(),
            first_target.local_addr().unwrap(),
        )
        .unwrap();
        let mut dataplane = CoreDataplane::bind(config).unwrap();
        let original_listen = dataplane.service("udp-main").unwrap().local_addr().unwrap();
        let updated_config = CoreRuntimeConfig::new(vec![UdpServiceConfig::new(
            "udp-main",
            Some(original_listen),
            second_target.local_addr().unwrap(),
        )
        .unwrap()])
        .unwrap();

        let outcome = dataplane.reapply_config(updated_config).unwrap();
        let updated_listen = dataplane.service("udp-main").unwrap().local_addr().unwrap();

        assert_eq!(
            outcome,
            ReapplyOutcome {
                unchanged: 0,
                updated: 1,
                rebound: 0,
                added: 0,
                removed: 0,
            },
        );
        assert_eq!(updated_listen, original_listen);
    }

    #[test]
    fn reapply_can_add_and_remove_services() {
        let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        let config = CoreRuntimeConfig::single_udp_service(
            "udp-main",
            "127.0.0.1:0".parse().unwrap(),
            first_target.local_addr().unwrap(),
        )
        .unwrap();
        let mut dataplane = CoreDataplane::bind(config).unwrap();
        let replacement = CoreRuntimeConfig::new(vec![UdpServiceConfig::new(
            "udp-secondary",
            Some("127.0.0.1:0".parse().unwrap()),
            second_target.local_addr().unwrap(),
        )
        .unwrap()])
        .unwrap();

        let outcome = dataplane.reapply_config(replacement).unwrap();

        assert_eq!(
            outcome,
            ReapplyOutcome {
                unchanged: 0,
                updated: 0,
                rebound: 0,
                added: 1,
                removed: 1,
            },
        );
        assert!(dataplane.service("udp-main").is_none());
        assert!(dataplane.service("udp-secondary").is_some());
    }
}
