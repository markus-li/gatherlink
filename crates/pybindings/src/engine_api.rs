//! Python-facing core dataplane API.
//!
//! This bridge intentionally exposes a narrow execution handle. Python can bind
//! compiled services, reapply compiled runtime config, and step the engine for
//! tests, but packet processing stays in Rust.

use gatherlink_dataplane::engine::CoreDataplane;
use gatherlink_dataplane::runtime_config::CoreRuntimeConfig;
use pyo3::prelude::*;

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
}
