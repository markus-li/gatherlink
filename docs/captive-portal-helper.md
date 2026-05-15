# Captive Portal Helper

## Purpose

Captive portal handling is a connectivity helper. It must not contaminate the
core transport or require Gatherlink to become a firewall.

The clean primitive is:

```text
temporary SOCKS5 proxy pinned to the captive WAN
```

Everything else is UX around that primitive.

## Why SOCKS5

SOCKS5 works in non-root mode because it does not require:

- routing changes
- firewall rules
- VLAN manipulation
- transparent interception
- DNS interception
- browser TLS MITM
- TUN/TAP

The proxy opens outbound connections using the selected captive WAN source
address/interface behavior that Gatherlink already validates for paths.

## Helper modes

Supported/future UX modes:

1. Manual/PAC mode
   - start temporary SOCKS5 proxy
   - show host/port
   - offer PAC file
   - user configures browser manually

2. Streamed browser mode
   - appliance starts isolated Chromium through SOCKS5
   - UI streams browser session to the user
   - user logs in without browser proxy changes

3. Standalone login browser/app
   - app discovers Gatherlink
   - app requests captive-login session metadata
   - embedded browser/webview uses the right SOCKS5 proxy automatically

4. Appliance/custom Chromium
   - prebuilt minimal Chromium/profile
   - launches with the correct SOCKS5 proxy
   - temporary profile destroyed after login

## Explicitly rejected approaches

Avoid these as primary design:

- HTML rewrite proxy
- HTTPS MITM
- DNS interception for arbitrary portal domains
- transparent proxy as default
- full routing/NAT manipulation as the only solution

They are either fragile, require root/firewall control, or create bad security
properties.

## Session lifecycle

Flow:

```text
WAN detects captive portal
  -> mark physical link captive_portal
  -> remove it from normal scheduling
  -> start temporary SOCKS5 login session
  -> user logs in by selected UX mode
  -> Gatherlink retests WAN/carriers
  -> stop SOCKS5 session
  -> mark link usable or still restricted
```

## Safety rules

The captive portal SOCKS5 helper must:

- be temporary
- bind to local/LAN only by default
- require explicit user/session activation
- be pinned to one physical WAN
- not log credentials
- not cache portal pages
- not become a permanent general proxy
- enforce TTL
- enforce connection limits
- shut down after success/failure/timeout

## Retesting

After login, retest:

- DNS
- HTTP
- HTTPS
- raw UDP
- stealth UDP
- QUIC
- WSS
- bootstrap/connect validation

Only then should carrier discovery and scheduler activation resume.
