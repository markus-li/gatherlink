//! Compact authenticated transport envelope.

use chacha20poly1305::aead::{AeadInPlace, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};
use std::ops::Range;

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
const ASSOCIATED_DATA_LEN: usize = AEAD_DOMAIN.len() + ENCRYPTED_DATA_HEADER_LEN;

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

/// Authenticated packet metadata for an in-place decrypt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecryptedPacketInPlace {
    /// Receiver index from the clear envelope.
    pub receiver_index: u32,
    /// Authenticated transport counter.
    pub counter: u64,
    /// Range inside the original packet buffer containing plaintext bytes.
    pub plaintext_range: Range<usize>,
}

/// Reserved outbound counter range for parallel packet protection.
///
/// Counter allocation stays single-threaded and owned by the mutable transport
/// key state. After a range is reserved, individual packet encryption can run on
/// worker threads without sharing mutable session counters.
#[derive(Clone)]
pub struct ReservedSendCounters {
    receiver_index: u32,
    cipher: ChaCha20Poly1305,
    first_counter: u64,
}

/// Directional transport encryption state.
#[derive(Clone)]
pub struct TransportKeys {
    local_receiver_index: u32,
    remote_receiver_index: u32,
    send_cipher: ChaCha20Poly1305,
    receive_cipher: ChaCha20Poly1305,
    next_send_counter: u64,
    replay_window: ReplayWindow,
}

impl std::fmt::Debug for TransportKeys {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TransportKeys")
            .field("local_receiver_index", &self.local_receiver_index)
            .field("remote_receiver_index", &self.remote_receiver_index)
            .field("next_send_counter", &self.next_send_counter)
            .field("replay_window", &self.replay_window)
            .finish_non_exhaustive()
    }
}

impl TransportKeys {
    /// Create compiled transport keys for one peer session.
    #[must_use]
    pub fn new(receiver_index: u32, send_key: [u8; 32], receive_key: [u8; 32]) -> Self {
        Self::new_with_receiver_indexes(receiver_index, receiver_index, send_key, receive_key)
    }

    /// Create compiled transport keys with distinct local and remote receiver indexes.
    #[must_use]
    pub fn new_with_receiver_indexes(
        local_receiver_index: u32,
        remote_receiver_index: u32,
        send_key: [u8; 32],
        receive_key: [u8; 32],
    ) -> Self {
        Self {
            local_receiver_index,
            remote_receiver_index,
            send_cipher: ChaCha20Poly1305::new((&send_key).into()),
            receive_cipher: ChaCha20Poly1305::new((&receive_key).into()),
            next_send_counter: 0,
            replay_window: ReplayWindow::default(),
        }
    }

    /// Encrypt an encoded Gatherlink frame inside the compact data envelope.
    pub fn encrypt_frame(&mut self, plaintext_frame: &[u8]) -> Result<Vec<u8>, CryptoError> {
        let counter = self.next_send_counter;
        self.next_send_counter = self.next_send_counter.checked_add(1).ok_or(CryptoError::InvalidInput)?;
        encrypt_frame_with_cipher(self.remote_receiver_index, &self.send_cipher, counter, plaintext_frame)
    }

    /// Encrypt a frame whose plaintext is appended directly into the envelope.
    pub fn encrypt_frame_with_plaintext<F>(
        &mut self,
        plaintext_len: usize,
        write_plaintext: F,
    ) -> Result<Vec<u8>, CryptoError>
    where
        F: FnOnce(&mut Vec<u8>) -> Result<(), CryptoError>,
    {
        let counter = self.next_send_counter;
        self.next_send_counter = self.next_send_counter.checked_add(1).ok_or(CryptoError::InvalidInput)?;
        encrypt_frame_with_cipher_writer(
            self.remote_receiver_index,
            &self.send_cipher,
            counter,
            plaintext_len,
            write_plaintext,
        )
    }

    /// Reserve a contiguous outbound counter range for parallel encryption.
    pub fn reserve_send_counters(&mut self, count: usize) -> Result<ReservedSendCounters, CryptoError> {
        let count = u64::try_from(count).map_err(|_| CryptoError::InvalidInput)?;
        let first_counter = self.next_send_counter;
        self.next_send_counter = self
            .next_send_counter
            .checked_add(count)
            .ok_or(CryptoError::InvalidInput)?;
        Ok(ReservedSendCounters {
            receiver_index: self.remote_receiver_index,
            cipher: self.send_cipher.clone(),
            first_counter,
        })
    }

    /// Authenticate/decrypt one packet and apply replay protection.
    pub fn decrypt_packet(&mut self, packet: &[u8]) -> Result<DecryptedPacket, CryptoError> {
        let decrypted = decrypt_packet_with_cipher(&self.receive_cipher, packet)?;
        if decrypted.receiver_index != self.local_receiver_index {
            return Err(CryptoError::SilentDrop);
        }
        if !self.replay_window.accept(decrypted.counter) {
            return Err(CryptoError::SilentDrop);
        }
        Ok(decrypted)
    }

    /// Authenticate/decrypt one packet in-place and apply replay protection.
    ///
    /// The clear header and trailing tag remain in the caller-owned buffer. The
    /// returned range points at the authenticated plaintext inside that buffer,
    /// letting relay hot paths forward the remaining opaque packet without
    /// allocating a second plaintext vector.
    pub fn decrypt_packet_in_place(&mut self, packet: &mut [u8]) -> Result<DecryptedPacketInPlace, CryptoError> {
        let decrypted = decrypt_packet_with_cipher_in_place(&self.receive_cipher, packet)?;
        if decrypted.receiver_index != self.local_receiver_index {
            return Err(CryptoError::SilentDrop);
        }
        if !self.replay_window.accept(decrypted.counter) {
            return Err(CryptoError::SilentDrop);
        }
        Ok(decrypted)
    }
}

impl ReservedSendCounters {
    /// Encrypt one frame plaintext using the reserved counter at `offset`.
    pub fn encrypt_frame_with_plaintext<F>(
        &self,
        offset: usize,
        plaintext_len: usize,
        write_plaintext: F,
    ) -> Result<Vec<u8>, CryptoError>
    where
        F: FnOnce(&mut Vec<u8>) -> Result<(), CryptoError>,
    {
        let offset = u64::try_from(offset).map_err(|_| CryptoError::InvalidInput)?;
        let counter = self
            .first_counter
            .checked_add(offset)
            .ok_or(CryptoError::InvalidInput)?;
        encrypt_frame_with_cipher_writer(
            self.receiver_index,
            &self.cipher,
            counter,
            plaintext_len,
            write_plaintext,
        )
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
    encrypt_frame_with_cipher(receiver_index, &cipher, counter, plaintext_frame)
}

fn encrypt_frame_with_cipher(
    receiver_index: u32,
    cipher: &ChaCha20Poly1305,
    counter: u64,
    plaintext_frame: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    encrypt_frame_with_cipher_writer(receiver_index, cipher, counter, plaintext_frame.len(), |output| {
        output.extend_from_slice(plaintext_frame);
        Ok(())
    })
}

fn encrypt_frame_with_cipher_writer<F>(
    receiver_index: u32,
    cipher: &ChaCha20Poly1305,
    counter: u64,
    plaintext_len: usize,
    write_plaintext: F,
) -> Result<Vec<u8>, CryptoError>
where
    F: FnOnce(&mut Vec<u8>) -> Result<(), CryptoError>,
{
    let mut output = Vec::with_capacity(ENCRYPTED_DATA_HEADER_LEN + plaintext_len + AEAD_TAG_LEN);
    output.push(PACKET_TYPE_ENCRYPTED_DATA_V1);
    output.extend_from_slice(&receiver_index.to_be_bytes());
    output.extend_from_slice(&counter.to_be_bytes());
    write_plaintext(&mut output)?;
    if output.len() != ENCRYPTED_DATA_HEADER_LEN + plaintext_len {
        return Err(CryptoError::InvalidInput);
    }
    let ad = associated_data(&output[..ENCRYPTED_DATA_HEADER_LEN]);
    let tag = cipher
        .encrypt_in_place_detached(
            Nonce::from_slice(&nonce_from_counter(counter)),
            &ad,
            &mut output[ENCRYPTED_DATA_HEADER_LEN..],
        )
        .map_err(|_| CryptoError::SilentDrop)?;
    output.extend_from_slice(&tag);
    Ok(output)
}

/// Decrypt without replay checking so callers can perform session lookup first.
pub fn decrypt_packet_without_replay(key: &[u8; 32], packet: &[u8]) -> Result<DecryptedPacket, CryptoError> {
    let cipher = ChaCha20Poly1305::new(key.into());
    decrypt_packet_with_cipher(&cipher, packet)
}

fn decrypt_packet_with_cipher(cipher: &ChaCha20Poly1305, packet: &[u8]) -> Result<DecryptedPacket, CryptoError> {
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
    let ad = associated_data(&packet[..ENCRYPTED_DATA_HEADER_LEN]);
    cipher
        .decrypt_in_place_detached(
            Nonce::from_slice(&nonce_from_counter(counter)),
            &ad,
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

fn decrypt_packet_with_cipher_in_place(
    cipher: &ChaCha20Poly1305,
    packet: &mut [u8],
) -> Result<DecryptedPacketInPlace, CryptoError> {
    if packet.len() < ENCRYPTED_DATA_HEADER_LEN + AEAD_TAG_LEN {
        return Err(CryptoError::SilentDrop);
    }
    if packet[0] != PACKET_TYPE_ENCRYPTED_DATA_V1 {
        return Err(CryptoError::SilentDrop);
    }
    let receiver_index = u32::from_be_bytes(packet[1..5].try_into().map_err(|_| CryptoError::SilentDrop)?);
    let counter = u64::from_be_bytes(packet[5..13].try_into().map_err(|_| CryptoError::SilentDrop)?);
    let ciphertext_end = packet.len() - AEAD_TAG_LEN;
    let (header_and_ciphertext, tag) = packet.split_at_mut(ciphertext_end);
    let ad = associated_data(&header_and_ciphertext[..ENCRYPTED_DATA_HEADER_LEN]);
    cipher
        .decrypt_in_place_detached(
            Nonce::from_slice(&nonce_from_counter(counter)),
            &ad,
            &mut header_and_ciphertext[ENCRYPTED_DATA_HEADER_LEN..],
            (&*tag).into(),
        )
        .map_err(|_| CryptoError::SilentDrop)?;
    Ok(DecryptedPacketInPlace {
        receiver_index,
        counter,
        plaintext_range: ENCRYPTED_DATA_HEADER_LEN..ciphertext_end,
    })
}

/// Read the clear receiver index without authenticating the packet.
///
/// This is only a demux hint. Callers must still authenticate the packet with
/// the session key selected by this index before trusting any bytes.
pub fn clear_receiver_index(packet: &[u8]) -> Result<u32, CryptoError> {
    if packet.len() < ENCRYPTED_DATA_HEADER_LEN + AEAD_TAG_LEN {
        return Err(CryptoError::SilentDrop);
    }
    if packet[0] != PACKET_TYPE_ENCRYPTED_DATA_V1 {
        return Err(CryptoError::SilentDrop);
    }
    Ok(u32::from_be_bytes(
        packet[1..5].try_into().map_err(|_| CryptoError::SilentDrop)?,
    ))
}

fn associated_data(header: &[u8]) -> [u8; ASSOCIATED_DATA_LEN] {
    let mut ad = [0_u8; ASSOCIATED_DATA_LEN];
    ad[..AEAD_DOMAIN.len()].copy_from_slice(AEAD_DOMAIN);
    ad[AEAD_DOMAIN.len()..].copy_from_slice(header);
    ad
}
