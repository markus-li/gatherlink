//! Runtime config accepted by the Rust dataplane.
//!
//! Python owns user config parsing, validation, expansion, and policy. This
//! module only models the already-compiled core data Rust needs to execute the
//! first userland UDP transport target.

use std::collections::HashSet;
use std::net::SocketAddr;

use gatherlink_protocol::frame::{FRAGMENT_METADATA_LEN, V1_HEADER_LEN};
use gatherlink_protocol::ids::{PathId, ServiceId, USER_SERVICE_ID_START};

use crate::udp_service::{UdpServiceConfig, UdpServiceError};

/// Default path MTU used by early userland tests until Python supplies real path plans.
pub const DEFAULT_PATH_MTU: usize = 1200;

/// Core runtime config for the userland UDP dataplane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreRuntimeConfig {
    services: Vec<UdpServiceConfig>,
    paths: Vec<CorePathConfig>,
    scheduler: SchedulerConfig,
    security: TransportSecurityConfig,
}

/// Transport-security state already compiled by Python.
///
/// Rust does not decide whether a peer is trusted. It only executes the packet
/// protection parameters that Python handed over after config/handshake policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportSecurityConfig {
    /// Plain Gatherlink frames on path sockets. This remains useful for labs.
    None,
    /// Static authenticated encryption material for early labs and future
    /// Python-compiled handshakes. Production handshakes should compile into
    /// this same low-level shape rather than teaching Rust identity policy.
    Static {
        receiver_index: u32,
        send_key: [u8; 32],
        receive_key: [u8; 32],
    },
}

impl Default for TransportSecurityConfig {
    fn default() -> Self {
        Self::None
    }
}

impl TransportSecurityConfig {
    /// Return the packet overhead added outside an encoded Gatherlink frame.
    pub fn packet_overhead(&self) -> usize {
        match self {
            Self::None => 0,
            Self::Static { .. } => {
                gatherlink_crypto::envelope::ENCRYPTED_DATA_HEADER_LEN + gatherlink_crypto::envelope::AEAD_TAG_LEN
            }
        }
    }
}

/// Minimal compiled scheduler config executed by Rust.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchedulerConfig {
    mode: SchedulerMode,
}

impl SchedulerConfig {
    /// Build a scheduler config from a mode already chosen by Python.
    pub fn new(mode: SchedulerMode) -> Self {
        Self { mode }
    }

    /// Return the scheduler mode Rust should execute.
    pub fn mode(&self) -> SchedulerMode {
        self.mode
    }
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        Self::new(SchedulerMode::RoundRobin)
    }
}

/// Scheduler modes chosen by Python and executed with already-compiled primitives.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchedulerMode {
    RoundRobin,
    WeightedRoundRobin,
    LowestLatency,
    LossAware,
    CapacityAware,
    LeastQueue,
    EarliestCompletionFirst,
    BlockingEstimation,
    Balanced,
    Adaptive,
}

/// Compiled per-path scheduler state. Python owns the policy that produces this.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathSchedulerState {
    Active,
    Busy,
    Drain,
    Disabled,
}

/// Primitive per-path scheduler facts already reduced by Python policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PathSchedulerPrimitives {
    tx_capacity_bps: Option<u64>,
    rx_capacity_bps: Option<u64>,
    latency_us: Option<u32>,
    loss_ppm: u32,
    reorder_hold_us: u32,
    max_in_flight_packets: u16,
    max_in_flight_bytes: u32,
}

impl PathSchedulerPrimitives {
    /// Build primitive scheduler facts from values that are cheap for Rust to follow.
    pub fn new(
        tx_capacity_bps: Option<u64>,
        rx_capacity_bps: Option<u64>,
        latency_us: Option<u32>,
        loss_ppm: u32,
        reorder_hold_us: u32,
        max_in_flight_packets: u16,
        max_in_flight_bytes: u32,
    ) -> Self {
        Self {
            tx_capacity_bps,
            rx_capacity_bps,
            latency_us,
            loss_ppm,
            reorder_hold_us,
            max_in_flight_packets,
            max_in_flight_bytes,
        }
    }

    /// Python's local-view transmit capacity estimate for this path.
    pub fn tx_capacity_bps(&self) -> Option<u64> {
        self.tx_capacity_bps
    }

    /// Python's local-view receive capacity estimate for this path.
    pub fn rx_capacity_bps(&self) -> Option<u64> {
        self.rx_capacity_bps
    }

    /// Python-selected latency estimate used by compiled scheduling policy.
    pub fn latency_us(&self) -> Option<u32> {
        self.latency_us
    }

    /// Loss estimate in parts per million, already smoothed by Python.
    pub fn loss_ppm(&self) -> u32 {
        self.loss_ppm
    }

    /// Reorder hold time that Python wants Rust to apply for this path.
    pub fn reorder_hold_us(&self) -> u32 {
        self.reorder_hold_us
    }

    /// Packet concurrency limit selected by Python, or zero when unlimited.
    pub fn max_in_flight_packets(&self) -> u16 {
        self.max_in_flight_packets
    }

    /// Byte concurrency limit selected by Python, or zero when unlimited.
    pub fn max_in_flight_bytes(&self) -> u32 {
        self.max_in_flight_bytes
    }
}

/// Already-compiled path runtime data handed to Rust by the Python control plane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CorePathConfig {
    path_id: PathId,
    mtu: usize,
    transport_bind: Option<SocketAddr>,
    transport_remote: Option<SocketAddr>,
    enabled: bool,
    state: PathSchedulerState,
    weight: u16,
    primitives: PathSchedulerPrimitives,
}

impl CorePathConfig {
    /// Create a path with a compact wire id, MTU, and current capacity hint.
    ///
    /// The `busy` flag is intentionally simple for now. Python can later replace it with richer live capacity state
    /// without changing the packet code that decides whether to fragment onto a less-busy path.
    pub fn new(path_id: PathId, mtu: usize, busy: bool) -> Result<Self, UdpServiceError> {
        let state = if busy {
            PathSchedulerState::Busy
        } else {
            PathSchedulerState::Active
        };
        Self::new_with_scheduler(path_id, mtu, true, state, 1)
    }

    /// Create a path with explicit compiled scheduler state.
    pub fn new_with_scheduler(
        path_id: PathId,
        mtu: usize,
        enabled: bool,
        state: PathSchedulerState,
        weight: u16,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_scheduler_primitives(path_id, mtu, enabled, state, weight, PathSchedulerPrimitives::default())
    }

    /// Create a path with explicit compiled scheduler state and primitive facts.
    pub fn new_with_scheduler_primitives(
        path_id: PathId,
        mtu: usize,
        enabled: bool,
        state: PathSchedulerState,
        weight: u16,
        primitives: PathSchedulerPrimitives,
    ) -> Result<Self, UdpServiceError> {
        if mtu <= V1_HEADER_LEN + FRAGMENT_METADATA_LEN {
            return Err(UdpServiceError::PathMtuTooSmall { path_id, mtu });
        }
        if weight == 0 {
            return Err(UdpServiceError::PathWeightTooSmall { path_id });
        }
        if primitives.loss_ppm() > 1_000_000 {
            return Err(UdpServiceError::PathSchedulerPrimitiveInvalid {
                path_id,
                field: "loss_ppm",
            });
        }

        Ok(Self {
            path_id,
            mtu,
            transport_bind: None,
            transport_remote: None,
            enabled,
            state,
            weight,
            primitives,
        })
    }

    /// Wire path id carried in every frame.
    pub fn path_id(&self) -> PathId {
        self.path_id
    }

    /// Maximum encoded frame size for this path.
    pub fn mtu(&self) -> usize {
        self.mtu
    }

    /// Local path transport socket bind address, when this path carries real frames.
    pub fn transport_bind(&self) -> Option<SocketAddr> {
        self.transport_bind
    }

    /// Remote path transport address, when this path carries real frames.
    pub fn transport_remote(&self) -> Option<SocketAddr> {
        self.transport_remote
    }

    /// Return a copy with production path transport endpoints attached.
    pub fn with_transport(mut self, bind: SocketAddr, remote: SocketAddr) -> Self {
        self.transport_bind = Some(bind);
        self.transport_remote = Some(remote);
        self
    }

    /// Capacity hint compiled by Python from live path telemetry.
    pub fn busy(&self) -> bool {
        self.state == PathSchedulerState::Busy
    }

    /// Return whether this path can be selected for new work.
    pub fn enabled(&self) -> bool {
        self.enabled && self.state != PathSchedulerState::Disabled
    }

    /// Return the compiled path scheduler state.
    pub fn state(&self) -> PathSchedulerState {
        self.state
    }

    /// Return the compiled round-robin weight.
    pub fn weight(&self) -> u16 {
        self.weight
    }

    /// Return the primitive scheduler facts Python compiled for Rust.
    pub fn primitives(&self) -> PathSchedulerPrimitives {
        self.primitives
    }

    /// Return whether this path is preferred for ordinary whole-packet sends.
    pub fn accepts_whole_packet(&self) -> bool {
        self.enabled() && self.state == PathSchedulerState::Active
    }

    /// Return whether this path can carry fragments or drain-mode traffic.
    pub fn accepts_fragmented_packet(&self) -> bool {
        self.enabled() && matches!(self.state, PathSchedulerState::Active | PathSchedulerState::Drain)
    }

    /// Maximum data payload bytes that fit without fragmentation.
    pub fn max_data_payload(&self) -> usize {
        self.mtu - V1_HEADER_LEN
    }

    /// Maximum fragment payload bytes once the fragment extension is present.
    pub fn max_fragment_payload(&self) -> usize {
        self.mtu - V1_HEADER_LEN - FRAGMENT_METADATA_LEN
    }
}

impl CoreRuntimeConfig {
    /// Build a core runtime config from already-validated UDP services.
    pub fn new(services: Vec<UdpServiceConfig>) -> Result<Self, UdpServiceError> {
        Self::new_with_paths(services, vec![CorePathConfig::new(0, DEFAULT_PATH_MTU, false)?])
    }

    /// Build a core runtime config from already-validated UDP services and paths.
    pub fn new_with_paths(
        services: Vec<UdpServiceConfig>,
        paths: Vec<CorePathConfig>,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_paths_and_scheduler(services, paths, SchedulerConfig::default())
    }

    /// Build a core runtime config from already-validated UDP services, paths, and scheduler state.
    pub fn new_with_paths_and_scheduler(
        services: Vec<UdpServiceConfig>,
        paths: Vec<CorePathConfig>,
        scheduler: SchedulerConfig,
    ) -> Result<Self, UdpServiceError> {
        Self::new_with_paths_scheduler_and_security(services, paths, scheduler, TransportSecurityConfig::None)
    }

    /// Build a core runtime config with explicit path, scheduler, and transport security state.
    pub fn new_with_paths_scheduler_and_security(
        services: Vec<UdpServiceConfig>,
        paths: Vec<CorePathConfig>,
        scheduler: SchedulerConfig,
        security: TransportSecurityConfig,
    ) -> Result<Self, UdpServiceError> {
        let max_user_services = usize::from(ServiceId::MAX - USER_SERVICE_ID_START) + 1;
        if services.len() > max_user_services {
            return Err(UdpServiceError::TooManyServices {
                count: services.len(),
                max: max_user_services,
            });
        }

        let mut finalized_services = Vec::with_capacity(services.len());
        let mut names = HashSet::new();
        let mut listens = HashSet::new();
        let mut service_ids = HashSet::new();
        let explicit_service_ids: HashSet<ServiceId> = services
            .iter()
            .map(UdpServiceConfig::service_id)
            .filter(|service_id| *service_id != 0)
            .collect();
        let mut next_service_id = USER_SERVICE_ID_START;
        for service in &services {
            if !names.insert(service.name()) {
                return Err(UdpServiceError::DuplicateServiceName(service.name().to_owned()));
            }

            if let Some(listen) = service.listen() {
                if !listens.insert(listen) {
                    return Err(UdpServiceError::DuplicateListenAddress(listen));
                }
            }

            let service_id = if service.service_id() == 0 {
                while explicit_service_ids.contains(&next_service_id) {
                    next_service_id = next_service_id.checked_add(1).ok_or(UdpServiceError::TooManyServices {
                        count: services.len(),
                        max: max_user_services,
                    })?;
                }
                let assigned = next_service_id;
                next_service_id = next_service_id.checked_add(1).ok_or(UdpServiceError::TooManyServices {
                    count: services.len(),
                    max: max_user_services,
                })?;
                assigned
            } else {
                service.service_id()
            };
            if !service_ids.insert(service_id) {
                return Err(UdpServiceError::DuplicateServiceId(service_id));
            }
            finalized_services.push(service.with_assigned_service_id(service_id)?);
        }

        let mut path_ids = HashSet::new();
        for path in &paths {
            if !path_ids.insert(path.path_id()) {
                return Err(UdpServiceError::DuplicatePathId(path.path_id()));
            }
        }
        if paths.is_empty() {
            return Err(UdpServiceError::MissingPath);
        }

        Ok(Self {
            services: finalized_services,
            paths,
            scheduler,
            security,
        })
    }

    /// Return the service configs in deterministic order.
    pub fn services(&self) -> &[UdpServiceConfig] {
        &self.services
    }

    /// Return path configs in scheduler preference order.
    pub fn paths(&self) -> &[CorePathConfig] {
        &self.paths
    }

    /// Return the compiled scheduler config.
    pub fn scheduler(&self) -> SchedulerConfig {
        self.scheduler
    }

    /// Return compiled transport security state.
    pub fn security(&self) -> &TransportSecurityConfig {
        &self.security
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
