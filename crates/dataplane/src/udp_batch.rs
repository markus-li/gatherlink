//! Batched UDP socket receive helpers.
//!
//! These helpers are execution plumbing for the Rust hot path. They do not
//! decide routing, policy, helper behavior, or control semantics; they only
//! amortize syscall overhead when a UDP socket is already known to be hot.

use std::cell::RefCell;
use std::io::{self, ErrorKind};
use std::net::{SocketAddr, UdpSocket};

#[cfg(unix)]
use std::mem::MaybeUninit;
#[cfg(unix)]
use std::os::fd::AsRawFd;

const RECV_BATCH_LIMIT: usize = 64;
const RECV_BUFFER_BYTES: usize = u16::MAX as usize;

thread_local! {
    static RECV_BUFFERS: RefCell<Vec<Vec<u8>>> = const { RefCell::new(Vec::new()) };
}

/// One borrowed datagram received by a batched UDP drain.
#[derive(Debug, Clone, Copy)]
pub struct BorrowedUdpDatagram<'a> {
    pub payload: &'a [u8],
    pub source: SocketAddr,
}

/// One mutable datagram received into caller-owned reusable storage.
#[derive(Debug)]
pub struct MutableUdpDatagram<'a> {
    pub payload: &'a mut [u8],
}

/// Drain queued UDP datagrams and call `handle` for each packet.
///
/// The callback must finish before the next callback because the payload slice
/// borrows one reusable receive buffer.
#[cfg(unix)]
pub fn drain_udp_socket<E, F>(socket: &UdpSocket, max_datagrams: usize, mut handle: F) -> io::Result<Result<usize, E>>
where
    F: FnMut(BorrowedUdpDatagram<'_>) -> Result<(), E>,
{
    let mut handled = 0_usize;
    while handled < max_datagrams {
        let batch_len = (max_datagrams - handled).min(RECV_BATCH_LIMIT);
        let batch_result = RECV_BUFFERS.with(|buffers| {
            let mut buffers = buffers.borrow_mut();
            while buffers.len() < batch_len {
                buffers.push(vec![0_u8; RECV_BUFFER_BYTES]);
            }

            let mut names = vec![MaybeUninit::<libc::sockaddr_storage>::zeroed(); batch_len];
            let mut iovecs = Vec::with_capacity(batch_len);
            let mut messages = Vec::with_capacity(batch_len);
            for index in 0..batch_len {
                iovecs.push(libc::iovec {
                    iov_base: buffers[index].as_mut_ptr().cast::<libc::c_void>(),
                    iov_len: buffers[index].len(),
                });
                messages.push(libc::mmsghdr {
                    msg_hdr: libc::msghdr {
                        msg_name: names[index].as_mut_ptr().cast::<libc::c_void>(),
                        msg_namelen: std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t,
                        msg_iov: &mut iovecs[index],
                        msg_iovlen: 1,
                        msg_control: std::ptr::null_mut(),
                        msg_controllen: 0,
                        msg_flags: 0,
                    },
                    msg_len: 0,
                });
            }

            let received = unsafe {
                libc::recvmmsg(
                    socket.as_raw_fd(),
                    messages.as_mut_ptr(),
                    batch_len as libc::c_uint,
                    libc::MSG_DONTWAIT,
                    std::ptr::null_mut(),
                )
            };
            if received < 0 {
                let error = io::Error::last_os_error();
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) {
                    return Ok(Ok(0));
                }
                return Err(error);
            }
            if received == 0 {
                return Ok(Ok(0));
            }

            for index in 0..received as usize {
                let source = sockaddr_storage_to_socket_addr(unsafe { names[index].assume_init_ref() })?;
                let length = messages[index].msg_len as usize;
                if let Err(error) = handle(BorrowedUdpDatagram {
                    payload: &buffers[index][..length],
                    source,
                }) {
                    return Ok(Err(error));
                }
            }
            Ok(Ok(received as usize))
        })?;
        match batch_result {
            Ok(0) => break,
            Ok(count) => handled += count,
            Err(error) => return Ok(Err(error)),
        }
    }
    Ok(Ok(handled))
}

#[cfg(not(unix))]
pub fn drain_udp_socket<E, F>(socket: &UdpSocket, max_datagrams: usize, mut handle: F) -> io::Result<Result<usize, E>>
where
    F: FnMut(BorrowedUdpDatagram<'_>) -> Result<(), E>,
{
    let mut buffer = vec![0_u8; RECV_BUFFER_BYTES];
    let mut handled = 0_usize;
    while handled < max_datagrams {
        match socket.recv_from(&mut buffer) {
            Ok((length, source)) => {
                if let Err(error) = handle(BorrowedUdpDatagram {
                    payload: &buffer[..length],
                    source,
                }) {
                    return Ok(Err(error));
                }
                handled += 1;
            }
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                break;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(Ok(handled))
}

/// Drain queued UDP datagrams into caller-owned reusable buffers.
///
/// This variant exists for relay hot paths that can authenticate a hop envelope
/// in-place and then forward a slice from the same receive buffer.
#[cfg(unix)]
pub fn drain_udp_socket_mut<E, F>(
    socket: &UdpSocket,
    buffers: &mut Vec<Vec<u8>>,
    max_datagrams: usize,
    mut handle: F,
) -> io::Result<Result<usize, E>>
where
    F: FnMut(usize, MutableUdpDatagram<'_>) -> Result<(), E>,
{
    let mut handled = 0_usize;
    while handled < max_datagrams {
        let batch_len = (max_datagrams - handled).min(RECV_BATCH_LIMIT);
        while buffers.len() < handled + batch_len {
            buffers.push(vec![0_u8; RECV_BUFFER_BYTES]);
        }

        let mut names = vec![MaybeUninit::<libc::sockaddr_storage>::zeroed(); batch_len];
        let mut iovecs = Vec::with_capacity(batch_len);
        let mut messages = Vec::with_capacity(batch_len);
        for index in 0..batch_len {
            iovecs.push(libc::iovec {
                iov_base: buffers[handled + index].as_mut_ptr().cast::<libc::c_void>(),
                iov_len: buffers[handled + index].len(),
            });
            messages.push(libc::mmsghdr {
                msg_hdr: libc::msghdr {
                    msg_name: names[index].as_mut_ptr().cast::<libc::c_void>(),
                    msg_namelen: std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t,
                    msg_iov: &mut iovecs[index],
                    msg_iovlen: 1,
                    msg_control: std::ptr::null_mut(),
                    msg_controllen: 0,
                    msg_flags: 0,
                },
                msg_len: 0,
            });
        }

        let received = unsafe {
            libc::recvmmsg(
                socket.as_raw_fd(),
                messages.as_mut_ptr(),
                batch_len as libc::c_uint,
                libc::MSG_DONTWAIT,
                std::ptr::null_mut(),
            )
        };
        if received < 0 {
            let error = io::Error::last_os_error();
            if matches!(
                error.kind(),
                ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
            ) {
                break;
            }
            return Err(error);
        }
        if received == 0 {
            break;
        }

        for index in 0..received as usize {
            let buffer_index = handled + index;
            let length = messages[index].msg_len as usize;
            if let Err(error) = handle(
                buffer_index,
                MutableUdpDatagram {
                    payload: &mut buffers[buffer_index][..length],
                },
            ) {
                return Ok(Err(error));
            }
        }
        handled += received as usize;
    }
    Ok(Ok(handled))
}

#[cfg(not(unix))]
pub fn drain_udp_socket_mut<E, F>(
    socket: &UdpSocket,
    buffers: &mut Vec<Vec<u8>>,
    max_datagrams: usize,
    mut handle: F,
) -> io::Result<Result<usize, E>>
where
    F: FnMut(usize, MutableUdpDatagram<'_>) -> Result<(), E>,
{
    let mut handled = 0_usize;
    while handled < max_datagrams {
        while buffers.len() <= handled {
            buffers.push(vec![0_u8; RECV_BUFFER_BYTES]);
        }
        match socket.recv_from(&mut buffers[handled]) {
            Ok((length, _source)) => {
                if let Err(error) = handle(
                    handled,
                    MutableUdpDatagram {
                        payload: &mut buffers[handled][..length],
                    },
                ) {
                    return Ok(Err(error));
                }
                handled += 1;
            }
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                break;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(Ok(handled))
}

/// Send payloads to one UDP target with batched syscalls where available.
#[cfg(unix)]
pub fn send_udp_many(socket: &UdpSocket, target: SocketAddr, payloads: &[&[u8]]) -> io::Result<usize> {
    let mut sent = 0_usize;
    while sent < payloads.len() {
        let batch_len = (payloads.len() - sent).min(RECV_BATCH_LIMIT);
        let mut target_storage = socket_addr_to_sockaddr_storage(target);
        let mut iovecs = Vec::with_capacity(batch_len);
        let mut messages = Vec::with_capacity(batch_len);
        for index in 0..batch_len {
            let payload = payloads[sent + index];
            iovecs.push(libc::iovec {
                iov_base: payload.as_ptr().cast::<libc::c_void>().cast_mut(),
                iov_len: payload.len(),
            });
            messages.push(libc::mmsghdr {
                msg_hdr: libc::msghdr {
                    msg_name: (&mut target_storage.storage as *mut libc::sockaddr_storage).cast::<libc::c_void>(),
                    msg_namelen: target_storage.length,
                    msg_iov: &mut iovecs[index],
                    msg_iovlen: 1,
                    msg_control: std::ptr::null_mut(),
                    msg_controllen: 0,
                    msg_flags: 0,
                },
                msg_len: 0,
            });
        }
        let batch_sent = unsafe {
            libc::sendmmsg(
                socket.as_raw_fd(),
                messages.as_mut_ptr(),
                batch_len as libc::c_uint,
                libc::MSG_DONTWAIT,
            )
        };
        if batch_sent < 0 {
            let error = io::Error::last_os_error();
            if matches!(
                error.kind(),
                ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
            ) && sent > 0
            {
                break;
            }
            return Err(error);
        }
        if batch_sent == 0 {
            break;
        }
        sent += batch_sent as usize;
    }
    Ok(sent)
}

/// Send payloads to one UDP target with ordinary sends on platforms without sendmmsg.
#[cfg(not(unix))]
pub fn send_udp_many(socket: &UdpSocket, target: SocketAddr, payloads: &[&[u8]]) -> io::Result<usize> {
    let mut sent = 0_usize;
    for payload in payloads {
        socket.send_to(payload, target)?;
        sent += 1;
    }
    Ok(sent)
}

#[cfg(unix)]
fn sockaddr_storage_to_socket_addr(storage: &libc::sockaddr_storage) -> io::Result<SocketAddr> {
    match storage.ss_family as libc::c_int {
        libc::AF_INET => {
            let addr = unsafe { &*(std::ptr::from_ref(storage).cast::<libc::sockaddr_in>()) };
            let ip = std::net::Ipv4Addr::from(u32::from_be(addr.sin_addr.s_addr));
            Ok(SocketAddr::new(ip.into(), u16::from_be(addr.sin_port)))
        }
        libc::AF_INET6 => {
            let addr = unsafe { &*(std::ptr::from_ref(storage).cast::<libc::sockaddr_in6>()) };
            let ip = std::net::Ipv6Addr::from(addr.sin6_addr.s6_addr);
            Ok(SocketAddr::new(ip.into(), u16::from_be(addr.sin6_port)))
        }
        family => Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unsupported UDP sockaddr family {family}"),
        )),
    }
}

#[cfg(unix)]
struct SockaddrStorage {
    storage: libc::sockaddr_storage,
    length: libc::socklen_t,
}

#[cfg(unix)]
fn socket_addr_to_sockaddr_storage(addr: SocketAddr) -> SockaddrStorage {
    match addr {
        SocketAddr::V4(addr) => {
            let mut storage = unsafe { std::mem::zeroed::<libc::sockaddr_storage>() };
            let sockaddr = libc::sockaddr_in {
                sin_family: libc::AF_INET as libc::sa_family_t,
                sin_port: addr.port().to_be(),
                sin_addr: libc::in_addr {
                    s_addr: u32::from_be_bytes(addr.ip().octets()).to_be(),
                },
                sin_zero: [0; 8],
            };
            unsafe {
                std::ptr::write(
                    (&mut storage as *mut libc::sockaddr_storage).cast::<libc::sockaddr_in>(),
                    sockaddr,
                );
            }
            SockaddrStorage {
                storage,
                length: std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
            }
        }
        SocketAddr::V6(addr) => {
            let mut storage = unsafe { std::mem::zeroed::<libc::sockaddr_storage>() };
            let sockaddr = libc::sockaddr_in6 {
                sin6_family: libc::AF_INET6 as libc::sa_family_t,
                sin6_port: addr.port().to_be(),
                sin6_flowinfo: addr.flowinfo(),
                sin6_addr: libc::in6_addr {
                    s6_addr: addr.ip().octets(),
                },
                sin6_scope_id: addr.scope_id(),
            };
            unsafe {
                std::ptr::write(
                    (&mut storage as *mut libc::sockaddr_storage).cast::<libc::sockaddr_in6>(),
                    sockaddr,
                );
            }
            SockaddrStorage {
                storage,
                length: std::mem::size_of::<libc::sockaddr_in6>() as libc::socklen_t,
            }
        }
    }
}
