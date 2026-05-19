# Configuration

Gatherlink accepts small user-facing JSON configs and normalizes them into a
canonical Pydantic model before any runtime logic sees them.

Current supported example formats are:

- `minimal-client`
- `minimal-server`
- `wireguard-client`
- `wireguard-server`
- `dns-helper`

Every config file must declare `schema_version`. The current schema is version
`1`; keeping this explicit in examples avoids guessing what a future versionless
file meant during migrations.

Schema-version handling lives in `python/gatherlink/config/versions.py`. To add
version `2`, add a v2 normalizer there, register it in `SUPPORTED_CONFIG_SCHEMAS`,
and keep the rest of the loader/CLI path pointed at `normalize_config_for_schema`.
That keeps migration logic in one place before the canonical Pydantic model runs.

Development install:

```bash
. .venv/bin/activate
python -m pip install -e .
```

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
```

Format detection is deliberately shallow. It only chooses which input mapping to
use; the canonical config model owns all real relationship checks, such as unique
service names and helper references.


`config show --canonical` prints the validated user-facing config after schema
version and format normalization. `config show --runtime` prints the explicit
runtime contract from `python/gatherlink/config/runtime.py`; this is the boundary
future runner, helper, and dataplane code should consume.
