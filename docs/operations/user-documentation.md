# User Documentation

User documentation is for people running Gatherlink. It should stay short,
step-by-step, and scenario based.

The canonical source lives in the repository under `docs/user/`. The GitHub
Wiki may publish a copy of those docs for easier browsing, but the Wiki must not
become the source of truth.

## Shape

Every user-facing page should:

- start with the task the user is trying to complete
- use numbered steps for the normal path
- include only the commands needed for that scenario
- keep explanations short
- link to troubleshooting instead of embedding long debug sections
- link to the config cookbook instead of embedding large config variants
- link to the operator runbook instead of duplicating start/stop/status flows
- say when behavior is Debian-only or only tested on Debian
- ask users to report bugs as GitHub issues

Avoid:

- design rationale
- protocol internals
- long architecture explanations
- future feature promises
- developer-only config theory

## Scenario Pages

The main user pages are:

- `docs/user/core-service.md`
- `docs/user/config-cookbook.md`
- `docs/user/socks5.md`
- `docs/user/wireguard.md`
- `docs/user/troubleshooting.md`

Add new user pages only when there is a real operator workflow. Do not mirror
every design doc into user docs.

## GitHub Wiki Publishing

For v0.9.1, publish the short user docs to the repository GitHub Wiki as release
documentation. This must be automated as part of the package/release process;
manual Wiki editing is allowed only for emergency correction and must be copied
back into `docs/user/` immediately.

Rules:

- repository docs remain canonical
- Wiki pages are generated or copied from `docs/user/`
- the release/package build must have a repeatable command that prepares the
  Wiki payload
- the release process should fail or warn loudly if the Wiki payload differs
  from the committed `docs/user/` source unexpectedly
- Wiki pages should keep the same short step-by-step style
- Wiki pages should link back to the exact release tag or commit they describe
- release notes should say which Wiki revision matches the release
- do not edit the Wiki by hand in ways that diverge from repository docs
- do not publish secrets, local VM hostnames, private IPs, or environment-specific
  lab material

The first Wiki set should cover:

- overview and tested platform note
- core UDP service setup
- config cookbook
- SOCKS5 helper
- WireGuard helper
- troubleshooting and GitHub issue reporting

## Release Checklist

Before a v0.9.1 release:

1. Review `docs/user/` for stale commands.
2. Verify sample configs work without Cloudflare, Traefik, or external accounts.
3. Run the automated Wiki payload generation/publish step from the release
   tooling.
4. Verify the generated Wiki payload matches the committed `docs/user/` source.
5. Link the release notes to the Wiki entry point.
6. Record the repo commit/tag used as the Wiki source.
