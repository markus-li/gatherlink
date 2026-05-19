# Plaintext Security Mode

## Purpose

Gatherlink needs a pre-crypto lab mode so the core UDP transport can be tested
with normal traffic generators before authentication, AEAD envelopes, replay
windows, and key management are implemented.

This mode is useful for local labs only. It is not a production security mode.

## Config Shape

Version 1 config should make security explicit:

```json
{
  "security": {
    "mode": "none"
  }
}
```

When omitted in early scaffold configs, Python may default to `none` so the lab
can run before crypto exists. Once authenticated modes exist, production-facing
templates should default to the safe mode and lab templates should remain
explicitly plaintext.

## Runtime Contract

Python owns config parsing, validation, warnings, and operator-facing policy.
Rust receives an already-compiled runtime security mode and executes the packet
path selected by Python.

Rust must not decide whether plaintext is acceptable for an environment.

## Required Warning

Every start or plan that uses `security.mode = "none"` must produce an obvious
warning in Python terminal output and logs:

```text
WARNING: security.mode=none; traffic is unauthenticated and unencrypted.
WARNING: use only in local labs or controlled debugging.
```

The warning should also appear when runtime config is hot-reapplied into
plaintext mode.

## Future Authenticated Modes

When crypto/authentication is implemented, plaintext mode should remain
available for local testing and packet debugging. It should never become silent.

Authenticated public UDP listeners must keep the architecture contract:

```text
invalid or auth-failing packet -> silent drop
```

Plaintext lab listeners are different because they intentionally skip auth. The
logs must make that difference clear so lab behavior is not mistaken for
production behavior.
