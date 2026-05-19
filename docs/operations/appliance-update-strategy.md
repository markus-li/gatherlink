# Appliance Update Strategy

## Purpose

Commercial appliances need safe updates without affecting open-source usability.

## Update principles

Updates should be explicit, staged, rollback-capable, observable, compatible with
config versioning, and safe during peer failover where possible.

## Open source vs appliance

Open source provides packages, CLI, source builds, and documented systemd
deployment. Commercial appliance may add managed update channels, health checks
before update, automatic rollback, fleet visibility, and update windows.

## Compatibility

Update strategy depends on protocol versioning, capability negotiation, config
migration, helper version compatibility, and Rust/Python boundary compatibility.

## Rollback

Rollback should preserve config, identity, last-known-good bootstrap cache, and
enough logs to diagnose failure.
