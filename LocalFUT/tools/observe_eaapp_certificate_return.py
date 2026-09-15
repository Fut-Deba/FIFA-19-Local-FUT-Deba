#!/usr/bin/env python3
"""Observe one unmodified certificate-handler return on the EA App build.

The complete observed EXE/CardsDLL fingerprint and the fixed candidate's
masked instruction signature are required before attaching the passive return
observer. The tool performs no scan, memory write or return replacement. It
stops after the first return or a 60-second timeout and always detaches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
SERVER_DIR = os.path.join(os.path.dirname(TOOLS_DIR), "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from fut_compat import process_image_path
from probe_eaapp_certificate_signature import PATTERN, verify_target


TARGET_RVA = 0x31C71A0
TIMEOUT_SECONDS = 60.0
SIGNATURE_WAIT_MS = 20_000
TRANSIENT_PROCESS_ERRORS = {6, 87, 1168}
ATTACH_RETRY_SECONDS = 20.0
ATTACH_RETRY_INTERVAL_SECONDS = 0.25


def process_became_unavailable(pid: int) -> bool:
    """Confirm a PID vanished across the fingerprint-to-attach boundary."""
    for attempt in range(2):
        try:
            process_image_path(pid)
        except OSError as exc:
            if getattr(exc, "errno", None) in TRANSIENT_PROCESS_ERRORS:
                return True
            return False
        if attempt == 0:
            time.sleep(0.1)
    return False


def attach_with_transient_retry(device, frida_module, pid: int):
    """Retry the short loader interval where Frida cannot attach yet."""
    deadline = time.monotonic() + ATTACH_RETRY_SECONDS
    retries = 0
    while True:
        try:
            return device.attach(pid), retries
        except frida_module.NotSupportedError:
            retries += 1
            if (
                process_became_unavailable(pid)
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(ATTACH_RETRY_INTERVAL_SECONDS)


def _signature_arrays() -> tuple[list[int], list[bool]]:
    expected = []
    keep = []
    for token in PATTERN.split():
        if token == "??":
            expected.append(0)
            keep.append(False)
        else:
            expected.append(int(token, 16))
            keep.append(True)
    return expected, keep


def agent_source() -> str:
    expected, keep = _signature_arrays()
    return """
'use strict';
const module = Process.getModuleByName('FIFA19.exe');
const target = module.base.add(%d);
const expected = %s;
const keep = %s;
const signatureDeadline = Date.now() + %d;
let armed = false;
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
function tryArm() {
  if (armed) return;
  if (!signatureMatches()) {
    if (Date.now() >= signatureDeadline) {
      clearInterval(timer);
      send({ event: 'refused', reason: 'candidate signature mismatch',
             rva: '0x%x' });
    }
    return;
  }
  armed = true;
  clearInterval(timer);
  try {
    Interceptor.attach(target, {
      onEnter() {
        this.startedAt = Date.now();
      },
      onLeave(retval) {
        send({ event: 'return', rva: '0x%x', value: retval.toString(),
               durationMs: Date.now() - this.startedAt });
      }
    });
    send({ event: 'armed', rva: '0x%x' });
  } catch (error) {
    send({ event: 'agent-error', description: String(error) });
  }
}
const timer = setInterval(tryArm, 20);
tryArm();
""" % (
        TARGET_RVA,
        json.dumps(expected),
        json.dumps(keep),
        SIGNATURE_WAIT_MS,
        TARGET_RVA,
        TARGET_RVA,
        TARGET_RVA,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        executable = verify_target(args.pid)
    except OSError as exc:
        if getattr(exc, "errno", None) in TRANSIENT_PROCESS_ERRORS:
            print(
                json.dumps(
                    {
                        "event": "process-unavailable",
                        "pid": args.pid,
                        "errorCode": exc.errno,
                        "phase": "preflight",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 3
        print(
            json.dumps(
                {
                    "event": "observer-error",
                    "pid": args.pid,
                    "errorType": type(exc).__name__,
                    "phase": "preflight",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 4
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "event": "refused",
                    "pid": args.pid,
                    "reason": str(exc),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 4
    print(
        json.dumps(
            {
                "event": "preflight-ok",
                "pid": args.pid,
                "imageName": os.path.basename(executable),
                "targetRva": "0x%x" % TARGET_RVA,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    import frida

    finished = threading.Event()
    outcome: dict[str, object] = {}
    session = None
    script = None
    phase = "native-attach"

    def on_message(message, _data):
        if message.get("type") == "send":
            payload = message.get("payload") or {}
        else:
            payload = {
                "event": "agent-error",
                "description": message.get("description"),
            }
        print(json.dumps(payload, sort_keys=True), flush=True)
        if payload.get("event") in ("return", "refused", "agent-error"):
            outcome.update(payload)
            finished.set()

    def on_detached(reason, *details):
        if finished.is_set():
            return
        payload = {
            "event": "process-detached",
            "reason": str(reason),
        }
        if details and details[0] is not None:
            payload["detail"] = str(details[0])
        print(json.dumps(payload, sort_keys=True), flush=True)
        outcome.update(payload)
        finished.set()

    try:
        device = frida.get_local_device()
        session, attach_retries = attach_with_transient_retry(
            device, frida, args.pid
        )
        if attach_retries:
            print(
                json.dumps(
                    {
                        "event": "native-attach-recovered",
                        "pid": args.pid,
                        "retries": attach_retries,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        phase = "session-handler"
        session.on("detached", on_detached)
        phase = "script-create"
        script = session.create_script(agent_source())
        script.on("message", on_message)
        phase = "script-load"
        script.load()
        phase = "observing-return"
        if not finished.wait(TIMEOUT_SECONDS):
            outcome.update(
                {"event": "timeout", "timeoutSeconds": TIMEOUT_SECONDS}
            )
            print(json.dumps(outcome, sort_keys=True), flush=True)
        if outcome.get("event") == "return":
            return 0
        if outcome.get("event") == "process-detached":
            return 3
        return 2
    except frida.ProcessNotFoundError:
        print(
            json.dumps(
                {
                    "event": "process-unavailable",
                    "pid": args.pid,
                    "errorCode": "process-not-found",
                    "phase": phase,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 3
    except Exception as exc:
        error_type = type(exc).__name__
        if process_became_unavailable(args.pid):
            payload = {
                "event": "process-unavailable",
                "pid": args.pid,
                "phase": phase,
                "recoveredFrom": error_type,
            }
            exit_code = 3
        else:
            payload = {
                "event": "observer-error",
                "pid": args.pid,
                "phase": phase,
                "errorType": error_type,
            }
            exit_code = 4
        print(json.dumps(payload, sort_keys=True), flush=True)
        return exit_code
    finally:
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
