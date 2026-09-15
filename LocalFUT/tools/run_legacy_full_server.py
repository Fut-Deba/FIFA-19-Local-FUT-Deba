#!/usr/bin/env python3
"""Run an exact recognized v1 build with recoverable temporary hosts state."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if os.fspath(SERVER_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(SERVER_DIR))

from fut_compat import fingerprint_summary, inspect_installed_build

from beta_readiness import audit as audit_beta_runtime


SERVICE_PORTS = (42230, 10051, 8199)
HOSTNAMES = (
    "spring18.gosredirector.ea.com",
    "gosca18.ea.com",
)
MARKER_START = "# BEGIN LOCALFUT19 LEGACY SESSION"
MARKER_END = "# END LOCALFUT19 LEGACY SESSION"


# Live v1 2026-09-13: STOP LOCAL FUT could not close FIFA 19, neither with a
# close request nor with a forced close. This runner starts the game from its
# elevated process, so the game runs elevated and the unelevated launcher is
# refused both. This process is not, so the launcher's stop request is
# honoured here.
GAME_CLOSE_TIMEOUT_SECONDS = 20.0


def close_game(game_process: subprocess.Popen) -> None:
    """Ask FIFA 19 to close, and end it if the request is ignored."""
    subprocess.run(["taskkill.exe", "/PID", str(game_process.pid)],
                   capture_output=True, check=False)
    try:
        game_process.wait(timeout=GAME_CLOSE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        game_process.kill()
        game_process.wait(timeout=10.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--account-mode", choices=("NORMAL", "RTG"),
                        default="NORMAL")
    return parser.parse_args()


def hosts_path() -> Path:
    windows = os.environ.get("WINDIR", "").strip()
    if not windows:
        raise RuntimeError("WINDIR is unavailable")
    return Path(windows) / "System32" / "drivers" / "etc" / "hosts"


def flush_dns() -> None:
    subprocess.run(
        ["ipconfig", "/flushdns"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def install_temporary_hosts(path: Path, original: bytes) -> None:
    # Latin-1 maps every byte to one character and back, so a hosts file with
    # non-ASCII comments keeps its exact bytes; decoding it as ASCII raised
    # and stopped the session with the same silent symptom as a conflict.
    text = original.decode("latin-1")
    for hostname in HOSTNAMES:
        if re.search(re.escape(hostname), text, re.IGNORECASE):
            raise RuntimeError(
                "%s is already present in hosts; run "
                "ADVANCED\\CLEANUP_OLD_REDIRECTS.cmd once "
                "and retry" % hostname
            )
    prefix = text.rstrip("\r\n")
    if prefix:
        prefix += "\r\n\r\n"
    block = [MARKER_START]
    block.extend("127.0.0.1 %s" % hostname for hostname in HOSTNAMES)
    block.append(MARKER_END)
    path.write_bytes((prefix + "\r\n".join(block) + "\r\n").encode("latin-1"))
    flush_dns()
    installed = path.read_bytes().decode("latin-1")
    for hostname in HOSTNAMES:
        if not re.search(
                r"(?im)^127\.0\.0\.1\s+%s\s*$" % re.escape(hostname),
                installed):
            raise RuntimeError("temporary hosts redirect verification failed")


def restore_hosts(path: Path, original: bytes) -> None:
    path.write_bytes(original)
    flush_dns()
    if path.read_bytes() != original:
        raise RuntimeError("hosts was not restored byte-for-byte")


def recover_stale_snapshot(path: Path, snapshot: Path) -> None:
    if not snapshot.exists():
        return
    saved = snapshot.read_bytes()
    current = path.read_bytes()
    if MARKER_START.encode("ascii") in current:
        restore_hosts(path, saved)
        snapshot.unlink()
        print("Recovered a stale LocalFUT19 hosts session byte-for-byte.")
    elif current == saved:
        snapshot.unlink()
    else:
        # The legacy redirect is already gone, so the snapshot has nothing
        # left to restore. Keep later machine-owned edits byte-for-byte and
        # discard only the obsolete recovery file.
        snapshot.unlink()
        print("Discarded an obsolete LocalFUT19 hosts snapshot; unrelated "
              "hosts changes were preserved byte-for-byte.")


def wait_for_ports(process: subprocess.Popen, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    pending = set(SERVICE_PORTS)
    while pending and time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "LocalFUT19 server exited with code %s" % process.returncode)
        for port in tuple(pending):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    pending.remove(port)
        if pending:
            time.sleep(0.1)
    if pending:
        raise RuntimeError("LocalFUT19 did not bind ports %s" % sorted(pending))


def validate_game(executable: Path) -> str:
    inspection = inspect_installed_build(executable)
    if not inspection.supported:
        detail = fingerprint_summary(inspection)
        raise RuntimeError("recognized v1 fingerprint required: %s%s" % (
            inspection.reason, "; " + detail if detail else ""))
    return inspection.profile.profile_id


def main() -> int:
    args = parse_args()
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("run PLAY_FUT19_LOCAL.cmd as Administrator")
    executable = args.game.expanduser().resolve()
    if executable.name.lower() != "fifa19.exe" or not executable.is_file():
        raise RuntimeError("--game must point to FIFA19.exe")
    profile_id = validate_game(executable)
    readiness = audit_beta_runtime()
    if not readiness.get("ready"):
        raise RuntimeError("runtime preflight failed: %s" % "; ".join(
            readiness.get("errors") or ["unknown error"]))

    data_root = args.data_root.expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    runtime_root = data_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    stop_file = runtime_root / "legacy.stop"
    snapshot = runtime_root / "legacy-hosts.original.bin"
    network_log = data_root / "server.log"
    if stop_file.exists():
        stop_file.unlink()

    path = hosts_path()
    recover_stale_snapshot(path, snapshot)
    original_hosts = path.read_bytes()
    server_process: subprocess.Popen | None = None
    game_process: subprocess.Popen | None = None
    hosts_changed = False
    try:
        with snapshot.open("xb") as stream:
            stream.write(original_hosts)
            stream.flush()
            os.fsync(stream.fileno())
        if snapshot.read_bytes() != original_hosts:
            raise RuntimeError("hosts recovery snapshot verification failed")

        environment = os.environ.copy()
        environment["LOCALFUT19_DATA_ROOT"] = os.fspath(data_root)
        environment["LOCALFUT19_NETWORK_LOG"] = os.fspath(network_log)
        environment["LOCALFUT19_STOP_FILE"] = os.fspath(stop_file)
        environment["LOCALFUT19_BUILD_PROFILE"] = profile_id
        environment["LOCALFUT19_PROFILE"] = args.account_mode
        if args.account_mode == "RTG":
            environment["LOCALFUT19_RTG_MODE"] = "1"
        else:
            environment.pop("LOCALFUT19_RTG_MODE", None)

        server_process = subprocess.Popen(
            [sys.executable, "-B", os.fspath(
                ROOT / "server" / "local_fut19_server.py")],
            cwd=ROOT, env=environment,
        )
        wait_for_ports(server_process)
        install_temporary_hosts(path, original_hosts)
        hosts_changed = True
        print("Temporary localhost routing installed; starting exact v1 build.")
        game_process = subprocess.Popen(
            [os.fspath(executable)], cwd=executable.parent,
        )
        while game_process.poll() is None:
            # STOP LOCAL FUT writes this request while the game is running.
            # Read it before the server check: the server watches the same
            # file and stops as well, which is expected here, not a failure.
            if stop_file.exists():
                print("Stop requested; closing FIFA 19.")
                close_game(game_process)
                break
            if server_process.poll() is not None:
                raise RuntimeError(
                    "LocalFUT19 server stopped while FIFA was running")
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        print("Stop requested; restoring the machine state.")
        return 130
    finally:
        restore_error: BaseException | None = None
        try:
            if hosts_changed or path.read_bytes() != original_hosts:
                restore_hosts(path, original_hosts)
            if path.read_bytes() == original_hosts and snapshot.exists():
                snapshot.unlink()
        except BaseException as error:
            restore_error = error
        stop_file.write_text(json.dumps({"requested": time.time()}),
                             encoding="utf-8")
        if server_process is not None and server_process.poll() is None:
            try:
                server_process.wait(timeout=12.0)
            except subprocess.TimeoutExpired:
                server_process.terminate()
                server_process.wait(timeout=5.0)
        if stop_file.exists():
            stop_file.unlink()
        if restore_error is not None:
            raise restore_error


if __name__ == "__main__":
    raise SystemExit(main())
