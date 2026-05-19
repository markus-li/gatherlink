# QUIC Carrier Through Traefik

## Purpose

This lab proves that the direct QUIC DATAGRAM carrier can run through a simple
UDP-capable reverse proxy without changing Gatherlink packet semantics.

Direct Gatherlink exposure must remain testable, and sample configs should keep
a no-proxy direct QUIC case. For public internet-facing deployments, the
recommended shape is to put the carrier behind Cloudflare Spectrum-style
TCP/UDP protection and/or Traefik UDP forwarding when available.

Traefik is used only as a UDP layer-4 forwarder:

```text
client -> Traefik UDP entrypoint -> Gatherlink QUIC sink -> normal Gatherlink receive path
```

After QUIC unwrap, the recovered packet must be the same Gatherlink UDP-format
carrier packet that the raw UDP carrier would have delivered.

This is separate from the HTTP/3 DATAGRAM carrier. HTTP/3 DATAGRAM is also a
v1.1 carrier, but it must have its own acceptance path because it adds HTTP/3
request/session machinery around the datagrams.

## Required Behavior

The test must prove:

1. The same Gatherlink service works directly over QUIC.
2. The same service works over QUIC through Traefik UDP forwarding.
3. The sink unwraps QUIC and enters the normal Gatherlink receive path.
4. No Gatherlink header, encryption, replay, routing, aggregation, or service
   behavior changes when Traefik is present.
5. Invalid QUIC setup or invalid Gatherlink packets fail closed.
6. Diagnostics identify whether packets arrived through direct QUIC or proxied
   QUIC.

## Minimal Traefik Shape

Use a dedicated UDP entrypoint for the Gatherlink QUIC carrier.

Example static Traefik config:

```yaml
entryPoints:
  gatherlink-quic:
    address: ":4433/udp"

providers:
  file:
    filename: /etc/traefik/dynamic.yml
```

Example dynamic config:

```yaml
udp:
  routers:
    gatherlink-quic:
      entryPoints:
        - gatherlink-quic
      service: gatherlink-quic

  services:
    gatherlink-quic:
      loadBalancer:
        servers:
          - address: "gatherlink-sink:4433"
```

Example Docker Compose shape:

```yaml
services:
  traefik:
    image: traefik:v3
    command:
      - --configFile=/etc/traefik/traefik.yml
    ports:
      - "4433:4433/udp"
    volumes:
      - ./traefik.yml:/etc/traefik/traefik.yml:ro
      - ./dynamic.yml:/etc/traefik/dynamic.yml:ro
    networks:
      - gatherlink

  gatherlink-sink:
    image: gatherlink-dev
    command: ["gatherlink", "run", "--config", "/config/sink.json"]
    volumes:
      - ./sink.json:/config/sink.json:ro
    networks:
      - gatherlink

networks:
  gatherlink:
```

The exact container image and command may differ while Gatherlink packaging is
still evolving. The important part is that Traefik forwards UDP datagrams from
its public entrypoint to the QUIC carrier port on the Gatherlink sink.

## Acceptance Steps

1. Start the Gatherlink sink with QUIC carrier enabled.
2. Send traffic from the peer directly to the sink QUIC endpoint.
3. Verify counters, diagnostics, and payload delivery.
4. Start Traefik with the UDP router config.
5. Change the peer endpoint to Traefik's UDP entrypoint.
6. Send the same traffic again.
7. Verify counters, diagnostics, and payload delivery again.
8. Compare direct QUIC and proxied QUIC diagnostics.
9. Send malformed or unauthenticated traffic through Traefik.
10. Verify silent drop or fail-closed diagnostics without forwarding invalid
    Gatherlink state.

## Notes

- Traefik must be configured as a UDP router/service, not as an HTTP router.
- Hostname, SNI, path, and HTTP routing rules do not apply to this lab.
- This lab does not introduce a new Gatherlink packet model.
- This lab does not authorize plaintext routing.
- Cloudflare Spectrum-style TCP/UDP proxying can be tested later with the same
  acceptance shape when an account/environment is available.
