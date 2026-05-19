//! Runtime transport security execution.
//!
//! Python owns identity, peer trust, handshake policy, and when to compile new
//! session material. This module only wraps and unwraps path-socket bytes.

use gatherlink_crypto::envelope::{DecryptedPacket, TransportKeys};
use gatherlink_crypto::errors::CryptoError;
use gatherlink_protocol::frame::Frame;

use crate::runtime_config::TransportSecurityConfig;

/// Mutable transport-security executor used by the dataplane hot path.
#[derive(Debug, Clone)]
pub enum TransportSecurity {
    None,
    Static { keys: TransportKeys },
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
        }
    }

    /// Protect one compact Gatherlink frame before path-socket send.
    ///
    /// Plain transport emits compact v1. Static secure transport encrypts
    /// compact v2 plaintext so service/path metadata is not visible on the
    /// carrier.
    pub fn protect_frame(&mut self, frame: &Frame) -> Result<Vec<u8>, CryptoError> {
        match self {
            Self::None => frame.encode_v1().map_err(|_| CryptoError::SilentDrop),
            Self::Static { keys } => {
                let compact_v2 = frame.encode_v2().map_err(|_| CryptoError::SilentDrop)?;
                keys.encrypt_frame(&compact_v2)
            }
        }
    }

    /// Authenticate and unwrap one path-socket packet into a compact frame.
    ///
    /// Plain transport decodes compact v1. Static secure transport decrypts
    /// compact v2 and returns the same compact frame model.
    pub fn unprotect_packet(&mut self, packet: &[u8]) -> Result<Frame, CryptoError> {
        match self {
            Self::None => Frame::decode_v1(packet).map_err(|_| CryptoError::SilentDrop),
            Self::Static { keys } => {
                let DecryptedPacket { plaintext, .. } = keys.decrypt_packet(packet)?;
                Frame::decode_v2(&plaintext).map_err(|_| CryptoError::SilentDrop)
            }
        }
    }
}
