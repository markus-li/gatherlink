//! Python-facing relay-hop executor API.
//!
//! Python owns relay authorization and lifecycle. This bridge exposes only the
//! compiled socket/key/session facts that Rust needs to authenticate one hop,
//! remove that outer hop envelope, and forward the remaining opaque packet.

use gatherlink_dataplane::relay::{
    RelayForwardError, RelayForwardOutcome, RelayHopExitForwarder, RelayHopForwarder, RelaySessionConfig,
    RelaySessionExecutor,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::net::SocketAddr;

/// Python handle for one compiled Rust relay-hop UDP forwarder.
#[pyclass(name = "RelayHopForwarder")]
pub struct PyRelayHopForwarder {
    inner: RelayHopForwarder,
}

/// Python handle for one final-hop relay exit forwarder.
#[pyclass(name = "RelayHopExitForwarder")]
pub struct PyRelayHopExitForwarder {
    inner: RelayHopExitForwarder,
}

#[pymethods]
impl PyRelayHopForwarder {
    /// Bind a Python-compiled relay-hop socket and next-hop executor.
    #[staticmethod]
    #[pyo3(signature = (
        listen,
        next_hop,
        relay_receiver_index,
        next_hop_receiver_index,
        send_key,
        receive_key,
        expires_at_unix_us,
        max_packet_size = None,
        max_packets = None,
        max_bytes = None,
    ))]
    pub fn bind(
        listen: &str,
        next_hop: &str,
        relay_receiver_index: u32,
        next_hop_receiver_index: u32,
        send_key: &[u8],
        receive_key: &[u8],
        expires_at_unix_us: u64,
        max_packet_size: Option<usize>,
        max_packets: Option<u64>,
        max_bytes: Option<u64>,
    ) -> PyResult<Self> {
        let listen = parse_socket_addr(listen)?;
        let next_hop = parse_socket_addr(next_hop)?;
        let executor = RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index,
                expires_at_unix_us,
                max_packet_size,
                max_packets,
                max_bytes,
            },
            next_hop_receiver_index,
            key_bytes(send_key, "send_key")?,
            key_bytes(receive_key, "receive_key")?,
        );
        let inner = RelayHopForwarder::bind(listen, next_hop, executor).map_err(relay_error_to_py)?;
        Ok(Self { inner })
    }

    /// Return the actual local relay socket address.
    pub fn local_addr(&self) -> PyResult<String> {
        self.inner
            .local_addr()
            .map(|addr| addr.to_string())
            .map_err(relay_error_to_py)
    }

    /// Poll one relay-hop packet without blocking.
    pub fn try_forward_one(&mut self, py: Python<'_>, now_unix_us: u64) -> PyResult<PyObject> {
        let output = PyDict::new_bound(py);
        match self.inner.try_forward_one(now_unix_us).map_err(relay_error_to_py)? {
            RelayForwardOutcome::NoPacket => {
                output.set_item("kind", "no_packet")?;
            }
            RelayForwardOutcome::Forwarded {
                source,
                received_bytes,
                emitted_bytes,
            } => {
                output.set_item("kind", "forwarded")?;
                output.set_item("source", source.to_string())?;
                output.set_item("received_bytes", received_bytes)?;
                output.set_item("emitted_bytes", emitted_bytes)?;
            }
            RelayForwardOutcome::Dropped {
                source,
                reason,
                received_bytes,
            } => {
                output.set_item("kind", "dropped")?;
                output.set_item("source", source.to_string())?;
                output.set_item("reason", format!("{reason:?}"))?;
                output.set_item("received_bytes", received_bytes)?;
            }
        }
        Ok(output.into())
    }

    /// Drain several ready relay-hop packets before returning to Python.
    pub fn try_forward_many(&mut self, py: Python<'_>, max_packets: usize, now_unix_us: u64) -> PyResult<PyObject> {
        let batch = py
            .allow_threads(|| self.inner.try_forward_many(max_packets, now_unix_us))
            .map_err(relay_error_to_py)?;
        relay_batch_to_py(py, batch)
    }

    /// Return relay-hop counters for monitoring and diagnostics.
    pub fn counters(&self, py: Python<'_>) -> PyResult<PyObject> {
        let counters = self.inner.counters();
        let output = PyDict::new_bound(py);
        output.set_item("forwarded_packets", counters.forwarded_packets)?;
        output.set_item("forwarded_bytes", counters.forwarded_bytes)?;
        output.set_item("dropped_packets", counters.dropped_packets)?;
        output.set_item("emitted_packets", counters.emitted_packets)?;
        output.set_item("emitted_bytes", counters.emitted_bytes)?;
        Ok(output.into())
    }
}

#[pymethods]
impl PyRelayHopExitForwarder {
    /// Bind a Python-compiled relay-hop exit socket and endpoint-core next hop.
    #[staticmethod]
    #[pyo3(signature = (
        listen,
        next_hop,
        relay_receiver_index,
        receive_key,
        expires_at_unix_us,
        max_packet_size = None,
        max_packets = None,
        max_bytes = None,
    ))]
    pub fn bind(
        listen: &str,
        next_hop: &str,
        relay_receiver_index: u32,
        receive_key: &[u8],
        expires_at_unix_us: u64,
        max_packet_size: Option<usize>,
        max_packets: Option<u64>,
        max_bytes: Option<u64>,
    ) -> PyResult<Self> {
        let listen = parse_socket_addr(listen)?;
        let next_hop = parse_socket_addr(next_hop)?;
        let executor = RelaySessionExecutor::new_with_hop_keys(
            RelaySessionConfig {
                relay_receiver_index,
                expires_at_unix_us,
                max_packet_size,
                max_packets,
                max_bytes,
            },
            relay_receiver_index,
            [0_u8; 32],
            key_bytes(receive_key, "receive_key")?,
        );
        let inner = RelayHopExitForwarder::bind(listen, next_hop, executor).map_err(relay_error_to_py)?;
        Ok(Self { inner })
    }

    /// Return the actual local relay-exit socket address.
    pub fn local_addr(&self) -> PyResult<String> {
        self.inner
            .local_addr()
            .map(|addr| addr.to_string())
            .map_err(relay_error_to_py)
    }

    /// Poll one relay-hop packet without blocking.
    pub fn try_forward_one(&mut self, py: Python<'_>, now_unix_us: u64) -> PyResult<PyObject> {
        relay_outcome_to_py(py, self.inner.try_forward_one(now_unix_us).map_err(relay_error_to_py)?)
    }

    /// Drain several ready final-hop packets before returning to Python.
    pub fn try_forward_many(&mut self, py: Python<'_>, max_packets: usize, now_unix_us: u64) -> PyResult<PyObject> {
        let batch = py
            .allow_threads(|| self.inner.try_forward_many(max_packets, now_unix_us))
            .map_err(relay_error_to_py)?;
        relay_batch_to_py(py, batch)
    }

    /// Return relay-hop counters for monitoring and diagnostics.
    pub fn counters(&self, py: Python<'_>) -> PyResult<PyObject> {
        relay_counters_to_py(py, self.inner.counters())
    }
}

fn parse_socket_addr(value: &str) -> PyResult<SocketAddr> {
    value
        .parse::<SocketAddr>()
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

fn key_bytes(value: &[u8], field: &str) -> PyResult<[u8; 32]> {
    value
        .try_into()
        .map_err(|_| pyo3::exceptions::PyValueError::new_err(format!("{field} must contain exactly 32 raw bytes")))
}

fn relay_error_to_py(error: RelayForwardError) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("{error:?}"))
}

fn relay_outcome_to_py(py: Python<'_>, outcome: RelayForwardOutcome) -> PyResult<PyObject> {
    let output = PyDict::new_bound(py);
    match outcome {
        RelayForwardOutcome::NoPacket => {
            output.set_item("kind", "no_packet")?;
        }
        RelayForwardOutcome::Forwarded {
            source,
            received_bytes,
            emitted_bytes,
        } => {
            output.set_item("kind", "forwarded")?;
            output.set_item("source", source.to_string())?;
            output.set_item("received_bytes", received_bytes)?;
            output.set_item("emitted_bytes", emitted_bytes)?;
        }
        RelayForwardOutcome::Dropped {
            source,
            reason,
            received_bytes,
        } => {
            output.set_item("kind", "dropped")?;
            output.set_item("source", source.to_string())?;
            output.set_item("reason", format!("{reason:?}"))?;
            output.set_item("received_bytes", received_bytes)?;
        }
    }
    Ok(output.into())
}

fn relay_counters_to_py(
    py: Python<'_>,
    counters: gatherlink_dataplane::relay::RelaySessionCounters,
) -> PyResult<PyObject> {
    let output = PyDict::new_bound(py);
    output.set_item("forwarded_packets", counters.forwarded_packets)?;
    output.set_item("forwarded_bytes", counters.forwarded_bytes)?;
    output.set_item("dropped_packets", counters.dropped_packets)?;
    output.set_item("emitted_packets", counters.emitted_packets)?;
    output.set_item("emitted_bytes", counters.emitted_bytes)?;
    Ok(output.into())
}

fn relay_batch_to_py(py: Python<'_>, batch: gatherlink_dataplane::relay::RelayForwardBatch) -> PyResult<PyObject> {
    let output = PyDict::new_bound(py);
    output.set_item("forwarded_packets", batch.forwarded_packets)?;
    output.set_item("dropped_packets", batch.dropped_packets)?;
    output.set_item("emitted_packets", batch.emitted_packets)?;
    output.set_item("received_bytes", batch.received_bytes)?;
    output.set_item("emitted_bytes", batch.emitted_bytes)?;
    Ok(output.into())
}
