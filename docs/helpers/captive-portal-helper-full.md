# Captive Portal Helper Full Design Notes

## Purpose

Captive portal handling is a helper around connectivity, not a core transport
feature.

## Final design direction

The canonical primitive is:

```text
temporary SOCKS5 proxy pinned to the captive WAN
```

All UX modes sit on top of that.

## Modes

### Manual/PAC

User configures browser manually or downloads a PAC file.

### Streamed browser

Appliance runs isolated Chromium through SOCKS5 and streams browser UI to the
user.

### Standalone login browser/app

A small app discovers Gatherlink, gets session metadata, and opens an embedded
browser using the correct SOCKS5 proxy.

### Custom Chromium/profile

Appliance or desktop bundle launches a minimal browser with the correct proxy
arguments.

## Rejected primary approaches

Avoid as primary mode:

- HTML rewrite proxy
- HTTPS MITM
- DNS interception of portal domains
- transparent proxy
- full routing/NAT manipulation

These are fragile or require firewall/root control.

## Safety

The SOCKS5 login proxy must be:

- temporary
- explicitly activated
- pinned to one physical WAN
- local/LAN scoped
- TTL-limited
- connection-limited
- credential-safe
- shut down after login/retest/timeout

## Retest after login

Retest:

- DNS
- HTTP
- HTTPS
- raw UDP
- stealth UDP
- QUIC
- WSS
- bootstrap/connect validation
