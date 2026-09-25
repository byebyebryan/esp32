#!/usr/bin/env python3
"""Record a read-only, bounded 349-status connected soak as private JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ALIVE = re.compile(
    r"I \((\d+)\) 349-status: alive, host=(yes|no), "
    r"cpu_idle_core0_pct=(na|\d+), cpu_idle_core1_pct=(na|\d+)"
)
HELLO = re.compile(r"device hello: .*?\bbuild=(\S+)\s+sha=(\S+)")
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout


def ipc(command: str, **kwargs: object) -> dict:
    request = json.dumps({"cmd": command, **kwargs}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "349d.sock"))
        conn.sendall(request)
        response = bytearray()
        while b"\n" not in response:
            block = conn.recv(65536)
            if not block:
                raise RuntimeError("349d IPC closed before reply")
            response.extend(block)
            if len(response) > 262144:
                raise RuntimeError("349d IPC reply too large")
    result = json.loads(response.split(b"\n", 1)[0])
    if not result.get("ok"):
        raise RuntimeError(f"349d IPC {command} failed")
    return result


def service() -> dict:
    fields = ("ActiveState", "MainPID", "ExecMainStartTimestampMonotonic", "NRestarts")
    output = run("systemctl", "--user", "show", "349d.service", *(f"-p{field}" for field in fields))
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return {
        "active": values.get("ActiveState"),
        "pid": int(values.get("MainPID", "0")),
        "start_mono_us": int(values.get("ExecMainStartTimestampMonotonic", "0")),
        "restarts": int(values.get("NRestarts", "0")),
    }


def journal(after: str | None = None) -> list[dict]:
    args = ["journalctl", "--user", "-u", "349d.service", "--no-pager", "-o", "json"]
    args += ["--after-cursor", after] if after else ["-n", "200"]
    return [json.loads(line) for line in run(*args).splitlines() if line.startswith("{")]


def event_kind(message: str) -> str | None:
    if "device hello:" in message:
        return "device_hello"
    for token, kind in (
        ("link down", "link_down"),
        ("link error:", "link_error"),
        ("link up on", "link_up"),
        ("device requested resync", "resync"),
        ("dropping oversized line buffer", "rx_overflow"),
        ("malformed data line", "parse_error"),
        ("notification monitor disconnected", "notification_monitor_down"),
        ("notifications unavailable:", "notification_monitor_error"),
        ("Started 349-status desk display daemon", "service_started"),
        ("Stopped 349-status desk display daemon", "service_stopped"),
    ):
        if token in message:
            return kind
    return None


def latest_alive(lines: list[str]) -> dict | None:
    for line in reversed(lines):
        match = ALIVE.search(line)
        if match:
            uptime, host, core0, core1 = match.groups()
            return {
                "device_uptime_ms": int(uptime),
                "host": host == "yes",
                "core0_idle_pct": None if core0 == "na" else int(core0),
                "core1_idle_pct": None if core1 == "na" else int(core1),
            }
    return None


def private_output(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise RuntimeError("output directory must be owned by this user and mode 0700")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)


def write_record(fd: int, record: dict) -> None:
    data = (json.dumps({"utc": utc_now(), **record}, separators=(",", ":")) + "\n").encode()
    while data:
        data = data[os.write(fd, data):]
    os.fsync(fd)


def on_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration", required=True, type=float, help="seconds")
    parser.add_argument("--interval", type=float, default=15, help="seconds")
    parser.add_argument("--expected-build", required=True)
    parser.add_argument("--expected-sha-prefix", required=True)
    parser.add_argument("--firmware-elf", required=True, type=Path)
    parser.add_argument("--device-path", required=True, type=Path)
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0 or args.interval > args.duration:
        parser.error("require 0 < interval <= duration")
    if len(args.expected_sha_prefix) < 9:
        parser.error("expected SHA prefix must be at least nine characters")
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    fd = private_output(args.output)
    started = time.monotonic()
    last_boot = time.clock_gettime(time.CLOCK_BOOTTIME)
    last_mono = started
    baseline_service = None
    cursor = None
    previous_alive = None
    last_alive_at = started
    samples = 0
    failures: list[str] = []
    try:
        elf_sha = hashlib.sha256(args.firmware_elf.read_bytes()).hexdigest()
        if not elf_sha.startswith(args.expected_sha_prefix):
            raise RuntimeError("firmware ELF does not match expected SHA prefix")
        status = ipc("status")
        baseline_service = service()
        entries = journal()
        cursor = entries[-1].get("__CURSOR") if entries else None
        if not cursor:
            raise RuntimeError("no service journal cursor at baseline")
        hellos = [HELLO.search(str(e.get("MESSAGE", ""))) for e in entries]
        latest_hello = next((match for match in reversed(hellos) if match), None)
        if latest_hello is None:
            raise RuntimeError("no device hello in recent service journal")
        build, sha = latest_hello.groups()
        if build != args.expected_build or not sha.startswith(args.expected_sha_prefix):
            raise RuntimeError(f"device hello mismatch: build={build} sha={sha}")
        if status.get("paused") or not status.get("link") or baseline_service["active"] != "active":
            raise RuntimeError("baseline is not active and linked")
        if status.get("last_rx_s") is None or status["last_rx_s"] > 20:
            raise RuntimeError("baseline device input is stale")
        if not args.device_path.exists():
            raise RuntimeError("baseline device path is absent")
        previous_alive = latest_alive(ipc("log", lines=200).get("lines", []))
        if previous_alive is None:
            raise RuntimeError("no device alive heartbeat in daemon log")
        if not previous_alive["host"]:
            raise RuntimeError("baseline device heartbeat reports host absent")
        write_record(fd, {
            "type": "baseline", "expected_build": args.expected_build,
            "expected_sha_prefix": args.expected_sha_prefix, "hello_build": build,
            "hello_sha": sha, "firmware_elf_sha256": elf_sha,
            "firmware_elf": str(args.firmware_elf), "service": baseline_service,
            "device_path": str(args.device_path), "status": status,
            "alive": previous_alive, "journal_cursor": cursor,
        })

        deadline = started + args.duration
        next_sample = started + args.interval
        while not STOP:
            remaining = min(next_sample, deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 1))
                continue
            sampled_at = time.monotonic()
            boot_now = time.clock_gettime(time.CLOCK_BOOTTIME)
            if (boot_now - last_boot) - (sampled_at - last_mono) > 3:
                failures.append("host_suspended")
            last_boot, last_mono = boot_now, sampled_at
            if sampled_at - next_sample > args.interval:
                failures.append("collection_gap")
            status = ipc("status")
            current_service = service()
            alive = latest_alive(ipc("log", lines=200).get("lines", []))
            new_entries = journal(cursor) if cursor else journal()
            events = []
            for entry in new_entries:
                cursor = entry.get("__CURSOR", cursor)
                message = str(entry.get("MESSAGE", ""))
                kind = event_kind(message)
                if kind:
                    event = {"kind": kind, "journal_us": entry.get("__REALTIME_TIMESTAMP")}
                    if kind == "device_hello":
                        match = HELLO.search(message)
                        if match:
                            event.update({"build": match.group(1), "sha": match.group(2)})
                    events.append(event)
                    failures.append(kind)
            if status.get("paused"):
                failures.append("paused")
            if not status.get("link") or not args.device_path.exists():
                failures.append("link_or_device_path_lost")
            if current_service != baseline_service:
                failures.append("service_changed")
            if alive is None:
                failures.append("alive_missing")
            else:
                if alive["device_uptime_ms"] < previous_alive["device_uptime_ms"]:
                    failures.append("device_uptime_reset")
                if not alive["host"]:
                    failures.append("device_host_missing")
                if alive["device_uptime_ms"] != previous_alive["device_uptime_ms"]:
                    last_alive_at = sampled_at
                if alive["core0_idle_pct"] is None or alive["core1_idle_pct"] is None:
                    failures.append("cpu_idle_missing")
                if any(pct is not None and not 0 <= pct <= 100 for pct in (alive["core0_idle_pct"], alive["core1_idle_pct"])):
                    failures.append("cpu_idle_invalid")
                previous_alive = alive
            if sampled_at - last_alive_at > max(30, args.interval * 2.5):
                failures.append("alive_heartbeat_gap")
            write_record(fd, {
                "type": "sample", "elapsed_s": round(sampled_at - started, 3),
                "status": status, "service": current_service, "alive": alive,
                "device_path_present": args.device_path.exists(), "events": events,
                "failures": sorted(set(failures)),
            })
            samples += 1
            if failures or sampled_at >= deadline:
                break
            next_sample += args.interval
        outcome = "interrupted" if STOP else "failed" if failures else "passed"
        write_record(fd, {
            "type": "final", "outcome": outcome,
            "elapsed_s": round(time.monotonic() - started, 3),
            "samples": samples, "failures": sorted(set(failures)),
            "journal_cursor": cursor,
        })
        return 0 if outcome == "passed" else 2
    except Exception as exc:
        write_record(fd, {"type": "final", "outcome": "failed", "error": str(exc),
                          "elapsed_s": round(time.monotonic() - started, 3), "samples": samples})
        print(f"soak recorder: {exc}", file=sys.stderr)
        return 2
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
