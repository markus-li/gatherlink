# Quickstart

Use this when you want the shortest path from a fresh Debian checkout to a
working Gatherlink CLI and Rust-backed core runner.

Gatherlink is currently tested on Debian. Other Linux distributions may work,
but Debian is the supported compatibility target.

## Minimum Development Setup

Install system tools:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential pkg-config curl git cargo rustc
python3 --version
```

Gatherlink requires Python 3.12 or newer. If `python3 --version` reports an
older interpreter, install a newer Python through your Debian release,
backports, or normal site Python tooling before creating the virtual
environment.

Create the virtual environment and install Python dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

Build the Rust/PyO3 dataplane binding into the same environment:

```bash
maturin develop --manifest-path crates/pybindings/Cargo.toml --release
```

Check the CLI and Rust binding:

```bash
gatherlink --help
gatherlink doctor --config configs/examples/minimal-client.json
```

If `doctor` says the Rust dataplane binding is missing, rerun the `maturin
develop` command from the repository root with the virtual environment active.

## First Config Checks

Validate and inspect a small example:

```bash
gatherlink config validate configs/examples/minimal-client.json
gatherlink config show --runtime configs/examples/minimal-client.json
gatherlink run plan configs/examples/minimal-client.json
```

The example configs are starting points. For real use, change bind addresses,
remote path endpoints, service listen addresses, service targets, and security
material for your nodes.

## First Local Smoke

Use the Rust lab smoke to prove the installed runner can move UDP packets in a
local test:

```bash
gatherlink lab rust-smoke configs/lab/local-dual-path.json --count 5
gatherlink lab rust-smoke configs/lab/local-dual-path-encrypted.json --count 5
```

These smoke tests do not replace VM or real-site acceptance, but they are the
fastest sanity check after setup.

## Normal Runtime Shape

For real two-node use:

1. Put a node-specific config on each node.
2. Validate and run `doctor` on each node.
3. Start the sink/receiver side first.
4. Start the source side.
5. Watch `services monitor`.

```bash
gatherlink config validate node-a.json
gatherlink doctor --config node-a.json
gatherlink run start node-b.json --name core.node-b --scheduler-reapply-interval 5
gatherlink run start node-a.json --name core.node-a --scheduler-reapply-interval 5
gatherlink services monitor core.node-a core.node-b --once
```

Normal Gatherlink services should run unprivileged. Lab setup and shaping tools
may need root when they create network namespaces, veth devices, routes, or
traffic shaping.
