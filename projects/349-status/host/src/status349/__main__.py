"""349ctl - control the 349-status display.

Talks to the running 349d over its Unix socket. With --port it talks to the
serial port directly instead (the daemon must be stopped).
"""

from __future__ import annotations

import argparse
import json
import socket as socketlib
import sys
import time
from pathlib import Path

from . import ipc
from .link import Link, LinkError, open_port
from .proto import classify

SOCKET_COMMANDS = {"status", "device-cards", "text", "notify", "pause", "resume", "reload", "log"}
DIRECT_COMMANDS = {"hello", "ping", "text", "listen"}


def print_line(line: str) -> None:
    is_data, obj = classify(line)
    if not is_data:
        print(f"# {line}")
    elif obj is None:
        print("# [malformed data line]")
    else:
        print(f"< {json.dumps(obj, ensure_ascii=False)}")


def wait_for(link: Link, msg_type: str, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in link.lines(timeout=0.25):
            print_line(line)
            is_data, obj = classify(line)
            if is_data and obj is not None and obj.get("t") == msg_type:
                return obj
    return None


def ipc_request(request: dict, path: Path | None = None) -> dict:
    target = path or ipc.socket_path()
    try:
        with socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(str(target))
            sock.sendall((json.dumps(request) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise LinkError(f"cannot reach 349d at {target}: {exc} (start 349d or use --port)") from exc
    return json.loads(data.decode() or "{}")


def request_for(args: argparse.Namespace) -> dict:
    if args.command == "device-cards":
        return {"cmd": "device_cards"}
    if args.command == "text":
        return {"cmd": "text", "value": args.value}
    if args.command == "notify":
        return {"cmd": "notify", "summary": args.summary, "body": args.body}
    if args.command == "log":
        return {"cmd": "log", "lines": args.n}
    return {"cmd": args.command}


def run_direct(args: argparse.Namespace) -> int:
    try:
        port = open_port(args.port)
    except LinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with Link(port) as link:
        if args.command == "listen":
            try:
                while True:
                    for line in link.lines(timeout=0.5):
                        print_line(line)
            except KeyboardInterrupt:
                return 0

        if args.command == "hello":
            link.send({"t": "hello"})
            expect = "hello"
        elif args.command == "ping":
            link.send({"t": "ping", "ts": time.time()})
            expect = "pong"
        else:
            link.send({"t": "text", "v": args.value})
            expect = "ack"

        if wait_for(link, expect, args.timeout) is None:
            print(f"error: no {expect} within {args.timeout}s", file=sys.stderr)
            return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="349ctl", description=__doc__)
    parser.add_argument("--socket", help="daemon socket path")
    parser.add_argument("--port", help="talk to the serial port directly (349d must be stopped)")
    parser.add_argument("--timeout", type=float, default=2.0, help="reply timeout for --port")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="daemon and link status")
    sub.add_parser("device-cards", help="read cached card IDs from the device")
    text = sub.add_parser("text", help="send a text message")
    text.add_argument("value")
    notify = sub.add_parser("notify", help="show a test notification")
    notify.add_argument("summary")
    notify.add_argument("body", nargs="?", default="")
    sub.add_parser("pause", help="release the serial port for flashing (sticky)")
    sub.add_parser("resume", help="reconnect after pause")
    sub.add_parser("reload", help="reload the config file")
    log = sub.add_parser("log", help="show recent device log lines")
    log.add_argument("-n", type=int, default=50)
    sub.add_parser("hello", help="direct: request a device hello")
    sub.add_parser("ping", help="direct: round-trip a ping")
    sub.add_parser("listen", help="direct: print everything until interrupted")
    args = parser.parse_args(argv)

    if args.port:
        if args.command not in DIRECT_COMMANDS:
            print(f"error: {args.command} requires the daemon; drop --port", file=sys.stderr)
            return 1
        return run_direct(args)

    if args.command not in SOCKET_COMMANDS:
        print(f"error: {args.command} requires --port (daemon bypass)", file=sys.stderr)
        return 1

    try:
        response = ipc_request(request_for(args), Path(args.socket) if args.socket else None)
    except LinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not response.get("ok"):
        print(f"error: {response.get('error', 'unknown')}", file=sys.stderr)
        return 1

    if args.command == "status":
        for key, value in response.items():
            if key != "ok":
                print(f"{key}: {value}")
    elif args.command == "device-cards":
        print(json.dumps(response.get("device_cards", {})))
    elif args.command == "log":
        for line in response.get("lines", []):
            print(line)
    elif args.command == "notify":
        print(f"notify id {response.get('id')}")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
