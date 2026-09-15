#!/usr/bin/env python3
"""Replace one verified local-certificate failure at the FIFA main menu.

This explicitly invasive diagnostic is limited to the observed EA App build
and one loopback TLS connection. It verifies the fixed handler signature,
replaces only natural return ``0x4`` with verified success ``0x15`` once, and
refuses every other value. It never reads application payload. One temporary
hosts mapping is installed only after the listener and hook are armed, then the
original hosts bytes are restored in ``finally``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import threading
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from observe_eaapp_certificate_return import TARGET_RVA, _signature_arrays
from probe_eaapp_certificate_signature import verify_target
from probe_eaapp_local_certificate_at_menu import (
    hosts_path,
    install_temporary_hosts,
    restore_hosts,
)
from probe_eaapp_local_certificate_return import (
    LocalTlsPeer,
    TcpAcceptSentinel,
    create_ephemeral_certificate,
    emit,
)


RETURN_TIMEOUT_SECONDS = 60.0
HANDSHAKE_TIMEOUT_SECONDS = 8.0
EXPECTED_FAILURE = "0x4"
VERIFIED_SUCCESS = "0x15"
BLAZE_SENTINEL_PORT = 10051


def minimal_redirector_response() -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<serverinstanceinfo>\n'
        '  <address member="0"><valu>\n'
        '    <hostname>127.0.0.1</hostname>'
        '<ip>2130706433</ip><port>10051</port>\n'
        '  </valu></address>\n'
        '  <secure>0</secure>\n'
        '</serverinstanceinfo>'
    ).encode("ascii")
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/xml\r\n"
        + ("Content-Length: %d\r\n" % len(xml)).encode("ascii")
        + b"Connection: close\r\n\r\n"
        + xml
    )


def active_agent_source() -> str:
    expected, keep = _signature_arrays()
    return """
'use strict';
const module = Process.getModuleByName('FIFA19.exe');
const target = module.base.add(%d);
const expected = %s;
const keep = %s;
let handled = false;

function signatureMatches() {
  try {
    const actual = new Uint8Array(target.readByteArray(expected.length));
    let valid = actual.length === expected.length;
    for (let i = 0; valid && i < expected.length; i++) {
      if (keep[i] && actual[i] !== expected[i]) valid = false;
    }
    return valid;
  } catch (_) {
    return false;
  }
}

if (!signatureMatches()) {
  send({ event: 'refused', reason: 'candidate signature mismatch',
         rva: '0x%x' });
} else {
  Interceptor.attach(target, {
    onEnter() {
      this.startedAt = Date.now();
    },
    onLeave(retval) {
      if (handled) return;
      handled = true;
      const original = retval.toString();
      if (original !== '0x4') {
        send({ event: 'unexpected-return', rva: '0x%x', value: original,
               durationMs: Date.now() - this.startedAt });
        return;
      }
      retval.replace(ptr('0x15'));
      send({ event: 'return-replaced', rva: '0x%x', original: original,
             replacement: '0x15', durationMs: Date.now() - this.startedAt });
    }
  });
  send({ event: 'armed', rva: '0x%x', expectedFailure: '0x4',
         replacement: '0x15' });
}
""" % (
        TARGET_RVA,
        json.dumps(expected),
        json.dumps(keep),
        TARGET_RVA,
        TARGET_RVA,
        TARGET_RVA,
        TARGET_RVA,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--serve-minimal-redirector", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("run this diagnostic as Administrator")
    executable = verify_target(args.pid)
    emit("preflight-ok", pid=args.pid, imageName=os.path.basename(executable))

    path = hosts_path()
    original_hosts = path.read_bytes()
    hosts_changed = False
    peer = None
    sentinel = None
    session_objects = None
    outcome: dict[str, object] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="localfut19-cert-success-") as temporary:
            cert_path, key_path = create_ephemeral_certificate(Path(temporary))
            response = None
            if args.serve_minimal_redirector:
                sentinel = TcpAcceptSentinel(BLAZE_SENTINEL_PORT)
                sentinel.start()
                response = minimal_redirector_response()
            peer = LocalTlsPeer(
                cert_path,
                key_path,
                response=response,
                wait_for_request=args.serve_minimal_redirector,
            )
            peer.start()

            import frida

            armed = threading.Event()
            finished = threading.Event()
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
                elif event in (
                    "return-replaced",
                    "unexpected-return",
                    "refused",
                    "agent-error",
                ):
                    outcome.update(payload)
                    finished.set()

            session.on("detached", on_detached)
            script = session.create_script(active_agent_source())
            script.on("message", on_message)
            script.load()
            session_objects = (session, script)
            if not armed.wait(10.0):
                raise RuntimeError("active certificate observer did not arm")
            emit(
                "observer-armed",
                pid=args.pid,
                expectedFailure=EXPECTED_FAILURE,
                replacement=VERIFIED_SUCCESS,
            )

            install_temporary_hosts(path, original_hosts)
            hosts_changed = True
            emit("hosts-installed", scope="menu-only-certificate-success-probe")
            emit("ready-enter-fut", timeoutSeconds=RETURN_TIMEOUT_SECONDS)

            if not finished.wait(RETURN_TIMEOUT_SECONDS):
                outcome.update(
                    {"event": "timeout", "timeoutSeconds": RETURN_TIMEOUT_SECONDS}
                )
            if outcome.get("event") == "return-replaced":
                if peer.handshake_complete.wait(HANDSHAKE_TIMEOUT_SECONDS):
                    outcome["tlsHandshake"] = "complete"
                elif peer.handshake_ended.is_set():
                    outcome["tlsHandshake"] = "ended-without-completion"
                else:
                    outcome["tlsHandshake"] = "timeout"
                emit("post-replacement-tls", status=outcome["tlsHandshake"])
                if args.serve_minimal_redirector:
                    outcome["redirectorRequest"] = (
                        "ready" if peer.request_ready.wait(2.0) else "not-observed"
                    )
                    outcome["redirectorResponse"] = (
                        "sent" if peer.response_sent.wait(2.0) else "not-sent"
                    )
                    outcome["blazeConnect"] = (
                        "accepted"
                        if sentinel.accepted.wait(HANDSHAKE_TIMEOUT_SECONDS)
                        else "not-observed"
                    )
                    emit(
                        "minimal-redirector-result",
                        request=outcome["redirectorRequest"],
                        response=outcome["redirectorResponse"],
                        blazeConnect=outcome["blazeConnect"],
                    )
    finally:
        restore_error = None
        try:
            if hosts_changed or path.read_bytes() != original_hosts:
                restore_hosts(path, original_hosts)
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
        if sentinel is not None:
            sentinel.stop()
        if restore_error is not None:
            raise restore_error

    emit("complete", result=outcome)
    success = (
        outcome.get("event") == "return-replaced"
        and outcome.get("tlsHandshake") == "complete"
    )
    if args.serve_minimal_redirector:
        success = (
            success
            and outcome.get("redirectorRequest") == "ready"
            and outcome.get("redirectorResponse") == "sent"
            and outcome.get("blazeConnect") == "accepted"
        )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
