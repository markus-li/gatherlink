# Contributing

Gatherlink is young, security-sensitive networking software. Contributions are
welcome, but changes need to protect the project boundaries.

## Before Changing Code

Read:

- [`docs/README.md`](docs/README.md)
- [`docs/architecture/architecture-contract.md`](docs/architecture/architecture-contract.md)
- [`docs/architecture/source-map.md`](docs/architecture/source-map.md)
- [`docs/operations/testing-strategy.md`](docs/operations/testing-strategy.md)
- the feature/helper/protocol doc for the area you are changing

## Project Laws

Keep this file as a contributor entry point, not a second policy home. The
standing project laws live in:

- [`docs/architecture/architecture-contract.md`](docs/architecture/architecture-contract.md)
- [`docs/architecture/source-map.md`](docs/architecture/source-map.md)
- [`docs/operations/development-discipline.md`](docs/operations/development-discipline.md)
- [`docs/protocol/security.md`](docs/protocol/security.md)

If those docs disagree, fix the owning doc and then keep this file as a short
pointer to it.

## Tests

Add or update focused tests for the code you touch.

Default verification:

```bash
cargo fmt -- --check
cargo test --workspace
.venv/bin/ruff check .
.venv/bin/black --check .
python3 -m compileall -q python tests tools
.venv/bin/pytest -q
```

Runtime, helper, crypto, relay, scheduler, diagnostics, and packet-path changes
also need the relevant lab or VM acceptance check. Do not rely on unit tests
alone when the behavior is cross-process or cross-path.

## Docs

Update docs when behavior, commands, config, diagnostics, security posture, or
operator workflow changes.

User docs must stay short, step-by-step, and scenario based. Put rationale in
design docs, not user pages.

## Pull Request Shape

Prefer small coherent changes:

- one feature or fix per pull request
- tests in the same pull request
- docs in the same pull request when behavior changes
- no unrelated refactors
- no generated cache files
- no secrets, private hostnames, local VM keys, or personal machine paths

## Security Changes

For security-sensitive changes, explain:

- what is protected
- what fails closed
- what is intentionally silent on the network
- which diagnostics remain local
- which tests prove invalid input is rejected

Do not add diagnostic detail that would weaken stealth receive or relay
fail-closed behavior.
