//! Path transport UDP sockets.
//!
//! These sockets carry encoded Gatherlink frames between peers. Python decides
//! which path endpoints exist; Rust only binds, sends, receives, and counts.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
use std::time::Duration;

use gatherlink_crypto::envelope::encrypt_frame_with_counter;
use gatherlink_protocol::ids::PathId;

use crate::runtime_config::{CorePathConfig, RelayHopSendConfig};
use crate::udp_service::UdpServiceError;

/// One bound UDP socket for one Gatherlink path flow.
#[derive(Debug)]
pub struct PathTransportSocket {
    path_id: PathId,
    socket: UdpSocket,
    remote: Option<SocketAddr>,
    relay_send: Option<RelayHopSendState>,
}

#[derive(Debug)]
struct RelayHopSendState {
    relay_receiver_index: u32,
    send_key: [u8; 32],
    next_counter: u64,
}

impl RelayHopSendState {
    fn from_config(config: &RelayHopSendConfig) -> Self {
        Self {
            relay_receiver_index: config.relay_receiver_index(),
            send_key: config.send_key(),
            next_counter: 0,
        }
    }

    fn wrap(&mut self, endpoint_packet: &[u8]) -> Result<Vec<u8>, UdpServiceError> {
        let counter = self.next_counter;
        self.next_counter = self.next_counter.wrapping_add(1);
        encrypt_frame_with_counter(self.relay_receiver_index, &self.send_key, counter, endpoint_packet)
            .map_err(|_error| UdpServiceError::RelayHopWrapFailed)
    }
}

impl PathTransportSocket {
    /// Bind one path transport socket from already-compiled path config.
    pub fn bind(path: &CorePathConfig) -> Result<Option<Self>, UdpServiceError> {
        let Some(bind) = path.transport_bind() else {
            return Ok(None);
        };
        let socket = UdpSocket::bind(bind).map_err(UdpServiceError::BindFailed)?;
        socket
            .set_read_timeout(Some(Duration::from_millis(5)))
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        Ok(Some(Self {
            path_id: path.path_id(),
            socket,
            remote: path.transport_remote(),
            relay_send: path.relay_send().map(RelayHopSendState::from_config),
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

    /// Return whether this live socket can be reused for a requested path.
    pub fn can_preserve_for(&self, path: &CorePathConfig) -> Result<bool, UdpServiceError> {
        Ok(self.path_id == path.path_id() && Some(self.local_addr()?) == path.transport_bind())
    }

    /// Clone the live socket while applying updated path metadata such as remote.
    pub fn clone_with_path(&self, path: &CorePathConfig) -> Result<Self, UdpServiceError> {
        let socket = self.socket.try_clone().map_err(UdpServiceError::CloneFailed)?;
        Ok(Self {
            path_id: path.path_id(),
            socket,
            remote: path.transport_remote(),
            relay_send: path.relay_send().map(RelayHopSendState::from_config),
        })
    }

    /// Send one already-encoded Gatherlink frame to the configured remote path endpoint.
    pub fn send_frame(&mut self, frame: &[u8]) -> Result<usize, UdpServiceError> {
        let remote = self
            .remote
            .ok_or(UdpServiceError::MissingPathRemote { path_id: self.path_id })?;
        self.send_frame_to(frame, remote)
    }

    /// Send one already-encoded Gatherlink frame to a caller-selected peer endpoint.
    pub fn send_frame_to(&mut self, frame: &[u8], remote: SocketAddr) -> Result<usize, UdpServiceError> {
        let packet = if let Some(relay_send) = self.relay_send.as_mut() {
            relay_send.wrap(frame)?
        } else {
            frame.to_vec()
        };
        self.socket
            .send_to(&packet, remote)
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

    /// Bind path transports while preserving compatible live sockets.
    ///
    /// Full config reapply should not tear down a shared sink carrier socket just
    /// because Python refreshed equivalent runtime state. New or incompatible
    /// paths are still bound before the set is swapped into the engine.
    pub fn rebind_preserving(&self, paths: &[CorePathConfig]) -> Result<Self, UdpServiceError> {
        let mut sockets = HashMap::new();
        for path in paths {
            let Some(_bind) = path.transport_bind() else {
                continue;
            };
            let socket = if let Some(current) = self.sockets.get(&path.path_id()) {
                if current.can_preserve_for(path)? {
                    current.clone_with_path(path)?
                } else {
                    PathTransportSocket::bind(path)?.ok_or(UdpServiceError::MissingPathTransport {
                        path_id: path.path_id(),
                    })?
                }
            } else {
                PathTransportSocket::bind(path)?.ok_or(UdpServiceError::MissingPathTransport {
                    path_id: path.path_id(),
                })?
            };
            sockets.insert(path.path_id(), socket);
        }
        Ok(Self { sockets })
    }

    /// Return whether any real path transport sockets are active.
    pub fn is_empty(&self) -> bool {
        self.sockets.is_empty()
    }

    /// Send an encoded frame on its selected path id.
    pub fn send_frame(&mut self, path_id: PathId, frame: &[u8]) -> Result<usize, UdpServiceError> {
        let socket = self
            .sockets
            .get_mut(&path_id)
            .ok_or(UdpServiceError::MissingPathTransport { path_id })?;
        socket.send_frame(frame)
    }

    /// Send an encoded frame to a caller-selected remote endpoint on one path.
    pub fn send_frame_to(
        &mut self,
        path_id: PathId,
        frame: &[u8],
        remote: SocketAddr,
    ) -> Result<usize, UdpServiceError> {
        let socket = self
            .sockets
            .get_mut(&path_id)
            .ok_or(UdpServiceError::MissingPathTransport { path_id })?;
        socket.send_frame_to(frame, remote)
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
