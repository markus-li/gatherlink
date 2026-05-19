fmt:
    cargo fmt
    ruff format python

test:
    cargo test
    pytest

check:
    cargo check
    @if rg -n '#\[cfg\(test\)\]|mod tests' crates -g '*.rs'; then echo "Rust tests belong in crate-level tests/ files, not inline production modules."; exit 1; fi
    ruff check python

lab-up:
    gatherlink lab up configs/lab/local-dual-path.json

lab-down:
    gatherlink lab down configs/lab/local-dual-path.json

lab-cleanup:
    gatherlink lab cleanup configs/lab/local-dual-path.json

lab-status:
    gatherlink lab status configs/lab/local-dual-path.json

lab-interfaces:
    gatherlink lab interfaces configs/lab/local-dual-path.json

lab-smoke:
    gatherlink lab smoke configs/lab/local-dual-path.json

lab-logs-tx:
    gatherlink services attach lab.local-dual-path

lab-logs-rx:
    gatherlink services attach lab.local-dual-path.sink

lab-stats:
    gatherlink services monitor lab.local-dual-path lab.local-dual-path.sink

lab-send:
    gatherlink lab send configs/lab/local-dual-path.json

lab-sink:
    gatherlink lab sink configs/lab/local-dual-path.json

services:
    gatherlink services list

service-attach NAME:
    gatherlink services attach {{NAME}}

service-status NAME:
    gatherlink services status {{NAME}}

service-close NAME:
    gatherlink services close {{NAME}}

lab-plan:
    gatherlink lab plan configs/lab/local-dual-path.json

lab-profiles:
    gatherlink lab profiles configs/lab/local-dual-path.json

lab-apply-profile PROFILE:
    gatherlink lab apply-profile configs/lab/local-dual-path.json {{PROFILE}}

lab-apply-shape SHAPE_CONFIG:
    gatherlink lab apply-shape-config configs/lab/local-dual-path.json {{SHAPE_CONFIG}}

lab-clear-shape PATH:
    gatherlink lab clear-shape configs/lab/local-dual-path.json {{PATH}}
