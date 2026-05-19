//! Compact authenticated transport envelope.

use chacha20poly1305::aead::{AeadInPlace, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};

use crate::errors::CryptoError;
use crate::nonce::nonce_from_counter;
use crate::replay::ReplayWindow;

/// Gatherlink encrypted data packet type.
pub const PACKET_TYPE_ENCRYPTED_DATA_V1: u8 = 0x01;
/// Clear envelope header length: packet type, receiver index, counter.
pub const ENCRYPTED_DATA_HEADER_LEN: usize = 13;
/// ChaCha20-Poly1305 tag length.
pub const AEAD_TAG_LEN: usize = 16;
/// Domain separator included in AEAD associated data.
pub const AEAD_DOMAIN: &[u8] = b"GATHERLINK_DATA_V1";

/// One encrypted packet after successful authentication.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecryptedPacket {
    /// Receiver index from the clear envelope.
    pub receiver_index: u32,
    /// Authenticated transport counter.
    pub counter: u64,
    /// Decrypted Gatherlink frame bytes.
    pub plaintext: Vec<u8>,
}

/// Directional transport encryption state.
#[derive(Debug, Clone)]
pub struct TransportKeys {
    receiver_index: u32,
    send_key: [u8; 32],
    receive_key: [u8; 32],
    next_send_counter: u64,
    replay_window: ReplayWindow,
}

impl TransportKeys {
    /// Create compiled transport keys for one peer session.
    #[must_use]
    pub fn new(receiver_index: u32, send_key: [u8; 32], receive_key: [u8; 32]) -> Self {
        Self {
            receiver_index,
            send_key,
            receive_key,
            next_send_counter: 0,
            replay_window: ReplayWindow::default(),
        }
    }

    /// Encrypt an encoded Gatherlink frame inside the compact data envelope.
    pub fn encrypt_frame(&mut self, plaintext_frame: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let counter = self.next_send_counter;
        self.next_send_counter = self.next_send_counter.checked_add(1).ok_or(CryptoError::InvalidInput)?;
        encrypt_frame_with_counter(self.receiver_index, &self.send_key, counter, plaintext_frame)
    }

    /// Authenticate/decrypt one packet and apply replay protection.
    pub fn decrypt_packet(&mut self, packet: &[u8]) -> Result<DecryptedPacket, CryptoError> {
        let decrypted = decrypt_packet_without_replay(&self.receive_key, packet)?;
        if decrypted.receiver_index != self.receiver_index {
            return Err(CryptoError::SilentDrop);
        }
        if !self.replay_window.accept(decrypted.counter) {
            return Err(CryptoError::SilentDrop);
        }
        Ok(decrypted)
    }
}

/// Encrypt a frame with an explicit counter. Tests and Python bindings can use this for deterministic checks.
pub fn encrypt_frame_with_counter(
    receiver_index: u32,
    key: &[u8; 32],
    counter: u64,
    plaintext_frame: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    let cipher = ChaCha20Poly1305::new(key.into());
    let mut output = Vec::with_capacity(ENCRYPTED_DATA_HEADER_LEN + plaintext_frame.len() + AEAD_TAG_LEN);
    output.push(PACKET_TYPE_ENCRYPTED_DATA_V1);
    output.extend_from_slice(&receiver_index.to_be_bytes());
    output.extend_from_slice(&counter.to_be_bytes());
    let mut ciphertext = plaintext_frame.to_vec();
    let tag = cipher
        .encrypt_in_place_detached(
            Nonce::from_slice(&nonce_from_counter(counter)),
            &associated_data(&output),
            &mut ciphertext,
        )
        .map_err(|_| CryptoError::SilentDrop)?;
    output.extend_from_slice(&ciphertext);
    output.extend_from_slice(&tag);
    Ok(output)
}

/// Decrypt without replay checking so callers can perform session lookup first.
pub fn decrypt_packet_without_replay(key: &[u8; 32], packet: &[u8]) -> Result<DecryptedPacket, CryptoError> {
    if packet.len() < ENCRYPTED_DATA_HEADER_LEN + AEAD_TAG_LEN {
        return Err(CryptoError::SilentDrop);
    }
    if packet[0] != PACKET_TYPE_ENCRYPTED_DATA_V1 {
        return Err(CryptoError::SilentDrop);
    }
    let receiver_index = u32::from_be_bytes(packet[1..5].try_into().map_err(|_| CryptoError::SilentDrop)?);
    let counter = u64::from_be_bytes(packet[5..13].try_into().map_err(|_| CryptoError::SilentDrop)?);
    let ciphertext_end = packet.len() - AEAD_TAG_LEN;
    let mut plaintext = packet[ENCRYPTED_DATA_HEADER_LEN..ciphertext_end].to_vec();
    let cipher = ChaCha20Poly1305::new(key.into());
    cipher
        .decrypt_in_place_detached(
            Nonce::from_slice(&nonce_from_counter(counter)),
            &associated_data(&packet[..ENCRYPTED_DATA_HEADER_LEN]),
            &mut plaintext,
            packet[ciphertext_end..].into(),
        )
        .map_err(|_| CryptoError::SilentDrop)?;
    Ok(DecryptedPacket {
        receiver_index,
        counter,
        plaintext,
    })
}

fn associated_data(header: &[u8]) -> Vec<u8> {
    let mut ad = Vec::with_capacity(AEAD_DOMAIN.len() + header.len());
    ad.extend_from_slice(AEAD_DOMAIN);
    ad.extend_from_slice(header);
    ad
}
