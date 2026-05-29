# Traffic Split Helper

The traffic split helper is an advanced optimization companion for the
dual-WireGuard profile and future transparent TCP proxy mode. It is not a
default Gatherlink behavior.

Purpose:

- keep Gatherlink from inspecting encrypted WireGuard payloads
- let operators run one WireGuard tunnel with a stability/TCP-oriented
  Gatherlink service profile
- let operators run a second WireGuard tunnel with a UDP/throughput-oriented
  Gatherlink service profile
- optionally generate local Debian policy-routing and firewall rules that send
  UDP to the fast tunnel and non-UDP traffic to the stable tunnel
- future work may generate narrow Debian nftables/TPROXY and
  policy-routing rules that intercept selected TCP flows for the TCP proxy
  helper while preserving the original destination
- future work may support a hybrid profile where TCP uses the TCP-aware
  Gatherlink proxy and non-TCP remains inside WireGuard-over-Gatherlink

Boundary:

- the helper owns local split policy and operator guidance
- the Debian platform backend owns executing `nft` and `ip` commands
- WireGuard still owns interfaces, keys, peers, and routes
- Gatherlink core still carries UDP services and does not inspect VPN payloads
- Rust receives only normal service/path scheduler primitives
- transparent TCP interception belongs here and in the Debian compatibility
  backend, not in core Gatherlink or Rust
- transparent rules must be opt-in, labeled, reversible, and narrow enough that
  normal site firewall software can place policy before and after
  Gatherlink-owned rules

Command:

```bash
gatherlink helpers traffic-split \
  --stable-interface wg-gl-stable \
  --fast-interface wg-gl-fast
```

By default this only prints commands. To apply the generated Debian rules:

```bash
gatherlink helpers traffic-split \
  --stable-interface wg-gl-stable \
  --fast-interface wg-gl-fast \
  --apply
```

To remove Gatherlink-generated state:

```bash
gatherlink helpers traffic-split \
  --stable-interface wg-gl-stable \
  --fast-interface wg-gl-fast \
  --revert
```

Operational posture:

- prefer copying the generated rules into the site's normal firewall tooling
  when possible
- use `--apply` for labs, appliances, or carefully reviewed Debian hosts
- keep remote management access outside the split rules until the host has been
  tested
- treat this as a performance profile, not a security boundary

Current rule shape:

- UDP traffic receives the fast tunnel firewall mark
- non-UDP traffic receives the stable tunnel firewall mark
- policy routing tables send those marks to the selected WireGuard interfaces
- IPv4 and IPv6 default routes are generated for both tables
- Gatherlink-generated nftables state uses the named table
  `inet gatherlink_split`, chain `output`, and rule comments starting with
  `gatherlink dual-wireguard split`
- policy routing uses deterministic marks and table ids by default:
  `0x5181`/`51881` for stable and `0x5182`/`51882` for fast
- `--revert` removes only those Gatherlink-named/deterministic objects; it does
  not try to edit unrelated site firewall policy

Future transparent TCP proxy rule shape:

- use TPROXY-style interception rather than plain DNAT/REDIRECT when original
  destination preservation is required
- intercept only explicitly configured TCP destinations, marks, interfaces, or
  CIDR sets
- avoid intercepting Gatherlink control/carrier ports, local management ports,
  loopback-only services, and the TCP proxy helper's own egress
- preserve original destination for the TCP proxy helper
- use a Gatherlink-named nftables table/chain and rule comments
- use deterministic marks and routing table ids documented by the helper
- default to print-only planning; require an explicit apply flag for host
  mutation
- cleanup must remove only Gatherlink-labeled objects

Future hybrid TCP proxy plus WireGuard rule shape:

- TCP selected by configured destination, interface, UID, mark, or CIDR enters
  the transparent TCP proxy helper
- non-TCP traffic stays on the WireGuard-over-Gatherlink path
- Gatherlink carrier/control traffic and local management traffic must be
  excluded so the proxy cannot loop through itself
- explicit TCP proxy listeners can be used to test the stream service before
  enabling transparent interception
- diagnostics must show whether a flow used explicit TCP proxy, transparent TCP
  proxy, or WireGuard-over-Gatherlink fallback
