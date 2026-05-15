# Deployment Archetypes Full Design Notes

## Purpose

Deployment archetypes define intended use cases and prevent product drift.

## Archetypes

### Single client to relay

One client with one or more WAN links connects to one relay.

### Dual/multi-WAN client

Client has several physical links and several logical paths.

### Site-to-site

Firewall/router sends selected prefixes through a Gatherlink virtual gateway.

### Hub-and-spoke

Multiple sites connect to one or more hubs/relays.

### Multihop relay

Traffic enters one Gatherlink node, transits through another, and exits later.

### Internet exit

A service exits through a selected regional or policy-matched exit node.

### Travel appliance

A mobile appliance uses WiFi/5G/Starlink and needs captive portal handling.

### Vessel/RV/remote site

Multiple unstable uplinks, strong diagnostics, and hooks for modem control.

### Cloud relay

Cloud-hosted relay provides stable endpoint and fallback path.

## Explicit non-archetypes

Gatherlink is not primarily:

- router OS
- firewall distro
- enterprise SASE platform
- general-purpose proxy manager
- DPI/security appliance
