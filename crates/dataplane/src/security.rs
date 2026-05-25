//! Runtime transport security execution.
//!
//! Python owns identity, peer trust, handshake policy, and when to compile new
//! session material. This module only wraps and unwraps path-socket bytes.

use std::collections::HashMap;

use gatherlink_crypto::envelope::{clear_receiver_index, DecryptedPacket, ReservedSendCounters, TransportKeys};
use gatherlink_crypto::errors::CryptoError;
use gatherlink_protocol::frame::Frame;
use gatherlink_protocol::ids::ServiceId;

use crate::runtime_config::{TransportSecurityConfig, TransportSecuritySessionConfig};

/// Mutable transport-security executor used by the dataplane hot path.
#[derive(Debug, Clone)]
pub enum TransportSecurity {
    None,
    Static {
        keys: TransportKeys,
    },
    StaticSessions {
        sessions: HashMap<u32, TransportKeys>,
        service_sessions: HashMap<ServiceId, u32>,
        default_session: Option<u32>,
    },
}

/// One authenticated frame and the session that produced it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnprotectedFrame {
    pub frame: Frame,
    pub local_receiver_index: Option<u32>,
}

/// Reserved protection state for one already-planned transmit batch.
#[derive(Clone)]
pub enum ReservedFrameProtection {
    None,
    Secure(ReservedSendCounters),
}

impl TransportSecurity {
    /// Compile executable transport security from Python-owned runtime state.
    pub fn from_config(config: &TransportSecurityConfig) -> Self {
        match config {
            TransportSecurityConfig::None => Self::None,
            TransportSecurityConfig::Static {
                local_receiver_index,
                remote_receiver_index,
                send_key,
                receive_key,
            } => Self::Static {
                keys: TransportKeys::new_with_receiver_indexes(
                    *local_receiver_index,
                    *remote_receiver_index,
                    *send_key,
                    *receive_key,
                ),
            },
            TransportSecurityConfig::StaticSessions(sessions) => Self::compile_static_sessions(sessions),
        }
    }

    fn compile_static_sessions(sessions: &[TransportSecuritySessionConfig]) -> Self {
        let mut compiled = HashMap::with_capacity(sessions.len());
        let mut service_sessions = HashMap::new();
        for session in sessions {
            let local_receiver_index = session.local_receiver_index();
            compiled.insert(
                local_receiver_index,
                TransportKeys::new_with_receiver_indexes(
                    local_receiver_index,
                    session.remote_receiver_index(),
                    session.send_key(),
                    session.receive_key(),
                ),
            );
            for service_id in session.service_ids() {
                service_sessions.entry(*service_id).or_insert(local_receiver_index);
            }
        }
        let default_session = if compiled.len() == 1 {
            compiled.keys().next().copied()
        } else {
            None
        };
        Self::StaticSessions {
            sessions: compiled,
            service_sessions,
            default_session,
        }
    }

    /// Protect one compact Gatherlink frame before path-socket send.
    ///
    /// Plain transport emits compact v1. Static secure transport encrypts
    /// compact v2 plaintext so service/path metadata is not visible on the
    /// carrier.
    pub fn protect_frame(&mut self, frame: &Frame) -> Result<Vec<u8>, CryptoError> {
        self.protect_frame_for_service(frame.service_id, frame)
    }

    /// Protect one frame using the session Python mapped to this service.
    pub fn protect_frame_for_service(&mut self, service_id: ServiceId, frame: &Frame) -> Result<Vec<u8>, CryptoError> {
        self.protect_frame_for_service_or_session(service_id, None, frame)
    }

    /// Protect one frame using an explicit session when Python/Rust app-source state has one.
    pub fn protect_frame_for_service_or_session(
        &mut self,
        service_id: ServiceId,
        session_key: Option<u32>,
        frame: &Frame,
    ) -> Result<Vec<u8>, CryptoError> {
        match self {
            Self::None => frame.encode_v1().map_err(|_| CryptoError::SilentDrop),
            Self::Static { keys } => keys.encrypt_frame_with_plaintext(frame.encoded_v2_len(), |output| {
                frame.encode_v2_into(output).map_err(|_| CryptoError::SilentDrop)
            }),
            Self::StaticSessions {
                sessions,
                service_sessions,
                default_session,
            } => {
                let session_key = session_key
                    .or_else(|| service_sessions.get(&service_id).copied())
                    .or(*default_session)
                    .ok_or(CryptoError::SilentDrop)?;
                let keys = sessions.get_mut(&session_key).ok_or(CryptoError::SilentDrop)?;
                keys.encrypt_frame_with_plaintext(frame.encoded_v2_len(), |output| {
                    frame.encode_v2_into(output).map_err(|_| CryptoError::SilentDrop)
                })
            }
        }
    }

    /// Return the local receiver index for the outbound service, when known.
    pub fn session_key_for_service(&self, service_id: ServiceId) -> Option<u32> {
        match self {
            Self::None | Self::Static { .. } => None,
            Self::StaticSessions {
                service_sessions,
                default_session,
                ..
            } => service_sessions.get(&service_id).copied().or(*default_session),
        }
    }

    /// Authenticate and unwrap one path-socket packet into a compact frame.
    ///
    /// Plain transport decodes compact v1. Static secure transport decrypts
    /// compact v2 and returns the same compact frame model.
    pub fn unprotect_packet(&mut self, packet: &[u8]) -> Result<Frame, CryptoError> {
        Ok(self.unprotect_packet_with_session(packet)?.frame)
    }

    /// Authenticate and unwrap one packet, preserving the matched receiver index.
    pub fn unprotect_packet_with_session(&mut self, packet: &[u8]) -> Result<UnprotectedFrame, CryptoError> {
        match self {
            Self::None => Ok(UnprotectedFrame {
                frame: Frame::decode_v1(packet).map_err(|_| CryptoError::SilentDrop)?,
                local_receiver_index: None,
            }),
            Self::Static { keys } => {
                let DecryptedPacket { plaintext, .. } = keys.decrypt_packet(packet)?;
                Ok(UnprotectedFrame {
                    frame: Frame::decode_v2_owned(plaintext).map_err(|_| CryptoError::SilentDrop)?,
                    local_receiver_index: None,
                })
            }
            Self::StaticSessions { sessions, .. } => {
                let receiver_index = clear_receiver_index(packet)?;
                let keys = sessions.get_mut(&receiver_index).ok_or(CryptoError::SilentDrop)?;
                let DecryptedPacket { plaintext, .. } = keys.decrypt_packet(packet)?;
                Ok(UnprotectedFrame {
                    frame: Frame::decode_v2_owned(plaintext).map_err(|_| CryptoError::SilentDrop)?,
                    local_receiver_index: Some(receiver_index),
                })
            }
        }
    }

    /// Return the compact Gatherlink frame length that must fit inside the configured path MTU.
    ///
    /// Python subtracts outer security-envelope overhead before compiling path
    /// MTU into Rust runtime config. That keeps scheduling and fragmentation
    /// decisions independent from the current security mode while still letting
    /// Rust avoid an allocation just to measure the encoded frame.
    pub fn path_frame_len(&self, frame: &Frame) -> usize {
        match self {
            Self::None => frame.encoded_v1_len(),
            Self::Static { .. } | Self::StaticSessions { .. } => frame.encoded_v2_len(),
        }
    }

    /// Reserve the mutable security state needed to protect a batch later.
    ///
    /// This keeps counter allocation serialized in the owning dataplane while
    /// allowing the CPU-heavy AEAD work to happen on per-path worker threads.
    pub fn reserve_frame_protection_batch(
        &mut self,
        service_id: ServiceId,
        session_key: Option<u32>,
        count: usize,
    ) -> Result<ReservedFrameProtection, CryptoError> {
        match self {
            Self::None => Ok(ReservedFrameProtection::None),
            Self::Static { keys } => Ok(ReservedFrameProtection::Secure(keys.reserve_send_counters(count)?)),
            Self::StaticSessions {
                sessions,
                service_sessions,
                default_session,
            } => {
                let session_key = session_key
                    .or_else(|| service_sessions.get(&service_id).copied())
                    .or(*default_session)
                    .ok_or(CryptoError::SilentDrop)?;
                let keys = sessions.get_mut(&session_key).ok_or(CryptoError::SilentDrop)?;
                Ok(ReservedFrameProtection::Secure(keys.reserve_send_counters(count)?))
            }
        }
    }
}

impl ReservedFrameProtection {
    /// Protect one frame using previously reserved mutable session state.
    pub fn protect_frame(&self, index: usize, frame: &Frame) -> Result<Vec<u8>, CryptoError> {
        match self {
            Self::None => frame.encode_v1().map_err(|_| CryptoError::SilentDrop),
            Self::Secure(counters) => counters.encrypt_frame_with_plaintext(index, frame.encoded_v2_len(), |output| {
                frame.encode_v2_into(output).map_err(|_| CryptoError::SilentDrop)
            }),
        }
    }
}
