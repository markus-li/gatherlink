# Configuration

Gatherlink accepts small user-facing JSON configs and normalizes them into a
canonical Pydantic model before any runtime logic sees them.

For the broader config/runtime boundary, live reload model, numeric-id guidance,
and runtime introspection requirements, see [`docs/runtime/config-runtime-state.md`](config-runtime-state.md).

Current supported example formats are:

- `minimal-client`
- `minimal-server`
- `wireguard-client`
- `wireguard-server`
- `dns-helper`

Every config file must declare `schema_version`. The current schema is version
`1`; keeping this explicit in examples avoids guessing what a future versionless
file meant during migrations.

Schema-version parsing lives in `python/gatherlink/config/versions.py`. Explicit
schema migration lives in `python/gatherlink/config/migration.py`.

Migrations use Pydantic-backed report models and explicit version-to-version
transform steps. Larger jumps must be chained through intermediary versions, and
supported downgrades should be explicit reverse transforms. If a downgrade would
lose required meaning, it must fail clearly instead of inventing unsafe defaults.

The CLI entry point is:

```bash
gatherlink config migrate configs/examples/minimal-client.json
gatherlink config migrate configs/examples/minimal-client.json --to-schema-version 1
gatherlink config migrate configs/examples/minimal-client.json --write
```

Dry-run is the default so operators can review transformed output and warnings
before replacing a config file.

Development install:

```bash
python3 --version  # must be 3.12 or newer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
maturin develop --manifest-path crates/pybindings/Cargo.toml --release
```

For the full minimum setup and first smoke checks, see
[`docs/user/quickstart.md`](../user/quickstart.md).

Useful commands after the editable install:

```bash
python -m gatherlink.cli.main config detect configs/examples/minimal-client.json
python -m gatherlink.cli.main config validate configs/examples/minimal-client.json
python -m gatherlink.cli.main config validate --json configs/examples/minimal-client.json
python -m gatherlink.cli.main config show configs/examples/minimal-client.json
```

The console script is also available:

```bash
gatherlink config detect configs/examples/minimal-client.json
gatherlink config validate configs/examples/minimal-client.json
gatherlink config validate --json configs/examples/minimal-client.json
gatherlink config show --runtime configs/examples/minimal-client.json
gatherlink config show --canonical configs/examples/minimal-client.json
gatherlink config summary configs/examples/minimal-client.json
```

Format detection is deliberately shallow. It only chooses which input mapping to
use; the canonical config model owns all real relationship checks, such as unique
service names and helper references.

## Scheduler Fields

Path scheduler hints live in user config under each path:

```json
{
  "name": "wan-a",
  "interface": "eth0",
  "scheduler": {
    "enabled": true,
    "state": "active",
    "weight": 1,
    "mtu": 1200
  }
}
```

Python compiles these hints into the runtime scheduler contract. Rust receives
only compact execution values: path id, enabled flag, state, weight, MTU, and
already-derived service/session context. This keeps policy, scoring, and future
adaptive behavior in Python while still letting Rust make cheap packet-time
choices.

Services may declare a priority label: `bulk`, `normal`, `high`, or `critical`.
Python compiles that label to a stable numeric runtime value. Priority belongs
to configured Gatherlink services, not packet inspection, and is currently
scaffolded for future multi-service fairness.

Service scheduling fanout is represented as primitive runtime fields, not Rust
policy names. `scheduler_fanout=1` is the normal one-path behavior,
`scheduler_fanout=0` means every eligible path, and values above one duplicate
over that many eligible paths. `scheduler_fanout_below_bytes=0` applies fanout
to every payload; a nonzero value applies fanout only at or below that payload
size, letting Python build policies such as `duplicate_small`.

`config show --canonical` prints the validated user-facing config after schema
version and format normalization. `config show --runtime` prints the explicit
runtime contract from `python/gatherlink/config/runtime.py`; this is the boundary
future runner, helper, and dataplane code should consume. Both views are
operator introspection, so secret-looking fields are redacted in the output even
when the source config file stores explicit session material.

Runtime JSON is also the first operator/automation view for compiled state.
Human terminal tables may be added later, but they should be derived from the
same compiled runtime model. `config summary` is the stable compact automation
view for compiled path, service, helper, security, and scheduler facts.
