# Security Policy

Gatherlink is security-sensitive networking software. Treat it as active
development unless a release explicitly says otherwise.

## Supported Versions

For v0.9, security fixes target the current `v0.9.x` release line and the active
development branch. Older pre-v0.9 commits are not supported as release lines.

Future support windows should be documented in the matching release notes.

## Reporting A Vulnerability

Do not open a public issue with exploit details.

Preferred reporting path:

1. Use GitHub private vulnerability reporting if it is enabled for the
   repository.
2. If private reporting is not enabled, open a minimal public GitHub issue that
   says you need to report a security issue and do not include exploit details.

Include when possible:

- affected commit or release
- operating system
- whether the issue requires local access, network access, or a configured peer
- high-level impact
- reproduction steps, if safe to share privately

## Security Maturity

For v0.9, assume:

- the project has not had an external security audit unless a release note says
  otherwise
- Debian is the tested platform
- authenticated sessions are the intended secure path
- static crypto is explicit lab/manual fallback only
- invalid encrypted packets should silently drop on the network side
- diagnostics and status output must not expose private keys, session keys,
  bootstrap tokens, passwords, or private endpoint material
- the project is suitable for personal/lab and small-site use, not a security
  product with formal assurance claims

## Public Disclosure

Please give maintainers time to reproduce, fix, and release before publishing
details. Coordinated disclosure is preferred over surprise public disclosure.
