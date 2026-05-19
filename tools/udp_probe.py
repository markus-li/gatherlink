"""Small UDP send/receive probe for manual Gatherlink service smoke tests."""

from __future__ import annotations

import argparse
import socket
import time


def _parse_host_port(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    return host, port


def _receive(bind: tuple[str, int], timeout: float, count: int, min_count: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.settimeout(timeout)
        udp_socket.bind(bind)
        received = 0
        while received < count:
            try:
                data, source = udp_socket.recvfrom(65535)
            except TimeoutError:
                if received >= min_count:
                    return
                raise
            received += 1
            print(data.decode("utf-8", errors="replace"))
            print(f"{source[0]}:{source[1]}")


def _send(
    target: tuple[str, int],
    payload: str,
    count: int,
    *,
    duration_seconds: float | None = None,
    interval_seconds: float = 0.0,
    payload_size: int | None = None,
) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        sent_packets = 0
        deadline = time.monotonic() + duration_seconds if duration_seconds is not None else None
        while sent_packets < count or (deadline is not None and time.monotonic() < deadline):
            index = sent_packets
            suffix = f"-{index + 1}" if count > 1 else ""
            data = f"{payload}{suffix}".encode()
            if payload_size is not None:
                if payload_size < len(data):
                    raise ValueError("payload_size must fit the generated payload prefix and sequence")
                data = data + (b"x" * (payload_size - len(data)))
            sent = udp_socket.sendto(data, target)
            sent_packets += 1
            print(f"sent {sent} bytes to {target[0]}:{target[1]}")
            if interval_seconds > 0:
                time.sleep(interval_seconds)
        return sent_packets


def main() -> None:
    """Run the UDP probe command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    receive_parser = subparsers.add_parser("receive", help="receive one UDP datagram")
    receive_parser.add_argument("bind", type=_parse_host_port)
    receive_parser.add_argument("--count", type=int, default=1)
    receive_parser.add_argument(
        "--min-count",
        type=int,
        default=None,
        help="Allow timeout after at least this many packets; defaults to --count.",
    )
    receive_parser.add_argument("--timeout", type=float, default=5.0)

    send_parser = subparsers.add_parser("send", help="send one UDP datagram")
    send_parser.add_argument("target", type=_parse_host_port)
    send_parser.add_argument("payload")
    send_parser.add_argument("--count", type=int, default=1)
    send_parser.add_argument("--duration", type=float, default=None)
    send_parser.add_argument("--interval", type=float, default=0.0)
    send_parser.add_argument("--payload-size", type=int, default=None)

    args = parser.parse_args()
    if args.command == "receive":
        min_count = args.count if args.min_count is None else args.min_count
        if min_count < 0 or min_count > args.count:
            parser.error("--min-count must be between 0 and --count")
        _receive(args.bind, args.timeout, args.count, min_count)
    elif args.command == "send":
        if args.count < 0:
            parser.error("--count must be non-negative")
        if args.duration is not None and args.duration < 0:
            parser.error("--duration must be non-negative")
        if args.interval < 0:
            parser.error("--interval must be non-negative")
        if args.payload_size is not None and args.payload_size < 1:
            parser.error("--payload-size must be positive")
        sent = _send(
            args.target,
            args.payload,
            args.count,
            duration_seconds=args.duration,
            interval_seconds=args.interval,
            payload_size=args.payload_size,
        )
        print(f"sent_packets={sent}")


if __name__ == "__main__":
    main()
