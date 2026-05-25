//! Userland UDP service primitives.
//!
//! The first test target is deliberately plain UDP sockets. No TUN/TAP, raw
//! sockets, route mutation, firewall policy, helper tunnels, or root-only
//! capabilities belong in this module.

use std::collections::HashMap;
use std::fmt;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::time::Duration;

use gatherlink_protocol::ids::{is_reserved_service_id, PathId, ServiceId};

use crate::udp_batch::{drain_udp_socket, send_udp_many};

const UDP_SOCKET_BUFFER_BYTES: usize = 1024 * 1024 * 1024;

/// Per-service fanout primitive selected by Python and executed by Rust.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceSchedulerConfig {
    fanout: u16,
    fanout_below_bytes: usize,
    flowlet_idle_us: u64,
    flowlet_max_hold_us: u64,
    path_run_datagrams: usize,
    path_policy: ServicePathPolicy,
    allowed_path_ids: Vec<PathId>,
    path_weights: Vec<(PathId, u16)>,
}

/// Per-service path selector primitive compiled by Python.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServicePathPolicy {
    /// Use the node-wide compiled scheduler.
    Inherit,
    /// Pick the currently best eligible path by capacity, latency, loss, and configured order.
    SingleBestPath,
    /// Use the node-wide compiled path weights with an independent per-service round-robin cursor.
    WeightedRoundRobin,
}

impl ServiceSchedulerConfig {
    /// Normal one-path service scheduling.
    pub const fn normal() -> Self {
        Self {
            fanout: 1,
            fanout_below_bytes: 0,
            flowlet_idle_us: 0,
            flowlet_max_hold_us: 0,
            path_run_datagrams: 0,
            path_policy: ServicePathPolicy::Inherit,
            allowed_path_ids: Vec::new(),
            path_weights: Vec::new(),
        }
    }

    /// Build a service scheduler primitive from Python-compiled values.
    ///
    /// `fanout == 0` means every eligible path. `fanout_below_bytes == 0`
    /// means the fanout applies to every payload. Otherwise, fanout applies
    /// only to payloads whose size is at or below the threshold; larger payloads
    /// fall back to one normally scheduled path.
    pub const fn new(fanout: u16, fanout_below_bytes: usize) -> Self {
        Self::new_with_flowlet(fanout, fanout_below_bytes, 0)
    }

    /// Build a service scheduler primitive with a flowlet stickiness timeout.
    ///
    /// `flowlet_idle_us == 0` disables stickiness. Otherwise, packets from the
    /// same local service source keep using the same selected path until that
    /// source has been idle for at least the configured duration.
    pub const fn new_with_flowlet(fanout: u16, fanout_below_bytes: usize, flowlet_idle_us: u64) -> Self {
        Self::new_with_bounded_flowlet(fanout, fanout_below_bytes, flowlet_idle_us, flowlet_idle_us)
    }

    /// Build a service scheduler primitive with idle and maximum stickiness bounds.
    ///
    /// `flowlet_max_hold_us` prevents a busy flow from pinning one path forever.
    /// When the maximum hold expires, Rust asks the compiled path scheduler for a
    /// fresh path even if the flow has not gone idle.
    pub const fn new_with_bounded_flowlet(
        fanout: u16,
        fanout_below_bytes: usize,
        flowlet_idle_us: u64,
        flowlet_max_hold_us: u64,
    ) -> Self {
        Self::new_with_burst_run(fanout, fanout_below_bytes, flowlet_idle_us, flowlet_max_hold_us, 0)
    }

    /// Build a service scheduler primitive with an optional hot-burst path run.
    ///
    /// `path_run_datagrams == 0` keeps Rust's default execution batching. A
    /// non-zero value is a Python-compiled primitive for order-sensitive helper
    /// traffic that should consult the scheduler more frequently during a hot
    /// service burst.
    pub const fn new_with_burst_run(
        fanout: u16,
        fanout_below_bytes: usize,
        flowlet_idle_us: u64,
        flowlet_max_hold_us: u64,
        path_run_datagrams: usize,
    ) -> Self {
        Self::new_with_path_policy(
            fanout,
            fanout_below_bytes,
            flowlet_idle_us,
            flowlet_max_hold_us,
            path_run_datagrams,
            ServicePathPolicy::Inherit,
        )
    }

    /// Build a service scheduler primitive with an explicit path selector.
    pub const fn new_with_path_policy(
        fanout: u16,
        fanout_below_bytes: usize,
        flowlet_idle_us: u64,
        flowlet_max_hold_us: u64,
        path_run_datagrams: usize,
        path_policy: ServicePathPolicy,
    ) -> Self {
        Self {
            fanout,
            fanout_below_bytes,
            flowlet_idle_us,
            flowlet_max_hold_us,
            path_run_datagrams,
            path_policy,
            allowed_path_ids: Vec::new(),
            path_weights: Vec::new(),
        }
    }

    /// Attach a Python-compiled list of eligible path ids to this primitive.
    pub fn with_allowed_path_ids(mut self, allowed_path_ids: Vec<PathId>) -> Self {
        self.allowed_path_ids = allowed_path_ids;
        self
    }

    /// Attach Python-compiled service-specific path weights to this primitive.
    pub fn with_path_weights(mut self, path_weights: Vec<(PathId, u16)>) -> Self {
        self.path_weights = path_weights;
        self
    }

    /// Requested fanout count. Zero means every eligible path.
    pub const fn fanout(&self) -> u16 {
        self.fanout
    }

    /// Payload threshold for fanout, or zero when fanout always applies.
    pub const fn fanout_below_bytes(&self) -> usize {
        self.fanout_below_bytes
    }

    /// Idle timeout for service/source path stickiness, or zero when disabled.
    pub const fn flowlet_idle_us(&self) -> u64 {
        self.flowlet_idle_us
    }

    /// Maximum time a busy service/source may remain pinned to one path.
    pub const fn flowlet_max_hold_us(&self) -> u64 {
        self.flowlet_max_hold_us
    }

    /// Maximum hot-burst datagrams to keep on one path, or zero for default.
    pub const fn path_run_datagrams(&self) -> usize {
        self.path_run_datagrams
    }

    /// Per-service path selector selected by Python.
    pub const fn path_policy(&self) -> ServicePathPolicy {
        self.path_policy
    }

    /// Optional path ids this service may use. Empty means every eligible path.
    pub fn allowed_path_ids(&self) -> &[PathId] {
        &self.allowed_path_ids
    }

    /// Optional path weights this service should use. Empty means path defaults.
    pub fn path_weights(&self) -> &[(PathId, u16)] {
        &self.path_weights
    }

    /// Return whether this service may use a path id.
    pub fn allows_path_id(&self, path_id: PathId) -> bool {
        if !self.allowed_path_ids.is_empty() {
            return self.allowed_path_ids.contains(&path_id);
        }
        self.path_weights.is_empty()
            || self
                .path_weights
                .iter()
                .any(|(weighted_path_id, weight)| *weighted_path_id == path_id && *weight > 0)
    }

    /// Return the fanout Rust should execute for a payload of this size.
    pub const fn effective_fanout(&self, payload_len: usize) -> u16 {
        if self.fanout_below_bytes == 0 || payload_len <= self.fanout_below_bytes {
            self.fanout
        } else {
            1
        }
    }
}

/// How a remote payload should be emitted on the local app-facing side.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceReturnMode {
    Fixed,
    LearnedSingleSource,
    PeerScopedSource,
}

/// UDP service configuration compiled by Python before it reaches Rust.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UdpServiceConfig {
    service_id: ServiceId,
    name: String,
    listen: Option<SocketAddr>,
    target: SocketAddr,
    priority: u16,
    return_mode: ServiceReturnMode,
    scheduler: ServiceSchedulerConfig,
}

impl UdpServiceConfig {
    /// Create a UDP service config with a stable name and explicit target.
    pub fn new(
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_service_id(0, name, listen, target)
    }

    /// Create a UDP service config with an explicit user/application service id.
    ///
    /// Service id `0` means "assign later" and is accepted only before the
    /// runtime config finalizes services. Explicit ids must live in the
    /// user/application range; Gatherlink-owned ids are reserved for control,
    /// time sync, diagnostics, and future internal services.
    pub fn new_with_service_id(
        service_id: ServiceId,
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_priority_and_service_id(service_id, name, listen, target, 100)
    }

    /// Create a UDP service config with compiled service priority.
    pub fn new_with_priority(
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_priority_and_service_id(0, name, listen, target, priority)
    }

    /// Create a UDP service config with explicit id and compiled service priority.
    pub fn new_with_priority_and_service_id(
        service_id: ServiceId,
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_return_mode_and_service_id(service_id, name, listen, target, priority, ServiceReturnMode::Fixed)
    }

    /// Create a UDP service config with compiled priority and return behavior.
    pub fn new_with_return_mode(
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
        return_mode: ServiceReturnMode,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_scheduler(
            0,
            name,
            listen,
            target,
            priority,
            return_mode,
            ServiceSchedulerConfig::normal(),
        )
    }

    /// Create a UDP service config with compiled id, priority, and return behavior.
    pub fn new_with_return_mode_and_service_id(
        service_id: ServiceId,
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
        return_mode: ServiceReturnMode,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_scheduler(
            service_id,
            name,
            listen,
            target,
            priority,
            return_mode,
            ServiceSchedulerConfig::normal(),
        )
    }

    /// Create a UDP service config with every Rust-executed primitive explicit.
    pub fn new_with_scheduler(
        service_id: ServiceId,
        name: impl Into<String>,
        listen: Option<SocketAddr>,
        target: SocketAddr,
        priority: u16,
        return_mode: ServiceReturnMode,
        scheduler: ServiceSchedulerConfig,
    ) -> Result<Self, UdpServiceError> {
        let name = name.into();
        if name.trim().is_empty() {
            return Err(UdpServiceError::EmptyServiceName);
        }
        if service_id != 0 && is_reserved_service_id(service_id) {
            return Err(UdpServiceError::ReservedServiceId {
                service: name,
                service_id,
            });
        }
        if priority == 0 {
            return Err(UdpServiceError::ServicePriorityTooSmall { service: name });
        }

        Ok(Self {
            service_id,
            name,
            listen,
            target,
            priority,
            return_mode,
            scheduler,
        })
    }

    /// Compact wire service id.
    pub fn service_id(&self) -> ServiceId {
        self.service_id
    }

    /// Return a copy with the runtime-assigned service id.
    pub(crate) fn with_assigned_service_id(&self, service_id: ServiceId) -> Result<Self, UdpServiceError> {
        Self::new_with_scheduler(
            service_id,
            self.name.clone(),
            self.listen,
            self.target,
            self.priority,
            self.return_mode,
            self.scheduler.clone(),
        )
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

    /// Return behavior selected by Python config.
    pub fn return_mode(&self) -> ServiceReturnMode {
        self.return_mode
    }

    /// Service fanout primitive selected by Python.
    pub fn scheduler(&self) -> ServiceSchedulerConfig {
        self.scheduler.clone()
    }
}

/// Bound userland UDP listener for a configured service.
#[derive(Debug)]
pub struct UserlandUdpService {
    config: UdpServiceConfig,
    socket: UdpSocket,
    peer_sources: HashMap<u32, UdpSocket>,
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
        let mut service = Self::from_bound_socket(config, socket)?;
        service.peer_sources = self.clone_peer_sources()?;
        Ok(service)
    }

    /// Receive one UDP datagram from the userland service socket.
    pub fn recv_from(&self, buffer: &mut [u8]) -> Result<(usize, SocketAddr), UdpServiceError> {
        self.socket
            .set_nonblocking(false)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        let result = self.socket.recv_from(buffer).map_err(UdpServiceError::ReceiveFailed);
        self.socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        result
    }

    /// Receive one queued UDP datagram without blocking.
    pub fn try_recv_from(&self, buffer: &mut [u8]) -> Result<Option<(usize, SocketAddr)>, UdpServiceError> {
        match self.socket.recv_from(buffer) {
            Ok(received) => Ok(Some(received)),
            Err(error) if error.kind() == ErrorKind::WouldBlock => Ok(None),
            Err(error) => Err(UdpServiceError::ReceiveFailed(error)),
        }
    }

    /// Drain queued service datagrams through a borrowed-payload callback.
    pub fn drain_datagrams<E, F>(
        &self,
        max_datagrams: usize,
        mut handle: F,
    ) -> Result<Result<usize, E>, UdpServiceError>
    where
        F: FnMut(&[u8], SocketAddr) -> Result<(), E>,
    {
        drain_udp_socket(&self.socket, max_datagrams, |datagram| {
            handle(datagram.payload, datagram.source)
        })
        .map_err(UdpServiceError::ReceiveFailed)
    }

    /// Receive one queued reply from any peer-scoped app-facing source socket.
    pub fn try_recv_peer_scoped(&self, buffer: &mut [u8]) -> Result<Option<(u32, usize, SocketAddr)>, UdpServiceError> {
        for (peer_scope, socket) in &self.peer_sources {
            let result = match socket.recv_from(buffer) {
                Ok((length, source)) => Ok(Some((*peer_scope, length, source))),
                Err(error) if error.kind() == ErrorKind::WouldBlock => Ok(None),
                Err(error) => Err(UdpServiceError::ReceiveFailed(error)),
            };
            if result.as_ref().is_ok_and(Option::is_some) {
                return result;
            }
        }
        Ok(None)
    }

    /// Emit one UDP datagram to this service's configured target.
    pub fn emit_to_target(&self, payload: &[u8]) -> Result<usize, UdpServiceError> {
        self.emit_to(payload, self.config.target())
    }

    /// Emit one UDP datagram to a caller-selected app-facing target.
    pub fn emit_to(&self, payload: &[u8], target: SocketAddr) -> Result<usize, UdpServiceError> {
        self.socket
            .send_to(payload, target)
            .map_err(UdpServiceError::SendFailed)
    }

    /// Emit many UDP datagrams to one target with batched syscalls where available.
    pub fn emit_many_to(&self, payloads: &[&[u8]], target: SocketAddr) -> Result<usize, UdpServiceError> {
        send_udp_many(&self.socket, target, payloads).map_err(UdpServiceError::SendFailed)
    }

    /// Emit from a peer-specific local UDP source so app replies map back to that peer.
    pub fn emit_to_from_peer_source(
        &mut self,
        peer_scope: u32,
        payload: &[u8],
        target: SocketAddr,
    ) -> Result<(usize, SocketAddr), UdpServiceError> {
        if !self.peer_sources.contains_key(&peer_scope) {
            let source = self.bind_peer_source(target)?;
            self.peer_sources.insert(peer_scope, source);
        }
        let socket = self
            .peer_sources
            .get(&peer_scope)
            .ok_or(UdpServiceError::MissingPeerSource { peer_scope })?;
        let sent = socket.send_to(payload, target).map_err(UdpServiceError::SendFailed)?;
        let local_addr = socket.local_addr().map_err(UdpServiceError::LocalAddrFailed)?;
        Ok((sent, local_addr))
    }

    fn from_bound_socket(config: UdpServiceConfig, socket: UdpSocket) -> Result<Self, UdpServiceError> {
        request_udp_socket_buffers(&socket);
        // Tests and future supervisors should fail predictably instead of
        // blocking forever when a service is miswired.
        socket
            .set_read_timeout(Some(Duration::from_millis(500)))
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;

        Ok(Self {
            config,
            socket,
            peer_sources: HashMap::new(),
        })
    }

    fn bind_peer_source(&self, target: SocketAddr) -> Result<UdpSocket, UdpServiceError> {
        let bind_addr = SocketAddr::new(target.ip(), 0);
        let socket = UdpSocket::bind(bind_addr).map_err(UdpServiceError::BindFailed)?;
        request_udp_socket_buffers(&socket);
        socket
            .set_read_timeout(Some(Duration::from_millis(500)))
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        Ok(socket)
    }

    fn clone_peer_sources(&self) -> Result<HashMap<u32, UdpSocket>, UdpServiceError> {
        self.peer_sources
            .iter()
            .map(|(peer_scope, socket)| {
                let cloned = socket.try_clone().map_err(UdpServiceError::CloneFailed)?;
                Ok((*peer_scope, cloned))
            })
            .collect()
    }
}

#[cfg(unix)]
fn request_udp_socket_buffers(socket: &UdpSocket) {
    // TODO(perf): Best effort only. The Debian compatibility layer should make
    // host sysctl caps visible, but Rust should still request enough queue
    // depth for high-rate userland UDP service and carrier sockets.
    let value = UDP_SOCKET_BUFFER_BYTES as libc::c_int;
    let value_ptr = (&value as *const libc::c_int).cast::<libc::c_void>();
    let value_len = std::mem::size_of_val(&value) as libc::socklen_t;
    unsafe {
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            value_ptr,
            value_len,
        );
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            value_ptr,
            value_len,
        );
    }
}

#[cfg(not(unix))]
fn request_udp_socket_buffers(_socket: &UdpSocket) {}

/// Errors for the first userland UDP service layer.
#[derive(Debug)]
pub enum UdpServiceError {
    EmptyServiceName,
    MissingListenAddress,
    DuplicateServiceName(String),
    DuplicateListenAddress(SocketAddr),
    DuplicateServiceId(ServiceId),
    ReservedServiceId {
        service: String,
        service_id: ServiceId,
    },
    TooManyServices {
        count: usize,
        max: usize,
    },
    DuplicatePathId(PathId),
    MissingPath,
    MissingPathTransport {
        path_id: PathId,
    },
    MissingPathRemote {
        path_id: PathId,
    },
    MissingPeerSource {
        peer_scope: u32,
    },
    PathMtuTooSmall {
        path_id: PathId,
        mtu: usize,
    },
    PathWeightTooSmall {
        path_id: PathId,
    },
    PathSchedulerPrimitiveInvalid {
        path_id: PathId,
        field: &'static str,
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
    RelayHopWrapFailed,
}

impl fmt::Display for UdpServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyServiceName => write!(formatter, "service name must not be empty"),
            Self::MissingListenAddress => write!(formatter, "service requires a listen address"),
            Self::DuplicateServiceName(name) => write!(formatter, "duplicate service name: {name}"),
            Self::DuplicateListenAddress(addr) => write!(formatter, "duplicate UDP listen address: {addr}"),
            Self::DuplicateServiceId(service_id) => write!(formatter, "duplicate service id: {service_id}"),
            Self::ReservedServiceId { service, service_id } => {
                write!(formatter, "service {service} uses reserved Gatherlink service id: {service_id}")
            }
            Self::TooManyServices { count, max } => {
                write!(formatter, "too many UDP services: {count} configured, maximum user services is {max}")
            }
            Self::DuplicatePathId(path_id) => write!(formatter, "duplicate path id: {path_id}"),
            Self::MissingPath => write!(formatter, "runtime config requires at least one path"),
            Self::MissingPathTransport { path_id } => write!(
                formatter,
                "path {path_id} has no bound transport socket for encoded frame delivery"
            ),
            Self::MissingPathRemote { path_id } => {
                write!(formatter, "path {path_id} has no configured or learned remote transport endpoint")
            }
            Self::MissingPeerSource { peer_scope } => {
                write!(formatter, "missing peer-scoped app source socket for peer scope {peer_scope}")
            }
            Self::PathMtuTooSmall { path_id, mtu } => {
                write!(formatter, "path {path_id} MTU {mtu} is too small for Gatherlink framing")
            }
            Self::PathWeightTooSmall { path_id } => {
                write!(formatter, "path {path_id} scheduler weight must be greater than zero")
            }
            Self::PathSchedulerPrimitiveInvalid { path_id, field } => {
                write!(formatter, "path {path_id} scheduler primitive {field} is invalid")
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
            Self::RelayHopWrapFailed => write!(formatter, "failed to wrap endpoint packet in relay-hop envelope"),
        }
    }
}

impl std::error::Error for UdpServiceError {}
