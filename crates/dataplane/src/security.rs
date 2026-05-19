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
                receiver_index,
                send_key,
                receive_key,
            } => Self::Static {
                keys: TransportKeys::new(*receiver_index, *send_key, *receive_key),
            },
        }
    }

    /// Protect one already-encoded compact v1 Gatherlink frame before path-socket send.
    ///
    /// The engine still uses v1 bytes internally as a compatibility boundary.
    /// Static secure transport converts that view into compact v2 before AEAD
    /// protection so service/path metadata is not visible on the carrier.
    pub fn protect_frame(&mut self, frame: &[u8]) -> Result<Vec<u8>, CryptoError> {
        match self {
            Self::None => Ok(frame.to_vec()),
            Self::Static { keys } => {
                let compact_v2 = Frame::decode_v1(frame)
                    .and_then(|decoded| decoded.encode_v2())
                    .map_err(|_| CryptoError::SilentDrop)?;
                keys.encrypt_frame(&compact_v2)
            }
        }
    }

    /// Authenticate and unwrap one path-socket packet before frame decoding.
    ///
    /// Static secure transport decrypts compact v2 and synthesizes compact v1
    /// bytes for the current engine. This keeps the public encrypted wire shape
    /// correct while older internal consumers migrate to compact frame objects.
    pub fn unprotect_packet(&mut self, packet: &[u8]) -> Result<Vec<u8>, CryptoError> {
        match self {
            Self::None => Ok(packet.to_vec()),
            Self::Static { keys } => {
                let DecryptedPacket { plaintext, .. } = keys.decrypt_packet(packet)?;
                Frame::decode_v2(&plaintext)
                    .and_then(|decoded| decoded.encode_v1())
                    .map_err(|_| CryptoError::SilentDrop)
            }
        }
    }
}
