"""Small UDP send/receive probe for manual Gatherlink service smoke tests."""

from __future__ import annotations

import argparse
import socket


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
            except socket.timeout:
                if received >= min_count:
                    return
                raise
            received += 1
            print(data.decode("utf-8", errors="replace"))
            print(f"{source[0]}:{source[1]}")


def _send(target: tuple[str, int], payload: str, count: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        for index in range(count):
            suffix = f"-{index + 1}" if count > 1 else ""
            data = f"{payload}{suffix}".encode()
            sent = udp_socket.sendto(data, target)
            print(f"sent {sent} bytes to {target[0]}:{target[1]}")


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

    args = parser.parse_args()
    if args.command == "receive":
        min_count = args.count if args.min_count is None else args.min_count
        if min_count < 0 or min_count > args.count:
            parser.error("--min-count must be between 0 and --count")
        _receive(args.bind, args.timeout, args.count, min_count)
    elif args.command == "send":
        _send(args.target, args.payload, args.count)


if __name__ == "__main__":
    main()
