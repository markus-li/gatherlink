# Release Artifacts And Debian Package

Gatherlink packaging starts with reproducible GitHub release artifacts for
Debian users. This is release tooling only; it must not hide runtime policy,
provisioning, topology, or helper behavior.

The local artifact command is:

```bash
.venv/bin/python tools/release/prepare_artifacts.py --version VERSION --out dist/VERSION
```

It creates:

- `gatherlink-VERSION-source.tar.gz`: tracked source archive
- `python-wheel/`: a local no-dependency Python wheel built from the checkout
- `rust-binaries/gatherlink-time-helper`: the Debian-oriented Rust time helper
  binary built in release mode
- `gatherlink_VERSION_amd64.deb`: a Debian-oriented operator package
- `SHA256SUMS`: checksums for the source archive, built wheel, Rust helper
  binary, and Debian package
- `wiki-user-docs/`: a GitHub Wiki payload copied from `docs/user/`

The `.deb` installs:

- Python package files and Python runtime dependencies under the private
  `/usr/lib/gatherlink/python` import path
- `/usr/bin/gatherlink` wrapper for the Python CLI
- `/usr/bin/gatherlink-time-helper` wrapper for the Rust helper binary
- user docs under `/usr/share/doc/gatherlink`
- example configs under `/usr/share/gatherlink/examples`

The package must not auto-start Gatherlink, mutate firewall state, create
tunnels, install privileged helper state, or silently enable systemd units.
Operators still make those decisions explicitly through config, helper commands,
and service lifecycle commands.

Validate a prepared artifact directory with:

```bash
gatherlink doctor --release-artifacts dist/VERSION
```

The tooling refuses obvious host-local or secret-looking paths such as
`.gatherlink`, `.venv`, `inventory.env`, private identity files, pending
handshake state, and sealed-secret envelopes. Release artifacts should be built
from a clean tree after the normal source, lab, VM, and soak checks pass.

Future package formats may wrap the same source, wheel, Rust binaries, and
Debian package content, but they should preserve the Debian compatibility
boundary and keep repository docs canonical.
