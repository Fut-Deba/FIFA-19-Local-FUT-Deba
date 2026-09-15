#!/usr/bin/env python3
"""Observe the local-certificate result from an already-running FIFA menu.

The diagnostic requires Administrator rights and the exact observed EA App
EXE/CardsDLL pair. It starts a loopback-only TLS peer, verifies and arms the
fixed passive certificate observer, and only then installs one temporary hosts
mapping for ``spring18.gosredirector.ea.com``. It never reads application
payload, scans or writes process memory, or replaces the return value. The
original hosts bytes are restored in ``finally`` after one return or timeout.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from observe_eaapp_certificate_return import agent_source
from probe_eaapp_certificate_signature import verify_target
from probe_eaapp_local_certificate_return import (
    HOSTNAME,
    LocalTlsPeer,
    create_ephemeral_certificate,
    emit,
)


# A hosts write is denied only while another process holds the file, so a
# few bounded retries cost a shutdown two seconds and save it from leaving
# the redirect installed on the machine.
HOSTS_WRITE_ATTEMPTS = 5
HOSTS_WRITE_RETRY_SECONDS = 0.4
MARKER_START = "# BEGIN LOCALFUT19_EAAPP_CERT_PROBE"
MARKER_END = "# END LOCALFUT19_EAAPP_CERT_PROBE"
RETURN_TIMEOUT_SECONDS = 60.0


def hosts_path() -> Path:
    windows = os.environ.get("WINDIR")
    if not windows:
        raise RuntimeError("WINDIR is unavailable")
    return Path(windows) / "System32" / "drivers" / "etc" / "hosts"


def flush_dns() -> None:
    subprocess.run(
        ["ipconfig", "/flushdns"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace a file's contents without ever leaving it truncated.

    ``Path.write_bytes`` truncates first and writes second. A process killed
    between the two steps leaves an empty file, and for the Windows hosts file
    that silently discards the machine's real configuration. The 2026-09-03
    10:18:30 shutdown did exactly that: the session logged
    ``eaapp-full-stop-requested`` and died before ``hosts-restored``, leaving a
    zero-byte hosts.

    Writing a sibling temporary file and renaming it over the target keeps the
    original intact until the replacement is complete on disk.

    Both strategies are retried, because a denial here is usually momentary.
    The 2026-09-07 tester shutdown lost both of them to
    ``PermissionError: [Errno 13]`` in the same instant and ended with the
    redirect still installed, although the identical write had succeeded 51
    minutes earlier in the same elevated process: another process was holding
    hosts open, not a right that was missing.
    """
    if not payload:
        raise RuntimeError("refusing to write an empty hosts file")
    for attempt in range(HOSTS_WRITE_ATTEMPTS):
        try:
            _write_hosts_bytes(path, payload)
            return
        except OSError:
            if attempt + 1 >= HOSTS_WRITE_ATTEMPTS:
                raise
            time.sleep(HOSTS_WRITE_RETRY_SECONDS)


def _write_hosts_bytes(path: Path, payload: bytes) -> None:
    """Perform one complete hosts write, preferring the atomic rename."""
    temporary = path.with_name(path.name + ".localfut19.tmp")
    try:
        with open(temporary, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return
    except OSError:
        # Windows tamper protection denies renaming over the hosts file even
        # from an elevated process, while writing it in place is allowed. Fall
        # back to a single in-place write, flushed to disk before returning,
        # and drop the temporary file. The recovery snapshot still covers a
        # process killed inside that much smaller window.
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    with open(path, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def install_temporary_hosts(path: Path, original: bytes) -> None:
    text = original.decode("ascii")
    if re.search(re.escape(HOSTNAME), text, re.IGNORECASE):
        raise RuntimeError(
            "%s is already present in hosts; refusing unrelated state" % HOSTNAME
        )
    text = text.rstrip("\r\n")
    if text:
        text += "\r\n\r\n"
    text += "%s\r\n127.0.0.1 %s\r\n%s\r\n" % (
        MARKER_START,
        HOSTNAME,
        MARKER_END,
    )
    _atomic_write_bytes(path, text.encode("ascii"))
    flush_dns()
    installed = path.read_text(encoding="ascii")
    if not re.search(
        r"(?m)^127\.0\.0\.1\s+spring18\.gosredirector\.ea\.com\s*$",
        installed,
    ):
        raise RuntimeError("temporary hosts redirect could not be verified")


def restore_hosts(path: Path, original: bytes) -> None:
    _atomic_write_bytes(path, original)
    flush_dns()
    if path.read_bytes() != original:
        raise RuntimeError("hosts was not restored byte-for-byte")
    emit("hosts-restored", status="byte-exact")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("run this diagnostic as Administrator")
    executable = verify_target(args.pid)
    emit("preflight-ok", pid=args.pid, imageName=os.path.basename(executable))

    path = hosts_path()
    original = path.read_bytes()
    hosts_changed = False
    peer = None
    observation = None
    session_objects = None
    try:
        with tempfile.TemporaryDirectory(prefix="localfut19-cert-menu-") as temporary:
            cert_path, key_path = create_ephemeral_certificate(Path(temporary))
            peer = LocalTlsPeer(cert_path, key_path)
            peer.start()

            import frida

            finished = threading.Event()
            armed = threading.Event()
            outcome: dict[str, object] = {}
            session = frida.get_local_device().attach(args.pid)

            def on_detached(reason, *details):
                if not finished.is_set():
                    outcome.update(
                        {"event": "process-detached", "reason": str(reason)}
                    )
                    finished.set()

            def on_message(message, _data):
                if message.get("type") == "send":
                    payload = message.get("payload") or {}
                else:
                    payload = {
                        "event": "agent-error",
                        "description": message.get("description"),
                    }
                emit("observer-message", pid=args.pid, payload=payload)
                event = payload.get("event")
                if event == "armed":
                    armed.set()
                elif event in ("return", "refused", "agent-error"):
                    outcome.update(payload)
                    finished.set()

            session.on("detached", on_detached)
            script = session.create_script(agent_source())
            script.on("message", on_message)
            script.load()
            session_objects = (session, script)
            if not armed.wait(10.0):
                raise RuntimeError("certificate observer did not arm")
            emit("observer-armed", pid=args.pid)

            install_temporary_hosts(path, original)
            hosts_changed = True
            emit("hosts-installed", scope="menu-only-certificate-probe")
            emit("ready-enter-fut", timeoutSeconds=RETURN_TIMEOUT_SECONDS)

            if not finished.wait(RETURN_TIMEOUT_SECONDS):
                outcome.update(
                    {"event": "timeout", "timeoutSeconds": RETURN_TIMEOUT_SECONDS}
                )
            observation = dict(outcome)
    finally:
        restore_error = None
        try:
            if hosts_changed or path.read_bytes() != original:
                restore_hosts(path, original)
        except BaseException as error:
            restore_error = error
        if session_objects is not None:
            session, script = session_objects
            try:
                script.unload()
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                pass
        if peer is not None:
            peer.stop()
        if restore_error is not None:
            raise restore_error

    emit("complete", result=observation)
    return 0 if observation and observation.get("event") == "return" else 2


if __name__ == "__main__":
    raise SystemExit(main())
