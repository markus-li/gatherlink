"""Compact authenticated transport envelope helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from gatherlink.security.replay import ReplayWindow

PACKET_TYPE_ENCRYPTED_DATA_V1 = 0x01
ENCRYPTED_DATA_HEADER_LEN = 13
AEAD_TAG_LEN = 16
AEAD_DOMAIN = b"GATHERLINK_DATA_V1"


@dataclass(frozen=True)
class DecryptedPacket:
    """One authenticated transport packet containing an encoded Gatherlink frame."""

    receiver_index: int
    counter: int
    plaintext: bytes


@dataclass
class TransportKeys:
    """Directional AEAD state for one authenticated Gatherlink peer session."""

    receiver_index: int
    send_key: bytes
    receive_key: bytes
    local_receiver_index: int | None = None
    remote_receiver_index: int | None = None
    next_send_counter: int = 0
    replay_window: ReplayWindow = field(default_factory=ReplayWindow)

    def __post_init__(self) -> None:
        """Preserve old shared-index configs while supporting real session indexes."""
        if self.local_receiver_index is None:
            self.local_receiver_index = self.receiver_index
        if self.remote_receiver_index is None:
            self.remote_receiver_index = self.receiver_index

    def encrypt_frame(self, plaintext_frame: bytes) -> bytes:
        """Encrypt an encoded Gatherlink frame using the next transport counter."""
        packet = encrypt_frame_with_counter(
            self.remote_receiver_index,
            self.send_key,
            self.next_send_counter,
            plaintext_frame,
        )
        self.next_send_counter += 1
        return packet

    def decrypt_packet(self, packet: bytes) -> DecryptedPacket:
        """Authenticate, decrypt, and replay-check one transport packet."""
        decrypted = decrypt_packet_without_replay(self.receive_key, packet)
        if decrypted.receiver_index != self.local_receiver_index:
            raise ValueError("receiver index mismatch")
        if not self.replay_window.accept(decrypted.counter):
            raise ValueError("replayed transport counter")
        return decrypted


def encrypt_frame_with_counter(receiver_index: int, key: bytes, counter: int, plaintext_frame: bytes) -> bytes:
    """Encrypt an encoded Gatherlink frame inside the compact data envelope."""
    _validate_u32(receiver_index, "receiver_index")
    _validate_u64(counter, "counter")
    _validate_key(key)
    header = bytearray()
    header.append(PACKET_TYPE_ENCRYPTED_DATA_V1)
    header.extend(receiver_index.to_bytes(4, "big"))
    header.extend(counter.to_bytes(8, "big"))
    ciphertext = ChaCha20Poly1305(key).encrypt(
        _nonce_from_counter(counter),
        plaintext_frame,
        AEAD_DOMAIN + bytes(header),
    )
    return bytes(header) + ciphertext


def decrypt_packet_without_replay(key: bytes, packet: bytes) -> DecryptedPacket:
    """Authenticate and decrypt one transport packet without replay-window mutation."""
    _validate_key(key)
    if len(packet) < ENCRYPTED_DATA_HEADER_LEN + AEAD_TAG_LEN:
        raise ValueError("packet must be silently dropped")
    if packet[0] != PACKET_TYPE_ENCRYPTED_DATA_V1:
        raise ValueError("packet must be silently dropped")
    receiver_index = int.from_bytes(packet[1:5], "big")
    counter = int.from_bytes(packet[5:13], "big")
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            _nonce_from_counter(counter),
            packet[ENCRYPTED_DATA_HEADER_LEN:],
            AEAD_DOMAIN + packet[:ENCRYPTED_DATA_HEADER_LEN],
        )
    except InvalidTag as exc:
        raise ValueError("packet must be silently dropped") from exc
    return DecryptedPacket(receiver_index=receiver_index, counter=counter, plaintext=plaintext)


def _nonce_from_counter(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")


def _validate_key(key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("transport key must be 32 bytes")


def _validate_u32(value: int, field: str) -> None:
    if value < 0 or value >= 2**32:
        raise ValueError(f"{field} must fit u32")


def _validate_u64(value: int, field: str) -> None:
    if value < 0 or value >= 2**64:
        raise ValueError(f"{field} must fit u64")
