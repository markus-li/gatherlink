//! Python-facing DTOs for the narrow Rust dataplane API.
//!
//! These are transport/runtime DTOs, not user config models. Python still owns
//! config validation and expansion before values reach this bridge.

use std::net::SocketAddr;

use gatherlink_dataplane::engine::{ForwardOutcome, ReapplyOutcome};
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
    #[pyo3(signature = (name, target, listen = None))]
    pub fn new(name: String, target: String, listen: Option<String>) -> PyResult<Self> {
        let target_addr = target
            .parse::<SocketAddr>()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
        let listen_addr = listen
            .map(|value| value.parse::<SocketAddr>())
            .transpose()
            .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;

        let inner =
            UdpServiceConfig::new(name, listen_addr, target_addr).map_err(udp_error_to_py)?;
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
}

impl PyUdpServiceConfig {
    pub(crate) fn inner(&self) -> UdpServiceConfig {
        self.inner.clone()
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
