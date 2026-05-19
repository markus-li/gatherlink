//! Python-facing core dataplane API.
//!
//! This bridge intentionally exposes a narrow execution handle. Python can bind
//! compiled services, reapply compiled runtime config, and step the engine for
//! tests, but packet processing stays in Rust.

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::metrics::{
    ControlCounterSnapshot, ControlMetadataSnapshot, InternalClockSyncSnapshot, PathCapacitySnapshot,
    PathControlSnapshot, PathLatencySnapshot, SinkTimeSnapshot,
};
use gatherlink_dataplane::runtime_config::CoreRuntimeConfig;
use gatherlink_protocol::control::ControlPayload;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::dto::{PyForwardOutcome, PyPathConfig, PyReapplyOutcome, PySchedulerConfig, PyUdpServiceConfig};
use crate::errors::{dataplane_error_to_py, udp_error_to_py};

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
    pub fn bind_with_scheduler(
        services: Vec<PyUdpServiceConfig>,
        paths: Vec<PyPathConfig>,
        scheduler: PySchedulerConfig,
    ) -> PyResult<Self> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let config = CoreRuntimeConfig::new_with_paths_and_scheduler(services, paths, scheduler.inner())
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
    pub fn reapply_config_with_scheduler(
        &mut self,
        services: Vec<PyUdpServiceConfig>,
        paths: Vec<PyPathConfig>,
        scheduler: PySchedulerConfig,
    ) -> PyResult<PyReapplyOutcome> {
        let services = services.into_iter().map(|service| service.inner()).collect();
        let paths = paths.into_iter().map(|path| path.inner()).collect();
        let config = CoreRuntimeConfig::new_with_paths_and_scheduler(services, paths, scheduler.inner())
            .map_err(udp_error_to_py)?;
        self.inner
            .reapply_config(config)
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

    /// Record a received remote data-frame observation in Rust-owned telemetry.
    pub fn observe_received_data_frame(&mut self, service_id: u64, path_id: u16, sequence: u64, payload_len: usize) {
        self.inner
            .observe_received_data_frame(service_id, path_id, sequence, payload_len);
    }

    /// Decode and record a received control metaband payload.
    #[pyo3(signature = (payload, frame_bytes, path_id=None))]
    pub fn observe_received_control_payload(
        &mut self,
        payload: &[u8],
        frame_bytes: usize,
        path_id: Option<u16>,
    ) -> PyResult<()> {
        let control = ControlPayload::decode(payload).map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        if let Some(path_id) = path_id {
            self.inner
                .observe_received_control_payload_on_path(path_id, &control, frame_bytes);
        } else {
            self.inner.observe_received_control_payload(&control, frame_bytes);
        }
        Ok(())
    }

    /// Decode and record a sent control metaband payload.
    #[pyo3(signature = (payload, frame_bytes, path_id=None))]
    pub fn observe_sent_control_payload(
        &mut self,
        payload: &[u8],
        frame_bytes: usize,
        path_id: Option<u16>,
    ) -> PyResult<()> {
        let control = ControlPayload::decode(payload).map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        if let Some(path_id) = path_id {
            self.inner
                .observe_sent_control_payload_on_path(path_id, &control, frame_bytes);
        } else {
            self.inner.observe_sent_control_payload(&control, frame_bytes);
        }
        Ok(())
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
    Ok(output)
}

fn control_metadata_dict(py: Python<'_>, metadata: ControlMetadataSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    let path_metadata = PyDict::new_bound(py);
    let path_capacity = PyDict::new_bound(py);
    let path_latency = PyDict::new_bound(py);
    let path_control = PyDict::new_bound(py);

    for (path_id, name) in metadata.path_metadata {
        path_metadata.set_item(path_id.to_string(), name)?;
    }
    for (path_id, capacity) in metadata.path_capacity {
        path_capacity.set_item(path_id.to_string(), path_capacity_dict(py, capacity)?)?;
    }
    for (path_id, latency) in metadata.path_latency {
        path_latency.set_item(path_id.to_string(), path_latency_dict(py, latency)?)?;
    }
    for (path_id, control) in metadata.path_control {
        path_control.set_item(path_id.to_string(), path_control_dict(py, control)?)?;
    }

    output.set_item("sent", control_counter_dict(py, metadata.sent)?)?;
    output.set_item("received", control_counter_dict(py, metadata.received)?)?;
    output.set_item("path_metadata_count", path_metadata.len())?;
    output.set_item("path_capacity_count", path_capacity.len())?;
    output.set_item("path_latency_count", path_latency.len())?;
    output.set_item("path_control_count", path_control.len())?;
    output.set_item("path_metadata", path_metadata)?;
    output.set_item("path_capacity", path_capacity)?;
    output.set_item("path_latency", path_latency)?;
    output.set_item("path_control", path_control)?;
    if let Some(sync) = metadata.internal_clock_sync {
        output.set_item("internal_clock_sync", internal_clock_sync_dict(py, sync)?)?;
    }
    if let Some(sink_time) = metadata.sink_time {
        output.set_item("sink_time", sink_time_dict(py, sink_time)?)?;
    }
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
    output.set_item("messages", counters.messages)?;
    output.set_item("bytes", counters.bytes)?;
    Ok(output)
}

fn path_capacity_dict(py: Python<'_>, capacity: PathCapacitySnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("tx_bps", capacity.tx_bps)?;
    output.set_item("rx_bps", capacity.rx_bps)?;
    Ok(output)
}

fn path_latency_dict(py: Python<'_>, latency: PathLatencySnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("tx_current_us", latency.tx_current_us)?;
    output.set_item("tx_mean_us", latency.tx_mean_us)?;
    output.set_item("rx_current_us", latency.rx_current_us)?;
    output.set_item("rx_mean_us", latency.rx_mean_us)?;
    Ok(output)
}

fn internal_clock_sync_dict(py: Python<'_>, sync: InternalClockSyncSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("exchange_id", sync.exchange_id)?;
    output.set_item("path_id", sync.path_id)?;
    output.set_item("mode", sync.mode)?;
    output.set_item("origin_us", sync.origin_us)?;
    output.set_item("receive_us", sync.receive_us)?;
    output.set_item("transmit_us", sync.transmit_us)?;
    Ok(output)
}

fn sink_time_dict(py: Python<'_>, sink_time: SinkTimeSnapshot) -> PyResult<Bound<'_, PyDict>> {
    let output = PyDict::new_bound(py);
    output.set_item("path_id", sink_time.path_id)?;
    output.set_item("sink_unix_us", sink_time.sink_unix_us)?;
    output.set_item("sink_internal_us", sink_time.sink_internal_us)?;
    output.set_item("ntp_state", sink_time.ntp_state)?;
    Ok(output)
}
