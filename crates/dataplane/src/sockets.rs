//! Path transport UDP sockets.
//!
//! These sockets carry encoded Gatherlink frames between peers. Python decides
//! which path endpoints exist; Rust only binds, sends, receives, and counts.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
#[cfg(unix)]
use std::os::fd::AsRawFd;

use gatherlink_crypto::envelope::encrypt_frame_with_counter;
use gatherlink_protocol::ids::PathId;

use crate::runtime_config::{CorePathConfig, RelayHopSendConfig};
use crate::udp_batch::{drain_udp_socket, send_udp_many};
use crate::udp_service::UdpServiceError;

const UDP_SOCKET_BUFFER_BYTES: usize = 1024 * 1024 * 1024;
/// Maximum carrier datagrams drained from one path socket before polling siblings.
///
/// This is execution fairness, not scheduling policy. Python still decides
/// which paths exist and how traffic is assigned; Rust prevents one hot socket
/// from monopolizing a receive budget while other configured paths overflow.
/// Small carrier packets are syscall-rate limited, so each socket visit can drain more frames.
const SMALL_PACKET_PATH_DRAIN_QUANTUM: usize = 32;
/// Jumbo/coalesced carrier packets are burstier, so lower per-visit drain preserves path fairness.
const LARGE_PACKET_PATH_DRAIN_QUANTUM: usize = 16;
const LARGE_PACKET_MTU_THRESHOLD: usize = 1500;

/// One bound UDP socket for one Gatherlink path flow.
#[derive(Debug)]
pub struct PathTransportSocket {
    path_id: PathId,
    socket: UdpSocket,
    remote: Option<SocketAddr>,
    relay_send: Option<RelayHopSendState>,
    drain_quantum: usize,
}

/// Cheap path socket facts exposed for Python diagnostics and scheduling.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PathSocketSnapshot {
    pub receive_buffer_bytes: u64,
    pub send_buffer_bytes: u64,
    pub drain_quantum: u64,
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
        request_udp_socket_buffers(&socket);
        socket
            .set_nonblocking(true)
            .map_err(UdpServiceError::ConfigureSocketFailed)?;
        Ok(Some(Self {
            path_id: path.path_id(),
            socket,
            remote: path.transport_remote(),
            relay_send: path.relay_send().map(RelayHopSendState::from_config),
            drain_quantum: drain_quantum_for_path(path),
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
            drain_quantum: drain_quantum_for_path(path),
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

    /// Send multiple already-encoded Gatherlink frames to one peer endpoint.
    pub fn send_frames_to(&mut self, frames: &[&[u8]], remote: SocketAddr) -> Result<usize, UdpServiceError> {
        if frames.is_empty() {
            return Ok(0);
        }
        if let Some(relay_send) = self.relay_send.as_mut() {
            let wrapped = frames
                .iter()
                .map(|frame| relay_send.wrap(frame))
                .collect::<Result<Vec<_>, _>>()?;
            let payloads = wrapped.iter().map(Vec::as_slice).collect::<Vec<_>>();
            return send_udp_many(&self.socket, remote, &payloads).map_err(UdpServiceError::SendFailed);
        }
        send_udp_many(&self.socket, remote, frames).map_err(UdpServiceError::SendFailed)
    }

    /// Receive one Gatherlink frame if this path socket has data available.
    pub fn try_recv_frame(&self, buffer: &mut [u8]) -> Result<Option<(usize, SocketAddr)>, UdpServiceError> {
        match self.socket.recv_from(buffer) {
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
        }
    }

    fn snapshot(&self) -> PathSocketSnapshot {
        PathSocketSnapshot {
            receive_buffer_bytes: udp_socket_buffer_bytes(&self.socket, SocketBufferKind::Receive),
            send_buffer_bytes: udp_socket_buffer_bytes(&self.socket, SocketBufferKind::Send),
            drain_quantum: self.drain_quantum as u64,
        }
    }
}

#[cfg(unix)]
fn request_udp_socket_buffers(socket: &UdpSocket) {
    // TODO(perf): This is a best-effort execution hint, not policy. Debian may
    // cap the value with net.core.rmem_max/wmem_max; the compatibility backend
    // and operator docs remain responsible for making those limits explicit.
    let value = UDP_SOCKET_BUFFER_BYTES as libc::c_int;
    let value_ptr = (&value as *const libc::c_int).cast::<libc::c_void>();
    let value_len = std::mem::size_of_val(&value) as libc::socklen_t;
    unsafe {
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_RCVBUF,
            value_ptr,
            value_len,
        );
        let _ = libc::setsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            value_ptr,
            value_len,
        );
    }
}

#[cfg(not(unix))]
fn request_udp_socket_buffers(_socket: &UdpSocket) {}

#[cfg(unix)]
#[derive(Debug, Clone, Copy)]
enum SocketBufferKind {
    Receive,
    Send,
}

#[cfg(unix)]
fn udp_socket_buffer_bytes(socket: &UdpSocket, kind: SocketBufferKind) -> u64 {
    let mut value = 0 as libc::c_int;
    let mut value_len = std::mem::size_of_val(&value) as libc::socklen_t;
    let option = match kind {
        SocketBufferKind::Receive => libc::SO_RCVBUF,
        SocketBufferKind::Send => libc::SO_SNDBUF,
    };
    let result = unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            option,
            (&mut value as *mut libc::c_int).cast::<libc::c_void>(),
            &mut value_len,
        )
    };
    if result == 0 && value > 0 {
        value as u64
    } else {
        0
    }
}

#[cfg(not(unix))]
#[derive(Debug, Clone, Copy)]
enum SocketBufferKind {
    Receive,
    Send,
}

#[cfg(not(unix))]
fn udp_socket_buffer_bytes(_socket: &UdpSocket, _kind: SocketBufferKind) -> u64 {
    0
}

fn drain_quantum_for_path(path: &CorePathConfig) -> usize {
    if path.mtu() > LARGE_PACKET_MTU_THRESHOLD {
        LARGE_PACKET_PATH_DRAIN_QUANTUM
    } else {
        SMALL_PACKET_PATH_DRAIN_QUANTUM
    }
}

/// Bound path transport sockets indexed by path id.
#[derive(Debug, Default)]
pub struct PathTransportSet {
    sockets: HashMap<PathId, PathTransportSocket>,
    path_order: Vec<PathId>,
    drain_cursor: usize,
}

impl PathTransportSet {
    /// Bind every path that has transport endpoints.
    pub fn bind(paths: &[CorePathConfig]) -> Result<Self, UdpServiceError> {
        let mut sockets = HashMap::new();
        let mut path_order = Vec::new();
        for path in paths {
            if let Some(socket) = PathTransportSocket::bind(path)? {
                let path_id = path.path_id();
                sockets.insert(path_id, socket);
                path_order.push(path_id);
            }
        }
        Ok(Self {
            sockets,
            path_order,
            drain_cursor: 0,
        })
    }

    /// Bind path transports while preserving compatible live sockets.
    ///
    /// Full config reapply should not tear down a shared sink carrier socket just
    /// because Python refreshed equivalent runtime state. New or incompatible
    /// paths are still bound before the set is swapped into the engine.
    pub fn rebind_preserving(&self, paths: &[CorePathConfig]) -> Result<Self, UdpServiceError> {
        let mut sockets = HashMap::new();
        let mut path_order = Vec::new();
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
            let path_id = path.path_id();
            sockets.insert(path_id, socket);
            path_order.push(path_id);
        }
        let drain_cursor = if path_order.is_empty() {
            0
        } else {
            self.drain_cursor % path_order.len()
        };
        Ok(Self {
            sockets,
            path_order,
            drain_cursor,
        })
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

    /// Send encoded frames to one remote endpoint on one path with batched syscalls when available.
    pub fn send_frames_to(
        &mut self,
        path_id: PathId,
        frames: &[&[u8]],
        remote: SocketAddr,
    ) -> Result<usize, UdpServiceError> {
        let socket = self
            .sockets
            .get_mut(&path_id)
            .ok_or(UdpServiceError::MissingPathTransport { path_id })?;
        socket.send_frames_to(frames, remote)
    }

    /// Receive up to the requested frame budget across all path sockets.
    ///
    /// TODO(perf): Keep this deliberately policy-free. Python controls the
    /// scheduling model, while Rust only drains already-bound carrier sockets.
    /// A hot path can have many frames queued on one socket, so this must not
    /// stop after one frame per path or the Python runner cadence becomes an
    /// artificial throughput cap.
    pub fn receive_available(&mut self, max_frames: usize) -> Result<Vec<ReceivedPathFrame>, UdpServiceError> {
        let mut received = Vec::new();
        let mut buffer = vec![0_u8; u16::MAX as usize];
        let path_count = self.path_order.len();
        if path_count == 0 {
            return Ok(received);
        }
        while received.len() < max_frames {
            let mut drained_this_round = false;
            let start_cursor = self.drain_cursor;
            for offset in 0..path_count {
                if received.len() >= max_frames {
                    break;
                }
                let cursor = (start_cursor + offset) % path_count;
                let path_id = self.path_order[cursor];
                let Some(socket) = self.sockets.get(&path_id) else {
                    continue;
                };
                if let Some((length, source)) = socket.try_recv_frame(&mut buffer)? {
                    drained_this_round = true;
                    received.push(ReceivedPathFrame {
                        path_id: socket.path_id(),
                        source,
                        bytes: buffer[..length].to_vec(),
                    });
                    self.drain_cursor = (cursor + 1) % path_count;
                }
            }
            if !drained_this_round {
                break;
            }
        }
        Ok(received)
    }

    /// Drain carrier frames through a caller-provided handler without allocating one Vec per frame.
    ///
    /// The detailed receive API above intentionally returns owned frames for
    /// tests and diagnostics. The production summary loop only needs to decode
    /// and count packets, so it can borrow one reusable buffer and avoid a hot
    /// allocation/copy per carrier packet.
    pub fn drain_available<E, F>(&mut self, max_frames: usize, mut handle: F) -> Result<Result<(), E>, UdpServiceError>
    where
        F: FnMut(PathId, SocketAddr, &[u8]) -> Result<(), E>,
    {
        let mut drained = 0_usize;
        let path_count = self.path_order.len();
        if path_count == 0 {
            return Ok(Ok(()));
        }
        while drained < max_frames {
            let mut drained_this_round = false;
            let start_cursor = self.drain_cursor;
            for offset in 0..path_count {
                if drained >= max_frames {
                    break;
                }
                let cursor = (start_cursor + offset) % path_count;
                let path_id = self.path_order[cursor];
                let Some(socket) = self.sockets.get(&path_id) else {
                    continue;
                };
                let remaining = max_frames - drained;
                let per_socket_budget = remaining.div_ceil(path_count).min(socket.drain_quantum).max(1);
                match drain_udp_socket(&socket.socket, per_socket_budget, |datagram| {
                    handle(socket.path_id(), datagram.source, datagram.payload)
                })
                .map_err(UdpServiceError::ReceiveFailed)?
                {
                    Ok(count) => {
                        if count > 0 {
                            drained += count;
                            drained_this_round = true;
                            self.drain_cursor = (cursor + 1) % path_count;
                        }
                    }
                    Err(error) => return Ok(Err(error)),
                }
            }
            if !drained_this_round {
                break;
            }
        }
        Ok(Ok(()))
    }

    /// Return actual bound path socket addresses.
    pub fn local_addrs(&self) -> Result<HashMap<PathId, SocketAddr>, UdpServiceError> {
        self.sockets
            .iter()
            .map(|(path_id, socket)| Ok((*path_id, socket.local_addr()?)))
            .collect()
    }

    /// Return local path socket facts without interpreting kernel pressure.
    pub fn socket_snapshots(&self) -> HashMap<PathId, PathSocketSnapshot> {
        self.sockets
            .iter()
            .map(|(path_id, socket)| (*path_id, socket.snapshot()))
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
