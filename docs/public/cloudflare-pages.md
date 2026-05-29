# Cloudflare Pages Deployment Notes

These notes are for a static public documentation website. They do not change
Gatherlink runtime behavior and do not add a hosted control plane.

## Scope

Allowed:

- publish static Markdown-rendered or static-site-generated documentation
- link to GitHub releases, issue templates, benchmark evidence, and user guides
- publish security caveats and supported-platform notes

Not allowed without a future roadmap:

- hosted accounts
- remote management APIs
- telemetry collection
- browser-based tunnel control
- secret upload or provisioning through a hosted service

## Suggested Static Build

The simplest public site can be generated from committed docs with any static
site generator that preserves working GitHub links. Keep generated output out of
the source tree unless a release roadmap explicitly adds a site build artifact.

Minimum navigation:

- home: [public landing](README.md)
- install: [quickstart](../user/quickstart.md)
- WireGuard setup: [multipath guide](../user/wireguard-multipath.md)
- security: [security policy](../../SECURITY.md) and [security design](../protocol/security.md)
- benchmarks: [benchmark methodology](../benchmarks/README.md)
- support: [GitHub issue chooser](https://github.com/markus-li/gatherlink/issues/new/choose)

## Release Rule

Website copy should be updated from the release notes and living assessment
during each release gate. If code and website copy disagree, fix the code-facing
docs first, then update the website copy as a mirror.
