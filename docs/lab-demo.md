# Lab Demo

The first test target is pure core userland UDP traffic. It should exercise
normal UDP sockets only: no TUN device, no tunnels, no helper services, no
firewall changes, no policy routing, no raw sockets, and no root privileges.

The first Rust dataplane tests bind loopback UDP sockets with ephemeral
ports and pass datagrams through normal userland sockets. Use the dry-run plan
command before real runner work:

```bash
gatherlink run plan configs/examples/minimal-client.json
```

That plan is expected to stay non-privileged for the MVP. Tunneling is helper
owned and must remain outside the core runtime path.


The core forwarding test receives local UDP, wraps it in a Gatherlink v1 data
frame, decodes that frame, and emits the original virtual UDP payload to the
configured target. That keeps the protocol boundary real while the first test
target remains simple and fully userland.
