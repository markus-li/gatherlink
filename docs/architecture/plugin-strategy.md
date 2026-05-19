# Plugin Strategy

## Purpose

Gatherlink should be extensible without letting plugins contaminate core design.

## Initial stance

Do not implement a complex plugin ABI yet. Use normal Python modules/classes
first.

## Extension areas

Possible extension points include diagnostics sinks, DNS upstream transports,
obfuscation profiles, carrier profiles, hook handlers, overlay planning
strategies, and scheduler scoring policies.

## Rules

Extensions must not run in the Rust hot path unless explicitly designed for it,
bypass config validation, require root by default, own firewall/routing policy,
or block diagnostics/dataplane loops.

## Versioning

Future plugin APIs should have explicit interface version, capability
declaration, failure isolation, config validation schema, and clear stability
marking.
