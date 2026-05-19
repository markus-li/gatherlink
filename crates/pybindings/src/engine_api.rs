//! Python-facing core dataplane API.
//!
//! This bridge intentionally exposes a narrow execution handle. Python can bind
//! compiled services, reapply compiled runtime config, and step the engine for
//! tests, but packet processing stays in Rust.

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::metrics::{ControlCounterSnapshot, ControlMetadataSnapshot, PathControlSnapshot};
use gatherlink_dataplane::runtime_config::CoreRuntimeConfig;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::dto::{
    PyForwardOutcome, PyPathConfig, PyReapplyOutcome, PyRemoteDeliverOutcome, PyReservedServiceEvent,
    PySchedulerConfig, PyTransportSecurityConfig, PyUdpServiceConfig,
};
use crate::errors::{dataplane_error_to_py, udp_error_to_py};
use gatherlink_dataplane::udp_service::ServiceSchedulerConfig;

/// Python handle for the Rust core userland UDP dataplane.
#[pyclass(name = "CoreDataplane")]
pub struct PyCoreDataplane {
    inner: CoreDataplane,
}

#[pymethods]
impl PyCoreDataplane {
    /// Bind compiled UDP services and create a Rust dataplane handle.
    #[staticmethod]
    pub fn bind(services: Vec<PyUdpServiceConfig>) -> PyResult<Self> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let config = CoreRuntimeConfig::new(services).map_err(udp_error_to_py)?;
        let inner = CoreDataplane::bind(config).map_err(dataplane_error_to_py)?;
        Ok(Self { inner })
    }

    /// Bind compiled UDP services with explicit path runtime state.
    #[staticmethod]
    pub fn bind_with_paths(services: Vec<PyUdpServiceConfig>, paths: Vec<PyPathConfig>) -> PyResult<Self> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let config = CoreRuntimeConfig::new_with_paths(services, paths).map_err(udp_error_to_py)?;
        let inner = CoreDataplane::bind(config).map_err(dataplane_error_to_py)?;
        Ok(Self { inner })
    }

    /// Bind compiled UDP services with explicit path and scheduler runtime state.
    #[staticmethod]
    #[pyo3(signature = (services, paths, scheduler, security = None))]
    pub fn bind_with_scheduler(
        services: Vec<PyUdpServiceConfig>,
        paths: Vec<PyPathConfig>,
        scheduler: PySchedulerConfig,
        security: Option<PyTransportSecurityConfig>,
    ) -> PyResult<Self> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let security = security.map(|security| security.inner()).unwrap_or_default();
        let config =
            CoreRuntimeConfig::new_with_paths_scheduler_and_security(services, paths, scheduler.inner(), security)
                .map_err(udp_error_to_py)?;
        let inner = CoreDataplane::bind(config).map_err(dataplane_error_to_py)?;
        Ok(Self { inner })
    }

    /// Reapply compiled UDP services from Python without reinterpreting config in Rust.
    pub fn reapply_config(&mut self, services: Vec<PyUdpServiceConfig>) -> PyResult<PyReapplyOutcome> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let config = CoreRuntimeConfig::new(services).map_err(udp_error_to_py)?;
        self.inner
            .reapply_config(config)
            .map(PyReapplyOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Reapply compiled UDP services and explicit path runtime state.
    pub fn reapply_config_with_paths(
        &mut self,
        services: Vec<PyUdpServiceConfig>,
        paths: Vec<PyPathConfig>,
    ) -> PyResult<PyReapplyOutcome> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let config = CoreRuntimeConfig::new_with_paths(services, paths).map_err(udp_error_to_py)?;
        self.inner
            .reapply_config(config)
            .map(PyReapplyOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Reapply compiled UDP services, path state, and scheduler state.
    #[pyo3(signature = (services, paths, scheduler, security = None))]
    pub fn reapply_config_with_scheduler(
        &mut self,
        services: Vec<PyUdpServiceConfig>,
        paths: Vec<PyPathConfig>,
        scheduler: PySchedulerConfig,
        security: Option<PyTransportSecurityConfig>,
    ) -> PyResult<PyReapplyOutcome> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let security = security.map(|security| security.inner()).unwrap_or_default();
        let config =
            CoreRuntimeConfig::new_with_paths_scheduler_and_security(services, paths, scheduler.inner(), security)
                .map_err(udp_error_to_py)?;
        self.inner
            .reapply_config(config)
            .map(PyReapplyOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Reapply only path scheduler primitives without rebinding live sockets.
    pub fn reapply_scheduler(
        &mut self,
        paths: Vec<PyPathConfig>,
        scheduler: PySchedulerConfig,
    ) -> PyResult<PyReapplyOutcome> {
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        self.inner
            .reapply_scheduler(paths, scheduler.inner())
            .map(PyReapplyOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Return the actual local address for a bound service.
    pub fn service_local_addr(&self, name: &str) -> PyResult<String> {
        let service = self
            .inner
            .service(name)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(format!("unknown UDP service: {name}")))?;
        Ok(service.local_addr().map_err(udp_error_to_py)?.to_string())
    }

    /// Forward one datagram for the named service through the Rust core.
    pub fn forward_one_for_service(&mut self, name: &str) -> PyResult<PyForwardOutcome> {
        self.inner
            .forward_one_for_service(name)
            .map(PyForwardOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Forward queued datagrams for the named service, allowing batch coalescing.
    pub fn forward_available_for_service(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> PyResult<Vec<PyForwardOutcome>> {
        self.inner
            .forward_available_for_service(name, max_datagrams)
            .map(|outcomes| outcomes.into_iter().map(PyForwardOutcome::from).collect())
            .map_err(dataplane_error_to_py)
    }

    /// Forward queued datagrams for the named service without blocking on a quiet app socket.
    pub fn forward_available_for_service_nonblocking(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> PyResult<Vec<PyForwardOutcome>> {
        self.inner
            .forward_available_for_service_nonblocking(name, max_datagrams)
            .map(|outcomes| outcomes.into_iter().map(PyForwardOutcome::from).collect())
            .map_err(dataplane_error_to_py)
    }

    /// Receive encoded path frames and emit decoded virtual UDP payloads.
    pub fn receive_available_from_paths(&mut self, max_frames: usize) -> PyResult<Vec<PyRemoteDeliverOutcome>> {
        self.inner
            .receive_available_from_paths(max_frames)
            .map(|outcomes| outcomes.into_iter().map(PyRemoteDeliverOutcome::from).collect())
            .map_err(dataplane_error_to_py)
    }

    /// Record a received remote data-frame observation in Rust-owned telemetry.
    pub fn observe_received_data_frame(&mut self, service_id: u16, path_id: u16, sequence: u64, payload_len: usize) {
        self.inner
            .observe_received_data_frame(service_id, path_id, sequence, payload_len);
    }

    /// Record a reserved-service payload for Python-owned decoding.
    pub fn observe_received_reserved_service_payload(
        &mut self,
        service_id: u16,
        path_id: u16,
        sequence: u64,
        payload: &[u8],
        frame_bytes: usize,
    ) {
        self.inner.observe_received_reserved_service_payload(
            service_id,
            path_id,
            sequence,
            payload.to_vec(),
            frame_bytes,
        );
    }

    /// Drain reserved Gatherlink service payloads for Python-owned decoders.
    pub fn drain_reserved_service_events(&mut self) -> Vec<PyReservedServiceEvent> {
        self.inner
            .drain_reserved_service_events()
            .into_iter()
            .map(PyReservedServiceEvent::from)
            .collect()
    }

    /// Apply a Python-decided service stop to Rust's hot path.
    pub fn disable_service(&mut self, service_id: u16, reason: &str) {
        self.inner.disable_service(service_id, reason);
    }

    /// Clear a Python-decided service stop from Rust's hot path.
    pub fn enable_service(&mut self, service_id: u16) {
        self.inner.enable_service(service_id);
    }

    /// Apply Python-decided service fanout primitives.
    #[pyo3(signature = (service_id, fanout, fanout_below_bytes = 0))]
    pub fn set_service_scheduler(&mut self, service_id: u16, fanout: u16, fanout_below_bytes: usize) {
        self.inner
            .set_service_scheduler(service_id, ServiceSchedulerConfig::new(fanout, fanout_below_bytes));
    }

    /// Frame and send one Python-composed service payload through the normal scheduler.
    pub fn transmit_service_payload(&mut self, service_id: u16, payload: &[u8]) -> PyResult<usize> {
        self.inner
            .transmit_service_payload(service_id, payload.to_vec())
            .map(|plans| plans.len())
            .map_err(dataplane_error_to_py)
    }

    /// Return Rust-owned counters in the same shape Python service monitoring expects.
    pub fn status_snapshot(&self, py: Python<'_>) -> PyResult<PyObject> {
        let snapshot = self.inner.metrics_snapshot();
        let root = PyDict::new_bound(py);
        let services = PyDict::new_bound(py);
        let paths = PyDict::new_bound(py);

        for (name, counters) in snapshot.services {
            services.set_item(name, counter_dict(py, counters)?)?;
        }
        for (path_id, counters) in snapshot.paths {
            paths.set_item(path_id.to_string(), counter_dict(py, counters)?)?;
        }

        root.set_item("services", services)?;
        root.set_item("path_stats", paths)?;
        root.set_item(
            "control_metadata",
            control_metadata_dict(py, snapshot.control_metadata)?,
        )?;
        root.set_item("security_drops", security_drop_dict(py, snapshot.security_drops)?)?;
        let disabled_services = PyDict::new_bound(py);
        for (service_id, reason) in self.inner.disabled_services_snapshot() {
            disabled_services.set_item(service_id.to_string(), reason)?;
        }
        root.set_item("disabled_services", disabled_services)?;
        Ok(root.into())
    }
}

fn counter_dict(
    py: Python<'_>,
    counters: gatherlink_dataplane::metrics::CounterSnapshot,
) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("packets", counters.packets)?;
    output.set_item("bytes", counters.bytes)?;
    output.set_item("tx_packets", counters.tx_packets)?;
    output.set_item("tx_bytes", counters.tx_bytes)?;
    output.set_item("rx_packets", counters.rx_packets)?;
    output.set_item("rx_bytes", counters.rx_bytes)?;
    output.set_item("missed_packets", counters.missed_packets)?;
    output.set_item("reordered_packets", counters.reordered_packets)?;
    output.set_item("packets_needing_reorder", counters.packets_needing_reorder)?;
    output.set_item("expected_duplicate_packets", counters.expected_duplicate_packets)?;
    output.set_item("unexpected_duplicate_packets", counters.unexpected_duplicate_packets)?;
    output.set_item("duplicate_packets", counters.duplicate_packets)?;
    output.set_item("send_failed_packets", counters.send_failed_packets)?;
    output.set_item("send_failed_bytes", counters.send_failed_bytes)?;
    output.set_item("fanout_send_failed_packets", counters.fanout_send_failed_packets)?;
    output.set_item("fanout_send_failed_bytes", counters.fanout_send_failed_bytes)?;
    output.set_item("security_drop_packets", counters.security_drop_packets)?;
    output.set_item("security_drop_bytes", counters.security_drop_bytes)?;
    Ok(output)
}

fn control_metadata_dict(py: Python<'_>, metadata: ControlMetadataSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    let path_metadata = PyDict::new_bound(py);
    let service_metadata = PyDict::new_bound(py);
    let service_endpoint_assertions = PyDict::new_bound(py);
    let service_disables = PyDict::new_bound(py);
    let path_capacity = PyDict::new_bound(py);
    let path_latency = PyDict::new_bound(py);
    let path_mtu = PyDict::new_bound(py);
    let path_control = PyDict::new_bound(py);

    for (path_id, control) in metadata.path_control {
        path_control.set_item(path_id.to_string(), path_control_dict(py, control)?)?;
    }

    output.set_item("sent", control_counter_dict(py, metadata.sent)?)?;
    output.set_item("received", control_counter_dict(py, metadata.received)?)?;
    output.set_item("path_metadata_count", path_metadata.len())?;
    output.set_item("service_metadata_count", service_metadata.len())?;
    output.set_item("service_endpoint_assertion_count", service_endpoint_assertions.len())?;
    output.set_item("service_disable_count", service_disables.len())?;
    output.set_item("path_capacity_count", path_capacity.len())?;
    output.set_item("path_latency_count", path_latency.len())?;
    output.set_item("path_mtu_count", path_mtu.len())?;
    output.set_item("path_control_count", path_control.len())?;
    output.set_item("path_metadata", path_metadata)?;
    output.set_item("service_metadata", service_metadata)?;
    output.set_item("service_endpoint_assertions", service_endpoint_assertions)?;
    output.set_item("service_disables", service_disables)?;
    output.set_item("path_capacity", path_capacity)?;
    output.set_item("path_latency", path_latency)?;
    output.set_item("path_mtu", path_mtu)?;
    output.set_item("path_control", path_control)?;
    Ok(output)
}

fn security_drop_dict(
    py: Python<'_>,
    counters: gatherlink_dataplane::metrics::SecurityDropSnapshot,
) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("packets", counters.packets)?;
    output.set_item("bytes", counters.bytes)?;
    Ok(output)
}

fn path_control_dict(py: Python<'_>, control: PathControlSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("tx", control_counter_dict(py, control.tx)?)?;
    output.set_item("rx", control_counter_dict(py, control.rx)?)?;
    Ok(output)
}

fn control_counter_dict(py: Python<'_>, counters: ControlCounterSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("frames", counters.frames)?;
    output.set_item("messages", 0)?;
    output.set_item("bytes", counters.bytes)?;
    Ok(output)
}
