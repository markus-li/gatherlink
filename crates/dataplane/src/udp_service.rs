//! Userland UDP service primitives.
//!
//! The first test target is deliberately plain UDP sockets. No TUN/TAP, raw
//! sockets, route mutation, firewall policy, helper tunnels, or root-only
//! capabilities belong in this module.

use std::fmt;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
use std::time::Duration;

use gatherlink_protocol::ids::PathId;

/// UDP service configuration compiled by Python before it reaches Rust.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UdpServiceConfig {
    name: String,
    listen: Option<SocketAddr>,
    target: SocketAddr,
    priority: u16,
}

impl UdpServiceConfig {
    /// Create a UDP service config with a stable name and explicit target.
    pub fn new(
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_priority(name, listen, target, 100)
    }

    /// Create a UDP service config with compiled service priority.
    pub fn new_with_priority(
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
    ) -> Result<Self, UdpServiceError> {
        let name = name.into();
        if name.trim().is_empty() {
            return Err(UdpServiceError::EmptyServiceName);
        }
        if priority == 0 {
            return Err(UdpServiceError::ServicePriorityTooSmall { service: name });
        }

        Ok(Self {
            name,
            listen,
            target,
            priority,
        })
    }

    /// Stable service name from the runtime config.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Local UDP listener address, when this node should accept local traffic.
    pub fn listen(&self) -> Option<SocketAddr> {
        self.listen
    }

    /// Remote UDP target emitted by this virtual service.
    pub fn target(&self) -> SocketAddr {
        self.target
    }

    /// Compiled service priority. Python owns how this affects future scheduling.
    pub fn priority(&self) -> u16 {
        self.priority
    }
}

/// Bound userland UDP listener for a configured service.
#[derive(Debug)]
pub struct UserlandUdpService {
    config: UdpServiceConfig,
    socket: UdpSocket,
}

impl UserlandUdpService {
    /// Bind a normal UDP socket for a service that has a listen endpoint.
    pub fn bind(config: UdpServiceConfig) -> Result<Self, UdpServiceError> {
        let listen = config.listen().ok_or(UdpServiceError::MissingListenAddress)?;
        let socket = UdpSocket::bind(listen).map_err(UdpServiceError::BindFailed)?;
        Self::from_bound_socket(config, socket)
    }

    /// Return the configured service.
    pub fn config(&self) -> &UdpServiceConfig {
        &self.config
    }

    /// Return the actual bound address, useful when tests bind port 0.
    pub fn local_addr(&self) -> Result<SocketAddr, UdpServiceError> {
        self.socket.local_addr().map_err(UdpServiceError::LocalAddrFailed)
    }

    /// Return whether a requested config can keep this service's live socket.
    ///
    /// The configured listener may be port 0 during tests or future dynamic
    /// allocation. Hot reapply therefore accepts either the original configured
    /// listen address or the actual bound address reported by the socket.
    pub fn can_preserve_socket_for(&self, config: &UdpServiceConfig) -> Result<bool, UdpServiceError> {
        if self.config.name() != config.name() {
            return Ok(false);
        }

        if self.config.listen() == config.listen() {
            return Ok(true);
        }

        Ok(Some(self.local_addr()?) == config.listen())
    }

    /// Clone the existing socket while applying a compatible service config.
    ///
    /// Python owns the decision to reapply config. Rust keeps the operation
    /// execution-focused: if the listener did not change, the same socket can
    /// keep receiving traffic while target/path metadata changes around it.
    pub fn clone_with_config(&self, config: UdpServiceConfig) -> Result<Self, UdpServiceError> {
        if !self.can_preserve_socket_for(&config)? {
            return Err(UdpServiceError::IncompatibleListenReapply {
                service: config.name().to_owned(),
                current: self.config.listen(),
                requested: config.listen(),
            });
        }

        let socket = self.socket.try_clone().map_err(UdpServiceError::CloneFailed)?;
        Self::from_bound_socket(config, socket)
    }

    /// Receive one UDP datagram from the userland service socket.
    pub fn recv_from(&self, buffer: &mut [u8]) -> Result<(usize, SocketAddr), UdpServiceError> {
        self.socket.recv_from(buffer).map_err(UdpServiceError::ReceiveFailed)
    }

    /// Receive one queued UDP datagram without blocking.
    pub fn try_recv_from(&self, buffer: &mut [u8]) -> Result<Option<(usize, SocketAddr)>, UdpServiceError> {
        self.socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        let result = match self.socket.recv_from(buffer) {
            Ok(received) => Ok(Some(received)),
            Err(error) if error.kind() == ErrorKind::WouldBlock => Ok(None),
            Err(error) => Err(UdpServiceError::ReceiveFailed(error)),
        };
        self.socket
            .set_nonblocking(false)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        result
    }

    /// Emit one UDP datagram to this service's configured target.
    pub fn emit_to_target(&self, payload: &[u8]) -> Result<usize, UdpServiceError> {
        self.socket
            .send_to(payload, self.config.target())
            .map_err(UdpServiceError::SendFailed)
    }

    fn from_bound_socket(config: UdpServiceConfig, socket: UdpSocket) -> Result<Self, UdpServiceError> {
        // Tests and future supervisors should fail predictably instead of
        // blocking forever when a service is miswired.
        socket
            .set_read_timeout(Some(Duration::from_millis(500)))
            .map_err(UdpServiceError::ConfigureSocketFailed)?;

        Ok(Self { config, socket })
    }
}

/// Errors for the first userland UDP service layer.
#[derive(Debug)]
pub enum UdpServiceError {
    EmptyServiceName,
    MissingListenAddress,
    DuplicateServiceName(String),
    DuplicateListenAddress(SocketAddr),
    DuplicatePathId(PathId),
    MissingPath,
    PathMtuTooSmall {
        path_id: PathId,
        mtu: usize,
    },
    PathWeightTooSmall {
        path_id: PathId,
    },
    ServicePriorityTooSmall {
        service: String,
    },
    IncompatibleListenReapply {
        service: String,
        current: Option<SocketAddr>,
        requested: Option<SocketAddr>,
    },
    BindFailed(std::io::Error),
    ConfigureSocketFailed(std::io::Error),
    CloneFailed(std::io::Error),
    LocalAddrFailed(std::io::Error),
    ReceiveFailed(std::io::Error),
    SendFailed(std::io::Error),
}

impl fmt::Display for UdpServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyServiceName => write!(formatter, "service name must not be empty"),
            Self::MissingListenAddress => write!(formatter, "service requires a listen address"),
            Self::DuplicateServiceName(name) => write!(formatter, "duplicate service name: {name}"),
            Self::DuplicateListenAddress(addr) => write!(formatter, "duplicate UDP listen address: {addr}"),
            Self::DuplicatePathId(path_id) => write!(formatter, "duplicate path id: {path_id}"),
            Self::MissingPath => write!(formatter, "runtime config requires at least one path"),
            Self::PathMtuTooSmall { path_id, mtu } => {
                write!(formatter, "path {path_id} MTU {mtu} is too small for Gatherlink framing")
            }
            Self::PathWeightTooSmall { path_id } => {
                write!(formatter, "path {path_id} scheduler weight must be greater than zero")
            }
            Self::ServicePriorityTooSmall { service } => {
                write!(formatter, "service {service} priority must be greater than zero")
            }
            Self::IncompatibleListenReapply {
                service,
                current,
                requested,
            } => write!(
                formatter,
                "cannot preserve UDP socket for service {service}: current listen {current:?} does not match requested listen {requested:?}",
            ),
            Self::BindFailed(error) => {
                write!(formatter, "failed to bind UDP service socket: {error}")
            }
            Self::ConfigureSocketFailed(error) => {
                write!(formatter, "failed to configure UDP socket: {error}")
            }
            Self::CloneFailed(error) => write!(formatter, "failed to clone UDP socket: {error}"),
            Self::LocalAddrFailed(error) => write!(
                formatter,
                "failed to read UDP socket local address: {error}",
            ),
            Self::ReceiveFailed(error) => {
                write!(formatter, "failed to receive UDP datagram: {error}")
            }
            Self::SendFailed(error) => write!(formatter, "failed to send UDP datagram: {error}"),
        }
    }
}

impl std::error::Error for UdpServiceError {}
