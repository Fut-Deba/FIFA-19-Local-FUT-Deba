#!/usr/bin/env python3
"""Passively observe the EA App certificate handler against a local TLS peer.

This bounded diagnostic verifies the complete observed EXE/CardsDLL pair,
creates an ephemeral self-signed certificate, listens only on
127.0.0.1:42230, follows FIFA19.exe process generations, and records the first
natural return from the fixed certificate handler. It performs no memory scan,
process write or return replacement. The companion PowerShell wrapper owns the
temporary hosts mapping and restores the original hosts bytes in ``finally``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import socket
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
TOOLS_DIR = os.path.join(ROOT, "tools")
for candidate in (SERVER_DIR, TOOLS_DIR):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fut_compat import fingerprint_file, process_image_path
from observe_eaapp_certificate_return import agent_source
from probe_eaapp_certificate_signature import (
    EXPECTED_CARDS_SHA256,
    EXPECTED_EXE_SHA256,
    EXPECTED_PRODUCT_VERSION,
)


HOSTNAME = "spring18.gosredirector.ea.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 42230
DEFAULT_WAIT_SECONDS = 120.0


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def verify_files(executable: str) -> str:
    executable = os.path.realpath(executable)
    exe = fingerprint_file(executable, "FIFA19.exe")
    cards_path = os.path.join(
        os.path.dirname(executable), "CardsDLL_Win64_retail.dll"
    )
    cards = fingerprint_file(cards_path, "CardsDLL_Win64_retail.dll")
    if (
        exe.sha256 != EXPECTED_EXE_SHA256
        or cards.sha256 != EXPECTED_CARDS_SHA256
        or exe.product_version != EXPECTED_PRODUCT_VERSION
        or cards.product_version != EXPECTED_PRODUCT_VERSION
    ):
        raise RuntimeError("refusing unrecognized or mixed FIFA 19 build")
    return executable


def create_ephemeral_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "redirector-cert.pem"
    key_path = directory / "redirector-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class LocalTlsPeer:
    def __init__(
        self,
        cert_path: Path,
        key_path: Path,
        response: bytes | None = None,
        wait_for_request: bool = False,
    ):
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._error: BaseException | None = None
        self.handshake_complete = threading.Event()
        self.handshake_ended = threading.Event()
        self.response_sent = threading.Event()
        self.request_ready = threading.Event()
        self._cert_path = cert_path
        self._key_path = key_path
        self._response = response
        self._wait_for_request = bool(wait_for_request)
        self._listener: socket.socket | None = None

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("local TLS peer did not become ready")
        if self._error is not None:
            raise RuntimeError("local TLS peer failed: %s" % self._error)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(self._cert_path), str(self._key_path))
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LISTEN_HOST, LISTEN_PORT))
            listener.listen(8)
            listener.settimeout(0.25)
            self._ready.set()
            emit("tls-listening", host=LISTEN_HOST, port=LISTEN_PORT)
            while not self._stop.is_set():
                try:
                    connection, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                emit("tls-accepted", peer=address[0])
                try:
                    connection.settimeout(5.0)
                    with context.wrap_socket(connection, server_side=True) as tls:
                        self.handshake_complete.set()
                        emit("tls-handshake-complete")
                        if self._response is not None:
                            if self._wait_for_request:
                                readable, _, _ = select.select([tls], [], [], 5.0)
                                if not readable:
                                    emit("tls-request-not-ready")
                                    continue
                                self.request_ready.set()
                                emit("tls-request-bytes-ready")
                            tls.sendall(self._response)
                            self.response_sent.set()
                            emit("tls-static-response-sent", size=len(self._response))
                            self._stop.wait(1.0)
                except (OSError, ssl.SSLError) as error:
                    self.handshake_ended.set()
                    emit("tls-handshake-ended", detail=type(error).__name__)
                finally:
                    try:
                        connection.close()
                    except OSError:
                        pass
        except BaseException as error:
            self._error = error
            self._ready.set()


class TcpAcceptSentinel:
    """Record one loopback TCP connection without reading its payload."""

    def __init__(self, port: int):
        self.port = int(port)
        self.accepted = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._listener: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("TCP sentinel did not become ready")
        if self._error is not None:
            raise RuntimeError("TCP sentinel failed: %s" % self._error)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LISTEN_HOST, self.port))
            listener.listen(2)
            listener.settimeout(0.25)
            self._ready.set()
            emit("tcp-sentinel-listening", host=LISTEN_HOST, port=self.port)
            while not self._stop.is_set() and not self.accepted.is_set():
                try:
                    connection, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self.accepted.set()
                emit("tcp-sentinel-accepted", peer=address[0], port=self.port)
                self._stop.wait(5.0)
                try:
                    connection.close()
                except OSError:
                    pass
        except BaseException as error:
            self._error = error
            self._ready.set()


def observe(executable: str, wait_seconds: float) -> dict[str, object]:
    import frida

    device = frida.get_local_device()
    deadline = time.monotonic() + wait_seconds
    finished = threading.Event()
    outcome: dict[str, object] = {}
    sessions: dict[int, tuple[object, object]] = {}
    seen: set[int] = set()
    lock = threading.Lock()

    def finish(payload: dict[str, object]) -> None:
        with lock:
            if finished.is_set():
                return
            outcome.update(payload)
            finished.set()

    def attach(pid: int) -> None:
        try:
            image = process_image_path(pid)
            if os.path.normcase(os.path.realpath(image)) != os.path.normcase(executable):
                return
            session = device.attach(pid)

            def on_detached(reason, *details):
                emit("process-detached", pid=pid, reason=str(reason))
                with lock:
                    sessions.pop(pid, None)

            def on_message(message, _data):
                if message.get("type") == "send":
                    payload = message.get("payload") or {}
                else:
                    payload = {
                        "event": "agent-error",
                        "description": message.get("description"),
                    }
                emit("observer-message", pid=pid, payload=payload)
                if payload.get("event") == "return":
                    finish({"event": "return", "pid": pid, **payload})
                elif payload.get("event") in ("refused", "agent-error"):
                    finish({"event": payload.get("event"), "pid": pid, **payload})

            session.on("detached", on_detached)
            script = session.create_script(agent_source())
            script.on("message", on_message)
            script.load()
            with lock:
                sessions[pid] = (session, script)
            emit("process-attached", pid=pid)
        except Exception as error:
            emit("attach-ended", pid=pid, detail=type(error).__name__)

    try:
        emit("waiting-for-fifa19", timeoutSeconds=wait_seconds)
        while not finished.is_set() and time.monotonic() < deadline:
            for process in device.enumerate_processes():
                if process.name.lower() != "fifa19.exe" or process.pid in seen:
                    continue
                seen.add(process.pid)
                attach(process.pid)
            finished.wait(0.05)
        if not finished.is_set():
            outcome.update({"event": "timeout", "timeoutSeconds": wait_seconds})
        return outcome
    finally:
        for session, script in list(sessions.values()):
            try:
                script.unload()
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-exe", required=True)
    parser.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executable = verify_files(args.game_exe)
    emit("preflight-ok", imageName=os.path.basename(executable))
    with tempfile.TemporaryDirectory(prefix="localfut19-cert-probe-") as temporary:
        cert_path, key_path = create_ephemeral_certificate(Path(temporary))
        peer = LocalTlsPeer(cert_path, key_path)
        peer.start()
        try:
            result = observe(executable, args.wait_seconds)
        finally:
            peer.stop()
    emit("complete", result=result)
    return 0 if result.get("event") == "return" else 2


if __name__ == "__main__":
    raise SystemExit(main())
