"""
Passphrase-sealed secret JSON envelopes.

This is local persistence UX, not transport security. Python owns the prompt,
file permissions, redaction, and durable format; runtime code receives only
explicitly opened secret JSON through normal config/provisioning paths.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pydantic import Field

from gatherlink.shared.models import GatherlinkBaseModel

SEALED_SECRET_FORMAT = "gatherlink.sealed-secret.v1"
SEALED_SECRET_AAD_DOMAIN = b"GATHERLINK_SEALED_SECRET_V1"
SEALED_SECRET_CIPHER = "chacha20poly1305"
SEALED_SECRET_KDF = "scrypt"
SEALED_SECRET_SCRYPT_N = 2**14
SEALED_SECRET_SCRYPT_R = 8
SEALED_SECRET_SCRYPT_P = 1
SEALED_SECRET_SALT_BYTES = 16
SEALED_SECRET_NONCE_BYTES = 12
SEALED_SECRET_KEY_BYTES = 32


class SealedSecretKdf(GatherlinkBaseModel):
    """KDF parameters needed to reopen one sealed secret."""

    name: Literal["scrypt"] = SEALED_SECRET_KDF
    salt: str
    length: int = SEALED_SECRET_KEY_BYTES
    n: int = SEALED_SECRET_SCRYPT_N
    r: int = SEALED_SECRET_SCRYPT_R
    p: int = SEALED_SECRET_SCRYPT_P


class SealedSecretEnvelope(GatherlinkBaseModel):
    """Durable sealed secret envelope with metadata safe for inspection."""

    schema_version: int = 1
    format: Literal["gatherlink.sealed-secret.v1"] = SEALED_SECRET_FORMAT
    label: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cipher: Literal["chacha20poly1305"] = SEALED_SECRET_CIPHER
    kdf: SealedSecretKdf
    nonce: str
    ciphertext: str

    def public_summary(self) -> dict[str, Any]:
        """Return metadata that is safe for status, REST, logs, and config show."""
        return {
            "schema_version": self.schema_version,
            "format": self.format,
            "label": self.label,
            "created_at": self.created_at.isoformat(),
            "cipher": self.cipher,
            "kdf": {
                "name": self.kdf.name,
                "length": self.kdf.length,
                "n": self.kdf.n,
                "r": self.kdf.r,
                "p": self.kdf.p,
            },
            "ciphertext": f"[sealed:{len(self.ciphertext)} base64 chars]",
        }


def seal_secret_json(payload: Any, *, passphrase: str, label: str) -> SealedSecretEnvelope:
    """Seal one JSON-serializable secret payload using an operator passphrase."""
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    salt = os.urandom(SEALED_SECRET_SALT_BYTES)
    nonce = os.urandom(SEALED_SECRET_NONCE_BYTES)
    key = _derive_key(passphrase, salt)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, _aad(label))
    return SealedSecretEnvelope(
        label=label,
        kdf=SealedSecretKdf(salt=_b64e(salt)),
        nonce=_b64e(nonce),
        ciphertext=_b64e(ciphertext),
    )


def open_secret_json(
    envelope: SealedSecretEnvelope | dict[str, Any],
    *,
    passphrase: str,
    expected_label: str | None = None,
) -> Any:
    """Open one sealed secret envelope and return its JSON payload."""
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    envelope = envelope if isinstance(envelope, SealedSecretEnvelope) else SealedSecretEnvelope.model_validate(envelope)
    if expected_label is not None and envelope.label != expected_label:
        raise ValueError("sealed secret label mismatch")
    key = _derive_key(passphrase, _b64d(envelope.kdf.salt, "kdf.salt"), envelope.kdf)
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            _b64d(envelope.nonce, "nonce"),
            _b64d(envelope.ciphertext, "ciphertext"),
            _aad(envelope.label),
        )
    except InvalidTag as exc:
        raise ValueError("sealed secret could not be opened") from exc
    return json.loads(plaintext.decode("utf-8"))


def _derive_key(passphrase: str, salt: bytes, kdf: SealedSecretKdf | None = None) -> bytes:
    params = kdf or SealedSecretKdf(salt=_b64e(salt))
    if params.name != SEALED_SECRET_KDF:
        raise ValueError(f"unsupported sealed secret KDF: {params.name}")
    return Scrypt(
        salt=salt,
        length=params.length,
        n=params.n,
        r=params.r,
        p=params.p,
    ).derive(passphrase.encode("utf-8"))


def _aad(label: str) -> bytes:
    return SEALED_SECRET_AAD_DOMAIN + b"\x00" + label.encode("utf-8")


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64d(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{field} must be base64 encoded") from exc
