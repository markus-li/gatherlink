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

That plan is expected to stay non-privileged. Tunneling is helper
owned and must remain outside the core runtime path.


The core forwarding test receives local UDP, wraps it in a Gatherlink v1 data
frame, decodes that frame, and emits the original virtual UDP payload to the
configured target. That keeps the protocol boundary real while the first test
target remains simple and fully userland.

Remote status / IPC-copy testing must use the production-owned boundary. Older
lab harnesses may start the real sink under a hidden local service name and
expose the normal `.sink` name through a source-side proxy backed by remote
status snapshots:

```bash
gatherlink lab up configs/lab/local-dual-path.json --sink-no-local-ipc
```

In that mode the actual sink owns `lab.local-dual-path.sink.hidden`, while the
usual `lab.local-dual-path.sink` record is handled by Python as a remote-status
proxy. The source periodically asks the hidden sink for a status snapshot over
reserved service id `8`. Rust carries the service payload over the same per-path
UDP transports as normal Gatherlink frames, then forwards the bytes to Python.
Python decodes the lab remote-status message and exposes the cached copy through
the proxy, so `gatherlink services monitor lab.local-dual-path.sink` can show
the remote sink view without reading the sink's local IPC socket directly.

That shape remains useful as a migration test, but it is not sufficient v0.9
evidence by itself. The production control/runtime modules now own
remote-status request/response handling, monitor-cadence IPC, and sparse
discovery announcements; lab code may enable or accelerate those production
hooks and assert on the result, but it must not be the only implementation. The
duplicated control metadata behavior is also a Python-compiled service fanout
setting, not a hard-coded Rust control special case.
