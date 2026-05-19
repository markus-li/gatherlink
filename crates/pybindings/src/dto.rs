//! Python-facing DTOs for the narrow Rust dataplane API.
//!
//! These are transport/runtime DTOs, not user config models. Python still owns
//! config validation and expansion before values reach this bridge.

use std::net::SocketAddr;

use gatherlink_dataplane::engine::{ForwardOutcome, ReapplyOutcome, RemoteDeliverOutcome, ReservedServiceEvent};
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, PathSchedulerPrimitives, PathSchedulerState, SchedulerConfig, SchedulerMode,
    TransportSecurityConfig, TransportSecuritySessionConfig,
};
use gatherlink_dataplane::udp_service::{ServiceReturnMode, ServiceSchedulerConfig, UdpServiceConfig};
use pyo3::prelude::*;

use crate::errors::udp_error_to_py;

/// Python DTO for one compiled core UDP service.
#[pyclass(name = "UdpServiceConfig")]
#[derive(Clone)]
pub struct PyUdpServiceConfig {
    inner: UdpServiceConfig,
}

#[pymethods]
impl PyUdpServiceConfig {
    /// Create a compiled UDP service config from explicit socket addresses.
    #[new]
    #[pyo3(signature = (
        name,
        target,
        listen = None,
        priority = 100,
        return_mode = "fixed",
        service_id = 0,
        scheduler_fanout = 1,
        scheduler_fanout_below_bytes = 0,
    ))]
    pub fn new(
        name: String,
        target: String,
        listen: Option<String>,
        priority: u16,
        return_mode: &str,
        service_id: u16,
        scheduler_fanout: u16,
        scheduler_fanout_below_bytes: usize,
    ) -> PyResult<Self> {
        let target_addr = target
            .parse::<SocketAddr>()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
        let listen_addr = listen
            .map(|value| value.parse::<SocketAddr>())
            .transpose()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;

        let return_mode = parse_service_return_mode(return_mode)?;
        let scheduler = ServiceSchedulerConfig::new(scheduler_fanout, scheduler_fanout_below_bytes);
        let inner = UdpServiceConfig::new_with_scheduler(
            service_id,
            name,
            listen_addr,
            target_addr,
            priority,
            return_mode,
            scheduler,
        )
        .map_err(udp_error_to_py)?;
        Ok(Self { inner })
    }

    /// Compact service id carried in Gatherlink frames.
    pub fn service_id(&self) -> u16 {
        self.inner.service_id()
    }

    /// Stable service name.
    pub fn name(&self) -> String {
        self.inner.name().to_owned()
    }

    /// Listen socket address string, if configured.
    pub fn listen(&self) -> Option<String> {
        self.inner.listen().map(|addr| addr.to_string())
    }

    /// Target socket address string.
    pub fn target(&self) -> String {
        self.inner.target().to_string()
    }

    /// Compiled service priority value.
    pub fn priority(&self) -> u16 {
        self.inner.priority()
    }

    /// Configured return behavior for remote payloads.
    pub fn return_mode(&self) -> String {
        format_service_return_mode(self.inner.return_mode()).to_owned()
    }

    /// Service-level fanout selected by Python. Zero means every eligible path.
    pub fn scheduler_fanout(&self) -> u16 {
        self.inner.scheduler().fanout()
    }

    /// Payload threshold for service fanout, or zero when fanout always applies.
    pub fn scheduler_fanout_below_bytes(&self) -> usize {
        self.inner.scheduler().fanout_below_bytes()
    }
}

impl PyUdpServiceConfig {
    pub(crate) fn inner(&self) -> UdpServiceConfig {
        self.inner.clone()
    }
}

fn parse_service_return_mode(mode: &str) -> PyResult<ServiceReturnMode> {
    match mode {
        "fixed" => Ok(ServiceReturnMode::Fixed),
        "learned-single-source" => Ok(ServiceReturnMode::LearnedSingleSource),
        "peer-scoped-source" => Ok(ServiceReturnMode::PeerScopedSource),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown service return mode: {other}"
        ))),
    }
}

fn format_service_return_mode(mode: ServiceReturnMode) -> &'static str {
    match mode {
        ServiceReturnMode::Fixed => "fixed",
        ServiceReturnMode::LearnedSingleSource => "learned-single-source",
        ServiceReturnMode::PeerScopedSource => "peer-scoped-source",
    }
}

/// Python DTO for one compiled runtime path.
#[pyclass(name = "PathConfig")]
#[derive(Clone)]
pub struct PyPathConfig {
    inner: CorePathConfig,
}

#[pymethods]
impl PyPathConfig {
    /// Create a compiled path config from explicit wire ids and an encoded-frame MTU.
    #[new]
    #[pyo3(signature = (
        path_id,
        mtu,
        busy = false,
        enabled = true,
        state = "active",
        weight = 1,
        tx_capacity_bps = None,
        rx_capacity_bps = None,
        latency_us = None,
        loss_ppm = 0,
        reorder_hold_us = 0,
        max_in_flight_packets = 0,
        max_in_flight_bytes = 0,
        transport_bind = None,
        transport_remote = None,
        relay_receiver_index = None,
        relay_send_key = None,
    ))]
    pub fn new(
        path_id: u16,
        mtu: usize,
        busy: bool,
        enabled: bool,
        state: &str,
        weight: u16,
        tx_capacity_bps: Option<u64>,
        rx_capacity_bps: Option<u64>,
        latency_us: Option<u32>,
        loss_ppm: u32,
        reorder_hold_us: u32,
        max_in_flight_packets: u16,
        max_in_flight_bytes: u32,
        transport_bind: Option<String>,
        transport_remote: Option<String>,
        relay_receiver_index: Option<u32>,
        relay_send_key: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        let state = if busy {
            PathSchedulerState::Busy
        } else {
            parse_path_scheduler_state(state)?
        };
        let primitives = PathSchedulerPrimitives::new(
            tx_capacity_bps,
            rx_capacity_bps,
            latency_us,
            loss_ppm,
            reorder_hold_us,
            max_in_flight_packets,
            max_in_flight_bytes,
        );
        let mut inner = CorePathConfig::new_with_scheduler_primitives(path_id, mtu, enabled, state, weight, primitives)
            .map_err(udp_error_to_py)?;
        if let Some(bind) = transport_bind {
            let bind_addr = bind
                .parse::<SocketAddr>()
                .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
            if let Some(remote) = transport_remote {
                let remote_addr = remote
                    .parse::<SocketAddr>()
                    .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
                inner = inner.with_transport(bind_addr, remote_addr);
            } else {
                inner = inner.with_transport_bind(bind_addr);
            }
        }
        if relay_receiver_index.is_some() || relay_send_key.is_some() {
            let receiver_index = relay_receiver_index.ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("relay_receiver_index is required with relay_send_key")
            })?;
            let key =
                relay_send_key.ok_or_else(|| pyo3::exceptions::PyValueError::new_err("relay_send_key is required"))?;
            let key: [u8; 32] = key
                .as_slice()
                .try_into()
                .map_err(|_| pyo3::exceptions::PyValueError::new_err("relay_send_key must contain 32 raw bytes"))?;
            inner = inner.with_relay_send(receiver_index, key);
        }
        Ok(Self { inner })
    }

    pub fn path_id(&self) -> u16 {
        self.inner.path_id()
    }

    pub fn mtu(&self) -> usize {
        self.inner.mtu()
    }

    pub fn busy(&self) -> bool {
        self.inner.busy()
    }

    pub fn enabled(&self) -> bool {
        self.inner.enabled()
    }

    pub fn state(&self) -> String {
        format_path_scheduler_state(self.inner.state()).to_owned()
    }

    pub fn weight(&self) -> u16 {
        self.inner.weight()
    }

    pub fn tx_capacity_bps(&self) -> Option<u64> {
        self.inner.primitives().tx_capacity_bps()
    }

    pub fn rx_capacity_bps(&self) -> Option<u64> {
        self.inner.primitives().rx_capacity_bps()
    }

    pub fn latency_us(&self) -> Option<u32> {
        self.inner.primitives().latency_us()
    }

    pub fn loss_ppm(&self) -> u32 {
        self.inner.primitives().loss_ppm()
    }

    pub fn reorder_hold_us(&self) -> u32 {
        self.inner.primitives().reorder_hold_us()
    }

    pub fn max_in_flight_packets(&self) -> u16 {
        self.inner.primitives().max_in_flight_packets()
    }

    pub fn max_in_flight_bytes(&self) -> u32 {
        self.inner.primitives().max_in_flight_bytes()
    }

    pub fn transport_bind(&self) -> Option<String> {
        self.inner.transport_bind().map(|addr| addr.to_string())
    }

    pub fn transport_remote(&self) -> Option<String> {
        self.inner.transport_remote().map(|addr| addr.to_string())
    }
}

impl PyPathConfig {
    pub(crate) fn inner(&self) -> CorePathConfig {
        self.inner.clone()
    }
}

fn parse_path_scheduler_state(state: &str) -> PyResult<PathSchedulerState> {
    match state {
        "active" => Ok(PathSchedulerState::Active),
        "busy" => Ok(PathSchedulerState::Busy),
        "drain" => Ok(PathSchedulerState::Drain),
        "disabled" => Ok(PathSchedulerState::Disabled),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown path scheduler state: {other}"
        ))),
    }
}

fn format_path_scheduler_state(state: PathSchedulerState) -> &'static str {
    match state {
        PathSchedulerState::Active => "active",
        PathSchedulerState::Busy => "busy",
        PathSchedulerState::Drain => "drain",
        PathSchedulerState::Disabled => "disabled",
    }
}

/// Python DTO for compiled scheduler state.
#[pyclass(name = "SchedulerConfig")]
#[derive(Clone)]
pub struct PySchedulerConfig {
    inner: SchedulerConfig,
}

#[pymethods]
impl PySchedulerConfig {
    /// Create a scheduler config selected by Python policy.
    #[new]
    #[pyo3(signature = (mode = "round_robin"))]
    pub fn new(mode: &str) -> PyResult<Self> {
        let mode = match mode {
            "round_robin" => SchedulerMode::RoundRobin,
            "weighted_round_robin" => SchedulerMode::WeightedRoundRobin,
            "lowest_latency" => SchedulerMode::LowestLatency,
            "loss_aware" => SchedulerMode::LossAware,
            "capacity_aware" => SchedulerMode::CapacityAware,
            "least_queue" => SchedulerMode::LeastQueue,
            "earliest_completion_first" => SchedulerMode::EarliestCompletionFirst,
            "blocking_estimation" => SchedulerMode::BlockingEstimation,
            "balanced" => SchedulerMode::Balanced,
            "adaptive" => SchedulerMode::Adaptive,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown scheduler mode: {other}"
                )));
            }
        };
        Ok(Self {
            inner: SchedulerConfig::new(mode),
        })
    }

    pub fn mode(&self) -> String {
        match self.inner.mode() {
            SchedulerMode::RoundRobin => "round_robin".to_owned(),
            SchedulerMode::WeightedRoundRobin => "weighted_round_robin".to_owned(),
            SchedulerMode::LowestLatency => "lowest_latency".to_owned(),
            SchedulerMode::LossAware => "loss_aware".to_owned(),
            SchedulerMode::CapacityAware => "capacity_aware".to_owned(),
            SchedulerMode::LeastQueue => "least_queue".to_owned(),
            SchedulerMode::EarliestCompletionFirst => "earliest_completion_first".to_owned(),
            SchedulerMode::BlockingEstimation => "blocking_estimation".to_owned(),
            SchedulerMode::Balanced => "balanced".to_owned(),
            SchedulerMode::Adaptive => "adaptive".to_owned(),
        }
    }
}

impl PySchedulerConfig {
    pub(crate) fn inner(&self) -> SchedulerConfig {
        self.inner
    }
}

/// Python DTO for one static secure transport session.
#[pyclass(name = "TransportSecuritySessionConfig")]
#[derive(Clone)]
pub struct PyTransportSecuritySessionConfig {
    inner: TransportSecuritySessionConfig,
}

#[pymethods]
impl PyTransportSecuritySessionConfig {
    /// Create one Python-compiled static transport session.
    #[new]
    #[pyo3(signature = (
        local_receiver_index,
        remote_receiver_index,
        send_key,
        receive_key,
        service_ids = None,
    ))]
    pub fn new(
        local_receiver_index: u32,
        remote_receiver_index: u32,
        send_key: &[u8],
        receive_key: &[u8],
        service_ids: Option<Vec<u16>>,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: TransportSecuritySessionConfig::new(
                local_receiver_index,
                remote_receiver_index,
                key_bytes(send_key, "send_key")?,
                key_bytes(receive_key, "receive_key")?,
                service_ids.unwrap_or_default(),
            ),
        })
    }

    pub fn local_receiver_index(&self) -> u32 {
        self.inner.local_receiver_index()
    }

    pub fn remote_receiver_index(&self) -> u32 {
        self.inner.remote_receiver_index()
    }

    pub fn service_ids(&self) -> Vec<u16> {
        self.inner.service_ids().to_vec()
    }
}

impl PyTransportSecuritySessionConfig {
    pub(crate) fn inner(&self) -> TransportSecuritySessionConfig {
        self.inner.clone()
    }
}

/// Python DTO for compiled transport-security state.
#[pyclass(name = "TransportSecurityConfig")]
#[derive(Clone)]
pub struct PyTransportSecurityConfig {
    inner: TransportSecurityConfig,
}

#[pymethods]
impl PyTransportSecurityConfig {
    /// Plain path-socket frames. This is intentionally explicit for lab use.
    #[staticmethod]
    pub fn none() -> Self {
        Self {
            inner: TransportSecurityConfig::None,
        }
    }

    /// Static authenticated encryption material compiled by Python.
    #[staticmethod]
    pub fn static_keys(receiver_index: u32, send_key: &[u8], receive_key: &[u8]) -> PyResult<Self> {
        Self::static_keys_v2(receiver_index, receiver_index, send_key, receive_key)
    }

    /// Static authenticated encryption material with distinct local/remote receiver indexes.
    #[staticmethod]
    pub fn static_keys_v2(
        local_receiver_index: u32,
        remote_receiver_index: u32,
        send_key: &[u8],
        receive_key: &[u8],
    ) -> PyResult<Self> {
        Ok(Self {
            inner: TransportSecurityConfig::Static {
                local_receiver_index,
                remote_receiver_index,
                send_key: key_bytes(send_key, "send_key")?,
                receive_key: key_bytes(receive_key, "receive_key")?,
            },
        })
    }

    /// Multiple static sessions sharing the same sink carrier sockets.
    #[staticmethod]
    pub fn static_sessions(sessions: Vec<PyTransportSecuritySessionConfig>) -> PyResult<Self> {
        Ok(Self {
            inner: TransportSecurityConfig::StaticSessions(
                sessions.into_iter().map(|session| session.inner()).collect(),
            ),
        })
    }

    pub fn mode(&self) -> String {
        match self.inner {
            TransportSecurityConfig::None => "none".to_owned(),
            TransportSecurityConfig::Static { .. } | TransportSecurityConfig::StaticSessions(_) => "static".to_owned(),
        }
    }
}

impl PyTransportSecurityConfig {
    pub(crate) fn inner(&self) -> TransportSecurityConfig {
        self.inner.clone()
    }
}

fn key_bytes(value: &[u8], field: &str) -> PyResult<[u8; 32]> {
    value
        .try_into()
        .map_err(|_| pyo3::exceptions::PyValueError::new_err(format!("{field} must be exactly 32 bytes")))
}

/// Observable result from one forwarded datagram.
#[pyclass(name = "ForwardOutcome")]
#[derive(Clone)]
pub struct PyForwardOutcome {
    inner: ForwardOutcome,
}

#[pymethods]
impl PyForwardOutcome {
    pub fn service(&self) -> String {
        self.inner.service.clone()
    }

    pub fn source(&self) -> String {
        self.inner.source.to_string()
    }

    pub fn target(&self) -> String {
        self.inner.target.to_string()
    }

    pub fn payload_len(&self) -> usize {
        self.inner.payload_len
    }

    pub fn sequence(&self) -> u64 {
        self.inner.sequence
    }

    pub fn path_id(&self) -> u16 {
        self.inner.path_id
    }

    pub fn frame_count(&self) -> usize {
        self.inner.frame_count
    }

    pub fn batch_count(&self) -> usize {
        self.inner.batch_count
    }

    pub fn fragment_count(&self) -> usize {
        self.inner.fragment_count
    }
}

impl From<ForwardOutcome> for PyForwardOutcome {
    fn from(inner: ForwardOutcome) -> Self {
        Self { inner }
    }
}

/// Observable result from one remote Gatherlink frame delivered locally.
#[pyclass(name = "RemoteDeliverOutcome")]
#[derive(Clone)]
pub struct PyRemoteDeliverOutcome {
    inner: RemoteDeliverOutcome,
}

#[pymethods]
impl PyRemoteDeliverOutcome {
    pub fn service(&self) -> String {
        self.inner.service.clone()
    }

    pub fn target(&self) -> String {
        self.inner.target.to_string()
    }

    pub fn payload_len(&self) -> usize {
        self.inner.payload_len
    }

    pub fn sequence(&self) -> u64 {
        self.inner.sequence
    }

    pub fn path_id(&self) -> u16 {
        self.inner.path_id
    }
}

impl From<RemoteDeliverOutcome> for PyRemoteDeliverOutcome {
    fn from(inner: RemoteDeliverOutcome) -> Self {
        Self { inner }
    }
}

/// Reserved Gatherlink service payload for Python-owned decoding.
#[pyclass(name = "ReservedServiceEvent")]
#[derive(Clone)]
pub struct PyReservedServiceEvent {
    inner: ReservedServiceEvent,
}

#[pymethods]
impl PyReservedServiceEvent {
    pub fn service_id(&self) -> u16 {
        self.inner.service_id
    }

    pub fn path_id(&self) -> u16 {
        self.inner.path_id
    }

    pub fn sequence(&self) -> u64 {
        self.inner.sequence
    }

    pub fn payload(&self) -> Vec<u8> {
        self.inner.payload.clone()
    }

    pub fn frame_bytes(&self) -> usize {
        self.inner.frame_bytes
    }

    pub fn peer_scope(&self) -> Option<u32> {
        self.inner.peer_scope
    }
}

impl From<ReservedServiceEvent> for PyReservedServiceEvent {
    fn from(inner: ReservedServiceEvent) -> Self {
        Self { inner }
    }
}

/// Summary returned when Python reapplies compiled runtime config.
#[pyclass(name = "ReapplyOutcome")]
#[derive(Clone)]
pub struct PyReapplyOutcome {
    inner: ReapplyOutcome,
}

#[pymethods]
impl PyReapplyOutcome {
    pub fn unchanged(&self) -> usize {
        self.inner.unchanged
    }

    pub fn updated(&self) -> usize {
        self.inner.updated
    }

    pub fn rebound(&self) -> usize {
        self.inner.rebound
    }

    pub fn added(&self) -> usize {
        self.inner.added
    }

    pub fn removed(&self) -> usize {
        self.inner.removed
    }
}

impl From<ReapplyOutcome> for PyReapplyOutcome {
    fn from(inner: ReapplyOutcome) -> Self {
        Self { inner }
    }
}
