# Secrets Age

`age` is only for at-rest material.

Allowed uses:

- sealed local identity backups
- sealed bootstrap/provisioning bundles
- sealed static lab/manual session material
- operator export/import files that are never sent as packet transport

Disallowed uses:

- packet encryption
- relay-hop encryption
- runtime AEAD session keys
- per-packet key wrapping
- replacing the authenticated session protocol

Packet transport uses the protocol security model in
[`docs/protocol/security.md`](security.md). Runtime config introspection must not expose
private keys, raw session keys, bootstrap secrets, or decrypted sealed material.
If a command needs to prove that secret material exists, it should report a
redacted marker, key id, fingerprint, or length instead of the secret bytes.
