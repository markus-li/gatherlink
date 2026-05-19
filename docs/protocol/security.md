# Security

Gatherlink's default security model should be intentionally close to
WireGuard: small primitives, static node identities, Noise-based session setup,
AEAD-protected packets, monotonically increasing counters, and silent drops for
invalid public UDP traffic.

This document records the preferred implementation path. Experimental or
compatibility modes may exist later, but the production baseline should stay
boring and auditable.

## Chosen Baseline

Use a WireGuard-like construction adapted to Gatherlink's service/path frame
model:

- long-term node identity: `Ed25519`
- handshake/static transport key agreement: `X25519`
- handshake pattern: Noise IK when the peer is known from signed topology,
  Noise XX only for explicit enrollment/bootstrap flows
- packet AEAD: `ChaCha20-Poly1305`
- hash/KDF: SHA-256/HKDF for the current Python-owned Noise IK style setup
- packet counters: per-direction monotonically increasing `u64`
- replay defense: sliding replay window per session/direction
- public receive behavior: no response unless the input is authenticated as a
  configured peer or approved bootstrap attempt

The reason for using separate Ed25519 and X25519 keys is boundary clarity:
Ed25519 signs identity, topology, and authority documents; X25519 establishes
ephemeral transport secrets. This avoids making one key type carry every
security meaning in the system.

## Current Implementation Stage

The current Rust dataplane can run path transports in two modes:

- `security.mode=none`: plaintext Gatherlink frames on path UDP sockets. This is
  intentionally retained for local labs and diagnostics and must log loudly.
- `security.mode=authenticated`: the normal v1 config-facing secure path.
  Python verifies signed topology/session documents, compiles short-lived
  directional ChaCha20-Poly1305 keys plus local/remote receiver indexes, and
  hands only those packet-rate facts to Rust. Rust still sees the compact AEAD
  executor state; it does not learn topology or identity policy.
- `security.mode=static`: explicit lab/manual provisioning. It uses the same
  Rust AEAD executor as authenticated mode, but the operator supplied the
  directional keys directly or through static-session tooling. It must stay
  warning-heavy and should not be treated as the normal v1 secure path.

Static mode is not the final production trust model. It exists so labs can
exercise the production AEAD packet path before Noise handshake orchestration is
complete. The intended production path, now represented by
`security.mode=authenticated`, is:

```text
Python verifies identity/topology/authentication policy
Python runs or supervises Noise session establishment
Python compiles receiver_index + directional traffic keys
Rust executes AEAD protect/unprotect + replay checks only
```

For labs and manual encrypted runs, Python can derive config-compatible static
AEAD keys from Gatherlink identities instead of hand-copying symmetric keys:

```bash
gatherlink secrets identity-create ./local.identity.json
gatherlink secrets identity-create ./peer.identity.json
gatherlink secrets identity-public ./local.identity.json
gatherlink secrets static-session --local ./local.identity.json --peer ./peer.identity.json --role initiator
gatherlink secrets static-session --local ./peer.identity.json --peer ./local.identity.json --role responder
```

For v1 provisioning, Python also creates and verifies signed topology bundles:

```bash
gatherlink secrets identity-create ./issuer.identity.json
gatherlink secrets identity-create ./node-a.identity.json
gatherlink secrets topology-create \
  --issuer ./issuer.identity.json \
  --output ./topology.signed.json \
  --generation 1 \
  --node node-a=./node-a.identity.json \
  --service wireguard=node-a=256
gatherlink secrets topology-verify ./topology.signed.json --trust-root ./issuer.identity.json
```

Topology bundles are Python-owned control-plane artifacts. They authorize names,
roles, services, generations, and validity windows. Rust receives only the
compiled session/runtime facts derived after Python verifies those artifacts.

Trust roots can also be exported and imported as public-only state:

```bash
gatherlink secrets trust-root-export ./issuer.identity.json ./issuer.public.json
gatherlink secrets trust-root-import lab-root ./issuer.public.json --state-dir .gatherlink/state
gatherlink secrets trust-root-list --state-dir .gatherlink/state
```

The import path stores only public identity material under the state
`trust-roots/` directory. Private identity files remain explicit local inputs
and must not be exposed through diagnostics, REST, reports, or config display.

The two `static-session` outputs are inverse security blocks: the initiator
`send_key` is the responder `receive_key`, and vice versa. This command still
produces `security.mode=static`; it is provisioning glue for the current AEAD
path, not a replacement for the authenticated Noise handshake. Python owns the
identity files, public identity exchange, transcript context, and eventual
trust policy. Rust receives only the compiled receiver index and traffic keys.

The v1 implementation now has a Python-owned Noise IK style authenticated
session setup. It consumes a verified topology bundle, confirms both identities
are present and not revoked, binds the prologue/transcript to the topology
generation and issuer, encrypts the initiator static X25519 key after the `es`
DH, and compiles short-lived directional AEAD facts for Rust. Rust still sees
only receiver indexes, traffic keys, counters, replay windows, and compact
packet execution state.

The operator-facing Noise IK path is available under `gatherlink secrets`:

```text
gatherlink secrets noise-init \
  --local node-a.identity.json \
  --peer node-b.public.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --initiation-output node-a-to-b.noise-init.json \
  --pending-output node-a-to-b.noise.pending.secret.json

gatherlink secrets noise-accept \
  --local node-b.identity.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --initiation node-a-to-b.noise-init.json \
  --response-output node-b-to-a.noise-response.json \
  --security-output node-b.security.secret.json

gatherlink secrets noise-complete \
  --local node-a.identity.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --pending node-a-to-b.noise.pending.secret.json \
  --response node-b-to-a.noise-response.json \
  --security-output node-a.security.secret.json
```

The public initiation contains the initiator ephemeral key and encrypted
initiator static X25519 key. The public response contains the responder
ephemeral key, receiver index, expiry, and initiation hash. The pending
initiator state and generated security blocks contain secret material and must
be stored with owner-only permissions. The generated security blocks are
config-compatible authenticated AEAD blocks with distinct local and remote
receiver indexes. Noise commands generate opaque non-zero receiver indexes by
default; `--receiver-index` exists only for deterministic tests and explicit
manual provisioning. When expanded for runtime, Python records the source mode
as `authenticated` and compiles the executor mode to Rust's static AEAD
primitive. Malformed, expired, tampered, wrong-peer, wrong-generation, or
revoked inputs fail closed locally and must not produce unauthenticated network
errors.

The older signed ephemeral document bridge remains as a compatibility/manual
tool while Noise IK becomes the normal v1 path:

```text
gatherlink secrets handshake-init \
  --local node-a.identity.json \
  --peer node-b.public.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --initiation-output node-a-to-b.init.signed.json \
  --pending-output node-a-to-b.pending.secret.json

gatherlink secrets handshake-accept \
  --local node-b.identity.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --initiation node-a-to-b.init.signed.json \
  --response-output node-b-to-a.response.signed.json \
  --security-output node-b.security.secret.json

gatherlink secrets handshake-complete \
  --local node-a.identity.json \
  --topology topology.signed.json \
  --trust-root root.public.json \
  --pending node-a-to-b.pending.secret.json \
  --response node-b-to-a.response.signed.json \
  --security-output node-a.security.secret.json
```

The signed bridge also produces config-compatible authenticated AEAD blocks with
distinct local and remote receiver indexes, but new v1 workflows should use the
Noise IK commands above.

Compiled transport security now distinguishes local and remote receiver
indexes. The local receiver index is what this process accepts on inbound
encrypted packets; the remote receiver index is what this process writes into
outbound encrypted packets for the peer. Older static lab configs may still use
one shared `receiver_index`, which expands to both values.

The Rust dataplane must not learn identity policy, topology authority rules,
peer allow/deny decisions, or control metadata semantics. Those stay in Python.
Rust may hold the minimum mutable state required for packet-rate execution:
traffic keys, send counters, receive replay windows, sockets, queues, and
counters.

## Compact Wire Shape

Data packets should have a compact WireGuard-style cleartext envelope and keep
the compact Gatherlink v2 logical frame encrypted. V2 is the secure-mode
decrypted header: the compact v1 logical frame without its visible version byte.
The public envelope carries only what is needed to select session state,
authenticate the packet, and reject replays.

Encrypted data packet:

```text
0      u8   packet_type        0x01 = encrypted data
1      u32  receiver_index     opaque session receiver index
5      u64  counter            per-direction packet counter
13     ...  ciphertext         encrypted compact Gatherlink v2 frame bytes
end-16 16B  tag                ChaCha20-Poly1305 tag
```

The clear header is 13 bytes. With the AEAD tag, the minimum transport overhead
for an encrypted data packet is 29 bytes plus the encrypted compact Gatherlink
v2 frame.
The AEAD associated data is exactly the 13-byte clear header plus the domain
separator `GATHERLINK_DATA_V1`.

The crypto suite, protocol version, and active key phase are session state
selected by the authenticated handshake. They are not repeated in every data
packet.

Data packet version handling is deliberately implicit. The receiver uses
`receiver_index` to find the candidate session, builds the AEAD associated data
from the clear header and that session's negotiated protocol context, and tries
to authenticate/decrypt. If authentication succeeds, the packet is for the
correct version/suite/key phase. If it fails, the receiver silently drops the
packet. There is no public fallback probe and no unauthenticated version oracle.

The encrypted plaintext is the compact Gatherlink v2 logical frame:

```text
V2LogicalHeader || optional_fragment_metadata || payload
```

This keeps service id, path id, frame kind, virtual UDP payload, and control
metadata hidden from unauthenticated observers and ordinary relays.

Untrusted relays must not derive forwarding from encrypted `service_id`.
Instead, relay forwarding is explicit control-plane state: a relay maps
`receiver_index` and local tunnel/session context to the next hop it has been
authorized to use. Endpoint exit/service routing happens only after decryption,
using `service_id` plus authenticated config/control context. If a future mode
needs per-hop label swapping, it should add a separate per-hop wrapper instead
of exposing endpoint service/path metadata in the end-to-end data envelope.

Handshake packets are also fixed-shape. They should use a small packet type byte
followed by the Noise message bytes and optional DoS-cookie fields. The exact
handshake message sizes are owned by the selected Noise/WireGuard-style
implementation, but the receiver rules below are part of the Gatherlink
protocol contract.

No packet contains unauthenticated feature negotiation, version replies, debug
strings, or parse errors.

## Identity

Every node has a stable signing identity:

```text
node_id = sha256("gatherlink node v1" || ed25519_public_key)
```

Human names, roles, relay authorization, exit authorization, services,
reachable prefixes, and capability declarations are metadata bound to that node
identity by signed control-plane documents. They are not inferred from packet
contents.

Python owns identity lifecycle:

- enrollment
- provisioning bundles
- trust-root selection
- topology validation
- key rotation policy
- revocation lists
- recovery/reset workflows

Rust receives compiled, already-validated identity/session material for fast
packet processing.

## Signed Documents

Gatherlink signs control-plane artifacts, not individual data packets. Signed
artifacts must use deterministic encoding, preferably canonical CBOR, and must
include domain separation in the signed bytes.

Examples:

```text
GATHERLINK_TOPOLOGY_V1 || canonical_cbor(topology_body)
GATHERLINK_NODE_CAPABILITY_V1 || canonical_cbor(capability_body)
GATHERLINK_CONFIG_APPLY_V1 || canonical_cbor(config_apply_body)
```

Signed document bodies should include:

- schema version
- issuer identity
- subject identity
- key id
- created time
- validity window
- generation/version
- artifact-specific body

Use signatures for topology packages, capability declarations, relay/exit
authorization, safe config-apply requests, provisioning manifests, and
revocation lists.

## Handshake

Authenticated session setup uses reserved service id `7` (`auth / crypto`).

Normal configured peers should use Noise IK:

```text
initiator knows responder static X25519 key from signed topology
responder authenticates initiator against signed topology/trust policy
handshake derives directional traffic keys
```

Enrollment/bootstrap flows may use Noise XX because one or both static keys may
not be known yet. XX must be treated as an enrollment protocol, not ordinary
data-plane peer authentication; it needs explicit trust-on-first-use,
out-of-band approval, or signed bootstrap tokens.

Handshake transcript binding must include:

- Gatherlink protocol version
- crypto suite id
- local and remote node ids
- static X25519 public keys
- signed topology generation or enrollment context
- intended session/service/path capability set

Public UDP listeners must not respond to unauthenticated packets with errors or
diagnostics. Invalid handshake and packet inputs are silent drops, with local
rate-limited counters only.

Version and suite negotiation happen only inside authenticated handshake state.
Peers may advertise supported versions or crypto suites as signed topology data
or authenticated handshake payloads. A public receiver must never send "wrong
version", "unsupported suite", or "try this instead" responses.

Handshake packet classes:

```text
0x10 handshake initiation
0x11 handshake response
0x12 cookie reply / retry token
0x01 encrypted data
```

The receiver must only send `0x11` after the initiation decrypts/authenticates
for the local static X25519 key and maps to an allowed peer or approved
bootstrap token. It must only send `0x12` when the request proves knowledge of
the local anti-DoS MAC/cookie key. Unknown peers, malformed packets, wrong
versions, unsupported suites, bad MACs, failed decrypts, expired timestamps,
revoked identities, and disallowed topology contexts all produce the same
network behavior: no response.

The initiator may retransmit handshake initiation with jittered backoff. The
responder must not create expensive peer/session state until the initiation has
passed the cheap public checks and authenticated Noise processing.

## Packet Envelope

The encrypted packet format keeps routing-visible data minimal and places the
compact Gatherlink v2 logical frame inside the AEAD plaintext. The compact wire
shape above is the default data envelope.

AEAD associated data includes the outer envelope fields and a domain separator.
The inner plaintext is the encoded compact Gatherlink v2 frame: frame kind,
service id, path id, sequence, optional fragment metadata, and payload.

This means relays/carriers can forward based on explicit receiver/session state,
but they do not see virtual UDP payloads or service/path labels. In relay mode,
the routing-visible state is the outer routing/relay-hop header plus the
authenticated relay session table.

## Encrypted Relay Routing

Secure Gatherlink does not support plaintext routing. Relays must never depend
on plaintext `service_id`, `path_id`, endpoint address, tenant name, route
label, or policy label inside an endpoint data packet. Data-plane routing
through untrusted peers requires an outer routing/relay-hop header and a
hop-authenticated relay envelope.

Relay mode wraps the end-to-end encrypted packet in a per-hop encrypted packet:

```text
HopPacket {
  packet_type
  relay_receiver_index
  relay_counter
  ciphertext(InnerPacket)
  relay_tag
}

InnerPacket {
  packet_type
  endpoint_receiver_index
  endpoint_counter
  ciphertext(V2LogicalHeader || payload)
  endpoint_tag
}
```

The relay can authenticate the hop packet and enforce relay policy, but it
cannot decrypt the endpoint packet. Therefore it cannot see endpoint
`service_id`, `path_id`, frame kind, sequence, control metadata, or payload.

Relay forwarding table entries are explicit control-plane state:

```text
relay_receiver_index -> {
  authenticated upstream peer
  configured next hop
  allowed direction
  expiry / revocation generation
  replay window
  rate limits
}
```

A relay forwards only after all hop-level checks pass:

```text
lookup relay_receiver_index
AEAD authenticate/decrypt hop payload
check relay replay window
check authorization, direction, expiry, and rate limits
forward inner packet to configured next hop
```

Failures are silent drops with local rate-limited diagnostics only:

- unknown relay receiver index
- malformed hop packet
- failed hop AEAD authentication
- replayed relay counter
- expired or revoked relay session
- unauthorized direction or next hop
- rate limit exceeded
- malformed inner packet envelope

This prevents blind forwarding. Random packets and forged relay traffic cannot
produce a valid hop AEAD tag, cannot advance the replay window before
authentication, and cannot cause the relay to emit unauthenticated responses.

Endpoint service routing happens only at the final decrypting peer:

```text
endpoint receiver_index -> endpoint session
AEAD decrypt endpoint packet
parse compact v2 logical frame
service_id + authenticated config/control context -> service/exit decision
```

Each relay hop adds one 29-byte hop envelope. Normal overhead:

```text
direct secure path: 29-byte endpoint envelope + 13-byte v2 header = 42 bytes
one relay hop:      29-byte hop envelope + 42-byte endpoint packet = 71 bytes
two relay hops:     29 + 29 + 42 = 100 bytes
```

Batching should be used to amortize relay overhead for small payloads.

## Compact V1 And V2 Logical Frames

The compact v1 logical frame is the versioned endpoint header used by plaintext
lab mode. Secure transport decrypts to v2, which is the same layout without the
visible `version` byte. Older 38-byte draft headers should be treated as
migration scaffolding.

V1 plaintext/lab header:

```text
0      u8   version          current value 1
1      u8   kind_flags       bits 0..1 kind, bit 2 fragment-present
2      u16  service_id       virtual UDP or internal Gatherlink service
4      u16  path_id          logical path/carrier id
6      u64  sequence         logical per-session/service packet sequence
14     ...  payload          when fragment-present is unset
```

V2 secure decrypted header:

```text
0      u8   kind_flags       bits 0..1 kind, bit 2 fragment-present
1      u16  service_id       virtual UDP or internal Gatherlink service
3      u16  path_id          logical path/carrier id
5      u64  sequence         logical per-session/service packet sequence
13     ...  payload          when fragment-present is unset
```

`kind_flags`:

```text
bits 0..1: kind
  0 = data
  1 = control
  2 = batch
  3 = reserved
bit 2: fragment metadata present
bits 3..7: reserved, must be zero
```

Normal compact v1 overhead is 14 bytes. Normal compact v2 overhead is 13 bytes.
Fragmented packets carry fixed fragment metadata immediately after the compact
header. V2 fragmented layout:

```text
13     u32  datagram_id
17     u16  fragment_index
19     u16  fragment_count
21     u16  original_len
23     ...  fragment_payload
```

Fragmented compact v1 overhead is 24 bytes. Fragmented compact v2 overhead is
23 bytes. There is no general `header_len` field; the fragment bit is the only
hot-path variable header indicator.

Compact v1/v2 removes fields that are redundant after secure receive or local
plaintext context:

- `version`: present in v1 plaintext; omitted from v2 because it is selected by
  authenticated session context and successful AEAD authentication
- `header_len`: replaced by the fragment-present bit and fixed fragment metadata
- `flags`: replaced by `kind_flags` with reserved bits that must be zero
- `session_id`: mapped from `receiver_index` to the authenticated peer session
- `route_id`: removed completely. It must not appear in packet format, runtime
  DTOs, scheduler hot path, transmit plans, or active compatibility views.
- `payload_len`: derived from authenticated plaintext length

After decryption, runtime code may expose a compatibility view that looks like
the older 38-byte draft `FrameHeader`. That view is synthesized, not parsed from
the packet:

```text
version     <- session/protocol context
kind        <- compact v2 kind_flags
header_len  <- legacy compatibility value only; compact wire uses fixed base plus fixed fragment metadata
flags       <- compatibility flags, normally zero
session_id  <- receiver_index/session mapping
service_id  <- compact v2 service_id
path_id     <- compact v2 path_id
sequence    <- compact v2 sequence
payload_len <- decrypted plaintext length minus compact header/fragment metadata
```

New code should prefer compact v1 in plaintext mode and compact v2 after secure
decryption. The compatibility view exists only to reuse older draft-header code
paths while the dataplane is being migrated.

## Sessions, Paths, And Duplicates

Normal multipath between the same two authenticated nodes uses one peer crypto
session across many paths:

```text
peer session
  send key / receive key
  transport send counter
  transport replay window
  path 1 metrics and carrier state
  path 2 metrics and carrier state
  path 3 metrics and carrier state
```

Do not create a separate cryptographic session per path unless the path is a
different trust domain, such as a relay-mediated session, exit/site-gateway
session, bootstrap path, or isolated tenant path.

Intentional multipath fanout sends the same inner Gatherlink frame more than
once, but each copy is encrypted as a distinct transport packet with its own
counter:

```text
path A: transport counter = 100, inner frame sequence = 55
path B: transport counter = 101, inner frame sequence = 55
path C: transport counter = 102, inner frame sequence = 55
```

Replay protection operates on the outer transport counter after AEAD
authentication. It rejects a captured encrypted packet sent again with the same
counter and tag.

Dataplane dedupe operates after decryption on the inner Gatherlink frame
identity, normally session/service/sequence. It rejects extra authenticated
copies of the same logical payload that arrived over different paths. This is
expected fanout behavior, not a crypto replay.

The ordering is therefore:

```text
decrypt/authenticate transport packet
check transport replay window
parse inner Gatherlink frame
dedupe by inner service/sequence policy
deliver or record as expected duplicate
```

This split is important: replay defense protects the cryptographic transport;
dedupe preserves Gatherlink's multipath semantics.

## Stealth Receive Contract

Gatherlink public UDP receive behavior should match WireGuard's useful stealth
property: unauthenticated traffic gets no response and no distinguishable
network error from Gatherlink.

Receiver requirements:

- bind UDP sockets in a way that avoids application-level replies to invalid
  input
- never emit unauthenticated version negotiation, errors, redirects, metrics,
  or diagnostics
- never expose capability information before authentication
- parse only the minimum fixed header before authentication
- perform cheap packet-type, length, receiver-index, and cookie/MAC checks
  before expensive cryptography
- collapse all authentication/decryption/topology failures into silent drop
- keep local counters coarse and rate-limited; do not send them to the peer
  until the peer is authenticated
- use jittered timers for handshake retransmission and keepalive behavior
- make response size no larger than the request until the peer is authenticated,
  except for cookie replies that require a valid anti-DoS MAC
- avoid timing and logging behavior that lets remote scanners distinguish
  unknown peer, bad key, bad topology, expired identity, or unsupported suite

Operationally, a scanner that does not know valid Gatherlink key material should
see the same thing as an unused UDP port: no Gatherlink response. This does not
hide traffic from a passive observer already watching the link, and it does not
override operating-system or firewall behavior outside the Gatherlink process.
Production deployments should pair this with firewall rules that drop unrelated
UDP traffic without ICMP port-unreachable responses.

## Nonces And Counters

Never use random nonces for packet encryption. Each traffic key has a
per-direction `u64` counter. The AEAD nonce is derived from the traffic
key/session context plus the encoded counter.

Rules:

- one counter space per traffic key and direction
- reject counter reuse locally
- reject replays remotely with a sliding window
- rekey before counter exhaustion
- treat counter wrap as a hard session failure
- do not mark a counter as seen until AEAD authentication succeeds
- do not treat different counters carrying the same decrypted frame sequence as
  transport replay; pass them to inner-frame dedupe

## Rekeying

Sessions should support WireGuard-like replacement:

- time-based rekey before traffic keys get old
- volume-based rekey before counters get large
- a fresh receiver index for each replacement receive session
- new Rust traffic-key/replay state when Python compiles a replacement session
- an optional short receive grace period for the previous receiver index once
  the service lifecycle supports overlapping sessions

V1 keeps the public packet header compact and does not add a plaintext key-phase
field. Rekey/rotation is represented by Python creating a replacement
authenticated session with a new receiver index and then hot-reapplying compiled
traffic-key facts to Rust. Exact intervals can be tuned later, but the
implementation must assume keys are temporary session material.

## Implementation Boundary

Rust `gatherlink-crypto` and `gatherlink-dataplane` own deterministic packet
security execution:

- traffic keys and receiver-index lookup facts compiled by Python
- envelope encrypt/decrypt
- nonce construction
- replay window
- counter exhaustion failure
- constant-shaped validation failures for public packets

Python owns control-plane trust and policy:

- signed topology/config artifacts
- Noise IK style authenticated session setup
- enrollment and provisioning UX
- trust root and revocation policy
- mapping node identities to allowed services, relays, exits, and paths
- compiling validated session inputs for Rust
- rekey cadence, receiver-index rotation policy, and hot reapply orchestration

This boundary matches the rest of Gatherlink: Rust is the fast deterministic
dataplane; Python is the policy/control plane.

## Sealed-Secret Boundary

Sealed-secret UX may protect provisioning bundles, exported node identities,
topology packages with embedded secrets, bootstrap tokens, and backups at rest.

Sealed secrets are not packet transport crypto and must not be used for
per-packet encryption.
