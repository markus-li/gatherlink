//! gatherlink-pybindings
//!
//! PyO3 bridge exposing the narrow Rust dataplane API to Python.

use pyo3::prelude::*;

pub mod dto;
pub mod engine_api;
pub mod errors;

/// Python extension module for the Gatherlink Rust dataplane.
#[pymodule]
fn gatherlink_pybindings(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<dto::PyUdpServiceConfig>()?;
    module.add_class::<dto::PyPathConfig>()?;
    module.add_class::<dto::PySchedulerConfig>()?;
    module.add_class::<dto::PyForwardOutcome>()?;
    module.add_class::<dto::PyRemoteDeliverOutcome>()?;
    module.add_class::<dto::PyReservedServiceEvent>()?;
    module.add_class::<dto::PyReapplyOutcome>()?;
    module.add_class::<engine_api::PyCoreDataplane>()?;
    Ok(())
}
