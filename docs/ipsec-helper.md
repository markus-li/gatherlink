# IPsec Helper

## Purpose

Gatherlink can carry UDP-encapsulated IPsec, especially IPsec NAT-T.

It is not an IPsec implementation.

## Supported

Templates/helpers may support:

- IKE UDP/500
- IPsec NAT-T UDP/4500

Example:

```text
local UDP/500  -> Gatherlink virtual UDP service -> remote UDP/500
local UDP/4500 -> Gatherlink virtual UDP service -> remote UDP/4500
```

## Not supported directly

Gatherlink core does not directly carry:

- raw ESP, IP protocol 50
- AH, IP protocol 51

Supporting those directly would require generic IP tunneling, raw sockets, TUN/TAP,
root privileges, or firewall/router behavior, which violates the core design.

## Real-world note

Most IPsec behind NAT already uses NAT-T, which wraps ESP in UDP/4500. That is
the practical compatibility target.

## Helper scope

The helper may provide:

- service templates
- diagnostics
- clear warnings about ESP/AH
- example configs for firewalls

It must not become an IPsec stack.
