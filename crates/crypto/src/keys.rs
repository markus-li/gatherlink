//! Key and identity primitives for the Python-controlled security layer.

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand_core::OsRng;
use sha2::{Digest, Sha256};
use x25519_dalek::{PublicKey as X25519PublicKey, StaticSecret};

use crate::errors::CryptoError;

/// Ed25519 public key length.
pub const ED25519_PUBLIC_KEY_LEN: usize = 32;
/// Ed25519 private seed length.
pub const ED25519_PRIVATE_KEY_LEN: usize = 32;
/// Ed25519 signature length.
pub const ED25519_SIGNATURE_LEN: usize = 64;
/// X25519 public/private key length.
pub const X25519_KEY_LEN: usize = 32;
/// Stable node id length.
pub const NODE_ID_LEN: usize = 32;
/// Domain separator for Gatherlink node ids.
pub const NODE_ID_DOMAIN: &[u8] = b"gatherlink node v1";

/// Generate an Ed25519 signing seed and public key.
#[must_use]
pub fn generate_ed25519_keypair() -> ([u8; ED25519_PRIVATE_KEY_LEN], [u8; ED25519_PUBLIC_KEY_LEN]) {
    let signing_key = SigningKey::generate(&mut OsRng);
    (signing_key.to_bytes(), signing_key.verifying_key().to_bytes())
}

/// Generate an X25519 static private/public keypair.
#[must_use]
pub fn generate_x25519_keypair() -> ([u8; X25519_KEY_LEN], [u8; X25519_KEY_LEN]) {
    let secret = StaticSecret::random_from_rng(OsRng);
    let public = X25519PublicKey::from(&secret);
    (secret.to_bytes(), public.to_bytes())
}

/// Compute the stable Gatherlink node id from an Ed25519 public key.
#[must_use]
pub fn node_id_from_ed25519_public(public_key: &[u8; ED25519_PUBLIC_KEY_LEN]) -> [u8; NODE_ID_LEN] {
    let mut hasher = Sha256::new();
    hasher.update(NODE_ID_DOMAIN);
    hasher.update(public_key);
    hasher.finalize().into()
}

/// Sign domain-separated document bytes with an Ed25519 seed.
pub fn sign_document(
    private_seed: &[u8; ED25519_PRIVATE_KEY_LEN],
    domain: &[u8],
    canonical_body: &[u8],
) -> [u8; ED25519_SIGNATURE_LEN] {
    let signing_key = SigningKey::from_bytes(private_seed);
    let mut message = Vec::with_capacity(domain.len() + canonical_body.len());
    message.extend_from_slice(domain);
    message.extend_from_slice(canonical_body);
    signing_key.sign(&message).to_bytes()
}

/// Verify a domain-separated Ed25519 document signature.
pub fn verify_document(
    public_key: &[u8; ED25519_PUBLIC_KEY_LEN],
    domain: &[u8],
    canonical_body: &[u8],
    signature: &[u8; ED25519_SIGNATURE_LEN],
) -> Result<(), CryptoError> {
    let verifying_key = VerifyingKey::from_bytes(public_key).map_err(|_| CryptoError::InvalidInput)?;
    let signature = Signature::from_bytes(signature);
    let mut message = Vec::with_capacity(domain.len() + canonical_body.len());
    message.extend_from_slice(domain);
    message.extend_from_slice(canonical_body);
    verifying_key
        .verify(&message, &signature)
        .map_err(|_| CryptoError::SilentDrop)
}

/// Derive a shared X25519 secret.
#[must_use]
pub fn x25519_shared_secret(
    private_key: &[u8; X25519_KEY_LEN],
    peer_public_key: &[u8; X25519_KEY_LEN],
) -> [u8; X25519_KEY_LEN] {
    let secret = StaticSecret::from(*private_key);
    let peer = X25519PublicKey::from(*peer_public_key);
    secret.diffie_hellman(&peer).to_bytes()
}
