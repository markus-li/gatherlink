//! Path transport UDP sockets.
//!
//! These sockets carry encoded Gatherlink frames between peers. Python decides
//! which path endpoints exist; Rust only binds, sends, receives, and counts.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
use std::time::Duration;

use gatherlink_protocol::ids::PathId;

use crate::runtime_config::CorePathConfig;
use crate::udp_service::UdpServiceError;

/// One bound UDP socket for one Gatherlink path flow.
#[derive(Debug)]
pub struct PathTransportSocket {
    path_id: PathId,
    socket: UdpSocket,
    remote: SocketAddr,
}

impl PathTransportSocket {
    /// Bind one path transport socket from already-compiled path config.
    pub fn bind(path: &CorePathConfig) -> Result<Option<Self>, UdpServiceError> {
        let Some(bind) = path.transport_bind() else {
            return Ok(None);
        };
        let Some(remote) = path.transport_remote() else {
            return Ok(None);
        };
        let socket = UdpSocket::bind(bind).map_err(UdpServiceError::BindFailed)?;
        socket
            .set_read_timeout(Some(Duration::from_millis(5)))
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        Ok(Some(Self {
            path_id: path.path_id(),
            socket,
            remote,
        }))
    }

    /// Wire path id this socket represents.
    pub fn path_id(&self) -> PathId {
        self.path_id
    }

    /// Actual local socket address, useful for tests using port zero.
    pub fn local_addr(&self) -> Result<SocketAddr, UdpServiceError> {
        self.socket.local_addr().map_err(UdpServiceError::LocalAddrFailed)
    }

    /// Send one already-encoded Gatherlink frame to the configured remote path endpoint.
    pub fn send_frame(&self, frame: &[u8]) -> Result<usize, UdpServiceError> {
        self.socket
            .send_to(frame, self.remote)
            .map_err(UdpServiceError::SendFailed)
    }

    /// Receive one Gatherlink frame if this path socket has data available.
    pub fn try_recv_frame(&self, buffer: &mut [u8]) -> Result<Option<(usize, SocketAddr)>, UdpServiceError> {
        self.socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        let result = match self.socket.recv_from(buffer) {
            Ok(received) => Ok(Some(received)),
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                Ok(None)
            }
            Err(error) => Err(UdpServiceError::ReceiveFailed(error)),
        };
        self.socket
            .set_nonblocking(false)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        result
    }
}

/// Bound path transport sockets indexed by path id.
#[derive(Debug, Default)]
pub struct PathTransportSet {
    sockets: HashMap<PathId, PathTransportSocket>,
}

impl PathTransportSet {
    /// Bind every path that has transport endpoints.
    pub fn bind(paths: &[CorePathConfig]) -> Result<Self, UdpServiceError> {
        let mut sockets = HashMap::new();
        for path in paths {
            if let Some(socket) = PathTransportSocket::bind(path)? {
                sockets.insert(path.path_id(), socket);
            }
        }
        Ok(Self { sockets })
    }

    /// Return whether any real path transport sockets are active.
    pub fn is_empty(&self) -> bool {
        self.sockets.is_empty()
    }

    /// Send an encoded frame on its selected path id.
    pub fn send_frame(&self, path_id: PathId, frame: &[u8]) -> Result<usize, UdpServiceError> {
        let socket = self
            .sockets
            .get(&path_id)
            .ok_or(UdpServiceError::MissingPathTransport { path_id })?;
        socket.send_frame(frame)
    }

    /// Receive at most one frame from each path socket.
    pub fn receive_available(&self, max_frames: usize) -> Result<Vec<ReceivedPathFrame>, UdpServiceError> {
        let mut received = Vec::new();
        for socket in self.sockets.values() {
            if received.len() >= max_frames {
                break;
            }
            let mut buffer = vec![0_u8; u16::MAX as usize];
            if let Some((length, source)) = socket.try_recv_frame(&mut buffer)? {
                received.push(ReceivedPathFrame {
                    path_id: socket.path_id(),
                    source,
                    bytes: buffer[..length].to_vec(),
                });
            }
        }
        Ok(received)
    }

    /// Return actual bound path socket addresses.
    pub fn local_addrs(&self) -> Result<HashMap<PathId, SocketAddr>, UdpServiceError> {
        self.sockets
            .iter()
            .map(|(path_id, socket)| Ok((*path_id, socket.local_addr()?)))
            .collect()
    }
}

/// One encoded frame received from one path socket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedPathFrame {
    pub path_id: PathId,
    pub source: SocketAddr,
    pub bytes: Vec<u8>,
}
