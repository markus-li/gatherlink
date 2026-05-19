//! Python-facing core dataplane API.
//!
//! This bridge intentionally exposes a narrow execution handle. Python can bind
//! compiled services, reapply compiled runtime config, and step the engine for
//! tests, but packet processing stays in Rust.

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::runtime_config::CoreRuntimeConfig;
use pyo3::prelude::*;

use crate::dto::{PyForwardOutcome, PyReapplyOutcome, PyUdpServiceConfig};
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
        let services = services
            .into_iter()
            .map(|service| service.inner())
            .collect();
        let config = CoreRuntimeConfig::new(services).map_err(udp_error_to_py)?;
        let inner = CoreDataplane::bind(config).map_err(dataplane_error_to_py)?;
        Ok(Self { inner })
    }

    /// Reapply compiled UDP services from Python without reinterpreting config in Rust.
    pub fn reapply_config(
        &mut self,
        services: Vec<PyUdpServiceConfig>,
    ) -> PyResult<PyReapplyOutcome> {
        let services = services
            .into_iter()
            .map(|service| service.inner())
            .collect();
        let config = CoreRuntimeConfig::new(services).map_err(udp_error_to_py)?;
        self.inner
            .reapply_config(config)
            .map(PyReapplyOutcome::from)
            .map_err(dataplane_error_to_py)
    }

    /// Return the actual local address for a bound service.
    pub fn service_local_addr(&self, name: &str) -> PyResult<String> {
        let service = self.inner.service(name).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown UDP service: {name}"))
        })?;
        Ok(service.local_addr().map_err(udp_error_to_py)?.to_string())
    }

    /// Forward one datagram for the named service through the Rust core.
    pub fn forward_one_for_service(&mut self, name: &str) -> PyResult<PyForwardOutcome> {
        self.inner
            .forward_one_for_service(name)
            .map(PyForwardOutcome::from)
            .map_err(dataplane_error_to_py)
    }
}

#[cfg(test)]
mod tests {
    use std::net::UdpSocket;
    use std::time::Duration;

    use super::*;

    #[test]
    fn python_facing_dataplane_forwards_one_udp_payload() {
        let target = UdpSocket::bind("127.0.0.1:0").unwrap();
        target
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let service = PyUdpServiceConfig::new(
            "udp-main".to_owned(),
            target.local_addr().unwrap().to_string(),
            Some("127.0.0.1:0".to_owned()),
        )
        .unwrap();
        let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
        let service_addr = dataplane.service_local_addr("udp-main").unwrap();
        let sender = UdpSocket::bind("127.0.0.1:0").unwrap();

        sender.send_to(b"python-bridge-core", service_addr).unwrap();
        let outcome = dataplane.forward_one_for_service("udp-main").unwrap();

        let mut buffer = [0_u8; 64];
        let (length, _source) = target.recv_from(&mut buffer).unwrap();
        assert_eq!(&buffer[..length], b"python-bridge-core");
        assert_eq!(outcome.service(), "udp-main");
        assert_eq!(outcome.payload_len(), b"python-bridge-core".len());
        assert_eq!(outcome.sequence(), 1);
    }

    #[test]
    fn python_facing_dataplane_forwards_ipv6_udp_payload() {
        let target = UdpSocket::bind("[::1]:0").unwrap();
        target
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let service = PyUdpServiceConfig::new(
            "udp-v6".to_owned(),
            target.local_addr().unwrap().to_string(),
            Some("[::1]:0".to_owned()),
        )
        .unwrap();
        let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
        let service_addr = dataplane.service_local_addr("udp-v6").unwrap();
        let sender = UdpSocket::bind("[::1]:0").unwrap();

        sender.send_to(b"python-ipv6-core", service_addr).unwrap();
        let outcome = dataplane.forward_one_for_service("udp-v6").unwrap();

        let mut buffer = [0_u8; 64];
        let (length, _source) = target.recv_from(&mut buffer).unwrap();
        assert_eq!(&buffer[..length], b"python-ipv6-core");
        assert_eq!(outcome.service(), "udp-v6");
        assert_eq!(outcome.target(), target.local_addr().unwrap().to_string());
    }

    #[test]
    fn python_facing_dataplane_reapplies_target_update() {
        let first_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        let second_target = UdpSocket::bind("127.0.0.1:0").unwrap();
        let service = PyUdpServiceConfig::new(
            "udp-main".to_owned(),
            first_target.local_addr().unwrap().to_string(),
            Some("127.0.0.1:0".to_owned()),
        )
        .unwrap();
        let mut dataplane = PyCoreDataplane::bind(vec![service]).unwrap();
        let listen = dataplane.service_local_addr("udp-main").unwrap();
        let updated = PyUdpServiceConfig::new(
            "udp-main".to_owned(),
            second_target.local_addr().unwrap().to_string(),
            Some(listen.clone()),
        )
        .unwrap();

        let outcome = dataplane.reapply_config(vec![updated]).unwrap();

        assert_eq!(dataplane.service_local_addr("udp-main").unwrap(), listen);
        assert_eq!(outcome.unchanged(), 0);
        assert_eq!(outcome.updated(), 1);
        assert_eq!(outcome.rebound(), 0);
        assert_eq!(outcome.added(), 0);
        assert_eq!(outcome.removed(), 0);
    }
}
