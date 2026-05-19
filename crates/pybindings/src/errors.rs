//! Python error conversion for Rust dataplane failures.

use gatherlink_dataplane::engine::DataplaneError;
use gatherlink_dataplane::udp_service::UdpServiceError;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::PyErr;

/// Convert UDP service configuration/bind errors into Python exceptions.
pub fn udp_error_to_py(error: UdpServiceError) -> PyErr {
    match error {
        UdpServiceError::EmptyServiceName
        | UdpServiceError::MissingListenAddress
        | UdpServiceError::DuplicateServiceName(_)
        | UdpServiceError::DuplicateListenAddress(_)
        | UdpServiceError::DuplicatePathId(_)
        | UdpServiceError::MissingPath
        | UdpServiceError::PathMtuTooSmall { .. }
        | UdpServiceError::PathWeightTooSmall { .. }
        | UdpServiceError::ServicePriorityTooSmall { .. }
        | UdpServiceError::IncompatibleListenReapply { .. } => PyValueError::new_err(error.to_string()),
        UdpServiceError::BindFailed(_)
        | UdpServiceError::ConfigureSocketFailed(_)
        | UdpServiceError::CloneFailed(_)
        | UdpServiceError::LocalAddrFailed(_)
        | UdpServiceError::ReceiveFailed(_)
        | UdpServiceError::SendFailed(_) => PyRuntimeError::new_err(error.to_string()),
    }
}

/// Convert dataplane execution errors into Python exceptions.
pub fn dataplane_error_to_py(error: DataplaneError) -> PyErr {
    match error {
        DataplaneError::UnknownService(_) | DataplaneError::NoDatagramForwarded => {
            PyValueError::new_err(error.to_string())
        }
        DataplaneError::UdpService(error) => udp_error_to_py(error),
        DataplaneError::Protocol(_)
        | DataplaneError::NoPathAvailable
        | DataplaneError::UnexpectedFrameKind
        | DataplaneError::BatchDatagramMismatch
        | DataplaneError::InvalidFragmentPlan
        | DataplaneError::TooManyFragments(_)
        | DataplaneError::FrameExceedsPathMtu { .. } => PyRuntimeError::new_err(error.to_string()),
    }
}
