//! Python-facing DTOs for the narrow Rust dataplane API.
//!
//! These are transport/runtime DTOs, not user config models. Python still owns
//! config validation and expansion before values reach this bridge.

use std::net::SocketAddr;

use gatherlink_dataplane::engine::{ForwardOutcome, ReapplyOutcome};
use gatherlink_dataplane::runtime_config::{
    CorePathConfig, PathSchedulerPrimitives, PathSchedulerState, SchedulerConfig, SchedulerMode,
};
use gatherlink_dataplane::udp_service::UdpServiceConfig;
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
    #[pyo3(signature = (name, target, listen = None, priority = 100))]
    pub fn new(name: String, target: String, listen: Option<String>, priority: u16) -> PyResult<Self> {
        let target_addr = target
            .parse::<SocketAddr>()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
        let listen_addr = listen
            .map(|value| value.parse::<SocketAddr>())
            .transpose()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;

        let inner =
            UdpServiceConfig::new_with_priority(name, listen_addr, target_addr, priority).map_err(udp_error_to_py)?;
        Ok(Self { inner })
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
}

impl PyUdpServiceConfig {
    pub(crate) fn inner(&self) -> UdpServiceConfig {
        self.inner.clone()
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
        route_id = 0,
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
    ))]
    pub fn new(
        path_id: u16,
        mtu: usize,
        route_id: u16,
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
        let inner =
            CorePathConfig::new_with_scheduler_primitives(path_id, route_id, mtu, enabled, state, weight, primitives)
                .map_err(udp_error_to_py)?;
        Ok(Self { inner })
    }

    pub fn path_id(&self) -> u16 {
        self.inner.path_id()
    }

    pub fn route_id(&self) -> u16 {
        self.inner.route_id()
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
        }
    }
}

impl PySchedulerConfig {
    pub(crate) fn inner(&self) -> SchedulerConfig {
        self.inner
    }
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
