//! Python-facing core dataplane API.
//!
//! This bridge intentionally exposes a narrow execution handle. Python can bind
//! compiled services, reapply compiled runtime config, and step the engine for
//! tests, but packet processing stays in Rust.

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::metrics::{
    ControlCounterSnapshot, ControlMetadataSnapshot, DataTrafficTimingSample, PathControlSnapshot,
};
use gatherlink_dataplane::runtime_config::CoreRuntimeConfig;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::dto::{
    PyForwardOutcome, PyPathConfig, PyReapplyOutcome, PyRemoteDeliverOutcome, PyReservedServiceEvent,
    PySchedulerConfig, PyTransportSecurityConfig, PyUdpServiceConfig,
};
use crate::errors::{dataplane_error_to_py, udp_error_to_py};
use gatherlink_dataplane::udp_service::{ServicePathPolicy, ServiceSchedulerConfig};

/// Python handle for the Rust core userland UDP dataplane.
#[pyclass(name = "CoreDataplane")]
pub struct PyCoreDataplane {
    inner: CoreDataplane,
}

fn parse_service_path_policy(policy: &str) -> PyResult<ServicePathPolicy> {
    match policy {
        "inherit" => Ok(ServicePathPolicy::Inherit),
        "single_best_path" => Ok(ServicePathPolicy::SingleBestPath),
        "weighted_round_robin" => Ok(ServicePathPolicy::WeightedRoundRobin),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown service scheduler path policy: {other}"
        ))),
    }
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

    /// Forward queued datagrams without allocating Python objects for every packet.
    ///
    /// The production runner only needs aggregate counters on the hot path.
    /// Detailed per-packet outcomes remain available through
    /// `forward_available_for_service_nonblocking` for tests and diagnostics.
    pub fn forward_available_for_service_nonblocking_summary(
        &mut self,
        name: &str,
        max_datagrams: usize,
    ) -> PyResult<(usize, usize)> {
        self.inner
            .forward_available_for_service_nonblocking_summary(name, max_datagrams)
            .map(|summary| (summary.packets, summary.bytes))
            .map_err(dataplane_error_to_py)
    }

    /// Receive encoded path frames and emit decoded virtual UDP payloads.
    pub fn receive_available_from_paths(&mut self, max_frames: usize) -> PyResult<Vec<PyRemoteDeliverOutcome>> {
        self.inner
            .receive_available_from_paths(max_frames)
            .map(|outcomes| outcomes.into_iter().map(PyRemoteDeliverOutcome::from).collect())
            .map_err(dataplane_error_to_py)
    }

    /// Receive path frames without allocating Python objects for every delivered packet.
    pub fn receive_available_from_paths_summary(&mut self, max_frames: usize) -> PyResult<(usize, usize)> {
        self.inner
            .receive_available_from_paths_summary(max_frames)
            .map(|summary| (summary.packets, summary.bytes))
            .map_err(dataplane_error_to_py)
    }

    /// Run a bounded Rust-side drain burst for the production Python runner.
    ///
    /// Python still owns config, service names, policy, and cadence. This method
    /// only amortizes the PyO3 boundary by executing the same nonblocking service
    /// and path drains several times before returning aggregate counters.
    pub fn run_available_summary(
        &mut self,
        service_names: Vec<String>,
        batch_size: usize,
        max_cycles: usize,
    ) -> PyResult<(usize, usize, usize, usize)> {
        let mut forwarded_packets = 0_usize;
        let mut forwarded_bytes = 0_usize;
        let mut delivered_packets = 0_usize;
        let mut delivered_bytes = 0_usize;
        let cycles = max_cycles.max(1);

        for _ in 0..cycles {
            let mut did_work = false;
            for service_name in &service_names {
                let outcomes = self
                    .inner
                    .forward_available_for_service_nonblocking_summary(service_name, batch_size)
                    .map_err(dataplane_error_to_py)?;
                if outcomes.packets > 0 {
                    did_work = true;
                    forwarded_packets += outcomes.packets;
                    forwarded_bytes += outcomes.bytes;
                }
            }

            let delivered = self
                .inner
                .receive_available_from_paths_summary(batch_size)
                .map_err(dataplane_error_to_py)?;
            if delivered.packets > 0 {
                did_work = true;
                delivered_packets += delivered.packets;
                delivered_bytes += delivered.bytes;
            }

            if !did_work {
                break;
            }
        }

        Ok((forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes))
    }

    /// Run a bounded Rust-side drain burst with Python-compiled service quanta.
    ///
    /// Python owns service policy and chooses the per-service packet budget.
    /// Rust only executes the supplied nonblocking service drains and path drain
    /// so mixed-service QoS does not require semantic branches in the dataplane.
    pub fn run_available_plan_summary(
        &mut self,
        service_plan: Vec<(String, usize)>,
        path_batch_size: usize,
        max_cycles: usize,
    ) -> PyResult<(usize, usize, usize, usize)> {
        let mut forwarded_packets = 0_usize;
        let mut forwarded_bytes = 0_usize;
        let mut delivered_packets = 0_usize;
        let mut delivered_bytes = 0_usize;
        let cycles = max_cycles.max(1);

        for _ in 0..cycles {
            let mut did_work = false;
            for (service_name, service_batch_size) in &service_plan {
                let outcomes = self
                    .inner
                    .forward_available_for_service_nonblocking_summary(service_name, (*service_batch_size).max(1))
                    .map_err(dataplane_error_to_py)?;
                if outcomes.packets > 0 {
                    did_work = true;
                    forwarded_packets += outcomes.packets;
                    forwarded_bytes += outcomes.bytes;
                }
            }

            let delivered = self
                .inner
                .receive_available_from_paths_summary(path_batch_size)
                .map_err(dataplane_error_to_py)?;
            if delivered.packets > 0 {
                did_work = true;
                delivered_packets += delivered.packets;
                delivered_bytes += delivered.bytes;
            }

            if !did_work {
                break;
            }
        }

        Ok((forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes))
    }

    /// Run a bounded Rust-side drain burst with Python-compiled packet and byte service budgets.
    ///
    /// Byte budgets are primitive execution caps. Python owns the meaning,
    /// activation threshold, and hysteresis; Rust only drains up to the
    /// requested limits and returns aggregate counters.
    pub fn run_available_budget_summary(
        &mut self,
        service_plan: Vec<(String, usize, usize)>,
        path_batch_size: usize,
        max_cycles: usize,
    ) -> PyResult<(usize, usize, usize, usize)> {
        let mut forwarded_packets = 0_usize;
        let mut forwarded_bytes = 0_usize;
        let mut delivered_packets = 0_usize;
        let mut delivered_bytes = 0_usize;
        let cycles = max_cycles.max(1);

        for _ in 0..cycles {
            let mut did_work = false;
            for (service_name, service_batch_size, service_byte_budget) in &service_plan {
                let outcomes = self
                    .inner
                    .forward_available_for_service_budget_summary(
                        service_name,
                        (*service_batch_size).max(1),
                        *service_byte_budget,
                    )
                    .map_err(dataplane_error_to_py)?;
                if outcomes.packets > 0 {
                    did_work = true;
                    forwarded_packets += outcomes.packets;
                    forwarded_bytes += outcomes.bytes;
                }
            }

            let delivered = self
                .inner
                .receive_available_from_paths_summary(path_batch_size)
                .map_err(dataplane_error_to_py)?;
            if delivered.packets > 0 {
                did_work = true;
                delivered_packets += delivered.packets;
                delivered_bytes += delivered.bytes;
            }

            if !did_work {
                break;
            }
        }

        Ok((forwarded_packets, forwarded_bytes, delivered_packets, delivered_bytes))
    }

    /// Record a received remote data-frame observation in Rust-owned telemetry.
    pub fn observe_received_data_frame(&mut self, service_id: u16, path_id: u16, sequence: u64, payload_len: usize) {
        self.inner
            .observe_received_data_frame(service_id, path_id, sequence, payload_len);
    }

    /// Record a reserved-service payload for Python-owned decoding.
    #[pyo3(signature = (service_id, path_id, sequence, payload, frame_bytes, peer_scope=None))]
    pub fn observe_received_reserved_service_payload(
        &mut self,
        service_id: u16,
        path_id: u16,
        sequence: u64,
        payload: &[u8],
        frame_bytes: usize,
        peer_scope: Option<u32>,
    ) {
        self.inner.observe_received_reserved_service_payload(
            service_id,
            path_id,
            sequence,
            payload.to_vec(),
            frame_bytes,
            peer_scope,
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
    #[pyo3(signature = (
        service_id,
        fanout,
        fanout_below_bytes = 0,
        flowlet_idle_us = 0,
        flowlet_max_hold_us = 0,
        path_run_datagrams = 0,
        path_policy = "inherit",
        allowed_path_ids = None,
        path_weights = None,
    ))]
    pub fn set_service_scheduler(
        &mut self,
        service_id: u16,
        fanout: u16,
        fanout_below_bytes: usize,
        flowlet_idle_us: u64,
        flowlet_max_hold_us: u64,
        path_run_datagrams: usize,
        path_policy: &str,
        allowed_path_ids: Option<Vec<u16>>,
        path_weights: Option<Vec<(u16, u16)>>,
    ) -> PyResult<()> {
        let path_policy = parse_service_path_policy(path_policy)?;
        self.inner.set_service_scheduler(
            service_id,
            ServiceSchedulerConfig::new_with_path_policy(
                fanout,
                fanout_below_bytes,
                flowlet_idle_us,
                flowlet_max_hold_us,
                path_run_datagrams,
                path_policy,
            )
            .with_allowed_path_ids(allowed_path_ids.unwrap_or_default())
            .with_path_weights(path_weights.unwrap_or_default()),
        );
        Ok(())
    }

    /// Frame and send one Python-composed service payload through the normal scheduler.
    pub fn transmit_service_payload(&mut self, service_id: u16, payload: &[u8]) -> PyResult<usize> {
        self.inner
            .transmit_service_payload(service_id, payload.to_vec())
            .map(|plans| plans.len())
            .map_err(dataplane_error_to_py)
    }

    /// Frame and send one Python-composed service payload through one exact path.
    pub fn transmit_service_payload_on_path(
        &mut self,
        service_id: u16,
        path_id: u16,
        payload: &[u8],
    ) -> PyResult<usize> {
        self.inner
            .transmit_service_payload_on_path(service_id, path_id, payload.to_vec())
            .map(|plans| plans.len())
            .map_err(dataplane_error_to_py)
    }

    /// Frame and send one Python-composed service payload to a learned peer scope.
    pub fn transmit_service_payload_to_peer(
        &mut self,
        service_id: u16,
        payload: &[u8],
        peer_scope: u32,
    ) -> PyResult<usize> {
        self.inner
            .transmit_service_payload_to_peer(service_id, payload.to_vec(), peer_scope)
            .map(|plans| plans.len())
            .map_err(dataplane_error_to_py)
    }

    /// Return Rust-owned counters in the same shape Python service monitoring expects.
    pub fn status_snapshot(&self, py: Python<'_>) -> PyResult<PyObject> {
        let snapshot = self.inner.metrics_snapshot();
        let root = PyDict::new_bound(py);
        let services = PyDict::new_bound(py);
        let paths = PyDict::new_bound(py);
        let service_paths = PyDict::new_bound(py);

        for (name, counters) in snapshot.services {
            services.set_item(name, counter_dict(py, counters)?)?;
        }
        for (path_id, counters) in snapshot.paths {
            paths.set_item(path_id.to_string(), counter_dict(py, counters)?)?;
        }
        for (service_name, path_counters) in snapshot.service_paths {
            let service_path_rows = PyDict::new_bound(py);
            for (path_id, counters) in path_counters {
                service_path_rows.set_item(path_id.to_string(), counter_dict(py, counters)?)?;
            }
            service_paths.set_item(service_name, service_path_rows)?;
        }

        root.set_item("services", services)?;
        root.set_item("path_stats", paths)?;
        root.set_item("service_path_stats", service_paths)?;
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

    /// Drain sparse real-data timing samples for Python-owned latency estimation.
    pub fn drain_data_timing_samples(&mut self, py: Python<'_>) -> PyResult<PyObject> {
        let (tx_samples, rx_samples) = self.inner.drain_data_timing_samples();
        let root = PyDict::new_bound(py);
        root.set_item("tx", timing_sample_list(py, tx_samples)?)?;
        root.set_item("rx", timing_sample_list(py, rx_samples)?)?;
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
    output.set_item("last_tx_at_us", counters.last_tx_at_us)?;
    output.set_item("last_rx_at_us", counters.last_rx_at_us)?;
    output.set_item("last_tx_gap_us", counters.last_tx_gap_us)?;
    output.set_item("last_rx_gap_us", counters.last_rx_gap_us)?;
    output.set_item("scheduler_in_flight_packets", counters.scheduler_in_flight_packets)?;
    output.set_item("scheduler_in_flight_bytes", counters.scheduler_in_flight_bytes)?;
    output.set_item(
        "scheduler_predicted_delivery_us",
        counters.scheduler_predicted_delivery_us,
    )?;
    output.set_item("reorder_buffer_packets", counters.reorder_buffer_packets)?;
    output.set_item("reorder_buffer_oldest_age_us", counters.reorder_buffer_oldest_age_us)?;
    output.set_item("socket_receive_buffer_bytes", counters.socket_receive_buffer_bytes)?;
    output.set_item("socket_send_buffer_bytes", counters.socket_send_buffer_bytes)?;
    output.set_item("socket_drain_quantum", counters.socket_drain_quantum)?;
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
    output.set_item("last_at_us", counters.last_at_us)?;
    output.set_item("last_gap_us", counters.last_gap_us)?;
    Ok(output)
}

fn timing_sample_list(py: Python<'_>, samples: Vec<DataTrafficTimingSample>) -> PyResult<Bound<'_, PyList>> {
    let output = PyList::empty_bound(py);
    for sample in samples {
        let item = PyDict::new_bound(py);
        item.set_item("path_id", sample.path_id)?;
        item.set_item("sequence", sample.sequence)?;
        item.set_item("packet_count", sample.packet_count)?;
        item.set_item("observed_at_us", sample.observed_at_us)?;
        match sample.peer_scope {
            Some(peer_scope) => item.set_item("peer_scope", peer_scope)?,
            None => item.set_item("peer_scope", py.None())?,
        }
        output.append(item)?;
    }
    Ok(output)
}
